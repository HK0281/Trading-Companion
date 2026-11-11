"""Explicit one-shot paid driver; all artifact checks precede credential loading."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock

from trading_companion.deepseek_model import ANSWER_OUTPUT_TOKENS
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.evaluation_pricing import load_prices, price_for, rate
from trading_companion.model_registry import configured_news_sorting
from trading_companion.news_triage_sorting import NewsTriageSorter
from trading_companion.news_triage_sorting_annotations import load_seal, write_blind
from trading_companion.news_triage_sorting_artifacts import canonical, write_json
from trading_companion.news_triage_sorting_freeze import rebuild_case
from trading_companion.news_triage_sorting_replay import (
    VERSION, PairBudget, VerifiedCostGuard, VerifiedPrice, load_manifest, run_pairs,
)

CLAIM = "sorting-live.claim.json"


def preflight(manifest, manifest_sha256, prices, codex, claude, max_requests, output):
    budget = PairBudget(max_requests)
    cases, plan = load_manifest(manifest)
    if (plan["version"] != VERSION or plan["manifest_sha256"] != manifest_sha256
            or any(c.expected_bundle is None or c.expected_plan is None for c, _ in cases)):
        raise ValueError("Use hash-bound v2 packets with their complete original evidence")
    for case, _ in cases:
        bundle = rebuild_case(case)
        if (any(s["status"] in ("partial", "unavailable") for c in bundle.companies for s in c.coverage)
                or bundle.plan.objective.kind == "decision" and any(
                    p.status != "available" for c in bundle.companies for p in c.trade_plans)):
            raise ValueError("Partial packets require human review, never a paid rerun")
    items, seal_hash = load_seal(manifest, codex, claude)
    cases = [(replace(c, important_items=tuple((i["description"], tuple(i["evidence_ids"]))
              for i in items[c.id])), first) for c, first in cases]
    data, price_hash = load_prices(prices)
    verified = {}
    for provider, source, model, currency in (
            ("jev", "typesafe", "jev-latest", "USD"),
            ("deepseek", "deepseek", "deepseek-flash", "CNY")):
        entry = price_for(data, source, model)
        if (entry is None or not entry["verified_at"] or entry["currency"] != currency
                or entry["input_pricing"] != "uniform"
                or any(rate(entry[k]) is None for k in ("input_per_million", "output_per_million"))):
            raise ValueError("Both approved model aliases require documented uniform prices")
        verified[provider] = VerifiedPrice(model, model, rate(entry["input_per_million"]), rate(entry["output_per_million"]))
    guard = VerifiedCostGuard(verified, jev_alias_estimate=True)
    reserves = {p:Decimal(tokens)*max(verified[p].input_rate, verified[p].output_rate)/1000000
                for p, tokens in (("jev", 30*40000), ("deepseek", 60*24000))}
    if any(reserves[p] >= guard.limits[p] for p in reserves):
        raise ValueError("Approved whole-run token scenario exceeds its local money threshold")
    if Path(output).exists() or (Path(manifest).parent/CLAIM).exists():
        raise ValueError("Never replace results or rerun a claimed manifest")
    metadata = dict(version="sorting-live-run-v1", manifest_sha256=manifest_sha256,
        annotation_seal_sha256=seal_hash, prices_sha256=price_hash,
        max_requests=max_requests, provider_limits=dict(jev=300, deepseek=270),
        thresholds=dict(CNY="15", USD="1"), token_scenario_reserves={p:str(v) for p,v in reserves.items()},
        jev_cost_status="estimated_unverified", source_requests=0, intent_requests=0,
        answer_model="deepseek-flash", answer_output_tokens=ANSWER_OUTPUT_TOKENS,
        answer_timeout_seconds=45, answer_temperature=None,
        enablement=False, no_automatic_resume=True)
    return cases, budget, guard, metadata


def run_live(manifest, manifest_sha256, prices, codex, claude, max_requests, output,
             *, configure=configured_news_sorting, answer_factory=DeepSeekToolModel, sorter_post=None):
    cases, budget, guard, metadata = preflight(
        manifest, manifest_sha256, prices, codex, claude, max_requests, output)
    # Only now may local credentials be read. Constructors do not send requests.
    jev, deepseek = configure()
    if jev.model != "jev-latest" or deepseek.model != "deepseek-flash":
        raise ValueError("Only the approved Jev and DeepSeek models may run")
    answer_config = replace(deepseek, timeout_seconds=45, max_output_tokens=ANSWER_OUTPUT_TOKENS)
    answer_factory(answer_config)
    metadata["started_at"] = datetime.now(timezone.utc).isoformat()
    root = Path(output)
    write_json(Path(manifest).parent/CLAIM, dict(**metadata, output=str(root.resolve())))
    root.mkdir(parents=True, exist_ok=False)
    write_json(root/"run.json", metadata)
    journal = root/"journal"
    journal.mkdir()
    lock, serial = Lock(), 0
    def event(kind, payload):
        nonlocal serial
        with lock:
            serial += 1
            write_json(journal/(str(serial).zfill(6)+".json"),
                       dict(kind=kind, recorded_at=datetime.now(timezone.utc).isoformat(), **payload))
    budget.attempt_sink = lambda provider, counts:event("attempt", dict(provider=provider, counts=counts))
    latest = dict(version=VERSION, status="partial", rows=[], attempts=dict(budget.counts),
                  stop_reason="not_started", enablement=False)
    turns = 0
    def progress(report):
        nonlocal latest, turns
        latest = deepcopy(canonical(report))
        turns += 1
        # Each completed arm is saved once, with no mutation of earlier artifacts.
        pair = latest["rows"][-1]
        side = next(reversed(pair["arms"]))
        write_json(root/("turn-"+str(turns).zfill(2)+".json"),
                   dict(id=pair["id"], question=pair["question"], important_items=pair["important_items"],
                        side=side, arm=pair["arms"][side], attempts=latest["attempts"], stop_reason=latest["stop_reason"]))
    try:
        report = run_pairs(cases, model_factory=lambda:answer_factory(answer_config),
            sorter_factory=lambda sink, attempt:NewsTriageSorter(
                configure=lambda:(jev, deepseek), post=sorter_post, sink=sink, attempt_guard=attempt),
            cost_guard=guard, budget=budget, progress=progress,
            triage_sink=lambda case_id, side, summary:event("triage", dict(id=case_id, side=side, summary=summary)))
    except BaseException as error:
        latest.update(status="partial", stop_reason="run_interrupted", attempts=dict(budget.counts))
        write_json(root/"report.json", dict(**latest, run=metadata))
        if not isinstance(error, Exception):raise
        return latest
    report["run"] = metadata
    write_json(root/"report.json", report)
    if report["status"] == "awaiting_blind_review":
        write_blind(root/"report.json", root/"blind")
    return report
