"""Repeatable fixed synthetic MVP evaluation, never a market or investment benchmark."""

import argparse
from datetime import datetime, time, timezone
from hashlib import sha256
import json
from math import isfinite
from time import perf_counter

from trading_companion.agent import run_natural_research_turn, run_research_turn
from trading_companion.agent_input import request_arguments
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.mvp_evaluation_cases import SCENARIOS
from trading_companion.research_followup import FOLLOWUP_TOOL_NAME
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_session import ResearchSession
from trading_companion.research_synthesis import build_research_synthesis
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolModelResponse


VERSION = "mvp-synthetic-evaluation-v1"
NOW = datetime.combine(REFERENCE_DATE, time(2), tzinfo=timezone.utc)
MAX_MODEL_CALLS = 512


class FixtureModel:
    """Return only catalogue-defined interpretations and proposals, not inferred language."""

    def __init__(self):
        self.step = None
        self.serial = 0

    def complete(self, messages, *, tools):
        self.serial += 1
        step = self.step
        if step is None:
            raise ModelCallError("script_exhausted", "No active fixed evaluation step")
        if messages[-1].role == "tool":
            return ExplanationDemoModel([]).complete(messages, tools=tools)
        name = tools[0].name
        if name == INTENT_TOOL_NAME:
            arguments = {"intent": step.intent, "instrument_mentions": list(step.mentions),
                "date_expression": step.date_expression, "indicator_mentions": list(step.indicators),
                "clarification_reason": None}
        elif name == TOOL_NAME and step.scope is not None and step.analyses:
            arguments = request_arguments(step.scope.request())
        elif name == FOLLOWUP_TOOL_NAME and step.saved:
            scope = json.loads(messages[0].content.split("Cached scope: ", 1)[1])
            arguments = {"request_id": scope["request_id"], "action": "explain", "topic_mentions": ["RSI"]}
        else:
            raise ModelCallError("script_exhausted", "Unexpected operation in a fixed evaluation step")
        return ToolModelResponse("tool_calls", tool_calls=(ToolCall(f"mvp_eval_{self.serial}", name, arguments),))


