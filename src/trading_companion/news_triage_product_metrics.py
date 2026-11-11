"""Whole-denominator product metrics, real cascade ledgers and unchanged support gates."""

from collections import Counter
from math import sqrt

from trading_companion.evaluation_pricing import actual_cost
from trading_companion.news_triage_product import OPTIONS


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def wilson_lower(successes, total):
    if not total:
        return None
    z, p = 1.959963984540054, successes / total
    return (p + z*z/(2*total) - z*sqrt(p*(1-p)/total + z*z/(4*total*total))) / (1+z*z/total)


def prediction(result, q):
    if result["status"] in ("ok", "unresolved") and result.get("answers"):
        return result["answers"][q]["choice"]
    return result["status"]


def scores(rows, arm):
    result = {"total_titles": len(rows), "statuses": dict(Counter(r["results"][arm]["status"] for r in rows)),
              "questions": {}}
    for q, options in OPTIONS.items():
        confusion = Counter((r["gold"][q], prediction(r["results"][arm], q)) for r in rows)
        correct = sum(n for (gold, pred), n in confusion.items() if gold == pred and gold in options)
        resolved = sum(n for (gold, pred), n in confusion.items() if pred in options)
        per_class = {}
        for label in options:
            tp = confusion[label, label]
            support = sum(n for (gold, pred), n in confusion.items() if gold == label)
            predicted = sum(n for (gold, pred), n in confusion.items() if pred == label)
            precision, recall = ratio(tp, predicted), ratio(tp, support)
            per_class[label] = {"support": support, "predicted": predicted, "correct": tp,
                                "precision": precision, "recall": recall,
                                "f1": ratio(2*tp, support+predicted)}
        result["questions"][q] = {"denominator": len(rows), "resolved_predictions": resolved,
                                   "coverage": ratio(resolved, len(rows)), "correct": correct,
                                   "accuracy_all_titles": ratio(correct, len(rows)),
                                   "human_unresolved": sum(n for (g, p), n in confusion.items() if g not in options),
                                   "confusion": [{"gold": g, "prediction": p, "count": n}
                                                 for (g, p), n in sorted(confusion.items())], "per_class": per_class}
    filters = []  # User abandoned stage C; classification never removes materials.
    good = sum(r["gold"]["relevance"] == "unrelated" for r in filters)
    relevant = [r for r in rows if r["gold"]["relevance"] in ("primary", "mentioned")]
    removed = high_removed = 0
    lower = wilson_lower(good, len(filters))
    result["filter_policy"] = {"proposed": len(filters), "correct": good, "precision": ratio(good, len(filters)),
                               "wilson_95_lower": lower, "relevant_support": len(relevant),
                               "relevant_removed": removed, "relevant_retention": ratio(len(relevant)-removed, len(relevant)),
                               "important_relevant_removed": high_removed,
                               "unclassified_policy": "retain_original_title", "policy": "retain_all"}
    high = result["questions"]["importance"]["per_class"]["high"]
    tone = result["questions"]["tone"]["per_class"]
    result["gates"] = {
        "importance_sorting": {"sufficient_support": high["support"] >= 30,
                               "passed": high["support"] >= 30 and (high["recall"] or 0) >= .95,
                               "required_support": 30, "required_recall": .95},
        "tone_labels": {"sufficient_support": all(tone[t]["support"] >= 30 and tone[t]["predicted"] >= 30
                                                     for t in ("positive", "negative")),
                        "passed": all(tone[t]["support"] >= 30 and tone[t]["predicted"] >= 30
                                      and (tone[t]["precision"] or 0) >= .95 for t in ("positive", "negative")),
                        "required_support_each": 30, "required_precision_each": .95},
        "unrelated_filtering": {"status": "abandoned", "passed": False,
                                "reason": "user_authorized_retain_all_2026_11_10"}}
    return result


def metrics(rows, arms, ledger, models, prices):
    output = {}
    for arm in arms:
        arm_ledger = [entry for entry in ledger if entry["arm"] == arm]
        scored = scores(rows, arm)
        scored["by_stratum"] = {value: scores([r for r in rows if r["stratum"] == value], arm)
                                 for value in sorted({r["stratum"] for r in rows})}
        scored["by_source"] = {value: scores([r for r in rows if r["source"] == value], arm)
                                for value in sorted({r["source"] for r in rows})}
        scored["calls"] = len(arm_ledger)
        scored["provider_calls"] = dict(Counter(entry["provider"] for entry in arm_ledger))
        scored["costs"] = {p: actual_cost(prices, "typesafe" if p == "jev" else p, models[p],
                                        [e for e in arm_ledger if e["provider"] == ("typesafe" if p == "jev" else p)])
                            for p in models}
        scored["elapsed_ms_sum"] = sum(e["elapsed_ms"] for e in arm_ledger)
        scored["handoffs"] = sum(bool(r["results"][arm].get("route_reasons")) for r in rows)
        scored["issuer_conflicts"] = sum(r["results"][arm].get("issuer_conflict", False) for r in rows)
        scored["scope"] = "separate_actual_calls_no_counterfactual_savings" if arm.startswith("cascade") else "independent_classifier"
        if arm == "split_jev_deepseek_batch":
            scored["scope"] = "separate_actual_question_split_and_importance_batches"
        output[arm] = scored
    return output
