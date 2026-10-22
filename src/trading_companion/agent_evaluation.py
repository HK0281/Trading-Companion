"""Opt-in fixed-scenario checks of the existing Agent, always using pinned synthetic data."""

import argparse
from dataclasses import dataclass
from datetime import date
import json
from math import isclose
from time import perf_counter

from trading_companion.agent import AgentTurnResult, run_natural_research_turn, run_research_turn
from trading_companion.agent_input import request_arguments
from trading_companion.development_fixtures import REFERENCE_DATE, ROOT, load_development_fixtures as _fixtures
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import FakeToolModel, ToolCall, ToolModelResponse


SUITE_VERSION = "agent-contract-evaluation-v1"
START, CUTOFF = date(2026, 9, 21), date(2026, 9, 25)


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    question: str
    input_mode: str
    indicators: tuple[IndicatorSpec, ...]
    expected_status: str
    expected_code: str
    expected_report: str | None
    model_call_limit: int
    expected_analyses: int = 1

    def request(self) -> MultiIndicatorRequest:
        return MultiIndicatorRequest("000001.SZ", START, CUTOFF, self.indicators, source="csv")

    def expectations(self) -> dict[str, object]:
        return {"agent_status": self.expected_status, "agent_code": self.expected_code,
                "report_status": self.expected_report,
                "explanation_status": "accepted" if self.expected_analyses else "not_performed",
                "model_call_limit": self.model_call_limit, "analyses_used": self.expected_analyses}


CASES = (
    EvaluationCase("complete", "分析 000001.SZ，2026-09-21 至 2026-09-25，指标："
                   "SMA3、EMA3、MACD(2,3,2)、RSI2、BOLL(3,2)、ATR2、VOLUME3", "explicit",
                   (IndicatorSpec("sma", window=3), IndicatorSpec("ema", window=3),
                    IndicatorSpec("macd", fast=2, slow=3, signal=2), IndicatorSpec("rsi", window=2),
                    IndicatorSpec("boll", window=3, stddev_multiplier=2), IndicatorSpec("atr", window=2),
                    IndicatorSpec("volume", window=3)), "completed", "research_completed", "complete", 2),
    EvaluationCase("partial", "分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3、SMA20、VOLUME3", "explicit",
                   (IndicatorSpec("sma", window=3), IndicatorSpec("sma", window=20), IndicatorSpec("volume", window=3)),
                   "limited", "partial_research", "limited", 2),
    EvaluationCase("natural", "分析平安银行，2026-09-21 至 2026-09-25，指标：SMA3、RSI2", "natural",
                   (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2)),
                   "completed", "research_completed", "complete", 3),
    EvaluationCase("clarification", "分析平安，2026-09-21 至 2026-09-25，指标：SMA3", "natural",
                   (IndicatorSpec("sma", window=3),), "needs_clarification", "instrument_needs_clarification",
                   None, 1, 0),
)


class _MeteredModel:
    """Enforce a case-local provider-call cap independently of Agent bookkeeping."""

    def __init__(self, delegate, limit):
        self._delegate = delegate
        self.limit = limit
        self.attempts = 0
        self.forwarded = 0
        self.blocked = 0
        self.responses = []

    def complete(self, messages, *, tools):
        self.attempts += 1
        if self.forwarded >= self.limit:
            self.blocked += 1
            raise ModelCallError("evaluation_call_limit", "Fixed evaluation call allowance exhausted")
        self.forwarded += 1
        response = self._delegate.complete(messages, tools=tools)
        self.responses.append(response)
        return response


def _usage_summary(responses, forwarded):
    """Sum known provider values without silently replacing missing usage with zero."""
    totals = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        known = [getattr(response.usage, name) for response in responses
                 if response.usage is not None and getattr(response.usage, name) is not None]
        totals["reported_" + name] = sum(known) if known else None
    complete = sum(response.usage is not None and all(getattr(response.usage, name) is not None
                   for name in ("prompt_tokens", "completion_tokens", "total_tokens")) for response in responses)
    return {**totals, "requests_with_complete_usage": complete,
            "requests_without_complete_usage": forwarded - complete,
            "complete_for_forwarded_requests": forwarded > 0 and complete == forwarded,
            "scope": "provider_reported_values_only_not_cost_or_total_billed_usage"}


def _fake_model(case):
    script = []
    if case.input_mode == "natural":
        script.append(ToolModelResponse("tool_calls", tool_calls=(ToolCall("eval_intent", INTENT_TOOL_NAME, {
            "intent": "research", "instrument_mentions": ["平安" if not case.expected_analyses else "平安银行"],
            "date_expression": "2026-09-21 至 2026-09-25",
            "indicator_mentions": ["SMA3"] if not case.expected_analyses else ["SMA3", "RSI2"],
            "clarification_reason": None,
        }),)))
    if not case.expected_analyses:
        return FakeToolModel(script)
    script.append(ToolModelResponse("tool_calls", tool_calls=(ToolCall(
        "eval_research", TOOL_NAME, request_arguments(case.request())),)))
    return ExplanationDemoModel(script)


