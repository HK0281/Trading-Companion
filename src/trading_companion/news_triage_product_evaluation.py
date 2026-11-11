"""Plan-only by default; separately paid independent and actual cascade experiments."""

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import random
import sys
import json
from time import monotonic
from uuid import uuid4

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.evaluation_pricing import DEFAULT_PRICES, MODEL_ID, estimate_cost, load_prices
from trading_companion.local_env import load_evaluation_key_environment
from trading_companion.model import ModelConfigurationError
from trading_companion.news_triage_evaluation import network_boundary
from trading_companion.news_triage_product import OPTIONS, UNRESOLVED, filter_allowed, input_state, route_reasons
from trading_companion.news_triage_product_annotations import load_annotations
from trading_companion.news_triage_product_batch import SPLIT_ARM, make_batches, run_split_batches
from trading_companion.news_triage_product_data import load_dataset, write_json
from trading_companion.news_triage_product_ensemble import ENSEMBLE_ARM, ENSEMBLE_POLICY, run_ensemble_batches
from trading_companion.news_triage_product_metrics import metrics
from trading_companion.news_triage_product_models import (
    BATCH_TEMPLATE, DEEPSEEK_TEMPLATE, QUESTION_BATCH_TEMPLATE, ProductDeepSeek, ProductJev,
)
from trading_companion.typesafe_jev import ClassifierError, JevConfig, count
from trading_companion.workflow_evaluation_runtime import check_output_root

DEFAULT_ARMS = ("jev", "deepseek", "cascade_0.65", "cascade_0.90")
BATCH_ARMS = (SPLIT_ARM, ENSEMBLE_ARM)
ARMS = (*DEFAULT_ARMS, *BATCH_ARMS)


def make_plan(*, dataset_path, annotations_path=None, split="debug", arms=None, live=False,
              call_limit=None, deepseek_model=None, prices_path=None, timeout_seconds=6, report_dir=None):
    if split not in ("debug", "sealed") or type(live) is not bool:
        raise ValueError("Choose debug or sealed explicitly")
    selected_arms = list(DEFAULT_ARMS) if arms is None else list(arms)
    if not selected_arms or len(set(selected_arms)) != len(selected_arms) or any(a not in ARMS for a in selected_arms):
        raise ValueError("Invalid experiment arms")
    if (type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds)
            or not 1 <= timeout_seconds <= 60):
        raise ValueError("Timeout must be 1..60 seconds")
    dataset = load_dataset(dataset_path)
    annotations = None if annotations_path is None else load_annotations(annotations_path, dataset)
    rows = [row for row in dataset["rows"] if row["split"] == split]
    if not rows:
        raise ValueError("Selected split is empty")
    by_id = {} if annotations is None else {r["id"]: r for r in annotations["rows"]}
    reviewed = all(by_id.get(r["id"], {}).get("status") == "reviewed" for r in rows)
    providers = [p for p in ("jev", "deepseek") if any(a == p or a.startswith("cascade") or a in BATCH_ARMS for a in selected_arms)]
    batch_arms = [a for a in BATCH_ARMS if a in selected_arms]
    batches = make_batches(rows) if batch_arms else []
    max_calls = len(rows) * sum(2 if a.startswith("cascade") else 1 for a in selected_arms) + len(batches)*len(batch_arms)
    if call_limit is not None and (type(call_limit) is not int or not 1 <= call_limit <= max_calls):
        raise ValueError("Call limit must be explicit and within the planned maximum")
    if deepseek_model is not None and (type(deepseek_model) is not str or not MODEL_ID.fullmatch(deepseek_model)):
        raise ValueError("Invalid DeepSeek model ID")
    if live and (call_limit is None or prices_path is None or "deepseek" in providers and deepseek_model is None):
        raise ValueError("Live requires call limit, explicit prices and any required DeepSeek model")
    prices, price_digest = load_prices(DEFAULT_PRICES if prices_path is None else prices_path)
    check_output_root(report_dir)
    models = {p: "jev-latest" if p == "jev" else deepseek_model or "deepseek-flash" for p in providers}
    plan = {"version": "news-triage-product-plan-v2", "status": "planned", "live": live,
            "dataset_sha256": dataset["dataset_sha256"], "task": dataset["task"],
            "annotations_sha256": None if annotations is None else annotations["annotations_sha256"],
            "live_annotation_ready": reviewed, "directory_is_fixture": dataset["directory_is_fixture"],
            "split": split, "titles": len(rows), "arms": selected_arms, "providers": providers, "models": models,
            "thresholds": [.65, .90], "call_limit": call_limit, "worst_case_calls": max_calls,
            "timeout_seconds": timeout_seconds, "deepseek_template": DEEPSEEK_TEMPLATE,
            "importance_batch_template": BATCH_TEMPLATE,
            "planned_importance_batches": len(batches) if SPLIT_ARM in batch_arms else 0,
            "importance_batch_sizes": [len(b["rows"]) for b in batches] if SPLIT_ARM in batch_arms else [],
            "question_batch_template": QUESTION_BATCH_TEMPLATE,
            "planned_question_batches": len(batches) if ENSEMBLE_ARM in batch_arms else 0,
            "question_batch_sizes": [len(b["rows"]) for b in batches] if ENSEMBLE_ARM in batch_arms else [],
            "ensemble_policy": deepcopy(ENSEMBLE_POLICY) if ENSEMBLE_ARM in batch_arms else None,
            "title_policy": "retain_all",
            "price_config_sha256": price_digest,
            "cost_preview": {p: estimate_cost(prices, "typesafe" if p == "jev" else p, models[p],
                                              call_limit or max_calls) for p in providers},
            "cost_scope": "No token assumptions; unknown price/usage/alias stays unknown; currencies are separate.",
            "execution": "separate_actual_arms_global_call_cap_failures_count_no_retries_stop_on_unknown_usage",
            "per_title_limits": {"calls": 6, "seconds": 90, "observed_tokens": 24000},
            "batch_limits": {"calls": "titles_in_batch_plus_one", "seconds": 90, "observed_tokens": 24000},
            "schedule": "seed_20261110_old_arms_then_split_then_ensemble_company_capture_batches_sequential",
            "time_scope": "Per-request cooperative timeout; not a hard wall-clock or remote billing cap.",
            "enablement": "Evaluation only; paired answer checks and user approval still required.",
            "report_dir": None if report_dir is None else str(report_dir)}
    return plan, dataset, annotations, prices


