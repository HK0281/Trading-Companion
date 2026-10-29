"""Planned denominators, per-class F1, observed calibration and request-level cascades."""

from trading_companion.classification_metrics import ratio, percentiles
from trading_companion.news_triage import OPTIONS
from trading_companion.evaluation_pricing import actual_cost

THRESHOLDS = tuple(i / 100 for i in range(50, 100, 5))


def _pred(row, question):
    return (row.get("answers") or {}).get(question, {}).get("choice")


def valid(row):
    return all(_pred(row, q) in OPTIONS[q] for q in OPTIONS)


def scores(rows):
    tasks, all_f1, combined = {}, [], {}
    combined_labels = [f"{q}:{label}" for q, labels in OPTIONS.items() for label in labels]
    for q, labels in OPTIONS.items():
        matrix = {label: dict.fromkeys((*labels, "missing"), 0) for label in labels}
        for row in rows:
            prediction = _pred(row, q)
            matrix[row["gold"][q]][prediction if prediction in labels else "missing"] += 1
        f1, valid_f1 = {}, []
        for label in labels:
            tp = matrix[label][label]
            fp = sum(matrix[other][label] for other in labels if other != label)
            fn = sum(v for key, v in matrix[label].items() if key != label)
            f1[label] = {"tp": tp, "fp": fp, "fn": fn, "support": sum(matrix[label].values()),
                         "f1": 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else 0}
            known_fn = fn - matrix[label]["missing"]
            valid_f1.append(2*tp/(2*tp+fp+known_fn) if 2*tp+fp+known_fn else 0)
            combined[f"{q}:{label}"] = {**dict.fromkeys(combined_labels, 0),
                                         **{f"{q}:{col}": n for col, n in matrix[label].items() if col != "missing"},
                                         "missing": matrix[label]["missing"]}
        correct = sum(matrix[label][label] for label in labels)
        available = sum(_pred(row, q) in labels for row in rows)
        tasks[q] = {"accuracy": ratio(correct, len(rows)), "valid_accuracy": ratio(correct, available),
                    "coverage": ratio(available, len(rows)), "macro_f1": sum(v["f1"] for v in f1.values())/len(labels),
                    "valid_macro_f1": sum(valid_f1)/len(labels) if available else None,
                    "classes": f1, "confusion": matrix}
        all_f1.extend(v["f1"] for v in f1.values())
    return {"planned_titles": len(rows), "questions": tasks,
            "combined_accuracy": ratio(sum(t["accuracy"]["numerator"] for t in tasks.values()), 3*len(rows)),
            "joint_accuracy": ratio(sum(all(_pred(row, q) == row["gold"][q] for q in OPTIONS) for row in rows), len(rows)),
            "macro_f1_all_14_labels": sum(all_f1)/len(all_f1),
            "mean_question_macro_f1": sum(t["macro_f1"] for t in tasks.values())/3,
            "combined_confusion": combined}


def calibration(rows, names):
    buckets = [[] for _ in range(10)]
    for row in rows:
        for q in names:
            answer = (row.get("answers") or {}).get(q, {})
            confidence = answer.get("confidence")
            if confidence is not None:
                buckets[min(9, int(confidence*10))].append((confidence, answer["choice"] == row["gold"][q]))
    total = sum(map(len, buckets))
    result, ece = [], 0
    for i, bucket in enumerate(buckets):
        n = len(bucket)
        confidence = sum(c for c, _ in bucket)/n if n else None
        accuracy = sum(ok for _, ok in bucket)/n if n else None
        if n:
            ece += n * abs(confidence - accuracy)
        result.append({"lower": i/10, "upper": (i+1)/10, "upper_inclusive": i == 9,
                       "n": n, "mean_confidence": confidence, "accuracy": accuracy})
    return {"ece": ece/total if total else None, "coverage": ratio(total, len(rows)*len(names)), "bins": result}


def confident(row, threshold):
    return valid(row) and all(row["answers"][q].get("confidence") is not None
                              and row["answers"][q]["confidence"] >= threshold for q in OPTIONS)