def _build_model(case, config):
    return _fake_model(case) if config is None else DeepSeekToolModel(config)


def _matches_scope(request, case):
    return (type(request) is MultiIndicatorRequest and request.instrument_id == "000001.SZ"
            and request.start_date == START and request.as_of_date == CUTOFF and request.source == "csv"
            and set(request.indicators) == set(case.indicators))


def _grade(case: EvaluationCase, outcome: AgentTurnResult, meter, *, provider):
    """Compare against scenario expectations, not just whether the Agent says completed."""
    report, explanation = outcome.safe_answer, outcome.explanation_validation
    result = outcome.research_result
    checks = {
        "agent_status": outcome.status == case.expected_status,
        "agent_code": outcome.code == case.expected_code,
        "input_mode": outcome.input_mode == case.input_mode,
        "report_status": (report.status if report else None) == case.expected_report,
        "explanation_status": explanation.status == ("accepted" if case.expected_analyses else "not_performed"),
        "agent_model_count_matches_meter": outcome.model_calls_attempted == meter.attempts,
        "model_calls": meter.attempts == meter.forwarded == case.model_call_limit and meter.blocked == 0,
        "analyses_used": outcome.analyses_used == case.expected_analyses,
        "response_count": len(outcome.model_responses) == len(meter.responses) == meter.forwarded,
        "provider_mode": bool(meter.responses) and all(response.provider == provider
                            and response.is_mock == (provider == "fake") for response in meter.responses),
        "no_failure_code": outcome.failure_code is None,
    }
    if case.expected_analyses:
        checks.update(
            confirmed_scope=_matches_scope(outcome.confirmed_request, case),
            result_scope=(result is not None and _matches_scope(result.request, case)
                          and outcome.confirmed_request is not None
                          and result.request.request_id == outcome.confirmed_request.request_id),
            synthetic_snapshot=(result is not None and result.source == "csv"
                and result.data_authenticity == "synthetic" and result.price_adjustment == "unknown"
                and result.data_status == "ok" and result.calculation_status == case.expected_report
                and len(result.bars) == 5 and result.latest_trade_date == CUTOFF),
            reference_values=(report is not None and any(item.indicator_id == "daily_bar" and item.field == "close_price"
                and item.status == "available" and item.value == 10.4 for item in report.fields)
                and any(item.indicator_id == IndicatorSpec("sma", window=3).indicator_id and item.field == "value"
                and item.status == "available" and type(item.value) is float
                and isclose(item.value, (10.1 + 10.0 + 10.4) / 3, rel_tol=0, abs_tol=1e-12) for item in report.fields)),
            checked_display=(outcome.display_answer is not None and explanation.text is not None
                             and explanation.text in outcome.display_answer and bool(explanation.claims)),
        )
        if case.name == "partial":
            checks["missing_sma20_preserved"] = report is not None and any(
                item.indicator_id == IndicatorSpec("sma", window=20).indicator_id and item.field == "value"
                and item.status == "insufficient_history" and item.value is None and item.fact_id is None
                and item.missing_bars == 15 for item in report.fields)
    else:
        understanding = outcome.understanding
        conditions = understanding.conditions if understanding else None
        checks.update(
            no_research=(outcome.confirmed_request is None and result is None and report is None
                         and outcome.model_draft is None and outcome.display_answer is None),
            identity_candidates=(understanding is not None and {item.instrument_id for item in understanding.candidates}
                                 == {"000001.SZ", "601318.SH"}),
            preserved_conditions=(conditions is not None and conditions.start_date == START
                and conditions.as_of_date == CUTOFF and conditions.source == "csv"
                and conditions.reference_date == REFERENCE_DATE and set(conditions.indicators) == set(case.indicators)),
        )
    return checks


_FAILURE_CODES = frozenset((
    "authentication_error", "rate_limited", "provider_error", "timeout", "invalid_response", "connection_error",
    "unsupported_response", "unknown_tool", "script_exhausted", "model_request_invalid", "model_configuration_error",
    "evaluation_call_limit", "proposal_refused", "proposal_incomplete", "final_refused", "final_incomplete",
    "unexpected_followup_call", "invalid_explanation_json", "invalid_explanation_shape", "explanation_context_mismatch",
    "explanation_evidence_mismatch", "explanation_relation_mismatch", "explanation_not_ready",
    "model_request_or_configuration_error",
))


