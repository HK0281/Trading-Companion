from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from trading_companion.research_session import ResearchSession
from trading_companion.screening import demo_screening_snapshot
from trading_companion.screening_agent import SCREEN_TOOL, is_screening_request, run_screening_turn
from trading_companion.tool_model import ToolCall, ToolModelResponse


NOW = datetime(2026, 10, 26, 5, tzinfo=timezone.utc)


class Source:
    def __init__(self): self.calls = 0
    def read_snapshot(self, directory):
        self.calls += 1
        return demo_screening_snapshot(clock=lambda: NOW)


class Model:
    def __init__(self, conditions=(), **changes):
        self.payload = {"intent": "screen", "conditions": list(conditions), "sort_by": "amount_cny",
                        "sort_direction": "desc", "limit": 5, **changes}
        self.calls = 0
    def complete(self, messages, *, tools):
        self.calls += 1
        return ToolModelResponse("tool_calls", tool_calls=(ToolCall("screening", SCREEN_TOOL.name, self.payload),))


class ScreeningAgentTests(unittest.TestCase):
    def run_turn(self, question, model=None, source=None):
        model, source = model or Model(), source or Source()
        with patch("socket.socket.connect", side_effect=AssertionError("No external requests")):
            result = run_screening_turn(question, session=ResearchSession(), model=model,
                directory=demo_screening_snapshot(clock=lambda: NOW).directory,
                source=source, now=NOW, clock=lambda: NOW)
        return result, source, model

    def test_explicit_two_conditions_are_grounded_and_read_one_snapshot(self):
        conditions = ({"field": "pe_dynamic", "operator": "lt", "value": 20, "phrase": "PE低于20"},
                      {"field": "turnover_percent", "operator": "gt", "value": 2, "phrase": "换手率大于2%"})
        result, source, model = self.run_turn("筛选PE低于20且换手率大于2%，按成交额降序选5只", Model(conditions))
        self.assertEqual(result.status, "completed")
        self.assertEqual(source.calls, 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(result.analyses_used, 0)
        self.assertIn("300001.SZ", result.display_answer)
        self.assertFalse(result.screening_result.to_dict()["trading_enabled"])

    def test_default_is_explicitly_labelled_activity_not_potential_forecast(self):
        result, source, _ = self.run_turn("帮我选几个潜力股")
        self.assertEqual(result.status, "completed")
        self.assertIn("未指定数值条件", result.display_answer)
        self.assertIn("人工样例", result.display_answer)
        from trading_companion.api_service import public_turn
        self.assertIn("screening:activity_shortlist", public_turn(result)["applied_defaults"])
        self.assertEqual(source.calls, 1)

    def test_wrong_threshold_operator_and_field_never_fetch_quotes(self):
        for item in ({"field": "pe_dynamic", "operator": "lt", "value": 30, "phrase": "PE低于20"},
                     {"field": "pe_dynamic", "operator": "gt", "value": 20, "phrase": "PE低于20"},
                     {"field": "pb", "operator": "lt", "value": 20, "phrase": "PE低于20"}):
            with self.subTest(item=item):
                result, source, _ = self.run_turn("筛选PE低于20", Model((item,)))
                self.assertEqual(result.code, "screening_intent_invalid")
                self.assertEqual(source.calls, 0)

    def test_omitted_condition_never_silently_broadens_the_screen(self):
        item = {"field": "pe_dynamic", "operator": "lt", "value": 20, "phrase": "PE低于20"}
        result, source, _ = self.run_turn("筛选PE低于20且换手率大于2%", Model((item,)))
        self.assertEqual(result.code, "screening_intent_invalid")
        self.assertEqual(source.calls, 0)

    def test_limit_and_order_are_bound_to_question(self):
        for question, model in (("选股按成交额升序选5只", Model()),
                                ("选股按成交额降序选10只", Model(limit=5)),
                                ("选股按成交额", Model(sort_by="pb"))):
            with self.subTest(question=question):
                result, source, _ = self.run_turn(question, model)
                self.assertEqual(result.code, "screening_intent_invalid")
                self.assertEqual(source.calls, 0)

    def test_plain_activity_defaults_do_not_depend_on_model_clarification_or_invented_thresholds(self):
        for model in (Model(intent="clarify"), Model(({
                "field": "pe_dynamic", "operator": "lt", "value": 10, "phrase": "PE低于10"},))):
            with self.subTest(payload=model.payload):
                result, source, _ = self.run_turn("帮我选几个潜力股", model)
                self.assertEqual(result.status, "completed")
                self.assertTrue(result.screening_defaulted)
                self.assertEqual(source.calls, 1)
                self.assertEqual(result.screening_result.request.conditions[0].field, "price")
                self.assertEqual(result.screening_result.request.limit, 5)

    def test_unsupported_and_trade_conditions_do_not_call_a_model_or_fetch_data(self):
        for question in ("筛选ROE大于20的股票", "筛选非ST股票", "筛选后自动买入", "筛选去年涨幅最高股票"):
            with self.subTest(question=question):
                _, source, model = self.run_turn(question)
                self.assertEqual((model.calls, source.calls), (0, 0))

    def test_educational_screening_methods_do_not_route_to_quote_screen(self):
        self.assertFalse(is_screening_request("如何使用PE筛选股票？"))
        self.assertTrue(is_screening_request("请筛选PE低于20的股票"))
        for question in ("帮我选几个潜力股", "推荐几只潜力股", "哪些股票值得关注", "找几只股票"):
            with self.subTest(question=question):
                self.assertTrue(is_screening_request(question))

    def test_partial_supported_conditions_do_not_drop_unsupported_clauses(self):
        for question in ("筛选PE低于20或PB小于1的股票", "筛选PE低于20且股价在10到20之间的股票",
                         "筛选科创板PE低于20的股票", "筛选银行行业PE低于20的股票",
                         "筛选滚动市盈率低于20的股票", "筛选PE低于20按成交额选三只"):
            with self.subTest(question=question):
                result, source, model = self.run_turn(question)
                self.assertEqual(result.code, "screening_condition_not_supported")
                self.assertEqual((model.calls, source.calls), (0, 0))

    def test_explicit_stock_universe_cannot_be_replaced_with_market_sample(self):
        result, source, model = self.run_turn("从平安银行和中芯国际中筛选PE低于20的股票")
        self.assertEqual(result.code, "screening_universe_not_supported")
        self.assertEqual((model.calls, source.calls), (0, 0))

    def test_float_cap_condition_cannot_be_changed_to_total_cap(self):
        item = {"field": "market_cap_cny", "operator": "lt", "value": 10000000000,
                "phrase": "流通市值低于100亿"}
        result, source, _ = self.run_turn("筛选流通市值低于100亿的股票", Model((item,)))
        self.assertEqual(result.code, "screening_intent_invalid")
        self.assertEqual(source.calls, 0)


if __name__ == "__main__":
    unittest.main()
