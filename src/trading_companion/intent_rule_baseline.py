"""Explicit rule versions, independent of immutable intent corpus annotations."""

from hashlib import sha256
import json
from pathlib import Path

VERSION = "conversation-memory-v1"
ROOT = Path(__file__).resolve().parents[2]
DESCRIPTION = ROOT / "data/evaluation/intent-rule-baselines-v1.json"
SOURCES = (
    "request_routing.py", "routing_context.py", "product_agent.py", "product_plan.py",
    "research_session.py", "research_memory.py", "research_followup.py", "research_decision.py",
    "research_intent.py", "research_instruments.py", "research_edit.py", "research_update.py",
    "research_objective.py", "agent.py", "agent_input.py", "api_service.py",
    "screening_intent.py", "screening_context.py", "screening_agent.py", "screening_followup.py",
    "indicator_knowledge.py", "open_knowledge.py", "intent_router_baseline.py",
    "product_scope.py", "product_indicator_edit.py",
    "decision_horizon.py", "trade_plan.py", "horizon_trade_plan.py", "product_decision_answer.py",
    "market_scope.py",
    "index_agent.py", "index_answer.py", "index_contracts.py", "index_source.py", "index_research.py",
    "index_chart.py", "index_snapshot.py", "ohlc_weeks.py", "_indicator_values.py", "answer_numeric_bindings.py",
    "answer_technical_validation.py", "answer_validation.py", "answer_clauses.py", "evidence_numbers.py", "research_price_context.py",
)


def source_hashes():
    return {name: sha256((Path(__file__).parent/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for name in SOURCES}


def source_digest(sources):
    return sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def current_baseline():
    value = json.loads(DESCRIPTION.read_text(encoding="utf-8"))
    current = value["current"]
    actual = source_hashes()
    if (value["version"] != "intent-rule-baselines-v1" or current["rule_baseline_version"] != VERSION
            or current["sources_sha256_lf"] != actual or current["source_digest"] != source_digest(actual)):
        raise ValueError("Current rule baseline source mismatch")
    return current


def historical_sources_match(historical):
    value = json.loads(DESCRIPTION.read_text(encoding="utf-8"))
    current_baseline()
    return historical == value["corpus_historical_sources_sha256_lf"]
