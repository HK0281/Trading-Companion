"""Outlook contracts use invented prices and scripted models, not forecast-quality claims."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tests.test_research_workflow import WorkflowScriptModel
from trading_companion.agent import run_natural_research_turn
from trading_companion.api import create_app
from trading_companion.api_service import ResearchApiService, public_turn
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.errors import DataSourceError
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_response import RESPONSE_VERSION
from trading_companion.research_session import ResearchSession
from trading_companion.research_tool import TOOL_NAME
from trading_companion.security_directory import Security
from trading_companion.tool_model import ToolCall, ToolModelResponse, validate_tool_messages


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
HISTORICAL_QUESTION = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2"
PARAGRAPHS = (
    "目前价格处在所选均线上方，但还不能据此确定后续方向。",
    "如果价格维持在均线上方，偏强走势可能延续；若失守则需要调整判断。",
    "接下来观察价格与均线的关系及量能配合，现有证据不能保证上涨。",
)
PRIVATE = "private-outlook-marker-never-export"


class OutlookScriptModel(WorkflowScriptModel):
    """Copy executable scope separately from an untrusted future-horizon phrase."""

    def __init__(self, *, mentions=("中际旭创",), date_expression=None, indicators=(),
                 proposed_cutoff=None, final_change=None, intent="research", clarification_reason=None,
                 outlook_expression=None):
        super().__init__(final_change=final_change)
        self.mentions = tuple(mentions)
        self.date_expression = date_expression
        self.indicators = tuple(indicators)
        self.proposed_cutoff = proposed_cutoff
        self.intent, self.clarification_reason = intent, clarification_reason
        self.outlook_expression = outlook_expression

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        self.calls.append(tuple(messages))
        if tools[0].name == INTENT_TOOL_NAME:
            arguments = {"intent": self.intent, "instrument_mentions": list(self.mentions),
                         "date_expression": self.date_expression,
                         "indicator_mentions": list(self.indicators), "clarification_reason": self.clarification_reason}
            if self.outlook_expression is not None:
                arguments["outlook_expression"] = self.outlook_expression
            return ToolModelResponse("tool_calls", tool_calls=(
                ToolCall("intent_" + str(len(self.calls)), INTENT_TOOL_NAME, arguments),))
        if len(tools) > 1 and self.views(messages):
            return self.final(messages)
        if messages[-1].role == "tool":
            if len(tools) > 1:
                return ToolModelResponse("complete", PRIVATE)
            legacy = ExplanationDemoModel([]).complete(messages, tools=tools)
            payload = json.loads(legacy.text)
            payload["version"] = RESPONSE_VERSION
            alias = payload["claims"][0]["evidence_id"]
            payload["paragraphs"] = [{"role": role, "text": text, "evidence_ids": [alias]}
                for role, text in zip(("answer", "outlook", "watch_for"), PARAGRAPHS)]
            if self.final_change is not None:
                self.final_change(payload)
            return ToolModelResponse("complete", json.dumps(payload, ensure_ascii=False))
        scope = self.scope(messages)
        if self.proposed_cutoff is not None:
            scope["as_of_date"] = self.proposed_cutoff
        return ToolModelResponse("tool_calls", tool_calls=(
            ToolCall("analysis_" + str(len(self.calls)), TOOL_NAME, scope),))


class ResearchOutlookTests(unittest.TestCase):
    def setUp(self):
        self.source, fixture_directory = load_development_fixtures()
        # This provider-shaped directory exists only inside an offline transport contract test.
        self.directory = replace(fixture_directory, is_fixture=False,
            securities=(*fixture_directory.securities, Security("300308.SZ", "中际旭创")))
        self.markets = []
        self.builder = Mock(side_effect=self.build_market)
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market or directory HTTP")),
            patch("requests.post", side_effect=AssertionError("No model HTTP")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials")),
            patch("trading_companion.research_tool._build_source", self.builder),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def artificial_bars(self, instrument_id):
        """Invent weekdays and prices; this is not an exchange calendar or collected history."""
        template = self.source.read_daily_bars()[0]
        days, day = [], date(2026, 9, 25)
        while len(days) < 100:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        bars = []
        for index, day in enumerate(reversed(days)):
            close = 10 + index * 0.025 + (index % 7 - 3) * 0.1
            bars.append(replace(template, instrument_id=instrument_id, trade_date=day,
                open_price=close - 0.05, high_price=close + 0.2, low_price=close - 0.2,
                close_price=close, volume_shares=100000 + index * 100))
        return bars

    def build_market(self, request):
        self.assertLess(request.as_of_date, REFERENCE_DATE)
        market = SimpleNamespace(source_name=request.source, adjustment="unadjusted",
            read_daily_bars=Mock(return_value=self.artificial_bars(request.instrument_id)))
        self.markets.append(market)
        return market

    def turn(self, question, *, model=None, **kwargs):
        options = dict(directory=self.directory, source="tencent", reference_date=REFERENCE_DATE,
                       current_time=NOW, workflow=True)
        options.update(kwargs)
        return run_natural_research_turn(question, model=OutlookScriptModel() if model is None else model, **options)

    def seed(self, session):
        model = OutlookScriptModel(mentions=("平安银行",),
            date_expression="2026-09-21 至 2026-09-25", indicators=("SMA3", "RSI2"))
        result = self.turn(HISTORICAL_QUESTION, model=model, session=session)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        return result

    def assert_historical_scope(self, result, *, instrument="300308.SZ"):
        request = result.confirmed_request
        self.assertIsNotNone(request)
        self.assertEqual(request.instrument_id, instrument)
        self.assertEqual(request.source, "tencent")
        self.assertEqual(request.as_of_date, date(2026, 9, 25))
        self.assertLess(request.as_of_date, REFERENCE_DATE)
        for call in self.builder.call_args_list:
            self.assertLess(call.args[0].as_of_date, REFERENCE_DATE)

    def assert_outlook_context(self, model, question, horizon):
        research_messages = [messages for messages in model.calls if messages[0].content
                             and "\nresearch_objective=" in messages[0].content]
        self.assertTrue(research_messages)
        for messages in research_messages:
            system = messages[0].content
            objective = json.JSONDecoder().raw_decode(system.split("\nresearch_objective=", 1)[1])[0]
            self.assertEqual(objective, {"kind": "outlook", "horizon": horizon})
            user_messages = "\n".join(message.content or "" for message in messages if message.role == "user")
            self.assertIn(question, user_messages)
            scope = OutlookScriptModel.scope(messages)
            self.assertNotIn("objective", scope)
            self.assertNotIn("horizon", scope)

    def test_full_stock_name_alone_uses_defaults_and_reaches_a_natural_answer(self):
        model = OutlookScriptModel()
        result = self.turn("分析一下中际旭创", model=model)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        self.assert_historical_scope(result)
        self.assertEqual(len(result.confirmed_request.indicators), 8)
        self.assertEqual(result.conversation_answer["origin"], "model")
        self.assertEqual(len(result.conversation_answer["paragraphs"]), 3)
        self.builder.assert_called_once()
        self.markets[0].read_daily_bars.assert_called_once()

    def test_complete_single_stock_request_repairs_spurious_model_clarification_or_refusal(self):
        cases = (("clarify", "request"), ("clarify", "date"), ("clarify", "indicator"),
                 ("clarify", "instrument"), ("unsupported", None))
        for intent, reason in cases:
            with self.subTest(intent=intent, reason=reason):
                self.builder.reset_mock()
                model = OutlookScriptModel(intent=intent, clarification_reason=reason)
                result = self.turn("分析一下中际旭创", model=model)
                self.assertEqual(result.status, "completed", (result.code, result.failure_code))
                self.assertEqual(result.understanding.intent, "research")
                self.assert_historical_scope(result)
                self.assertEqual(len(result.confirmed_request.indicators), 8)
                self.builder.assert_called_once()

    def test_bounded_repair_does_not_authorize_unknown_stocks_multiple_stocks_or_unsupported_data(self):
        cases = (
            ("分析一下不存在的股票", ("不存在的股票",), "clarify", "request"),
            ("分析中际旭创和平安银行", ("中际旭创", "平安银行"), "clarify", "instrument"),
            ("分析中际旭创和平安银行", ("中际旭创",), "research", None),
            ("分析中际旭创，然后帮我下单买入", ("中际旭创",), "research", None),
            ("分析中际旭创的新闻和基本面", ("中际旭创",), "research", None),
            ("预测中际旭创下个月的目标价", ("中际旭创",), "research", None),
        )
        for question, mentions, intent, reason in cases:
            with self.subTest(question=question):
                model = OutlookScriptModel(mentions=mentions, intent=intent, clarification_reason=reason)
                result = self.turn(question, model=model)
                self.assertNotEqual(result.status, "completed")
                self.assertIsNone(result.confirmed_request)
                self.assertEqual(result.analyses_used, 0)
                self.builder.assert_not_called()

    def test_model_cannot_repair_a_name_to_an_invented_unmentioned_code(self):
        result = self.turn("分析一下中际旭创",
            model=OutlookScriptModel(mentions=("000001.SZ",), intent="clarify", clarification_reason="instrument"))
        self.assertEqual(result.code, "ungrounded_intent")
        self.assertIsNone(result.confirmed_request)
        self.builder.assert_not_called()

    def test_explicit_stock_outlook_horizons_do_not_become_market_query_dates(self):
        for horizon in ("下个月", "下周", "未来30天", "明天"):
            with self.subTest(horizon=horizon):
                self.builder.reset_mock()
                question = "预测一下中际旭创" + horizon + "的走势"
                model = OutlookScriptModel(date_expression=horizon)
                result = self.turn(question, model=model)
                self.assertEqual(result.status, "completed", (result.code, result.failure_code))
                self.assert_historical_scope(result)
                self.assertEqual(result.confirmed_request.start_date, date(2025, 9, 26))
                self.assert_outlook_context(model, question, horizon)
                self.builder.assert_called_once()

    def test_new_optional_outlook_field_keeps_the_same_historical_only_tool_scope(self):
        question = "预测一下中际旭创下个月的走势"
        model = OutlookScriptModel(outlook_expression="下个月")
        result = self.turn(question, model=model)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        self.assert_historical_scope(result)
        self.assert_outlook_context(model, question, "下个月")

    def test_past_observation_window_and_future_outlook_can_coexist(self):
        question = "结合中际旭创最近三个月的表现，预测下个月的走势"
        model = OutlookScriptModel(date_expression="最近三个月")
        result = self.turn(question, model=model)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        self.assert_historical_scope(result)
        self.assertEqual(result.confirmed_request.start_date, date(2026, 6, 28))
        self.assert_outlook_context(model, question, "下个月")

    def test_explicit_historical_dates_are_not_replaced_by_the_outlook_horizon(self):
        question = "分析中际旭创2026-09-21至2026-09-25的日线，只看SMA3，预测下个月的走势"
        model = OutlookScriptModel(date_expression="2026-09-21至2026-09-25", indicators=("SMA3",))
        result = self.turn(question, model=model)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        self.assert_historical_scope(result)
        self.assertEqual(result.confirmed_request.start_date, date(2026, 9, 21))
        self.assert_outlook_context(model, question, "下个月")

    def test_outlook_context_reaches_the_compatible_fixed_flow_as_well(self):
        question = "预测一下中际旭创下个月的走势"
        model = OutlookScriptModel(date_expression="下个月")
        result = self.turn(question, model=model, workflow=False)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        self.assertEqual(result.response_validation.status, "accepted")
        self.assert_historical_scope(result)
        self.assert_outlook_context(model, question, "下个月")

    def test_outlook_without_a_stock_or_confirmed_context_does_not_query_prices(self):
        for question in ("那下个月呢", "预测一下它下周的走势"):
            with self.subTest(question=question):
                result = self.turn(question, model=OutlookScriptModel(mentions=(), date_expression="下个月"),
                                   session=ResearchSession())
                self.assertNotEqual(result.status, "completed")
                self.assertIsNone(result.confirmed_request)
                self.assertEqual(result.analyses_used, 0)
        self.builder.assert_not_called()

    def test_contextual_outlook_researches_inherited_history_with_a_new_request_id(self):
        for question, horizon in (("那下个月呢", "下个月"), ("预测一下它下周的走势", "下周")):
            with self.subTest(question=question):
                self.builder.reset_mock()
                session = ResearchSession()
                initial = self.seed(session)
                model = OutlookScriptModel(mentions=(), date_expression=horizon)
                result = self.turn(question, model=model, session=session)
                self.assertEqual(result.status, "completed", (result.code, result.failure_code))
                self.assert_historical_scope(result, instrument="000001.SZ")
                first, second = initial.confirmed_request, result.confirmed_request
                self.assertNotEqual(first.request_id, second.request_id)
                self.assertEqual((second.start_date, second.as_of_date, second.indicators),
                                 (first.start_date, first.as_of_date, first.indicators))
                self.assertEqual(self.builder.call_count, 2)
                self.assertEqual(result.analyses_used, 1)
                self.assert_outlook_context(model, question, horizon)

    def test_contextual_outlook_cannot_inherit_expired_changed_source_or_changed_identity(self):
        changed_directory = replace(self.directory, securities=tuple(
            item for item in self.directory.securities if item.instrument_id != "000001.SZ"))
        stale_directory = replace(self.directory, retrieved_at=self.directory.retrieved_at - timedelta(days=8))
        cases = (({"current_time": NOW + timedelta(minutes=15)}, "research_context_expired"),
                 ({"current_time": NOW - timedelta(seconds=1)}, "research_context_expired"),
                 ({"source": "akshare_eastmoney"}, "research_context_configuration_changed"),
                 ({"directory": changed_directory}, "research_context_identity_changed"),
                 ({"directory": stale_directory}, "directory_unusable"))
        for options, expected_code in cases:
            with self.subTest(code=expected_code):
                session = ResearchSession()
                self.seed(session)
                self.builder.reset_mock()
                model = OutlookScriptModel(mentions=(), date_expression="下个月")
                result = self.turn("那下个月呢", model=model, session=session, **options)
                self.assertEqual(result.code, expected_code)
                self.assertEqual((result.model_calls_attempted, result.analyses_used), (0, 0))
                self.builder.assert_not_called()

    def test_failed_new_outlook_does_not_relabel_the_saved_answer_as_current(self):
        session = ResearchSession()
        initial = self.seed(session)
        failed_market = SimpleNamespace(source_name="tencent", adjustment="unadjusted",
            read_daily_bars=Mock(side_effect=DataSourceError(PRIVATE)))
        self.builder.side_effect = None
        self.builder.return_value = failed_market
        result = self.turn("那下个月呢", model=OutlookScriptModel(mentions=(), date_expression="下个月"),
                           session=session)
        self.assertNotEqual(result.status, "completed")
        self.assertNotEqual(result.confirmed_request.request_id, initial.confirmed_request.request_id)
        self.assertNotIn(PRIVATE, json.dumps(public_turn(result)))
        self.assertEqual(result.conversation_answer["origin"], "application")
        self.assertNotIn(initial.conversation_answer["paragraphs"][0], result.conversation_answer["paragraphs"])
        lookup = session.saved_result(directory=self.directory, source="tencent", development_source=None,
            reference_date=REFERENCE_DATE, now=NOW)
        self.assertNotEqual(lookup.status, "ready")

    def test_future_date_proposed_by_the_model_is_rejected_before_any_market_read(self):
        for workflow in (True, False):
            with self.subTest(workflow=workflow):
                model = OutlookScriptModel(date_expression="下个月", proposed_cutoff="2026-10-31")
                result = self.turn("预测一下中际旭创下个月的走势", model=model, workflow=workflow)
                self.assertNotEqual(result.status, "completed")
                self.assertEqual(result.analyses_used, 0)
                self.assert_historical_scope(result)
                self.builder.assert_not_called()

    def test_numeric_prediction_prices_are_not_published_as_accepted_model_answers(self):
        for workflow in (True, False):
            with self.subTest(workflow=workflow):
                model = OutlookScriptModel(date_expression="下个月",
                    final_change=lambda payload: payload["paragraphs"][1].update(text="下个月目标价为999.9元。"))
                result = self.turn("预测一下中际旭创下个月的走势", model=model, workflow=workflow)
                self.assertEqual(result.response_validation.status, "rejected")
                self.assertEqual(result.conversation_answer["origin"], "application")
                self.assertNotIn("999.9", json.dumps(public_turn(result)))
                self.assertIsNotNone(result.research_result)

    def test_ambiguous_name_clarification_preserves_the_original_outlook_horizon(self):
        session = ResearchSession()
        original = "预测平安下个月的走势，只看SMA3"
        first_model = OutlookScriptModel(mentions=("平安",), date_expression="下个月", indicators=("SMA3",))
        first = self.turn(original, model=first_model, session=session)
        self.assertEqual(first.status, "needs_clarification")
        self.assertEqual(first.analyses_used, 0)
        self.builder.assert_not_called()
        model = OutlookScriptModel(mentions=("平安银行",))
        result = self.turn("平安银行", model=model, session=session)
        self.assertEqual(result.status, "completed", (result.code, result.failure_code))
        self.assert_historical_scope(result, instrument="000001.SZ")
        self.assert_outlook_context(model, original, "下个月")

    def test_api_contextual_outlook_replays_idempotently_without_extra_model_or_market_work(self):
        model = OutlookScriptModel(mentions=("平安银行",),
            date_expression="2026-09-21 至 2026-09-25", indicators=("SMA3", "RSI2"))
        service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda _: self.directory,
            provider="fake", data_mode="tencent", reference_date=REFERENCE_DATE, wall_clock=lambda: NOW)
        with TestClient(create_app(service=service), base_url="http://localhost") as client:
            credentials = client.post("/v1/sessions", json={}).json()
            path = "/v1/sessions/" + credentials["session_id"] + "/messages"
            headers = {"Authorization": "Bearer " + credentials["access_token"]}
            initial = client.post(path, headers=headers,
                json={"request_id": "a" * 32, "question": HISTORICAL_QUESTION})
            self.assertEqual(initial.status_code, 200)
            self.assertEqual(initial.json()["result"]["status"], "completed")
            body = {"request_id": "b" * 32, "question": "那下个月呢"}
            response = client.post(path, headers=headers, json=body)
            self.assertEqual(response.status_code, 200)
            turn = response.json()["result"]
            self.assertEqual(turn["status"], "completed", turn["code"])
            self.assertEqual(turn["parameters"]["instrument_id"], "000001.SZ")
            self.assertEqual(turn["parameters"]["as_of_date"], "2026-09-25")
            self.assertNotEqual(turn["report"]["request_id"], initial.json()["result"]["report"]["request_id"])
            self.assertEqual(self.builder.call_count, 2)
            calls = len(model.calls)
            replay = client.post(path, headers=headers, json=body)
            self.assertTrue(replay.json()["replayed"])
            self.assertEqual(replay.json()["result"], turn)
            self.assertEqual(len(model.calls), calls)
            self.assertEqual(self.builder.call_count, 2)


if __name__ == "__main__":
    unittest.main()
