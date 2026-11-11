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
from trading_companion.news_triage_sorting_artifacts import canonical, read_json, write_json
from trading_companion.news_triage_sorting_freeze import rebuild_case
from trading_companion.news_triage_sorting_replay import (
    VERSION, PairBudget, VerifiedCostGuard, VerifiedPrice, load_manifest, run_pairs,
)

CLAIM = "sorting-live.claim.json"
RERUN_CLAIM = "sorting-live-interrupted-rerun.claim.json"
# Message #585 authorizes precisely this first-arm interruption, never result-based reruns.
APPROVED_INTERRUPTED_REPORT_SHA256 = "52859bd0977c9c5d1dfc27834eabe4f4a764ff9791bae190f6f2dd9e91cfa067"


def interrupted_rerun(previous, manifest, cases, metadata, budget, guard, output):
    previous=Path(previous).resolve()
    report, report_hash=read_json(previous/"report.json")
    claim, claim_hash=read_json(Path(manifest).parent/CLAIM)
    run,_=read_json(previous/"run.json")
    bindings=("manifest_sha256","annotation_seal_sha256","prices_sha256","thresholds","provider_limits","max_requests")
    if (report_hash!=APPROVED_INTERRUPTED_REPORT_SHA256 or report.get("status")!="partial"
            or report.get("stop_reason")!="price_or_usage_unknown" or report.get("run")!=run
            or any(run.get(k)!=metadata[k] or claim.get(k)!=run.get(k) for k in bindings)
            or Path(claim.get("output","")).resolve()!=previous or (previous/"blind").exists()
            or Path(output).resolve().is_relative_to(previous)
            or len(report.get("rows",[]))!=1 or set(report["rows"][0]["arms"])!={"on"}
            or report["rows"][0]["id"]!=cases[0][0].id or cases[0][1]!="on"
            or {p.name for p in previous.glob("turn-*.json")}!={"turn-01.json"}):
        raise ValueError("Only the hash-bound first-arm interruption approved in #585 may rerun once")
    arm=deepcopy(report["rows"][0]["arms"]["on"])
    calls=(arm.get("triage") or {}).get("calls",[])
    missing=[c for c in calls if c.get("usage") is None]
    if (len(missing)!=1 or missing[0].get("provider")!="deepseek" or missing[0].get("model") is not None
            or missing[0].get("status")!="usage_unknown" or arm["triage"].get("reason")!="usage_unknown"):
        raise ValueError("Interrupted triage does not match the approved legacy timeout")
    # Legacy records omitted the per-request estimate. Its pre-send triage ceiling
    # was 8,000 tokens; retain that larger reservation without editing old artifacts.
    missing[0].update(status="timeout_estimated",response_received=False,reserved_tokens=8000)
    counts={p:sum(c["provider"]==p for c in [*calls,*arm["answer_calls"]]) for p in budget.counts}
    if counts!=report.get("attempts") or sum(counts.values())>=budget.max_requests:
        raise ValueError("Interrupted attempt accounting mismatch")
    budget.counts.update(counts)
    prior_costs=guard.reconcile(arm,budget,ledger_key=("interrupted",report_hash))
    if not prior_costs["known"] or budget.stop_reason:
        raise ValueError("Interrupted costs must remain within the original allowances")
    return dict(approval_message=585,report=str(previous/"report.json"),report_sha256=report_hash,
                original_claim_sha256=claim_hash,stop_reason=report["stop_reason"],attempts=counts,
                costs=prior_costs,legacy_timeout_reserved_tokens=8000)


def preflight(manifest, manifest_sha256, prices, codex, claude, max_requests, output, *, rerun_interrupted=None):
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
    claimed=(Path(manifest).parent/CLAIM).exists()
    if (Path(output).exists() or (Path(manifest).parent/RERUN_CLAIM).exists()
            or claimed and rerun_interrupted is None or rerun_interrupted is not None and not claimed):
        raise ValueError("Never replace results or rerun a claimed manifest")
    metadata = dict(version="sorting-live-run-v1", manifest_sha256=manifest_sha256,
        annotation_seal_sha256=seal_hash, prices_sha256=price_hash,
        max_requests=max_requests, provider_limits=dict(jev=300, deepseek=270),
        thresholds=dict(CNY="15", USD="1"), token_scenario_reserves={p:str(v) for p,v in reserves.items()},
        jev_cost_status="estimated_unverified", source_requests=0, intent_requests=0,
        answer_model="deepseek-flash", answer_output_tokens=ANSWER_OUTPUT_TOKENS,
        answer_timeout_seconds=45, answer_temperature=None,
        enablement=False, no_automatic_resume=True)
    if rerun_interrupted is not None:
        metadata["interrupted_rerun"]=interrupted_rerun(
            rerun_interrupted,manifest,cases,metadata,budget,guard,output)
        if any(guard.spent[p]+reserves[p]>=guard.limits[p] for p in reserves):
            raise ValueError("Interrupted costs plus the new run reserve exceed an original money limit")
    return cases, budget, guard, metadata


def run_live(manifest, manifest_sha256, prices, codex, claude, max_requests, output,
             *, configure=configured_news_sorting, answer_factory=DeepSeekToolModel, sorter_post=None,
             rerun_interrupted=None):
    cases, budget, guard, metadata = preflight(
        manifest, manifest_sha256, prices, codex, claude, max_requests, output,rerun_interrupted=rerun_interrupted)
    # Only now may local credentials be read. Constructors do not send requests.
    jev, deepseek = configure()
    if jev.model != "jev-latest" or deepseek.model != "deepseek-flash":
        raise ValueError("Only the approved Jev and DeepSeek models may run")
    answer_config = replace(deepseek, timeout_seconds=45, max_output_tokens=ANSWER_OUTPUT_TOKENS)
    answer_factory(answer_config)
    metadata["started_at"] = datetime.now(timezone.utc).isoformat()
    root = Path(output)
    claim_name=RERUN_CLAIM if rerun_interrupted is not None else CLAIM
    write_json(Path(manifest).parent/claim_name, dict(**metadata, output=str(root.resolve())))
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
