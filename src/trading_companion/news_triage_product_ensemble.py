"""Predeclared agreement/union policy, using only this arm's actual calls."""

from copy import deepcopy

from trading_companion.news_triage_product import OPTIONS, UNRESOLVED, input_state
from trading_companion.news_triage_product_batch import make_batches

ENSEMBLE_ARM = "ensemble_jev_deepseek_batch"
ENSEMBLE_POLICY = {
    "version": "news-triage-product-ensemble-v1",
    "jev_questions": list(OPTIONS), "deepseek_questions": list(OPTIONS),
    "relevance": "jev_only_deepseek_diagnostic",
    "tone": "valid_agreement_else_unresolved",
    "importance": "any_valid_high_else_valid_agreement_else_valid_disagreement_unknown",
    "unavailable_importance": "unresolved_unless_other_valid_high",
    "invalid_or_abstained_question": "unresolved_preserve_other_questions",
    "confidence": "jev_relevance_only_no_synthetic_ensemble_probabilities",
    "filtering": "retain_all",
}


def unanswered():
    return {"choice": UNRESOLVED, "confidence": None, "probabilities": None}


def component(source, provider, item=None):
    answers, states = {}, {}
    available = source["status"] in ("ok", "unresolved")
    for q in OPTIONS:
        answer, reason = None, source["reason"]
        if available:
            if provider == "jev":
                answer = (source.get("answers") or {}).get(q)
                reason = "model_abstention" if answer and answer["choice"] == UNRESOLVED else "ok"
            elif item is not None:
                answer, state = item["answers"][q], item["question_status"][q]
                reason = state["reason"]
            else:
                reason = "missing_item"
        valid = answer is not None and answer["choice"] in OPTIONS[q] and reason == "ok"
        answers[q] = deepcopy(answer) if valid else unanswered()
        states[q] = {"provider": provider, "status": "ok" if valid else "unresolved",
                     "reason": reason if reason != "ok" or valid else "missing_answer",
                     "call_ids": source["call_ids"]}
    complete = all(s["status"] == "ok" for s in states.values())
    return {"status": "ok" if complete else source["status"] if not available else "unresolved",
            "reason": "ok" if complete else source["reason"] if not available else "partial_questions",
            "answers": answers, "question_status": states, "call_ids": source["call_ids"],
            "call_status": source["status"], "proposed_filter": False}


def combine(jev, deepseek):
    answers, states = {}, {}
    for q in OPTIONS:
        left, right = jev["answers"][q], deepseek["answers"][q]
        valid_left, valid_right = (c["question_status"][q]["status"] == "ok" for c in (jev, deepseek))
        choice, reason = UNRESOLVED, "provider_unavailable"
        if q == "relevance":
            choice = left["choice"]
            reason = "jev_only" if valid_left else "jev_unavailable"
        elif q == "importance" and (valid_left and left["choice"] == "high"
                                    or valid_right and right["choice"] == "high"):
            choice, reason = "high", "any_valid_high"
        elif valid_left and valid_right:
            if left["choice"] == right["choice"]:
                choice, reason = left["choice"], "agreement"
            elif q == "importance":
                choice, reason = "unknown", "importance_disagreement"
            else:
                reason = "tone_disagreement"
        answers[q] = deepcopy(left) if q == "relevance" else {**unanswered(), "choice": choice}
        states[q] = {"provider": "jev" if q == "relevance" else "ensemble",
                     "status": "ok" if choice in OPTIONS[q] else "unresolved", "reason": reason,
                     "call_ids": jev["call_ids"] if q == "relevance" else jev["call_ids"]+deepseek["call_ids"]}
    if answers["relevance"]["choice"] == "unrelated" and answers["tone"]["choice"] not in ("neutral", UNRESOLVED):
        answers["tone"] = unanswered()
        states["tone"].update(status="unresolved", reason="unrelated_directional_tone")
    complete = all(s["status"] == "ok" for s in states.values())
    return {"status": "ok" if complete else "unresolved", "reason": "ok" if complete else "partial_questions",
            "answers": answers, "question_status": states, "call_ids": jev["call_ids"]+deepseek["call_ids"],
            "component_results": {"jev": jev, "deepseek": deepseek}, "proposed_filter": False}


def run_ensemble_batches(rows, call):
    reports = []
    for batch in make_batches(rows):
        items, bid = batch["rows"], batch["id"]
        common = {"budget_id": ENSEMBLE_ARM+":"+bid, "budget_calls": len(items)+1, "batch_id": bid,
                  "selected_questions": tuple(OPTIONS)}
        first = {r["id"]: call("jev", r, ENSEMBLE_ARM, **common) for r in items}
        payload = [{"id": r["id"], **input_state(r)} for r in items]
        second = call("deepseek", items[0], ENSEMBLE_ARM, items=payload, **common)
        returned = {r["id"]: r for r in second.get("items", [])}
        failed = unresolved = 0
        for row in items:
            jev = component(first[row["id"]], "jev")
            ds = component(second, "deepseek", returned.get(row["id"]))
            result = combine(jev, ds)
            result.update(batch_id=bid, issuer_conflict=bool(row["identity"]["issuer_verified"]
                          and result["answers"]["relevance"]["choice"] == "unrelated"))
            row["results"][ENSEMBLE_ARM] = result
            failed += any(c["status"] != "ok" for c in (jev, ds))
            unresolved += result["status"] != "ok"
        reports.append({"id": bid, "item_ids": [r["id"] for r in items], "size": len(items),
                        "status": second["status"], "reason": second["reason"], "call_ids": second["call_ids"],
                        "elapsed_ms": second.get("elapsed_ms"), "failed_items": failed,
                        "valid_items": len(items)-failed, "unresolved_items": unresolved,
                        "extra_returned_items": second.get("extra_returned_items", 0),
                        "unknown_returned_items": second.get("unknown_returned_items", 0)})
    return reports
