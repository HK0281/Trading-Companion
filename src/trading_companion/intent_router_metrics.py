"""Eight-label classification, explicit abstentions, calibration and locked cascades."""

from math import sqrt

from trading_companion.classification_metrics import ratio, percentiles
from trading_companion.evaluation_pricing import actual_cost
from trading_companion.intent_router_task import LABELS

THRESHOLDS = tuple(i/100 for i in range(50, 100, 5)) + (.99,)
SEMANTIC = tuple(k for k in LABELS if k != "clarify")


def prediction(result):
    return result.get("prediction") if result.get("status") == "ok" and result.get("prediction") in LABELS else None


def wilson(correct, n):
    if not n:
        return {"lower": None, "upper": None, "n": 0}
    z, p = 1.959963984540054, correct/n
    mid = (p+z*z/(2*n))/(1+z*z/n)
    radius = z*sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return {"lower": max(0, mid-radius), "upper": min(1, mid+radius), "n": n}


def scores(rows, provider, *, non_clarify=False):
    rows = [r for r in rows if r["gold"] is not None and (not non_clarify or r["gold"] != "clarify")]
    labels = SEMANTIC if non_clarify else LABELS
    matrix = {label: dict.fromkeys((*LABELS, "failed", "not_run"), 0) for label in labels}
    for row in rows:
        result = row["results"][provider]
        pred = prediction(result)
        matrix[row["gold"]][pred or ("not_run" if result["status"] == "not_run" else "failed")] += 1
    classes = {}
    for label in labels:
        tp = matrix[label][label]
        fp = sum(matrix[k][label] for k in labels if k != label)
        fn = sum(matrix[label].values())-tp
        classes[label] = {"support": tp+fn, "accuracy": ratio(tp, tp+fn), "precision": ratio(tp, tp+fp),
                          "recall": ratio(tp, tp+fn), "f1": 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0}
    correct = sum(matrix[k][k] for k in labels)
    return {"accuracy": ratio(correct, len(rows)), "accuracy_interval": wilson(correct, len(rows)),
            "macro_f1": sum(v["f1"] for v in classes.values())/len(labels) if rows else None,
            "classes": classes, "confusion": matrix}


def calibration(rows, provider, *, business_only=False):
    bins = [[] for _ in range(10)]
    for row in rows:
        result = row["results"][provider]
        pred, conf = prediction(result), result.get("confidence")
        if pred is not None and conf is not None and (not business_only or pred != "clarify"):
            bins[min(9, int(conf*10))].append((conf, pred == row["gold"]))
    n = sum(map(len, bins))
    table, ece = [], 0
    for i, values in enumerate(bins):
        confidence = sum(v[0] for v in values)/len(values) if values else None
        accuracy = sum(v[1] for v in values)/len(values) if values else None
        if values:
            ece += len(values)*abs(confidence-accuracy)
        table.append(dict(lower=i/10, upper=(i+1)/10, upper_inclusive=i==9, n=len(values), mean_confidence=confidence, accuracy=accuracy))
    return {"ece": ece/n if n else None, "coverage": ratio(n, len(rows)), "bins": table}


def adopted(result, threshold):
    return threshold is not None and prediction(result) in SEMANTIC and result.get("confidence") is not None and result["confidence"] >= threshold


def selective(rows, threshold):
    chosen = [r for r in rows if adopted(r["results"]["jev"], threshold)]
    correct = sum(prediction(r["results"]["jev"]) == r["gold"] for r in chosen)
    return {"threshold": threshold, "accuracy": ratio(correct, len(chosen)), "coverage": ratio(len(chosen), len(rows)),
            "interval": wilson(correct, len(chosen))}


def choose_threshold(rows):
    points = [selective(rows, t) for t in THRESHOLDS]
    # The same support/coverage/lower-bound requirements are fixed before live runs.
    candidates = [p for p in points if p["accuracy"]["denominator"] >= 40 and p["coverage"]["value"] >= .4
                  and p["accuracy"]["value"] >= .95 and p["interval"]["lower"] >= .85]
    return max(candidates, key=lambda p: (p["coverage"]["value"], p["threshold"]))["threshold"] if candidates else None


