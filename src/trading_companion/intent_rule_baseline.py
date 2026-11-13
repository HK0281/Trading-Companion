"""Explicit rule versions, independent of immutable intent corpus annotations."""

from hashlib import sha256
import json
from pathlib import Path

VERSION = "personal-memory-preferences-v1"
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
    "answer_technical_validation.py", "answer_validation.py", "answer_clauses.py", "answer_claim_recovery.py",
    "evidence_numbers.py", "research_price_context.py",
    "atr_plan_math.py", "index_scenario.py", "index_scenario_answer.py", "index_demo.py", "conversation_memory.py",
    "research_intent_v2.py", "research_intent_context.py", "research_intent_dispatch.py",
    "research_intent_adapter.py", "product_research.py", "model_registry.py", "api_server.py", "turn_trace.py",
    "news_triage_shadow.py",
    "news_triage_sorting.py", "news_triage_sorting_control.py", "news_triage_sorting_replay.py",
    "news_triage_sorting_artifacts.py", "news_triage_sorting_freeze.py",
    "news_triage_sorting_annotations.py", "news_triage_sorting_live.py",
    "multi_analysis.py", "tencent_adapter.py",
    "turn_control.py", "product_answer.py", "product_narrative_answer.py",
    "research_documents.py",
    "document_collection.py",
    "company_news_body.py",
    "company_announcement_body.py",
    "company_research.py",
    "company_research_contracts.py",
    "company_news_sina.py",
    "collection_execution.py",
    "snapshot_store.py",
    "personal_memory.py", "personal_memory_store.py", "personal_memory_commands.py", "security_directory.py",
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


def historical_baseline(*, version, expected_digest):
    """Resolve immutable corpus provenance, never current execution eligibility."""
    current_baseline()
    value = json.loads(DESCRIPTION.read_text(encoding="utf-8"))
    records = value.get("previous_rules")
    if type(version) is not str or type(expected_digest) is not str or type(records) is not list:
        raise ValueError("Invalid historical rule baseline identity")
    matches = [record for record in records if type(record) is dict
               and record.get("rule_baseline_version") == version]
    # Even an exact duplicate is ambiguous; never select by a matching digest alone.
    if len(matches) != 1:
        raise ValueError("Historical rule baseline version must be registered exactly once")
    record = matches[0]
    sources = record.get("sources_sha256_lf")
    if (type(sources) is not dict or not sources
            or any(type(name) is not str or type(fingerprint) is not str
                   or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint)
                   for name, fingerprint in sources.items())
            or record.get("source_digest") != expected_digest or source_digest(sources) != expected_digest):
        raise ValueError("Historical rule baseline source mismatch")
    return record