def build_models(plan):
    models = {}
    for provider in plan["providers"]:
        name = "TYPESAFE_API_KEY" if provider == "jev" else "DEEPSEEK_API_KEY"
        key = load_evaluation_key_environment(name).get(name, "")
        if provider == "jev":
            models[provider] = ProductJev(JevConfig(key, timeout_seconds=plan["timeout_seconds"]))
        else:
            models[provider] = ProductDeepSeek(DeepSeekConfig(plan["models"][provider], key,
                                                            timeout_seconds=plan["timeout_seconds"]))
    return models


def empty_result(reason="call_limit"):
    return {"status": "not_run", "reason": reason, "answers": None, "proposed_filter": False, "call_ids": []}


def run_experiment(plan, dataset, annotations, prices, models, *, clock=monotonic):
    """Injectable runner; the CLI alone loads credentials after all review gates."""
    if ENSEMBLE_ARM in plan["arms"] and plan.get("ensemble_policy") != ENSEMBLE_POLICY:
        raise ValueError("Ensemble policy must be frozen in the execution plan")
    annotated = {r["id"]: r for r in annotations["rows"]}
    rows = [{**r, "gold": annotated[r["id"]]["gold"],
             "results": {arm: empty_result() for arm in plan["arms"]}}
            for r in dataset["rows"] if r["split"] == plan["split"]]
    order = list(range(len(rows)))
    random.Random(20261110).shuffle(order)
    ledger, stop_reason, item_budgets = [], None, {}
    def call(provider, row, arm, *, items=None, selected_questions=None, budget_id=None, budget_calls=6, batch_id=None):
        nonlocal stop_reason
        if len(ledger) >= plan["call_limit"] or stop_reason:
            return empty_result(stop_reason or "call_limit")
        budget = item_budgets.setdefault(budget_id or row["id"], {"start": clock(), "calls": 0, "tokens": 0})
        if budget["calls"] >= budget_calls or budget["tokens"] >= 24000 or clock()-budget["start"] >= 90:
            return empty_result("batch_budget" if budget_id is not None else "per_title_budget")
        budget["calls"] += 1
        record = {"id": len(ledger)+1, "item_id": row["id"], "arm": arm, "provider": "typesafe" if provider == "jev" else provider,
                  "requested_model": plan["models"][provider], "model": None, "usage": {},
                  "status": "failed", "reason": "classifier_internal_error", "answers": None}
        ledger.append(record)  # Reserve before every actual attempt, including failures.
        if batch_id is not None:
            record["batch_id"] = batch_id
        record["requested_questions"] = list(selected_questions or (("importance",) if items is not None else OPTIONS))
        if items is not None:
            record.update(item_id=None, item_ids=[item["id"] for item in items])
        started = clock()
        try:
            if items is not None:
                response = (models[provider].classify_questions_batch(items) if selected_questions is not None
                            else models[provider].classify_importance_batch(items))
            elif selected_questions is not None:
                response = models[provider].classify(input_state(row), selected_questions=selected_questions)
            else:
                response = models[provider].classify(input_state(row))
            record.update(response, status="ok", reason="ok")
            if any(a["choice"] == UNRESOLVED for a in response.get("answers", {}).values()):
                record["status"] = "unresolved"
            if items is not None and (any(r["status"] != "ok" for r in response["items"])
                                      or response["extra_returned_items"] or response["unknown_returned_items"]):
                record.update(status="unresolved", reason="batch_protocol_or_abstention")
        except ClassifierError as error:
            # Only allow-listed protocol metadata, never exception bodies/credentials.
            for name in ("model", "usage"):
                if name in error.metadata:
                    record[name] = error.metadata[name]
            record["reason"] = error.code
        except Exception:
            stop_reason = "classifier_internal_error"
        record["elapsed_ms"] = max(0, (clock()-started)*1000)
        usage = record["usage"]
        complete = (type(usage) is dict and all(count(usage.get(q)) for q in
                    ("prompt_tokens", "completion_tokens", "total_tokens"))
                    and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])
        if not complete:
            stop_reason = "usage_unknown_or_invalid"
        else:
            budget["tokens"] += usage["total_tokens"]
        result = {"status": record["status"], "reason": record["reason"], "answers": deepcopy(record["answers"]),
                  "model": record["model"], "call_ids": [record["id"]], "proposed_filter": False,
                  "elapsed_ms": record["elapsed_ms"]}
        for key in ("items", "extra_returned_items", "unknown_returned_items"):
            if key in record:
                result[key] = deepcopy(record[key])
        return result
    for index in order:
        row = rows[index]
        for arm in plan["arms"]:
            if arm in BATCH_ARMS:
                continue
            if arm in ("jev", "deepseek"):
                result = call(arm, row, arm)
                result["proposed_filter"] = filter_allowed(row["identity"], result, deepseek_reviewed=arm == "deepseek")
            else:
                threshold = float(arm.split("_")[1])
                first = call("jev", row, arm)
                reasons = route_reasons(row["identity"], first, threshold)
                result = first
                if reasons:
                    second = call("deepseek", row, arm)
                    result = {**second, "call_ids": first["call_ids"] + second["call_ids"]}
                    if second["status"] == "not_run" and first["call_ids"]:
                        result.update(status="unresolved", reason="review_not_available",
                                      answers={q: {"choice": UNRESOLVED, "confidence": None, "probabilities": None} for q in OPTIONS})
                    result["review_status"] = second["status"]
                    result["proposed_filter"] = filter_allowed(row["identity"], result, deepseek_reviewed=True)
                result["route_reasons"] = reasons
            result["issuer_conflict"] = bool(row["identity"]["issuer_verified"] and result["answers"]
                                               and result["answers"]["relevance"]["choice"] == "unrelated")
            row["results"][arm] = result
    batched = {}
    for arm, runner in ((SPLIT_ARM, run_split_batches), (ENSEMBLE_ARM, run_ensemble_batches)):
        if arm in plan["arms"]:
            batched[arm] = runner(rows, call)
    batch_reports = [b for batches in batched.values() for b in batches]
    scored = metrics(rows, plan["arms"], ledger, plan["models"], prices)
    for arm, batches in batched.items():
        key = "importance_batches" if arm == SPLIT_ARM else "question_batches"
        scored[arm][key] = {"planned": len(batches),
            "attempted": sum(bool(b["call_ids"]) for b in batches), "batches": batches,
            "failed_items": sum(b["failed_items"] for b in batches),
            "extra_returned_items": sum(b["extra_returned_items"] for b in batches),
            "unknown_returned_items": sum(b["unknown_returned_items"] for b in batches)}
    return {"version": "news-triage-product-evaluation-v2",
            "status": "partial" if stop_reason or any(r["results"][a]["status"] not in ("ok", "unresolved")
                                                       or r["results"][a].get("review_status") in ("failed", "not_run")
                                                       for r in rows for a in plan["arms"])
                      or any(b["failed_items"] or b["extra_returned_items"] or b["unknown_returned_items"] for b in batch_reports)
                      or any(e["arm"] in BATCH_ARMS and e["status"] == "failed" for e in ledger)
                      else "completed",
            "evaluated_at": datetime.now(timezone.utc).isoformat(), "plan": plan,
            "scope": "real_title_classification_only_no_product_enablement", "rows": rows, "ledger": ledger,
            "accounting": {"attempted": len(ledger), "limit": plan["call_limit"], "stop_reason": stop_reason,
                           "unresolved_and_not_run_stay_in_denominators": True},
            "metrics": scored, "title_policy": "retain_all",
            "product_enablement": False}


def evaluate(**kwargs):
    plan, dataset, annotations, prices = make_plan(**kwargs)
    if not plan["live"]:
        return plan
    # Even --live cannot bypass human review, an unfilled sheet or a fixture directory.
    if not plan["live_annotation_ready"] or plan["directory_is_fixture"]:
        raise ValueError("Live requires two complete independent human reviews and any user adjudications")
    print(json.dumps(plan, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
    try:
        with network_boundary(plan["providers"]) as denied:
            models = build_models(plan)
            report = run_experiment(plan, dataset, annotations, prices, models)
            report["accounting"]["network_violations"] = len(denied)
            if denied:
                report["status"] = "partial"
    except ModelConfigurationError:
        return {"status": "invalid_request", "code": "triage_input_invalid", "detail": "模型配置无效，未开始评估。"}
    if plan["report_dir"] is not None:
        target = Path(plan["report_dir"]) / uuid4().hex
        target.mkdir(parents=True, exist_ok=False)
        write_json(target / "report.json", report)
        report["report_file"] = str(target / "report.json")
    return report