def costs(results, provider, model, prices, *, simulated=False):
    name = "typesafe" if provider == "jev" else provider
    ledger = [{"provider": name, "requested_model": model, "model": r.get("model"),
               "usage": r.get("usage") or dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"))}
              for r in results if r.get("attempted")]
    value = actual_cost(prices, name, model, ledger)
    if not ledger:
        value.update(known_subtotal="0.000000", estimated_total="0.000000", complete=True)
    if simulated:
        value["scope"] = "simulated_usage_not_actual_spend"
    return value


def provider_metrics(rows, provider, *, model=None, prices=None, simulated=False):
    observations = [r["results"][provider] for r in rows]
    n = len(rows)
    predicted = [(r["gold"], prediction(r["results"][provider])) for r in rows]
    negatives = [r for r in rows if "near_miss" in r.get("tags", [])]
    needs = [r for r in rows if r["gold"] == "clarify"]
    bad_routes = sum(g != "clarify" and p in SEMANTIC and p != g for g, p in predicted)
    unknown = sum(g == "clarify" and p in SEMANTIC for g, p in predicted)
    return {"all": scores(rows, provider), "non_clarify": scores(rows, provider, non_clarify=True),
            "misroute_rate": ratio(bad_routes, n), "ask_rate": ratio(sum(p == "clarify" for _, p in predicted), n),
            "correct_ask": ratio(sum(g == p == "clarify" for g,p in predicted), len(needs)),
            "excess_ask": ratio(sum(g != "clarify" and p == "clarify" for g,p in predicted), n),
            "forced_route": ratio(unknown, len(needs)),
            "executor_clarification": {"status": "not_determined", "count": len(needs)} if provider == "rules" else None,
            "false_trigger_rate": ratio(sum(prediction(r["results"][provider]) in r["forbidden_intents"] for r in negatives), len(negatives)),
            "negative_error_rate": ratio(sum(prediction(r["results"][provider]) != r["gold"] for r in negatives), len(negatives)),
            "statuses": {s: sum(o["status"] == s for o in observations) for s in ("ok", "failed", "not_run")},
            "actual_models": sorted({o["model"] for o in observations if o.get("model")}),
            "latency": {**{s: percentiles([o["elapsed_ms"] for o in observations if o["status"] == s and o.get("elapsed_ms") is not None])
                           for s in ("ok", "failed")},
                        "attempted": percentiles([o["elapsed_ms"] for o in observations if o.get("attempted") and o.get("elapsed_ms") is not None]),
                        "timeout": percentiles([o["elapsed_ms"] for o in observations if o.get("code") == "timeout" and o.get("elapsed_ms") is not None])},
            "cost": costs(observations, provider, model, prices, simulated=simulated) if provider != "rules" else None,
            "calibration": calibration(rows, provider) if provider == "jev" else None,
            "business_calibration": calibration(rows, provider, business_only=True) if provider == "jev" else None}


def paired(rows, provider):
    both = sum(prediction(r["results"][provider]) == r["gold"] and prediction(r["results"]["rules"]) == r["gold"] for r in rows)
    model = sum(prediction(r["results"][provider]) == r["gold"] for r in rows)
    rule = sum(prediction(r["results"]["rules"]) == r["gold"] for r in rows)
    return {"both_correct": both, "only_model": model-both, "only_rules": rule-both,
            "neither": len(rows)-model-rule+both, "n": len(rows),
            "model_interval": wilson(model, len(rows)), "rules_interval": wilson(rule, len(rows))}


def cascade(rows, threshold, models, prices, *, fallback, simulated=False):
    transformed, fallback_results, times = [], [], []
    missing = 0
    for row in rows:
        jev = row["results"]["jev"]
        direct = adopted(jev, threshold)
        if direct:
            selected = jev
        elif fallback == "deepseek":
            selected = row["results"]["deepseek"]
            fallback_results.append(selected)
        else:
            selected = {"status": "ok", "prediction": "clarify"}
        if not jev.get("attempted"):
            selected = {"status": "not_run", "prediction": None}
        valid = prediction(selected) is not None
        missing += int(not valid or not jev.get("attempted"))
        transformed.append({**row, "results": {"cascade": selected}})
        parts = [jev] + ([selected] if not direct and fallback == "deepseek" else [])
        if all(p.get("attempted") and p.get("elapsed_ms") is not None for p in parts):
            times.append(sum(p["elapsed_ms"] for p in parts))
    needs = [r for r in transformed if r["gold"] == "clarify"]
    negatives = [r for r in transformed if "near_miss" in r.get("tags", [])]
    return {"threshold": threshold, "fallback": fallback, "scores": scores(transformed, "cascade"),
            "misroute_rate": ratio(sum(r["gold"] != "clarify" and prediction(r["results"]["cascade"]) in SEMANTIC
                and prediction(r["results"]["cascade"]) != r["gold"] for r in transformed), len(rows)),
            "forced_route": ratio(sum(prediction(r["results"]["cascade"]) in SEMANTIC for r in needs), len(needs)),
            "false_trigger_rate": ratio(sum(prediction(r["results"]["cascade"]) in r["forbidden_intents"] for r in negatives), len(negatives)),
            "ask_rate": ratio(sum(prediction(r["results"]["cascade"]) == "clarify" for r in transformed), len(rows)),
            "fallback_demand": ratio(sum(not adopted(r["results"]["jev"], threshold) for r in rows), len(rows)),
            "missing_required_results": missing, "estimated_serial_latency": percentiles(times),
            "cost_by_provider": {"jev": costs([r["results"]["jev"] for r in rows], "jev", models["jev"], prices, simulated=simulated),
                "deepseek": costs(fallback_results, "deepseek", models.get("deepseek"), prices, simulated=simulated) if fallback == "deepseek" else None},
            "scope": "counterfactual_from_saved_results_not_deployed_latency_or_actual_spend"}


def metrics(rows, classifiers, models, prices, *, simulated=False, threshold=None, split="dev"):
    result = {"providers": {p: provider_metrics(rows, p, model=models.get(p), prices=prices, simulated=simulated)
                            for p in ("rules", *classifiers)}, "paired": {p: paired(rows,p) for p in classifiers},
              "selective": None, "cascades": []}
    if "jev" in classifiers:
        result["selective"] = [selective(rows,t) for t in THRESHOLDS] if split == "dev" else [selective(rows,threshold)]
        result["cascades"] = [cascade(rows,threshold,models,prices,fallback=f,simulated=simulated)
                               for f in (("ask", "deepseek") if "deepseek" in classifiers else ("ask",))]
    return result


def cutover(report):
    """Predeclared evidence gates; passing only supports proposing step 2b."""
    p, values = report["plan"], report["metrics"]["providers"]
    if "jev" not in values or p["suite"] != "paraphrase" or p["split"] != "holdout":
        return {"status": "not_applicable", "checks": {}}
    model, rules = values["jev"], values["rules"]
    checks = {}
    for scope in ("all", "non_clarify"):
        checks[scope+"_accuracy"] = model[scope]["accuracy"]["value"] >= rules[scope]["accuracy"]["value"]
        checks[scope+"_macro_f1"] = model[scope]["macro_f1"] >= rules[scope]["macro_f1"]
    subset = [r for r in report["rows"] if r["case_kind"] == "semantic" and "paraphrase" in r["tags"]]
    for scope, non_clarify in (("all",False),("non_clarify",True)):
        checks["paraphrase_gain_"+scope] = bool(subset) and scores(subset,"jev",non_clarify=non_clarify)["accuracy"]["value"]-scores(subset,"rules",non_clarify=non_clarify)["accuracy"]["value"] >= .05-1e-12
    checks["negative_triggers"] = model["false_trigger_rate"]["value"] <= rules["false_trigger_rate"]["value"]
    checks["forced_route"] = model["forced_route"]["value"] <= rules["forced_route"]["value"]
    point = report["metrics"]["selective"][0]
    checks["selective_accuracy"] = (point["accuracy"]["denominator"] >= 40 and point["accuracy"]["value"] >= .95
        and point["coverage"]["value"] >= .4 and point["interval"]["lower"] >= .85)
    latency = model["latency"]["attempted"]
    checks["latency"] = latency["n"] == model["all"]["accuracy"]["denominator"] and latency["p95_ms"] <= 1500
    checks["coverage"] = model["statuses"]["ok"]/model["all"]["accuracy"]["denominator"] >= .99
    checks["evidence_complete"] = (report["status"] == "completed" and p["mode"] == "live" and p["corpus"]["annotation_status"] == "reviewed"
                                    and model["cost"]["complete"] and p["threshold_lock"] is not None
                                    and p["threshold_lock"]["selection"]["actual_models"] == model["actual_models"])
    checks["protected_zero_calls"] = report["protected"]["model_calls"] == 0 and report["protected"]["n"] == 64
    cascade_checks = [{"fallback": c["fallback"], "complete": c["missing_required_results"] == 0,
                       "serial_p95_within_3000ms": c["estimated_serial_latency"]["n"] == len(report["rows"])
                           and c["estimated_serial_latency"]["p95_ms"] <= 3000}
                      for c in report["metrics"]["cascades"]]
    return {"status": "supports_proposal_only" if all(checks.values()) else "not_demonstrated", "checks": checks,
            "cascade_diagnostics": cascade_checks}
