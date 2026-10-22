"""Fixed offline runtime checks using a fake monotonic clock, without waiting or HTTP."""

from datetime import datetime, timezone
import json

from trading_companion.agent import run_natural_research_turn
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel, QUESTION
from trading_companion.research_session import ResearchSession
from trading_companion.turn_control import TurnControl, TurnLimits


class _DemoModel:
    def __init__(self, control, clock, scenario):
        self.control, self.clock, self.scenario = control, clock, scenario
        self.model, self.calls = ConversationDemoModel().initial, 0

    def complete(self, messages, *, tools):
        self.calls += 1
        response = self.model.complete(messages, tools=tools)
        if self.scenario == "late_proposal" and self.calls == 2:
            self.clock[0] += 90
        if self.scenario == "late_explanation" and self.calls == 3:
            self.clock[0] += 90
        if self.scenario == "cancelled" and self.calls == 3:
            self.control.cancel()
        return response


def main() -> int:
    source, directory = load_development_fixtures()
    expected = {
        "complete": ("research_completed", 3, 1, True, True),
        "late_proposal": ("turn_deadline_exceeded", 2, 0, False, False),
        "late_explanation": ("turn_deadline_exceeded", 3, 1, True, False),
        "cancelled": ("turn_cancelled", 3, 1, False, False),
        "call_limit": ("model_call_budget_exhausted", 2, 1, True, False),
    }
    cases = []
    for name, expectation in expected.items():
        clock = [0.0]
        control = TurnControl(TurnLimits(model_calls=2 if name == "call_limit" else 3), clock=lambda: clock[0])
        session = ResearchSession()
        result = run_natural_research_turn(QUESTION, model=_DemoModel(control, clock, name),
            directory=directory, development_source=source, session=session,
            current_time=datetime(2026, 9, 26, 2, tzinfo=timezone.utc), control=control)
        saved = session.to_dict()["saved_result"] is not None
        observed = (result.code, result.model_calls_attempted, result.analyses_used,
                    result.display_answer is not None, saved)
        cases.append({"case": name, "passed": observed == expectation, "code": result.code,
                      "has_display": result.display_answer is not None, "session_saved": saved, "runtime": result.runtime})
    passed = all(case["passed"] for case in cases)
    print(json.dumps({"status": "passed" if passed else "failed", "is_mock": True,
        "data_authenticity": "synthetic", "market_data_requested": False,
        "scope": "cooperative_control_contracts_not_real_latency_or_hard_interrupt", "cases": cases},
        ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