def selective(rows):
    result = []
    for threshold in THRESHOLDS:
        per_question = {}
        for q in OPTIONS:
            selected = [row for row in rows if (a := (row.get("answers") or {}).get(q, {})).get("confidence") is not None
                        and a["confidence"] >= threshold]
            per_question[q] = {"coverage": ratio(len(selected), len(rows)),
                               "accuracy": ratio(sum(_pred(r, q) == r["gold"][q] for r in selected), len(selected))}
        chosen = [r for r in rows if confident(r, threshold)]
        result.append({"threshold": threshold, "questions": per_question,
                       "title_coverage": ratio(len(chosen), len(rows)), "selected_scores": scores(chosen)})
    return result


def costs(rows, provider, requested_model, prices, *, simulated=False):
    ledger = [{"provider": "typesafe" if provider == "jev" else "deepseek", "requested_model": requested_model,
               "model": row.get("model"), "usage": row.get("usage") or
               dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"))}
              for row in rows if row.get("attempted")]
    result = actual_cost(prices, "typesafe" if provider == "jev" else "deepseek", requested_model, ledger)
    if not rows:
        result.update(known_subtotal="0.000000", estimated_total="0.000000", complete=True)
    result["scope"] = "synthetic_usage_scenario_not_actual_spend" if simulated else result["scope"]
    return result


def cascade(rows, models, prices, *, simulated=False):
    curves = []
    for threshold in THRESHOLDS:
        adopted, fallback, latencies = [], [], []
        for row in rows:
            jev, deepseek = row["results"]["jev"], row["results"]["deepseek"]
            direct = confident(jev, threshold)
            selected = jev if direct else deepseek
            adopted.append({"gold": row["gold"], "answers": selected.get("answers")})
            if not direct:
                fallback.append(deepseek)
            if jev.get("elapsed_ms") is not None and (direct or deepseek.get("elapsed_ms") is not None):
                latencies.append(jev["elapsed_ms"] + (0 if direct else deepseek["elapsed_ms"]))
        curves.append({"threshold": threshold, "scores": scores(adopted),
                       "deepseek_demand": ratio(len(fallback), len(rows)),
                       "jev_direct": ratio(len(rows)-len(fallback), len(rows)),
                       "missing_required_results": sum(not valid(r) for r in adopted),
                       "missing_required_calls": sum(not row["results"]["jev"].get("attempted") for row in rows)
                                                  + sum(not r.get("attempted") for r in fallback),
                       "cost_by_provider": {
                           "jev": costs([r["results"]["jev"] for r in rows], "jev", models["jev"], prices, simulated=simulated),
                           "deepseek": costs(fallback, "deepseek", models["deepseek"], prices, simulated=simulated)},
                       "estimated_serial_latency": percentiles(latencies),
                       "scope": "counterfactual_from_saved_results_not_deployed_cascade_or_actual_spend"})
    return curves


def metrics(rows, classifiers, models, prices, *, simulated=False):
    result = {}
    for provider in ("rules", *classifiers):
        observations = [{**r["results"][provider], "gold": r["gold"]} for r in rows]
        statuses = ("ok", "failed", "not_run")
        result[provider] = {**scores(observations),
            "statuses": {status: sum(r["status"] == status for r in observations) for status in statuses},
            "latency": {status: percentiles([r["elapsed_ms"] for r in observations
                                           if r["status"] == status and r["elapsed_ms"] is not None]) for status in statuses},
            "timeout_latency": percentiles([r["elapsed_ms"] for r in observations if r["code"] == "timeout"]),
            "actual_models": sorted({r["model"] for r in observations if r.get("model")}),
            "calibration": {q: calibration(observations, [q]) for q in OPTIONS} if provider == "jev" else None,
            "combined_calibration": calibration(observations, list(OPTIONS)) if provider == "jev" else None,
            "selective": selective(observations) if provider == "jev" else None,
            "cost": costs(observations, provider, models[provider], prices, simulated=simulated) if provider != "rules" else
                    {"scope": "no_provider_calls", "amount": "0", "currency": None}}
    return {"providers": result, "cascade": cascade(rows, models, prices, simulated=simulated)
            if "jev" in classifiers and "deepseek" in classifiers else None}
