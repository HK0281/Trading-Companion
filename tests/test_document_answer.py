"""Offline current-answer checks for source excerpts, stable references and exact literals."""

from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from trading_companion.product_answer import synthesize_product_answer
from trading_companion.product_narrative_answer import _Checker, _context, _messages, _trim_answer_context
from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle, model_research_context
from trading_companion.research_documents import DocumentCandidate, ResearchDocument
from trading_companion.tool_model import MAX_TOTAL_CHARACTERS, validate_tool_messages
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import NOW, SECURITIES, plan


BODY_TEXT = "公司披露业务收入同比增长3.14%，新增资金314万元，相关业务保持稳定。"
UNREAD_TEXT = "未提供的全文另外记载987654.32万元，该段不得用于回答。"


def document_bundle(*, count=1, title_characters=30, body_companies=None, excerpts_per_document=1, shared_text=False,
                    complete=True):
    securities = SECURITIES[:count]
    selected = plan(securities=securities, kinds=("financial", "technical", "news", "announcements"))
    companies, supplemental, items = [], [], []
    enabled = set(range(count)) if body_companies is None else set(body_companies)
    for index, security in enumerate(securities):
        prefix = security.instrument_id
        financial = ProductEvidence(prefix + ":financial", prefix, "financial", "营业收入", "1.23亿元",
            "fixture_financial", date="2026-06-30", raw_value=123_000_000, unit="cny")
        technical = ProductEvidence(prefix + ":close", prefix, "technical", "收盘价", "10.00元",
            "tencent", date=selected.end_date.isoformat(), raw_value=10.0, unit="CNY/share")
        titles = tuple(ProductEvidence(prefix + ":news:" + str(number), prefix, "news", "news_title",
            security.name + "业务进展" + "相关情况需结合来源核对。" * (title_characters // 12),
            "sina_company_news", date=selected.end_date.isoformat(),
            url="https://finance.sina.com.cn/stock/doc-" + str(index) + str(number) + "abc.shtml",
            content_kind="title", publication_precision="date", retrieved_at=NOW.isoformat(), unit="text")
            for number in range(5))
        announcement = ProductEvidence(prefix + ":announcement", prefix, "announcements", "announcement_title",
            security.name + "董事会公告", "eastmoney_announcements", content_kind="title", unit="text")
        companies.append(ProductCompany(prefix, security.name, (financial, technical, *titles, announcement), ()))
        if index not in enabled:
            continue
        parent = titles[0]
        candidate = DocumentCandidate(parent.id, prefix, security.name, parent.kind, parent.source,
            parent.display, parent.url, parent.date)
        text = BODY_TEXT if index == 0 or shared_text else "该公司披露自身业务收入同比增长4.23%，相关业务保持稳定。"
        document = ResearchDocument(candidate, (text, "仅用于填充原文的背景。" * 60, UNREAD_TEXT),
                                    NOW.isoformat(), "sina-artibody-v3", complete=complete,
                                    incomplete_reasons=() if complete else ('document_tables_omitted',),
                                    omitted_tables=0 if complete else 1)
        chosen = [text]
        if excerpts_per_document == 2:
            chosen.append(document.paragraphs[1][:480])
        excerpts = []
        for number, value in enumerate(chosen, 1):
            digest = sha256(value.encode("utf-8")).hexdigest()
            metadata = dict(document_id=document.id, parent_title_id=parent.id, sha256=document.content_sha256,
                            paragraph_index=number, excerpt_sha256=digest, extractor_version=document.extractor_version,
                            document_complete=document.complete)
            supplemental.append(replace(parent, id=document.id + ":" + str(number), label="正文片段", display=value,
                content_kind="body_excerpt", raw_value=None, document=metadata, dependencies=(parent.id,)))
            excerpts.append(dict(paragraph_index=number, text=value, sha256=digest))
        items.append(dict(instrument_id=prefix, kind="news", parent_title_id=parent.id, title=parent.display,
            url=parent.url, source=parent.source, status="available",
            code="document_read" if complete else "document_tables_omitted", document_id=document.id,
            **document.coverage_metadata(),
            sha256=document.content_sha256, extractor_version=document.extractor_version,
            extracted_characters=len(document.text), paragraph_count=len(document.paragraphs), excerpts=excerpts,
            retrieved_at=document.retrieved_at, published_date=parent.date, publication_precision="date"))
    return ProductResearchBundle(selected, tuple(companies), tuple(supplemental),
        dict(version="research-documents-v1", status="available" if complete else "partial",
             code="documents_read" if complete else "documents_partial", items=items))


class DocumentAnswerTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No network in document-answer tests")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def body_record(self, context, instrument=SECURITIES[0].instrument_id):
        return next(item for item in context["evidence"]
                    if item["instrument_id"] == instrument and item["content_kind"] == "body_excerpt")

    def claim(self, text, *, bundle=None, context=None, references=None):
        bundle = bundle or document_bundle()
        context = _context(bundle) if context is None else context
        record = self.body_record(context)
        checker = _Checker(bundle, context)
        output = checker.claim(dict(text=text, evidence_ids=references or [record["id"]]),
                               SECURITIES[0].instrument_id, "drivers")
        return output, checker

    def test_three_company_original_reference_mapping_stays_stable_and_bodies_are_global_suffix(self):
        bundle = document_bundle(count=3)
        base = replace(bundle, supplemental_evidence=(), documents=None)
        before = {item.id: "E" + str(index) for index, item in enumerate(base.evidence, 1)}
        after = {item.id: "E" + str(index) for index, item in enumerate(bundle.evidence, 1)}
        self.assertEqual(before, {identifier: after[identifier] for identifier in before})
        context = model_research_context(bundle)
        original_ids = set(before.values())
        body = [r for r in context["evidence"] if r["content_kind"] == "body_excerpt"]
        self.assertEqual({r["instrument_id"] for r in body}, {s.instrument_id for s in SECURITIES[:3]})
        self.assertTrue(all(int(r["id"][1:]) > len(base.evidence) for r in body))
        self.assertTrue(all(r["id"] in original_ids for r in context["evidence"] if r not in body))
        for record in body:
            self.assertIn(before[record["document"]["parent_title_id"]], {r["id"] for r in context["evidence"]})

    def test_full_context_only_replaces_same_stock_same_domain_nonparent_titles(self):
        bundle = document_bundle(count=3, title_characters=480, body_companies=(0,))
        base = replace(bundle, supplemental_evidence=(), documents=None)
        baseline = model_research_context(base)
        context = model_research_context(bundle)
        before = {item["id"]: item for item in baseline["evidence"]}
        after = {item["id"]: item for item in context["evidence"]}
        self.assertTrue(any(item["content_kind"] == "body_excerpt" for item in after.values()))
        removed = set(before) - set(after)
        self.assertTrue(removed, "Dense fixture must exercise removal instead of only spare capacity")
        parent = "E" + str(next(i for i, item in enumerate(base.evidence, 1) if item.id.endswith(":news:0")))
        for identifier in removed:
            item = before[identifier]
            self.assertEqual((item["instrument_id"], item["kind"], item["content_kind"]),
                             (SECURITIES[0].instrument_id, "news", "title"))
            self.assertNotEqual(identifier, parent)
        for identifier, item in before.items():
            if item["kind"] in ("financial", "technical") or item["instrument_id"] != SECURITIES[0].instrument_id:
                self.assertEqual(after[identifier], item)
        self.assertIn(parent, after)

    def test_document_and_complete_request_budgets_keep_parent_and_never_expose_fulltext_raw_value(self):
        bundle = document_bundle(count=3, title_characters=480, excerpts_per_document=2)
        context = model_research_context(bundle)
        offered = [item for item in context["evidence"] if item["content_kind"] == "body_excerpt"]
        self.assertTrue(offered)
        self.assertLessEqual(len(json.dumps(offered, ensure_ascii=False)), 3000)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 14500)
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertNotIn(UNREAD_TEXT, encoded)
        self.assertNotIn("987654.32", encoded)
        self.assertTrue(all(item.raw_value is None for item in bundle.supplemental_evidence))
        model = NarrativeModel()
        synthesize_product_answer("分析新闻和公告", bundle, model=model, repair_invalid=False)
        self.assertTrue(model.calls)
        for messages in model.calls:
            validate_tool_messages(messages, tools=[])
            self.assertLessEqual(sum(len(json.dumps(message.to_dict(), ensure_ascii=True)) for message in messages),
                                 MAX_TOTAL_CHARACTERS)
            self.assertNotIn(UNREAD_TEXT, json.dumps([m.content for m in messages], ensure_ascii=False))

    def test_partial_flag_reaches_the_final_model_request_with_original_numbers_and_budgets(self):
        bundle = document_bundle(complete=False)
        model = NarrativeModel()
        result = synthesize_product_answer("分析新闻和公告", bundle, model=model, repair_invalid=False)
        self.assertTrue(model.calls)
        context = json.loads(model.calls[0][2].content.split("\n", 1)[1])
        offered = [r for r in context['evidence'] if r['content_kind'] == 'body_excerpt']
        self.assertTrue(offered)
        self.assertTrue(all(r['document']['document_complete'] is False for r in offered))
        self.assertLessEqual(len(json.dumps(offered, ensure_ascii=False)), 3000)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 14500)
        validate_tool_messages(model.calls[0], tools=[])
        self.assertEqual(result.answer.documents['status'], 'partial')
        self.assertTrue(all(i['complete'] is False for i in result.answer.documents['items']))
        accepted, checker = self.claim("正文披露业务收入同比增长3.14%。", bundle=bundle)
        self.assertIsNotNone(accepted)
        self.assertEqual(checker.issues, [])
        rejected, checker = self.claim("正文披露业务收入同比增长3.1%。", bundle=bundle)
        self.assertIsNone(rejected)
        self.assertTrue(checker.issues)

    def test_final_request_capacity_drops_optional_bodies_before_additional_baseline_evidence(self):
        bundle = document_bundle(count=3, title_characters=480, body_companies=(0,))
        context = _context(bundle)
        baseline = _context(replace(bundle, supplemental_evidence=(), documents=None))
        self.assertTrue(any(r["content_kind"] == "body_excerpt" for r in context["evidence"]))
        question = "Analyze the supplied evidence. " + "x" * 1850
        _trim_answer_context(question, context)
        _trim_answer_context(question, baseline)
        self.assertTrue(context["evidence"])
        self.assertFalse(any(r["content_kind"] == "body_excerpt" for r in context["evidence"]))
        self.assertEqual(context["evidence"], baseline["evidence"])
        self.assertEqual(context["price_levels"], baseline["price_levels"])
        validate_tool_messages(_messages(question, context), tools=())

    def test_only_offered_excerpts_supply_numeric_literals(self):
        bundle = document_bundle()
        accepted, checker = self.claim("正文披露业务收入同比增长3.14%。", bundle=bundle)
        self.assertIsNotNone(accepted)
        self.assertEqual(checker.issues, [])
        rejected, checker = self.claim("正文披露新增资金987654.32万元。", bundle=bundle)
        self.assertIsNone(rejected)
        self.assertTrue(checker.issues)
        context = _context(bundle)
        omitted = self.body_record(context)["id"]
        context["evidence"] = [r for r in context["evidence"] if r["id"] != omitted]
        checker = _Checker(bundle, context)
        self.assertIsNone(checker.claim(dict(text="正文披露同比增长3.14%。", evidence_ids=[omitted]),
                                       SECURITIES[0].instrument_id, "drivers"))

    def test_excerpt_numbers_disallow_rounding_percentage_scaling_and_amount_unit_conversion(self):
        for text in ("正文披露收入同比增长3.1%。", "正文披露收入同比增长31.4%。",
                     "正文披露收入同比增长3.140%。", "正文披露新增资金0.0314亿元。",
                     "正文披露新增资金3140000元。"):
            with self.subTest(text=text):
                output, checker = self.claim(text)
                self.assertIsNone(output)
                self.assertTrue(checker.issues)
        output, checker = self.claim("正文披露新增资金314万元。")
        self.assertIsNotNone(output)
        self.assertEqual(checker.issues, [])

    def test_existing_structured_financial_unit_conversion_remains_available(self):
        bundle = document_bundle()
        context = _context(bundle)
        financial = next(r for r in context["evidence"] if r["kind"] == "financial")
        checker = _Checker(bundle, context)
        output = checker.claim(dict(text="营业收入为12300万元。", evidence_ids=[financial["id"]]),
                               SECURITIES[0].instrument_id, "drivers")
        self.assertIsNotNone(output)
        self.assertEqual(checker.issues, [])

    def test_exact_optional_quote_requires_the_selected_body_reference(self):
        text = "原文写道「相关业务保持稳定」。"
        output, checker = self.claim(text)
        self.assertIsNotNone(output)
        self.assertEqual(checker.issues, [])
        bundle = document_bundle()
        context = _context(bundle)
        parent = next(r["id"] for r in context["evidence"] if r["content_kind"] == "title")
        output, checker = self.claim(text, bundle=bundle, context=context, references=[parent])
        self.assertIsNone(output)
        self.assertTrue(checker.issues)

    def test_mismatching_quote_and_cross_stock_body_are_rejected(self):
        output, checker = self.claim("原文写道「相关业务高速增长」。")
        self.assertIsNone(output)
        self.assertTrue(checker.issues)
        bundle = document_bundle(count=2)
        context = _context(bundle)
        other = self.body_record(context, SECURITIES[1].instrument_id)
        output, checker = self.claim("原文写道「相关业务保持稳定」。", bundle=bundle,
                                     context=context, references=[other["id"]])
        self.assertIsNone(output)
        self.assertTrue(checker.issues)

    def test_company_with_no_offered_body_cannot_claim_an_unreferenced_verbatim_quote(self):
        bundle = document_bundle(count=2, body_companies=(0,))
        context = _context(bundle)
        for drop_all_bodies in (False, True):
            with self.subTest(drop_all_bodies=drop_all_bodies):
                candidate = {**context, "evidence": [r for r in context["evidence"]
                    if not drop_all_bodies or r["content_kind"] != "body_excerpt"]}
                checker = _Checker(bundle, candidate)
                output = checker.claim(dict(text="原文写道「完全不存在的引句」。"),
                                       SECURITIES[1].instrument_id, "drivers")
                self.assertIsNone(output)
                self.assertTrue(checker.issues)

    def test_same_text_in_two_companies_does_not_publish_unoffered_company_excerpt(self):
        from trading_companion.document_collection import documents_for_context
        bundle = document_bundle(count=2, shared_text=True)
        context = _context(bundle)
        context["evidence"] = [r for r in context["evidence"] if r["content_kind"] != "body_excerpt"
                               or r["instrument_id"] == SECURITIES[0].instrument_id]
        result = documents_for_context(bundle, context)
        self.assertTrue(result["items"][0]["excerpts"])
        self.assertEqual(result["items"][1]["excerpts"], [])

    def test_saved_unoffered_candidate_can_be_marked_as_supplied_in_a_later_answer(self):
        from trading_companion.document_collection import documents_for_context
        bundle = document_bundle(excerpts_per_document=2)
        prior = documents_for_context(bundle, {})
        saved = replace(bundle, documents=prior)
        candidate = saved.supplemental_evidence[1]
        context = dict(evidence=[dict(content_kind='body_excerpt', document=candidate.document)])
        current = documents_for_context(saved, context)
        self.assertEqual(prior['items'][0]['excerpts'], [])
        self.assertEqual(current['items'][0]['excerpts'], [dict(
            paragraph_index=candidate.document['paragraph_index'], text=candidate.display,
            sha256=candidate.document['excerpt_sha256'])])

    def test_repair_reuses_identical_context_and_publishes_only_offered_excerpts(self):
        bundle = document_bundle(count=2)
        def change(payload, attempt, context):
            record = self.body_record(context)
            quote = "相关业务高速增长" if attempt == 1 else "相关业务保持稳定"
            payload["narratives"][0]["drivers"][0] = dict(title="原文依据",
                text="原文写道「" + quote + "」。", evidence_ids=[record["id"]])
        model = NarrativeModel(change)
        run = synthesize_product_answer("分析新闻和公告", bundle, model=model, repair_invalid=True)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[0][2].content, model.calls[1][2].content)
        self.assertNotIn("相关业务高速增长", run.answer.text)
        self.assertIn("相关业务保持稳定", run.answer.text)
        context = json.loads(model.calls[0][2].content.split("\n", 1)[1])
        supplied = {r["document"]["excerpt_sha256"] for r in context["evidence"] if r["content_kind"] == "body_excerpt"}
        public = run.answer.to_dict()["documents"]
        self.assertTrue(public["items"])
        self.assertTrue(all(excerpt["sha256"] in supplied for item in public["items"] for excerpt in item.get("excerpts", [])))
        self.assertNotIn(UNREAD_TEXT, json.dumps(public, ensure_ascii=False))

    def test_public_evidence_contains_only_the_body_candidates_offered_to_the_model(self):
        bundle = document_bundle(count=2, excerpts_per_document=2)
        model = NarrativeModel()
        run = synthesize_product_answer("分析新闻和公告", bundle, model=model)
        self.assertEqual(len(model.calls), 1)
        context = json.loads(model.calls[0][2].content.split("\n", 1)[1])
        offered_ids = {r['id'] for r in context['evidence'] if r['content_kind'] == 'body_excerpt'}
        expected = {item.id for index, item in enumerate(bundle.evidence, 1) if f'E{index}' in offered_ids}
        self.assertTrue(expected)
        self.assertLess(len(expected), len(bundle.supplemental_evidence))
        self.assertEqual({item.id for item in run.answer.evidence if item.content_kind == 'body_excerpt'}, expected)
        self.assertEqual([item for item in run.answer.evidence if item.content_kind != 'body_excerpt'],
                         [item for item in bundle.evidence if item.content_kind != 'body_excerpt'])
        public = run.answer.to_dict()
        excerpts = {(item['document_id'], e['paragraph_index'], e['sha256'])
                    for item in public['documents']['items'] for e in item.get('excerpts', [])}
        evidence = {(e['document']['document_id'], e['document']['paragraph_index'], e['document']['excerpt_sha256'])
                    for e in public['evidence'] if e.get('content_kind') == 'body_excerpt'}
        self.assertEqual(evidence, excerpts)

    def test_no_model_request_publishes_no_candidate_body_excerpts(self):
        bundle = document_bundle()
        model = NarrativeModel()
        with patch('trading_companion.product_narrative_answer._trim_answer_context', side_effect=ValueError):
            run = synthesize_product_answer("分析新闻和公告", bundle, model=model)
        self.assertEqual(model.calls, [])
        self.assertFalse(any(e.content_kind == 'body_excerpt' for e in run.answer.evidence))
        self.assertTrue(all(not item.get('excerpts') for item in run.answer.to_dict()['documents']['items']))


if __name__ == "__main__":
    unittest.main()