def _case_report(case, outcome, meter, provider, elapsed_ms):
    checks = _grade(case, outcome, meter, provider=provider)
    report = outcome.safe_answer
    return {
        "case": case.name, "question": case.question, "status": "passed" if all(checks.values()) else "failed",
        "expected": case.expectations(), "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "observed": {
            "agent_status": outcome.status, "agent_code": outcome.code, "stage": outcome.stage,
            "report_status": report.status if report else None,
            "explanation_status": outcome.explanation_validation.status,
            "explanation_code": outcome.explanation_validation.code,
            "failure_code": (outcome.failure_code if outcome.failure_code in _FAILURE_CODES
                             else "unclassified_failure" if outcome.failure_code is not None else None),
            "data_authenticity": outcome.research_result.data_authenticity if outcome.research_result else None,
            "agent_model_calls_attempted": outcome.model_calls_attempted, "analyses_used": outcome.analyses_used,
        },
        "model_calls_attempted": meter.attempts, "provider_calls_forwarded": meter.forwarded,
        "model_budget_rejections": meter.blocked, "elapsed_ms": elapsed_ms,
        "responses": [{"status": response.status, "provider": response.provider,
                       "requested_model": response.requested_model, "model": response.model, "is_mock": response.is_mock}
                      for response in meter.responses],
        "token_usage": _usage_summary(meter.responses, meter.forwarded),
    }


def run_evaluation(*, provider: str = "fake", case: str = "complete") -> dict[str, object]:
    """Run selected fixed cases once; stop after failure, never broaden data or retry."""
    if type(provider) is not str or provider not in ("fake", "deepseek"):
        raise ModelConfigurationError("Choose fake or deepseek explicitly")
    if type(case) is not str or case not in (*[item.name for item in CASES], "all"):
        raise ModelConfigurationError("Choose a fixed evaluation case")
    selected = CASES if case == "all" else tuple(item for item in CASES if item.name == case)
    report = {
        "version": SUITE_VERSION, "status": "not_run", "code": "not_started", "provider": provider,
        "evaluation_scope": "fixed_synthetic_contract_checks_not_language_or_investment_quality",
        "live_provider_selected": provider == "deepseek", "fixture_data_authenticity": "synthetic",
        "market_data_requested": False, "directory_network_requested": False,
        "reference_date": REFERENCE_DATE.isoformat(), "selected_cases": [item.name for item in selected],
        "model_call_limit": sum(item.model_call_limit for item in selected),
        "analysis_limit": sum(item.expected_analyses for item in selected), "cases": [],
        "summary": {},
    }
    responses, forwarded = [], 0
    try:
        source, directory = _fixtures()
        # Only explicit DeepSeek selection reads environment credentials.
        config = DeepSeekConfig.from_env() if provider == "deepseek" else None
        stopped = False
        for item in selected:
            if stopped:
                report["cases"].append({"case": item.name, "status": "skipped", "code": "stopped_after_failure"})
                continue
            meter = _MeteredModel(_build_model(item, config), item.model_call_limit)
            start = perf_counter()
            if item.input_mode == "explicit":
                outcome = run_research_turn(item.question, model=meter, development_source=source)
            else:
                outcome = run_natural_research_turn(item.question, model=meter, directory=directory,
                    development_source=source, reference_date=REFERENCE_DATE)
            elapsed_ms = round((perf_counter() - start) * 1000)
            result = _case_report(item, outcome, meter, provider, elapsed_ms)
            report["cases"].append(result)
            responses.extend(meter.responses)
            forwarded += meter.forwarded
            stopped = result["status"] == "failed"
        report.update(status="failed" if stopped else "passed",
                      code="scenario_expectations_failed" if stopped else "scenario_expectations_met")
    except ModelConfigurationError:
        report.update(status="invalid_request", code="model_configuration_error")
    except (DataSourceError, DataValidationError):
        report.update(status="error", code="evaluation_fixture_unusable")
    cases = report["cases"]
    executed = [item for item in cases if item["status"] != "skipped"]
    report["summary"] = {
        "selected": len(selected), "executed": len(executed),
        "passed": sum(item["status"] == "passed" for item in executed),
        "failed": sum(item["status"] == "failed" for item in executed),
        "not_run": len(selected) - len(executed),
        "model_calls_attempted": sum(item["model_calls_attempted"] for item in executed),
        "provider_calls_forwarded": forwarded,
        "analyses_used": sum(item["observed"]["analyses_used"] for item in executed),
        "token_usage": _usage_summary(responses, forwarded),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixed Agent contract checks; synthetic data only, no retries or file writes.")
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake",
                        help="fake is offline; deepseek explicitly permits bounded paid model calls")
    parser.add_argument("--case", choices=(*[item.name for item in CASES], "all"), default="complete",
                        help="default complete: at most 2 model calls; all: at most 8 calls, stop on first failure")
    args = parser.parse_args(argv)
    report = run_evaluation(provider=args.provider, case=args.case)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