class _Meter:
    """Count every attempted forwarding independently of Agent-reported statistics."""

    def __init__(self, delegate, allowance):
        self.delegate, self.allowance = delegate, allowance
        self.attempts = 0
        self.forwarded = 0
        self.responses = []
        self.prompt_fingerprints = []

    def complete(self, messages, *, tools):
        self.attempts += 1
        if self.forwarded >= self.allowance:
            raise ModelCallError("evaluation_call_limit", "Fixed evaluation allowance exhausted")
        self.forwarded += 1
        prompt = {"system_messages": [item.content for item in messages if item.role == "system"],
                  "tools": [item.to_dict() for item in tools]}
        self.prompt_fingerprints.append(sha256(json.dumps(prompt, sort_keys=True,
            ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest())
        response = self.delegate.complete(messages, tools=tools)
        self.responses.append(response)
        return response


def _usage(responses, forwarded):
    values = {}
    names = ("prompt_tokens", "completion_tokens", "total_tokens")
    for name in names:
        known = [getattr(item.usage, name) for item in responses
                 if item.usage is not None and getattr(item.usage, name) is not None]
        values["reported_" + name] = sum(known) if known else None
    complete = sum(item.usage is not None and all(getattr(item.usage, name) is not None for name in names)
                   for item in responses)
    return {**values, "requests_with_complete_usage": complete,
            "requests_without_complete_usage": forwarded - complete,
            "complete_for_forwarded_requests": forwarded > 0 and complete == forwarded,
            "scope": "provider_reported_values_only_not_cost_or_billed_usage"}


def _same_scope(request, scope):
    return (request is not None and request.instrument_id == scope.instrument_id
            and request.start_date == scope.start and request.as_of_date == scope.end
            and request.source == "csv" and set(request.indicators) == set(scope.indicators))


def _grade(step, outcome, session, directory, source, meter, attempts, forwarded, response_index, provider):
    report, result = outcome.safe_answer, outcome.research_result
    request = outcome.confirmed_request
    synthesis = outcome.research_synthesis
    if step.saved:
        lookup = session.saved_result(directory=directory, source="csv", development_source=source,
            reference_date=REFERENCE_DATE, now=NOW, allow_history=True)
        saved = lookup.snapshot if lookup.status == "ready" else None
        if saved is not None:
            from trading_companion.research_report import build_research_report
            request, result = saved.request, saved.result
            report = build_research_report(result, expected_request=request)
            if outcome.followup_answer is not None and outcome.followup_answer.action == "overview":
                synthesis = build_research_synthesis(result, expected_request=request) if result.synthesis_allowed else None
    responses = meter.responses[response_index:]
    checks = {
        "agent_status": outcome.status == step.status,
        "agent_code": outcome.code in step.codes,
        "model_calls": meter.attempts - attempts == meter.forwarded - forwarded == step.calls,
        "agent_model_count": outcome.model_calls_attempted == meter.attempts - attempts,
        "analysis_count": outcome.analyses_used == step.analyses,
        "report_status": (report.status if report is not None else None) == step.report_status,
        "explanation_status": outcome.explanation_validation.status == step.explanation_status,
        "no_failure_code": outcome.failure_code is None,
        "synthesis_status": (synthesis.status if synthesis is not None else None) == step.synthesis_status,
        "response_count": len(responses) == meter.forwarded - forwarded,
        "provider_mode": all(item.provider == provider and item.is_mock == (provider == "fake") for item in responses),
        "no_raw_draft_display": outcome.model_draft is None or outcome.display_answer is None
            or outcome.model_draft not in outcome.display_answer,
    }
    if step.synthesis_status is not None:
        checks.update(
            synthesis_scope=synthesis is not None and request is not None and result is not None
                and (synthesis.request_id, synthesis.instrument_id, synthesis.trade_date)
                    == (request.request_id, request.instrument_id, result.latest_trade_date.isoformat()),
            checked_synthesis=synthesis is not None and synthesis.code == "checked_reference_synthesis"
                and tuple(item.dimension for item in synthesis.dimensions) == ("trend", "momentum", "volatility", "volume")
                and bool(synthesis.to_dict()["evidence_ids"]),
            synthesis_display=synthesis is not None and outcome.display_answer is not None
                and synthesis.text in outcome.display_answer,
        )
    if step.scope is not None:
        checks.update(
            confirmed_scope=_same_scope(request, step.scope),
            result_scope=result is not None and _same_scope(result.request, step.scope)
                and request is not None and result.request.request_id == request.request_id,
            synthetic_snapshot=result is not None and result.data_authenticity == "synthetic" and result.source == "csv",
            checked_numbers=report is not None and all(item.status != "available"
                or type(item.value) in (int, float) and isfinite(item.value) and item.fact_id is not None for item in report.fields),
            reference_close=report is not None and any(item.indicator_id == "daily_bar" and item.field == "close_price"
                and item.status == "available" and item.value == 10.4 for item in report.fields),
        )
    elif step.analyses == 0:
        checks["no_new_research"] = outcome.research_result is None and outcome.confirmed_request is None
    return checks


def _percentile(values, ratio):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    left = int(position)
    return round(ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * (position - left), 3)


def run_mvp_evaluation(*, provider="fake", case="single_sma", repeat=1, model_call_limit=12):
    """Execute independent repeats, stop on first failed scenario, and never retry a step."""
    if (type(provider) is not str or provider not in ("fake", "deepseek")
            or type(case) is not str or case not in ("all", *(item.name for item in SCENARIOS))
            or type(repeat) is not int or not 1 <= repeat <= 3
            or type(model_call_limit) is not int or not 0 <= model_call_limit <= MAX_MODEL_CALLS):
        raise ModelConfigurationError("Choose bounded fixed evaluation options")
    selected = tuple(item for item in SCENARIOS if case in ("all", item.name))
    planned_calls = sum(item.model_call_limit for item in selected) * repeat
    if planned_calls > model_call_limit:
        raise ModelConfigurationError("Evaluation allowance must cover the fixed selected scenario plan")
    report = {"version": VERSION, "status": "not_run", "code": "not_started", "provider": provider,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt_fingerprint_scope": "system_messages_and_tool_definitions_not_full_context_or_weights",
        "evaluation_scope": "fixed_synthetic_mvp_behavior_not_language_or_investment_quality",
        "fixture_data_authenticity": "synthetic", "market_data_requested": False,
        "directory_network_requested": False, "reference_date": REFERENCE_DATE.isoformat(),
        "live_provider_selected": provider == "deepseek", "human_review_required": True,
        "selected_cases": [item.name for item in selected], "repeat": repeat,
        "planned_model_calls": planned_calls, "model_call_limit": model_call_limit, "cases": []}
    config = None
    try:
        source, directory = load_development_fixtures()
        if provider == "deepseek":
            from trading_companion.deepseek_model import DeepSeekConfig
            config = DeepSeekConfig.from_env()
    except (DataSourceError, DataValidationError, OSError):
        report.update(status="invalid_request", code="fixture_unavailable")
    except ModelConfigurationError:
        report.update(status="invalid_request", code="model_configuration_error")
    total_calls, attempts, analyses, responses, timings = 0, 0, 0, [], []
    if report["status"] != "invalid_request":
        stopped = False
        for scenario in selected:
            for repetition in range(1, repeat + 1):
                if stopped:
                    break
                if provider == "fake":
                    delegate = FixtureModel()
                else:
                    from trading_companion.deepseek_tool_model import DeepSeekToolModel
                    delegate = DeepSeekToolModel(config)
                meter = _Meter(delegate, min(scenario.model_call_limit, model_call_limit - total_calls))
                session = ResearchSession()
                items = []
                for index, step in enumerate(scenario.steps, 1):
                    if provider == "fake":
                        delegate.step = step
                    before, forwarded, response_index = meter.attempts, meter.forwarded, len(meter.responses)
                    started = perf_counter()
                    try:
                        if step.input_mode == "explicit":
                            outcome = run_research_turn(step.question, model=meter, development_source=source)
                        else:
                            outcome = run_natural_research_turn(step.question, model=meter, directory=directory,
                                development_source=source, reference_date=REFERENCE_DATE, session=session, current_time=NOW)
                        elapsed = round((perf_counter() - started) * 1000, 3)
                        checks = _grade(step, outcome, session, directory, source, meter,
                                        before, forwarded, response_index, provider)
                        analyses += outcome.analyses_used
                        timings.append(elapsed)
                        item = {"step": index, "question": step.question,
                            "status": "passed" if all(checks.values()) else "failed", "checks": checks,
                            "failed_checks": [key for key, value in checks.items() if not value],
                            "observed": {"status": outcome.status, "code": outcome.code,
                                "model_calls": outcome.model_calls_attempted, "analyses": outcome.analyses_used},
                            "system_tool_sha256": meter.prompt_fingerprints[forwarded:],
                            "responses": [{"status": response.status, "provider": response.provider,
                                "requested_model": response.requested_model, "model": response.model,
                                "is_mock": response.is_mock} for response in meter.responses[response_index:]],
                            "answer": outcome.display_answer, "elapsed_ms": elapsed}
                    except Exception:
                        # A provider or runtime exception is never copied into an evaluation report.
                        item = {"step": index, "question": step.question, "status": "failed",
                                "code": "evaluation_internal_error", "checks": {}, "failed_checks": ["step_completed"]}
                    items.append(item)
                    if item["status"] != "passed":
                        stopped = True
                        break
                total_calls += meter.forwarded
                attempts += meter.attempts
                responses.extend(meter.responses)
                report["cases"].append({"case": scenario.name, "category": scenario.category, "repeat": repetition,
                    "status": "passed" if len(items) == len(scenario.steps) and all(item["status"] == "passed" for item in items) else "failed",
                    "steps": items, "model_calls_attempted": meter.attempts, "provider_calls_forwarded": meter.forwarded,
                    "token_usage": _usage(meter.responses, meter.forwarded)})
            if stopped:
                break
        report.update(status="failed" if stopped else "passed",
                      code="scenario_expectations_failed" if stopped else "scenario_expectations_met")
    executed = len(report["cases"])
    passed = sum(item["status"] == "passed" for item in report["cases"])
    report["summary"] = {"selected": len(selected) * repeat, "executed": executed, "passed": passed,
        "failed": executed - passed, "not_run": len(selected) * repeat - executed,
        "model_calls_attempted": attempts, "provider_calls_forwarded": total_calls, "analyses_used": analyses,
        "token_usage": _usage(responses, total_calls),
        "step_latency_ms": {"p50": _percentile(timings, .5), "p95": _percentile(timings, .95),
                            "scope": "executed_step_wall_time_including_local_steps_not_production_latency"},
        "quality_acceptance": "pending_live_repeats_and_human_review"}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fixed artificial MVP checks; DeepSeek is explicit and may incur charges.")
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    parser.add_argument("--case", choices=("all", *(item.name for item in SCENARIOS)), default="single_sma")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--model-call-limit", type=int, default=12,
                        help="Explicit whole-run allowance (0-512), checked before credential reads")
    options = parser.parse_args(argv)
    try:
        report = run_mvp_evaluation(provider=options.provider, case=options.case, repeat=options.repeat,
                                    model_call_limit=options.model_call_limit)
    except ModelConfigurationError:
        print(json.dumps({"version": VERSION, "status": "invalid_request", "code": "invalid_evaluation_options"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
