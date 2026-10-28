"""The whole-run allowance is independent of turns and provider success."""

from concurrent.futures import ThreadPoolExecutor
import unittest

from trading_companion.evaluation_common import EvaluationModelMeter, reported_usage
from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.tool_model import ToolModelResponse


class EvaluationAccountingTests(unittest.TestCase):
    def test_failed_forwarding_consumes_allowance_and_is_not_retried(self):
        class Provider:
            def complete(self, messages, *, tools):
                raise ModelCallError("timeout", "private-accounting-exception")
        meter = EvaluationModelMeter(Provider(), 1)
        with self.assertRaises(ModelCallError):
            meter.complete((), tools=())
        with self.assertRaises(ModelCallError) as blocked:
            meter.complete((), tools=())
        self.assertEqual(blocked.exception.code, "evaluation_budget_exhausted")
        self.assertEqual((meter.attempts, meter.forwarded, meter.blocked, meter.usage), (2, 1, 1, [None]))

    def test_budget_is_atomic_across_competing_delegates(self):
        class Provider:
            def complete(self, messages, *, tools):
                return ToolModelResponse("complete", "fixed", usage=TokenUsage(2, 1, 3))
        meter = EvaluationModelMeter(Provider(), 3)

        def invoke(index):
            try:
                meter.complete((), tools=())
                return True
            except ModelCallError:
                return False
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(sum(executor.map(invoke, range(20))), 3)
        self.assertEqual((meter.attempts, meter.forwarded, meter.blocked), (20, 3, 17))
        self.assertEqual(reported_usage(meter.usage)["reported_total_tokens"], 9)

    def test_missing_partial_and_inconsistent_usage_are_not_complete_or_free(self):
        usage = reported_usage([None, TokenUsage(10, None, None), TokenUsage(2, 3, 99)])
        self.assertEqual(usage["requests_without_complete_usage"], 3)
        self.assertEqual(usage["reported_prompt_tokens"], 12)
        self.assertEqual(usage["reported_total_tokens"], 99)
        self.assertFalse(usage["complete_for_forwarded_requests"])
        self.assertIsNone(reported_usage([None])["reported_total_tokens"])
        self.assertIsNone(reported_usage([])["reported_total_tokens"])
        zero = reported_usage([TokenUsage(0, 0, 0)])
        self.assertEqual(zero["reported_total_tokens"], 0)
        self.assertTrue(zero["complete_for_forwarded_requests"])

    def test_nonresponse_retains_unknown_usage(self):
        class Provider:
            def complete(self, messages, *, tools):
                return object()
        meter = EvaluationModelMeter(Provider(), 1)
        meter.complete((), tools=())
        self.assertEqual(meter.usage, [None])

    def test_invalid_allowance_is_rejected(self):
        for allowance in (0, -1, True, 1.0, None):
            with self.subTest(allowance=allowance), self.assertRaises(ValueError):
                EvaluationModelMeter(None, allowance)
