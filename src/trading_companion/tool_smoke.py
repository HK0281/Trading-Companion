"""Opt-in provider smoke test with fixed synthetic data and no execution loop."""

import argparse
import json
from pathlib import Path

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.local_env import load_model_environment
from trading_companion.research_tool import (
    RESEARCH_TOOL_DEFINITION, TOOL_NAME, ResearchToolExecutor, ToolExecutionError,
)
from trading_companion.tool_model import (
    FakeToolModel, ToolCall, ToolCapableModel, ToolMessage, ToolModelResponse,
)


def _fixture_arguments() -> dict[str, object]:
    """Return detached test arguments; this is not a product default."""
    return {
        "instrument_id": "000001.SZ", "start_date": "2026-09-21", "as_of_date": "2026-09-25",
        "indicators": [{"kind": "sma", "window": 3}, {"kind": "sma", "window": 20},
                       {"kind": "volume", "window": 3}],
    }


def _matches_fixture(call: ToolCall) -> bool:
    """Accept reordered indicators, but never repair or broaden a proposal."""
    arguments = call.arguments
    expected = _fixture_arguments()
    if call.name != TOOL_NAME or arguments.keys() != expected.keys():
        return False
    if any(arguments[key] != expected[key] for key in ("instrument_id", "start_date", "as_of_date")):
        return False
    indicators = arguments["indicators"]
    if type(indicators) is not list or len(indicators) != len(expected["indicators"]):
        return False
    # Compare JSON types too: 3.0 is not the explicitly requested integer period.
    return (sorted(json.dumps(item, sort_keys=True) for item in indicators)
            == sorted(json.dumps(item, sort_keys=True) for item in expected["indicators"]))


def _fake_model() -> FakeToolModel:
    return FakeToolModel([
        ToolModelResponse("tool_calls", tool_calls=(
            ToolCall("smoke_research_1", TOOL_NAME, _fixture_arguments()),)),
        ToolModelResponse("complete", "离线脚本回答：人工CSV已计算；SMA20历史不足。本输出未验证真实模型或回答质量。"),
    ])


def _messages() -> list[ToolMessage]:
    return [
        ToolMessage("system", "This is a synthetic tool-protocol smoke test, not market research or "
                    "investment advice. Propose analyze_daily_indicators exactly once with the user's "
                    "explicit arguments. The application supplies trusted artificial CSV data. Do not "
                    "specify source, paths, URLs or request IDs. After the tool result, return a brief "
                    "Chinese explanation using only that evidence, acknowledging synthetic data and "
                    "unavailable fields. Do not propose another call or claim live market data."),
        ToolMessage("user", "Use these exact test arguments (indicator order may vary): "
                    + json.dumps(_fixture_arguments(), separators=(",", ":"))
                    + ". Call the declared tool; do not calculate or invent its result yourself."),
    ]


def _metadata(response: ToolModelResponse) -> dict[str, object]:
    payload = response.to_dict()
    return {key: payload[key] for key in (
        "status", "provider", "requested_model", "model", "is_mock", "usage",
    )}


def _stop(report: dict[str, object], status: str, code: str) -> dict[str, object]:
    report.update(status=status, code=code)
    return report


def run_smoke_test(*, provider: str = "fake") -> dict[str, object]:
    """Try at most two model calls and one fixed, local synthetic analysis."""
    if provider not in ("fake", "deepseek"):
        raise ModelConfigurationError("Choose fake or deepseek explicitly")
    report = {
        "status": "not_verified", "code": "not_started", "stage": "configuration",
        "provider": provider, "live_provider_selected": provider == "deepseek",
        "data_authenticity": "synthetic", "market_data_requested": False,
        "model_calls_attempted": 0, "analyses_used": 0,
        "tool_round_trip_completed": False, "answer_validation": "not_performed",
        "model_responses": [], "tool_result": None, "answer": None,
    }
    try:
        model: ToolCapableModel
        model = _fake_model() if provider == "fake" else DeepSeekToolModel(
            DeepSeekConfig.from_env(load_model_environment()))
        messages = _messages()
        tools = (RESEARCH_TOOL_DEFINITION,)

        report["stage"] = "proposal"
        report["model_calls_attempted"] = 1
        proposal = model.complete(messages, tools=tools)
        report["model_responses"].append(_metadata(proposal))
        if proposal.status != "tool_calls":
            return _stop(report, "not_verified", {
                "complete": "tool_call_not_proposed", "refused": "proposal_refused",
                "incomplete": "proposal_incomplete",
            }[proposal.status])
        if len(proposal.tool_calls) != 1:
            return _stop(report, "not_verified", "multiple_calls_not_allowed")
        if not _matches_fixture(proposal.tool_calls[0]):
            return _stop(report, "not_verified", "smoke_arguments_mismatch")

        report["stage"] = "execution"
        source = CsvDailyBarSource(
            Path(__file__).resolve().parents[2] / "data" / "sample_daily_bars_synthetic.csv",
            is_synthetic=True,
        )
        executor = ResearchToolExecutor(development_source=source)
        try:
            execution = executor.execute(proposal, messages=messages)
        finally:
            report["analyses_used"] = executor.analyses_used
        report["tool_result"] = execution.messages[0].to_dict()
        if execution.status != "executed":
            return _stop(report, "not_verified", "tool_execution_not_usable")
        result = execution.full_result
        if (result is None or result.data_status != "ok" or not result.synthesis_allowed
                or result.data_authenticity != "synthetic"):
            return _stop(report, "not_verified", "synthetic_result_not_usable")

        report["stage"] = "final"
        report["model_calls_attempted"] = 2
        final = model.complete([*messages, proposal.as_message(), *execution.messages], tools=tools)
        report["model_responses"].append(_metadata(final))
        if final.status != "complete":
            return _stop(report, "not_verified", {
                "tool_calls": "unexpected_followup_call", "refused": "final_refused",
                "incomplete": "final_incomplete",
            }[final.status])
        report.update(tool_round_trip_completed=True, answer=final.text)
        return _stop(report, "ok", "tool_round_trip_completed")
    except ModelConfigurationError:
        return _stop(report, "invalid_request", "model_configuration_error")
    except ModelRequestError:
        return _stop(report, "error", "model_request_invalid")
    except ModelCallError as error:
        # Model adapters guarantee a classified, sanitized code; never print raw exceptions.
        return _stop(report, "error", error.code)
    except ToolExecutionError:
        return _stop(report, "error", "tool_execution_error")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fixed synthetic tool smoke test; not a conversational or trading agent.")
    parser.add_argument("--provider", choices=("fake", "deepseek"), default="fake",
                        help="fake is offline; deepseek explicitly permits up to two paid API requests")
    args = parser.parse_args(argv)
    report = run_smoke_test(provider=args.provider)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["status"] == "ok" else 2 if report["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
