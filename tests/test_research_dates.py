"""Local dates use the same historical semantics and never supply invented model conditions."""

from dataclasses import replace
from datetime import date

from trading_companion.research_intent import prepare_research_question
from trading_companion.tool_model import FakeToolModel
from tests.test_research_intent import GuardedTests, intent_arguments, interpretation


LOCAL = "date_expression:local_recognition"


class LocalResearchDateTests(GuardedTests):
    def october(self, question, expression=None, *, task="research", reason=None, **changes):
        values = intent_arguments(expression=expression, indicators=[], task=task, reason=reason)
        values.update(changes)
        model = FakeToolModel([interpretation(values)])
        result = prepare_research_question(
            question, model=model,
            directory=replace(self.directory, retrieved_at=self.directory.retrieved_at.replace(month=10, day=29)),
            source="csv", reference_date=date(2026, 10, 29), development=True)
        self.assertEqual(model.responses_consumed, 1)
        self.market.assert_not_called()
        return result

    def test_local_and_complete_model_expressions_have_identical_dates_and_policies(self):
        cases = (
            ("最近", date(2025, 10, 29), date(2026, 10, 28)),
            ("近期", date(2025, 10, 29), date(2026, 10, 28)),
            ("近60日", date(2026, 8, 30), date(2026, 10, 28)),
            ("近三个月", date(2026, 7, 31), date(2026, 10, 28)),
            ("2026-09-01至2026-10-28", date(2026, 9, 1), date(2026, 10, 28)),
            ("截至2026-09-01", date(2025, 9, 2), date(2026, 9, 1)),
            ("2026-09-01", date(2025, 9, 2), date(2026, 9, 1)),
            ("过去两周", date(2026, 10, 15), date(2026, 10, 28)),
            ("近一年", date(2025, 10, 29), date(2026, 10, 28)),
            ("最近二十自然日", date(2026, 10, 9), date(2026, 10, 28)),
            ("截止 2026-09-01", date(2025, 9, 2), date(2026, 9, 1)),
            ("到2026-09-01", date(2025, 9, 2), date(2026, 9, 1)),
            ("2026-09-01 ～ 2026-10-28", date(2026, 9, 1), date(2026, 10, 28)),
        )
        for expression, start, end in cases:
            with self.subTest(expression=expression):
                question = "分析平安银行" + expression + "的走势"
                local = self.october(question)
                complete = self.october(question, expression)
                self.assertEqual(local.status, "ready", local.message)
                self.assertEqual(complete.status, "ready", complete.message)
                self.assertEqual(replace(local.request, request_id=complete.request.request_id), complete.request)
                self.assertEqual((local.request.start_date, local.request.as_of_date), (start, end))
                self.assertEqual(tuple(value for value in local.applied_defaults if value != LOCAL),
                                 complete.applied_defaults)
                self.assertIn(LOCAL, local.applied_defaults)
                self.assertNotIn(LOCAL, complete.applied_defaults)

    def test_partial_range_endpoint_or_relative_phrase_is_completed_from_original(self):
        for original, partial in (("2026-09-01至2026-10-28", "2026-09-01"),
                                  ("2026-09-01至2026-10-28", "2026-10-28"),
                                  ("最近三个月", "最近"), ("近60日", "60日")):
            with self.subTest(original=original, partial=partial):
                question = "分析平安银行" + original + "的走势"
                recovered = self.october(question, partial)
                complete = self.october(question, original)
                self.assertEqual(recovered.status, "ready", recovered.message)
                self.assertEqual(replace(recovered.request, request_id=complete.request.request_id), complete.request)
                self.assertIn(LOCAL, recovered.applied_defaults)

    def test_multiple_or_unsupported_dates_never_choose_a_supported_substring(self):
        for expression in ("最近和近三个月", "最近最近", "近三个月截至2026-10-01", "去年最近",
                           "上周", "去年", "近20个交易日", "最近半月", "最近几天", "最近数月",
                           "最近400天", "最近0日", "最近-20日", "最近3.5个月", "最近一百二日",
                           "最近三个月半", "近20日左右", "2026-09-011", "2026-09-01以前",
                           "2026-02-30", "2026-09-31至2026-10-28", "2025-01-01至2026-10-28",
                           "2026-10-29", "2026-10-30", "2026-10-28至2026-09-01"):
            with self.subTest(expression=expression):
                result = self.october("分析平安银行" + expression + "的走势")
                self.assertEqual(result.status, "needs_clarification", result.message)
                self.assertIsNone(result.request)
        for original, partial in (("最近和近三个月", "最近"), ("最近最近", "最近"),
                                  ("最近半月", "最近"), ("最近几天", "最近")):
            self.assertEqual(self.october("分析平安银行" + original + "走势", partial).code,
                             "conflicting_dates")

    def test_future_horizon_is_separate_with_missing_or_legacy_model_fields(self):
        for historical in ("", "近三个月"):
            question = "分析平安银行" + historical + "的走势，展望未来一个月"
            for legacy in (None, "未来一个月"):
                with self.subTest(historical=historical, legacy=legacy):
                    result = self.october(question, legacy)
                    self.assertEqual(result.status, "ready", result.message)
                    self.assertEqual(result.conditions.objective.horizon, "未来1个月")
                    self.assertEqual(result.request.as_of_date, date(2026, 10, 28))
                    self.assertEqual((result.request.as_of_date - result.request.start_date).days + 1,
                                     90 if historical else 365)
                    self.assertEqual(LOCAL in result.applied_defaults, bool(historical))

    def test_model_invented_dates_still_fail_before_local_recovery(self):
        for expression in ("2026-10-28", "近90日"):
            result = self.october("分析平安银行近三个月走势", expression)
            self.assertEqual(result.code, "ungrounded_intent")
            self.assertIsNone(result.request)

    def test_indicator_and_security_time_like_names_are_masked(self):
        result = self.prepare("分析平安银行近三个月走势，只看3日均线",
                              intent_arguments(expression=None, indicators=["3日均线"]))
        self.assertEqual(result.status, "ready", result.message)
        self.assertEqual((result.request.as_of_date - result.request.start_date).days + 1, 90)
        directory = replace(self.directory, securities=(replace(self.directory.securities[0], name="近期科技"),))
        result = self.prepare("分析近期科技", intent_arguments(["近期科技"], expression=None, indicators=[]),
                              directory=directory)
        self.assertEqual(result.status, "ready")
        self.assertNotIn(LOCAL, result.applied_defaults)

    def test_spurious_date_clarification_is_repaired_only_for_a_bounded_research_request(self):
        for expression in ("最近", "近三个月"):
            result = self.october("分析平安银行" + expression + "走势", task="clarify", reason="date")
            self.assertEqual(result.status, "ready", result.message)
            self.assertIn(LOCAL, result.applied_defaults)
        for suffix, reason in (("，神秘附加条件", "date"), ("", "indicator"), ("", "request")):
            result = self.october("分析平安银行近三个月走势" + suffix, task="clarify", reason=reason)
            self.assertEqual(result.code, "intent_needs_clarification")
            self.assertIsNone(result.request)
