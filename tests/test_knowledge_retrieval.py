"""Offline retrieval and citation-membership checks, not prose truth evaluation."""

from dataclasses import FrozenInstanceError, replace
import json
import unittest
from unittest.mock import patch

from trading_companion.knowledge_data import CORPUS_VERSION
from trading_companion.knowledge_retrieval import (
    KnowledgeEntry, KnowledgeHit, KnowledgeRetrieval, KnowledgeRetriever, KnowledgeSource,
    default_knowledge_retriever, retrieve_knowledge,
)
from trading_companion.open_knowledge import (
    GENERAL_KNOWLEDGE_VERSION, KNOWLEDGE_CONTEXT_PREFIX, GeneralKnowledgeAnswer,
    answer_general_knowledge, is_general_knowledge_question,
)
from trading_companion.tool_model import ToolModelResponse, validate_tool_messages


class ContextModel:
    def __init__(self, payload=None, *, mutate=None):
        self.payload, self.mutate, self.calls = payload, mutate, []

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        self.calls.append((messages, tools))
        context = json.loads(messages[2].content.removeprefix(KNOWLEDGE_CONTEXT_PREFIX))
        references = ([{"paragraph": 0, "sources": [context["snippets"][0]["id"]]}]
                      if context["snippets"] else [])
        payload = self.payload if self.payload is not None else {
            "version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["这是概念解释，应结合假设理解。"], "citations": references}
        if self.mutate is not None:
            payload = self.mutate(payload)
        return ToolModelResponse("complete", json.dumps(payload, ensure_ascii=False))


class LocalKnowledgeRetrievalTests(unittest.TestCase):
    def test_versioned_corpus_covers_all_five_categories_without_loading_a_database(self):
        retrieval = retrieve_knowledge("PE和PB有什么区别")
        self.assertEqual(retrieval.corpus_version, CORPUS_VERSION)
        self.assertEqual(retrieval.corpus_size, 24)
        self.assertEqual(set(retrieval.categories), {"financial", "technical", "risk", "market_rules", "research_methods"})
        self.assertFalse(retrieval.to_dict()["network_requested"])
        self.assertEqual({hit.entry.entry_id for hit in retrieval.hits}, {"valuation_pe", "valuation_pb"})

    def test_retrieval_order_is_deterministic_and_request_aliases_are_contiguous(self):
        first = retrieve_knowledge("MACD和RSI怎么搭配技术指标，是否能看出背离")
        for _ in range(5):
            self.assertEqual(first, retrieve_knowledge("MACD和RSI怎么搭配技术指标，是否能看出背离"))
        self.assertEqual([hit.alias for hit in first.hits], ["K" + str(i + 1) for i in range(len(first.hits))])
        self.assertLessEqual(len(first.hits), 4)

    def test_curated_aliases_retrieve_specific_topics(self):
        examples = {
            "T + 1和T+0是什么": "a_share_t1", "ST和退市是什么": "a_share_st",
            "科创板涨跌幅规则怎么理解": "a_share_limits", "怎么理解零股": "a_share_lots",
            "日线缺失是否代表停牌": "a_share_suspension", "前复权与后复权有什么区别": "adjusted_prices",
            "财报怎么看": "financial_statements", "净资产收益率有什么局限": "financial_roe",
            "利润与经营现金流为什么不同": "profit_and_cash", "营收同比怎么算": "financial_periods",
            "仓位怎么计算": "position_sizing", "分散如何影响相关性": "diversification",
            "回撤回本怎么算": "drawdown", "止损与滑点是什么": "stop_and_execution",
            "胜率和盈亏比什么关系": "expectancy_costs", "ADX与方向有什么关系": "technical_adx",
            "OBV等于资金流吗": "technical_obv", "VWMA和SMA怎么比较": "technical_vwma",
            "布林带与ATR怎么搭配": "technical_combinations", "回测怎样避免前视偏差": "replay_biases",
        }
        for question, identifier in examples.items():
            with self.subTest(question=question):
                self.assertIn(identifier, [hit.entry.entry_id for hit in retrieve_knowledge(question).hits])

    def test_nfkc_pe_slashes_and_latin_boundaries_are_consistent(self):
        self.assertEqual(retrieve_knowledge("Ｐ／Ｅ是什么").hits[0].entry.entry_id, "valuation_pe")
        self.assertEqual(retrieve_knowledge("p / b有什么含义").hits[0].entry.entry_id, "valuation_pb")
        self.assertEqual(retrieve_knowledge("open apple abs spectrum").hits, ())

    def test_unknown_topics_do_not_get_incidental_generic_references(self):
        for question in ("DCF是什么", "如何解释期权波动率微笑", "天气怎么样"):
            with self.subTest(question=question):
                result = retrieve_knowledge(question)
                self.assertEqual(result.hits, ())
                self.assertEqual(result.to_dict()["status"], "no_match")

    def test_provenance_explicitly_separates_maintainer_explanation_from_official_reference(self):
        official = retrieve_knowledge("T+1").hits[0].to_dict()
        project = retrieve_knowledge("OBV").hits[0].to_dict()
        self.assertEqual(official["source_type"], "official_reference")
        self.assertEqual(project["source_type"], "project_reference")
        for value in (official, project):
            self.assertEqual(value["content_origin"], "maintainer_explanation")
            self.assertFalse(value["live_verified"])
            self.assertTrue(value["url"].startswith("https://"))
            self.assertTrue(value["reference_version"])
            self.assertEqual(value["maintained_on"], "2026-10-26")

    def test_results_are_immutable_and_serialized_views_are_detached(self):
        result = retrieve_knowledge("PE")
        with self.assertRaises(FrozenInstanceError):
            result.hits[0].entry.excerpt = "changed"
        payload = result.model_context()
        payload["snippets"][0]["excerpt"] = "changed"
        payload["searched_categories"].clear()
        self.assertNotEqual(result.model_context()["snippets"][0]["excerpt"], "changed")
        self.assertTrue(result.categories)

    def test_invalid_queries_and_limits_are_rejected(self):
        for question in (None, "", "  ", "PE\x00", "x" * 2001, 12):
            with self.subTest(question=question), self.assertRaises(ValueError):
                retrieve_knowledge(question)
        for limit in (True, 0, 5, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                retrieve_knowledge("PE", limit=limit)

    def test_custom_corpus_supports_empty_index_and_rejects_duplicate_ids(self):
        entry = retrieve_knowledge("PE").hits[0].entry
        self.assertEqual(KnowledgeRetriever(()).search("PE").hits, ())
        self.assertEqual(KnowledgeRetriever((entry,)).search("PE").corpus_size, 1)
        with self.assertRaises(ValueError):
            KnowledgeRetriever((entry, entry))

    def test_entry_and_source_bounds_are_validated(self):
        entry = retrieve_knowledge("PE").hits[0].entry
        for changes in ({"entry_id": "bad id"}, {"category": "unknown"}, {"excerpt": "x" * 501},
                        {"aliases": ["PE"]}, {"maintained_on": "not-a-date"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(entry, **changes)
        for url in ("http://example.com", "https://user:password@example.com", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                replace(entry.source, url=url)

    def test_retrieval_rejects_forged_aliases_duplicate_hits_and_nonfinite_scores(self):
        result = retrieve_knowledge("PE")
        for hits in ((replace(result.hits[0], alias="K9"),),
                     (replace(result.hits[0], score=float("nan")),),
                     (result.hits[0], replace(result.hits[0], alias="K2"))):
            with self.subTest(hits=hits), self.assertRaises(ValueError):
                replace(result, hits=hits)


class RetrievedEducationTests(unittest.TestCase):
    def test_new_market_rule_topics_are_reachable_through_the_existing_knowledge_route(self):
        for question in ("ST是什么意思", "什么是*ST", "科创板和主板规则有什么区别", "新股涨跌幅怎么理解",
                         "交易日是什么", "申报数量是什么意思", "零股是什么", "复牌是什么意思"):
            with self.subTest(question=question):
                self.assertTrue(is_general_knowledge_question(question))
                model = ContextModel()
                run = answer_general_knowledge(question, model=model)
                self.assertEqual(run.answer.status, "accepted")
                self.assertTrue(run.answer.retrieval.hits)

    def test_default_path_sends_bounded_retrieval_context_and_one_tool_free_call(self):
        model = ContextModel()
        with patch("socket.create_connection", side_effect=AssertionError("No external source fetch")):
            run = answer_general_knowledge("T+1是什么", model=model)
        self.assertEqual((run.answer.status, run.model_calls_attempted), ("accepted", 1))
        messages, tools = model.calls[0]
        self.assertEqual(tools, ())
        self.assertEqual(messages[1].content, "T+1是什么")
        self.assertTrue(messages[2].content.startswith(KNOWLEDGE_CONTEXT_PREFIX))
        payload = run.answer.to_dict()
        citation = payload["citations"][0]
        self.assertEqual(citation["paragraph_indices"], [0])
        self.assertEqual(citation["snippet_id"], "a_share_t1")
        self.assertIn("sse.com.cn", citation["url"])
        self.assertFalse(payload["market_data_requested"])
        self.assertFalse(payload["trading_enabled"])
        self.assertNotIn("question", payload)

    def test_missing_or_invented_references_are_rejected_without_retry(self):
        variants = (None, [], [{"paragraph": 0, "sources": ["K999"]}],
                    [{"paragraph": 0, "sources": ["K1", "K1"]}],
                    [{"paragraph": True, "sources": ["K1"]}],
                    [{"paragraph": -1, "sources": ["K1"]}],
                    [{"paragraph": 1, "sources": ["K1"]}],
                    [{"paragraph": 0, "sources": []}],
                    [{"paragraph": 0, "sources": ["K1"], "url": "https://example.com"}],
                    [{"paragraph": 0, "sources": ["K1"]}, {"paragraph": 0, "sources": ["K1"]}])
        for references in variants:
            payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["PE是估值倍数。"]}
            if references is not None:
                payload["citations"] = references
            with self.subTest(references=references):
                model = ContextModel(payload)
                run = answer_general_knowledge("PE是什么", model=model)
                self.assertEqual(run.answer.code, "general_knowledge_output_invalid")
                self.assertEqual((run.model_calls_attempted, len(model.calls)), (1, 1))
                self.assertEqual(run.answer.to_dict()["citations"], [])

    def test_every_paragraph_requires_a_reference_when_retrieval_matched(self):
        model = ContextModel({"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["PE定义。", "PE假设算例。"],
                              "citations": [{"paragraph": 0, "sources": ["K1"]}]})
        self.assertEqual(answer_general_knowledge("PE是什么", model=model).answer.status, "failed")

    def test_multiple_used_references_are_aggregated_without_inventing_bindings(self):
        payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["PE与PB分母不同。", "两种角度需要结合。"],
                   "citations": [{"paragraph": 1, "sources": ["K2"]}, {"paragraph": 0, "sources": ["K1", "K2"]}]}
        answer = answer_general_knowledge("PE和PB有什么区别", model=ContextModel(payload)).answer
        self.assertEqual(answer.status, "accepted")
        citations = answer.to_dict()["citations"]
        self.assertEqual([item["paragraph_indices"] for item in citations], [[0], [0, 1]])
        self.assertEqual([item["id"] for item in citations], ["K1", "K2"])

    def test_inline_citation_markers_and_model_supplied_urls_are_rejected(self):
        for paragraph in ("PE概念[K1]", "PE概念[1]", "PE概念【来源】", "来源https://example.com"):
            with self.subTest(paragraph=paragraph):
                payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": [paragraph],
                           "citations": [{"paragraph": 0, "sources": ["K1"]}]}
                self.assertEqual(answer_general_knowledge("PE是什么", model=ContextModel(payload)).answer.status, "failed")

    def test_no_match_is_explicit_and_can_continue_as_general_education_without_citations(self):
        run = answer_general_knowledge("DCF是什么", model=ContextModel())
        payload = run.answer.to_dict()
        self.assertEqual(run.answer.status, "accepted")
        self.assertTrue(run.answer.text.startswith("本地知识库暂未覆盖这个主题"))
        self.assertEqual(payload["retrieval"]["status"], "no_match")
        self.assertEqual(payload["citations"], [])
        self.assertEqual(run.model_calls_attempted, 1)

    def test_legacy_no_citation_json_is_only_accepted_without_retrieved_notes(self):
        payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["DCF是折现现金流方法。"]}
        self.assertEqual(answer_general_knowledge("DCF是什么", model=ContextModel(payload)).answer.status, "accepted")
        self.assertEqual(answer_general_knowledge("PE是什么", model=ContextModel(payload)).answer.status, "failed")

    def test_no_match_cannot_cite_a_nonexistent_local_source(self):
        payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["DCF解释。"],
                   "citations": [{"paragraph": 0, "sources": ["K1"]}]}
        self.assertEqual(answer_general_knowledge("DCF是什么", model=ContextModel(payload)).answer.status, "failed")

    def test_explicit_empty_retriever_preserves_opt_in_generic_path(self):
        answer = answer_general_knowledge("PE是什么", model=ContextModel(), retriever=KnowledgeRetriever(())).answer
        self.assertEqual(answer.status, "accepted")
        self.assertEqual(answer.to_dict()["retrieval"]["corpus_size"], 0)
        self.assertEqual(answer.to_dict()["citations"], [])

    def test_bad_retriever_result_fails_before_any_model_call(self):
        for retriever in (object(), type("Bad", (), {"search": lambda self, query: {}})()):
            model = ContextModel()
            run = answer_general_knowledge("PE是什么", model=model, retriever=retriever)
            self.assertEqual(run.answer.code, "general_knowledge_retrieval_unavailable")
            self.assertEqual((run.model_calls_attempted, len(model.calls)), (0, 0))

    def test_old_answer_constructor_keeps_existing_shape_without_a_retrieval_object(self):
        payload = GeneralKnowledgeAnswer("accepted", "general_knowledge_answered", "概念解释。", "private").to_dict()
        self.assertEqual(payload["version"], GENERAL_KNOWLEDGE_VERSION)
        self.assertEqual(payload["paragraphs"], ["概念解释。"])
        self.assertEqual(payload["citations"], [])
        self.assertNotIn("question", payload)
        self.assertNotIn("retrieval", payload)

    def test_citation_serialization_is_detached(self):
        answer = answer_general_knowledge("PE是什么", model=ContextModel()).answer
        payload = answer.to_dict()
        payload["citations"][0]["url"] = "changed"
        payload["citations"][0]["paragraph_indices"].append(99)
        self.assertNotEqual(answer.to_dict()["citations"][0]["url"], "changed")
        self.assertEqual(answer.to_dict()["citations"][0]["paragraph_indices"], [0])

    def test_maximum_plain_input_still_fits_the_real_protocol(self):
        question = "PE是什么？" + "a" * (2000 - len("PE是什么？"))
        model = ContextModel()
        run = answer_general_knowledge(question, model=model)
        self.assertEqual(run.answer.status, "accepted")
        self.assertEqual(run.model_calls_attempted, 1)

    def test_large_chinese_question_drops_whole_lower_ranked_notes_to_fit_protocol(self):
        prefix = "PE、PB、MACD、RSI与财报怎么理解？"
        question = prefix + "学" * (2000 - len(prefix))
        retrieved = retrieve_knowledge(question)
        model = ContextModel()
        run = answer_general_knowledge(question, model=model)
        self.assertEqual(run.answer.status, "accepted")
        self.assertLess(len(run.answer.retrieval.hits), len(retrieved.hits))
        self.assertEqual(run.answer.retrieval.hits, retrieved.hits[:len(run.answer.retrieval.hits)])
        self.assertEqual(run.model_calls_attempted, 1)

    def test_model_cannot_cite_a_retrieved_note_that_was_cut_from_its_context(self):
        prefix = "PE、PB、MACD、RSI与财报怎么理解？"
        question = prefix + "学" * (2000 - len(prefix))
        payload = {"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": ["概念说明。"],
                   "citations": [{"paragraph": 0, "sources": ["K4"]}]}
        run = answer_general_knowledge(question, model=ContextModel(payload))
        self.assertLess(len(run.answer.retrieval.hits), 4)
        self.assertEqual(run.answer.code, "general_knowledge_output_invalid")

    def test_citation_membership_is_not_claimed_to_prove_prose_entailment(self):
        answer = answer_general_knowledge("PE是什么", model=ContextModel()).answer
        self.assertIn("not_prose_semantics", answer.to_dict()["validation_scope"])
        self.assertIn("not_link_fulltext_or_current_rule_verification", answer.to_dict()["retrieval"]["scope"])


if __name__ == "__main__":
    unittest.main()
