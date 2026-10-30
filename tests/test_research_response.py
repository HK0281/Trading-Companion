"""All market/model responses here are artificial; no credentials or HTTP are used."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from trading_companion.agent import run_natural_research_turn
from trading_companion.api_service import public_turn
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelCallError
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_response import RESPONSE_VERSION, validate_research_response
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import TurnControl, TurnLimits


QUESTION = "分析平安银行的走势，预测它接下来的方向，2026-09-21 至 2026-09-25，只看SMA3和RSI2"
PARAGRAPHS = (
    "目前价格处在所选均线上方，短线表现偏强，但还不足以确认持续上涨。",
    "如果价格继续维持在均线上方，偏强走势有望延续；若重新跌回均线下方，这个判断就需要调整。",
    "接下来重点观察价格能否守住均线，以及动量是否配合；当前没有新闻或基本面信息支持进一步判断。",
)


class ResponseModel:
    """Protocol fixture, not a model-quality evaluator."""

    def __init__(self, *, mentions=("平安银行",), short_indicators=True, change=None, error=False, control=None):
        self.mentions, self.short_indicators = mentions, short_indicators
        self.change, self.error, self.control = change, error, control
        self.calls = []

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        self.calls.append(tuple(messages))
        if messages[-1].role == "tool":
            if self.error:
                raise ModelCallError("timeout", "Artificial timeout")
            legacy = ExplanationDemoModel([]).complete(messages, tools=tools)
            payload = json.loads(legacy.text)
            payload["version"] = RESPONSE_VERSION
            alias = payload["claims"][0]["evidence_id"]
            payload["paragraphs"] = [{"role": role, "text": text, "evidence_ids": [alias]}
                for role, text in zip(("answer", "outlook", "watch_for"), PARAGRAPHS)]
            if self.change is not None:
                self.change(payload)
            if self.control is not None:
                self.control.cancel()
            return ToolModelResponse("complete", json.dumps(payload, ensure_ascii=False))
        if tools[0].name == INTENT_TOOL_NAME:
            explicit_dates = "2026-09-21" in messages[-1].content
            arguments = {"intent": "research", "instrument_mentions": list(self.mentions),
                "date_expression": "2026-09-21 至 2026-09-25" if explicit_dates else None,
                "indicator_mentions": ["SMA3", "RSI2"] if self.short_indicators else [],
                "clarification_reason": None}
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent", INTENT_TOOL_NAME, arguments),))
        marker = "\nconfirmed_request="
        if marker in messages[0].content:
            scope = json.JSONDecoder().raw_decode(messages[0].content.split(marker, 1)[1])[0]
        else:
            scope = json.loads(messages[-1].content)["confirmed_request"]
        return ToolModelResponse("tool_calls", tool_calls=(ToolCall("research", TOOL_NAME, scope),))


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.source, self.fixture_directory = load_development_fixtures()
        # Provider-shaped fixtures only: this flag never describes a network read in these tests.
        self.directory = replace(self.fixture_directory, is_fixture=False)
        self.market = type("ProviderShapedFixture", (), {
            "source_name": "tencent", "adjustment": "unadjusted",
            "read_daily_bars": lambda _: self.source.read_daily_bars(),
        })()
        for context in (
            patch("requests.get", side_effect=AssertionError("No HTTP")),
            patch("requests.post", side_effect=AssertionError("No HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No key reads")),
        ):
            context.start()
            self.addCleanup(context.stop)

    def run_turn(self, *, model=None, text=QUESTION, online=True, session=None, control=None):
        model = ResponseModel() if model is None else model
        with patch("trading_companion.research_tool._build_source", return_value=self.market) as source:
            result = run_natural_research_turn(text, model=model,
                directory=self.directory if online else self.fixture_directory,
                source="tencent", development_source=None if online else self.source,
                reference_date=REFERENCE_DATE, session=session, control=control,
                current_time=datetime(2026, 9, 26, 2, tzinfo=timezone.utc))
        return result, model, source

    def payload(self):
        result, model, _ = self.run_turn()
        return result, json.loads(result.model_draft)

    def check(self, result, payload):
        return validate_research_response(json.dumps(payload, ensure_ascii=False),
            execution=result._explanation_execution, expected_request=result.confirmed_request)

    def test_original_question_reaches_proposal_and_final_without_replacing_confirmed_scope(self):
        result, model, source = self.run_turn()
        for messages in model.calls[1:]:
            content = json.loads(messages[1].content)
            self.assertEqual(content["original_question"], QUESTION)
            self.assertEqual(content["confirmed_request"]["instrument_id"], "000001.SZ")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.response_validation.status, "accepted")
        self.assertEqual(result.explanation_validation.status, "accepted")
        self.assertEqual((result.model_calls_attempted, result.analyses_used), (3, 1))
        source.assert_called_once()

    def test_natural_answer_precedes_separate_report_and_maps_citations_to_full_fact_ids(self):
        result, _, _ = self.run_turn()
        value = public_turn(result)
        answer = value["conversation"]
        self.assertEqual(answer["paragraphs"], list(PARAGRAPHS))
        self.assertEqual(answer["origin"], "model")
        self.assertIn("not_verified", answer["grounding_scope"])
        self.assertIn("收盘价", value["answer"])
        self.assertNotIn(result.model_draft, json.dumps(value, ensure_ascii=False))
        identifiers = {fact.fact_id for fact in result.research_result.facts}
        for item in answer["citations"]:
            self.assertTrue(set(item["fact_ids"]) <= identifiers)

    def test_data_and_report_replacement_invalidates_previously_accepted_response(self):
        result, _, _ = self.run_turn()
        self.assertEqual(replace(result, status="failed").conversation_answer["origin"], "application")
        changed = replace(result, confirmed_request=replace(result.confirmed_request, request_id="other-request"))
        self.assertEqual(changed.response_validation.status, "rejected")
        self.assertEqual(changed.conversation_answer["origin"], "application")
        self.assertNotIn(PARAGRAPHS[0], str(changed.conversation_answer))

    def test_wrong_stock_date_request_or_reversed_relation_rejects_whole_narrative(self):
        result, payload = self.payload()
        variants = []
        for key, value in (("instrument_id", "600000.SH"), ("request_id", "other"), ("trade_date", "2026-09-24")):
            changed = deepcopy(payload)
            changed[key] = value
            variants.append(changed)
        changed = deepcopy(payload)
        changed["claims"][0]["value"] = "below"
        variants.append(changed)
        for variant in variants:
            with self.subTest(variant=variant):
                checked = self.check(result, variant)
                self.assertEqual(checked.status, "rejected")
                self.assertEqual(checked.paragraphs, ())

    def test_bad_shapes_empty_text_invalid_citations_or_new_numbers_reject_all(self):
        result, payload = self.payload()
        changes = (
            lambda item: item.update(extra="private-marker"),
            lambda item: item.update(paragraphs=[]),
            lambda item: item["paragraphs"][0].update(role="outlook"),
            lambda item: item["paragraphs"][0].update(text="  "),
            lambda item: item["paragraphs"][0].update(text="x" * 601),
            lambda item: item["paragraphs"][0].update(text="target 99.9"),
            lambda item: item["paragraphs"][0].update(text="上涨概率 90%"),
            lambda item: item["paragraphs"][0].update(text="尚未请求的SMA(90)"),
            lambda item: item["paragraphs"][0].update(evidence_ids=[]),
            lambda item: item["paragraphs"][0].update(evidence_ids=["f9999"]),
            lambda item: item["paragraphs"][0].update(evidence_ids=[{}]),
            lambda item: item["paragraphs"][0].update(evidence_ids=item["paragraphs"][0]["evidence_ids"] * 2),
        )
        for change in changes:
            changed = deepcopy(payload)
            change(changed)
            checked = self.check(result, changed)
            self.assertEqual(checked.status, "rejected", changed)
            self.assertEqual(checked.paragraphs, ())

    def test_exact_requested_indicator_label_can_contain_numbers(self):
        result, payload = self.payload()
        payload["paragraphs"][0]["text"] = "目前价格高于SMA(3)，需要观察能否保持。"
        self.assertEqual(self.check(result, payload).status, "accepted")

    def test_duplicate_json_keys_fences_and_oversize_never_display_prose(self):
        result, payload = self.payload()
        encoded = json.dumps(payload)
        for text in ('{"version":"research-response-v1",' + encoded[1:], "```json\n" + encoded + "\n```", "x" * 8001):
            checked = validate_research_response(text, execution=result._explanation_execution,
                                                expected_request=result.confirmed_request)
            self.assertNotEqual(checked.status, "accepted")
            self.assertEqual(checked.paragraphs, ())

    def test_synthetic_default_five_bars_returns_direct_abstention_without_final_call(self):
        text = "分析平安银行的走势 预测它接下来的方向"
        result, model, source = self.run_turn(text=text, online=False, model=ResponseModel(short_indicators=False))
        self.assertEqual(result.code, "research_not_ready")
        self.assertEqual((len(model.calls), result.analyses_used), (2, 1))
        source.assert_not_called()
        answer = result.conversation_answer
        self.assertEqual(answer["origin"], "application")
        self.assertIn("不能判断平安银行", answer["paragraphs"][0])
        self.assertIn("人工演示行情", answer["paragraphs"][0])
        self.assertIn("5 条", answer["paragraphs"][1])
        self.assertIn("60 条", answer["paragraphs"][1])
        self.assertLess(len("".join(answer["paragraphs"])), 260)
        self.assertNotIn("历史记录不足，还缺", str(answer))

    def test_synthetic_with_valid_relations_still_cannot_describe_real_stock_outlook(self):
        result, _, _ = self.run_turn(online=False)
        self.assertEqual(result.response_validation.status, "accepted")
        self.assertEqual(result.conversation_answer["origin"], "application")
        self.assertNotIn(PARAGRAPHS[0], str(result.conversation_answer))

    def test_online_insufficient_history_avoids_final_model_call(self):
        result, model, _ = self.run_turn(text="分析平安银行的走势", model=ResponseModel(short_indicators=False))
        self.assertEqual(len(model.calls), 2)
        self.assertIn("历史日线不足", result.conversation_answer["paragraphs"][0])
        self.assertNotIn("人工", str(result.conversation_answer))

    def test_model_failure_is_short_and_never_displays_invalid_or_raw_prose(self):
        for model in (ResponseModel(error=True), ResponseModel(change=lambda item: item["paragraphs"][0].update(text="target 99"))):
            result, _, _ = self.run_turn(model=model)
            self.assertEqual(result.status, "limited")
            self.assertEqual(result.conversation_answer["origin"], "application")
            self.assertNotIn("target", str(result.conversation_answer))
            self.assertNotIn(PARAGRAPHS[0], str(result.conversation_answer))

    def test_source_failure_and_empty_data_have_short_relevant_answers(self):
        from trading_companion.errors import DataSourceError
        for bars, error in (([], None), (None, DataSourceError("private source error"))):
            with patch.object(self.market, "read_daily_bars", return_value=bars, side_effect=error):
                result, model, _ = self.run_turn()
            answer = result.conversation_answer
            self.assertEqual(len(model.calls), 2)
            self.assertEqual(answer["origin"], "application")
            self.assertLess(len(str(answer["paragraphs"])), 100)
            self.assertNotIn("private", str(answer))

    def test_original_question_survives_stock_name_clarification(self):
        session = ResearchSession()
        original = QUESTION.replace("平安银行", "平安")
        first, _, _ = self.run_turn(text=original, model=ResponseModel(mentions=("平安",)), session=session)
        self.assertEqual(first.status, "needs_clarification")
        result, model, _ = self.run_turn(text="平安银行", session=session)
        self.assertEqual(result.status, "completed")
        self.assertEqual(json.loads(model.calls[-1][1].content)["original_question"], original)
        self.assertNotIn("user_question", json.dumps(result.session_state))

    def test_cancelled_late_response_and_exhausted_budget_never_promote_model_prose(self):
        control = TurnControl()
        result, _, _ = self.run_turn(model=ResponseModel(control=control), control=control)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(result.conversation_answer)
        result, model, _ = self.run_turn(control=TurnControl(TurnLimits(model_calls=2)))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.failure_code, "model_call_budget_exhausted")
        self.assertNotIn(PARAGRAPHS[0], str(result.conversation_answer))

    def test_read_only_projection_never_reexecutes_or_returns_mutable_shared_citations(self):
        result, model, _ = self.run_turn()
        with patch.object(model, "complete", side_effect=AssertionError("No extra call")), \
                patch.object(self.market, "read_daily_bars", side_effect=AssertionError("No extra read")):
            first = result.conversation_answer
            first["citations"][0]["fact_ids"].append("injected")
            self.assertNotIn("injected", str(result.conversation_answer))

    def test_http_contract_exposes_narrative_and_replays_without_extra_model_calls(self):
        from fastapi.testclient import TestClient
        from trading_companion.api import create_app
        from trading_companion.api_service import ResearchApiService
        model = ResponseModel()
        service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda _: self.directory,
            provider="fake", data_mode="tencent", reference_date=REFERENCE_DATE,
            wall_clock=lambda: datetime(2026, 9, 26, 2, tzinfo=timezone.utc))
        with patch("trading_companion.research_tool._build_source", return_value=self.market), \
                TestClient(create_app(service=service), base_url="http://localhost") as client:
            session = client.post("/v1/sessions", json={}).json()
            path = "/v1/sessions/" + session["session_id"] + "/messages"
            headers = {"Authorization": "Bearer " + session["access_token"]}
            body = {"request_id": "a" * 32, "question": QUESTION}
            response = client.post(path, headers=headers, json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"]["conversation"]["paragraphs"], list(PARAGRAPHS))
            replay = client.post(path, headers=headers, json=body)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(len(model.calls), 3)
            self.assertNotIn("model_draft", response.text)

    def test_live_server_sets_a_bounded_response_budget_without_reading_real_config(self):
        from fastapi.testclient import TestClient
        from trading_companion.api_server import main
        from trading_companion.deepseek_model import DeepSeekConfig
        from trading_companion.deepseek_model import ANSWER_OUTPUT_TOKENS
        from contextlib import redirect_stdout
        from io import StringIO
        configured = DeepSeekConfig("fixture-model", "artificial-test-key")
        def serve(app, **kwargs):
            with TestClient(app, base_url="http://localhost") as client:
                self.assertEqual(client.post("/v1/sessions", json={}).status_code, 201)
        with patch("trading_companion.api_server.load_model_environment", return_value={}), \
                patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", return_value=configured), \
                patch("trading_companion.model_registry.DeepSeekToolModel") as constructor, \
                patch("uvicorn.run", side_effect=serve), redirect_stdout(StringIO()):
            self.assertEqual(main(["--provider", "deepseek", "--data", "synthetic"]), 0)
        self.assertEqual(constructor.call_args.args[0].max_output_tokens, ANSWER_OUTPUT_TOKENS)
        self.assertEqual(configured.max_output_tokens, 512)


if __name__ == "__main__":
    unittest.main()
