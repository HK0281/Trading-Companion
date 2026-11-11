"""Company/capture-bound batches and question-level results for the split arm."""

from trading_companion.news_triage_product import OPTIONS, UNRESOLVED, digest, input_state

SPLIT_ARM = "split_jev_deepseek_batch"


def make_batches(rows):
    groups = {}
    for row in rows:
        # Missing provenance cannot merge independent records into an invented round.
        key = (row["instrument_id"], row.get("capture_batch"), row.get("capture_file"))
        if not all(key):
            key = (row["instrument_id"], "singleton", row["id"])
        groups.setdefault(key, []).append(row)
    batches = []
    for key in sorted(groups):
        ordered = sorted(groups[key], key=lambda r: r["id"])
        for start in range(0, len(ordered), 10):
            items = ordered[start:start+10]
            batches.append({"id": "Q"+digest([key, [r["id"] for r in items]])[:20], "rows": items})
    return batches


def run_split_batches(rows, call):
    reports = []
    for batch in make_batches(rows):
        items, bid = batch["rows"], batch["id"]
        common = {"budget_id": "batch:"+bid, "budget_calls": len(items)+1, "batch_id": bid}
        first = {r["id"]: call("jev", r, SPLIT_ARM, selected_questions=("relevance", "tone"), **common)
                 for r in items}
        payload = [{"id": r["id"], **input_state(r)} for r in items]
        second = call("deepseek", items[0], SPLIT_ARM, items=payload, **common)
        importance = {r["id"]: r for r in second.get("items", [])}
        failed = 0
        for row in items:
            jev, ds = first[row["id"]], importance.get(row["id"])
            answers, states = {}, {}
            for q in OPTIONS:
                provider = "deepseek" if q == "importance" else "jev"
                source = second if provider == "deepseek" else jev
                answer = ds["answer"] if provider == "deepseek" and ds else (
                    (source.get("answers") or {}).get(q) if provider == "jev" else None)
                valid = answer is not None and source["status"] in ("ok", "unresolved")
                if valid:
                    answers[q] = answer
                    reason = ds["reason"] if ds and provider == "deepseek" else source["reason"]
                else:
                    answers[q] = {"choice": UNRESOLVED, "confidence": None, "probabilities": None}
                    reason = source["reason"]
                ok = valid and answers[q]["choice"] != UNRESOLVED
                states[q] = {"provider": provider, "status": "ok" if ok else "unresolved",
                             "reason": reason, "call_ids": source["call_ids"]}
            failed += states["importance"]["status"] != "ok"
            complete = all(s["status"] == "ok" for s in states.values())
            row["results"][SPLIT_ARM] = {"status": "ok" if complete else "unresolved",
                "reason": "ok" if complete else "partial_questions", "answers": answers, "question_status": states,
                "call_ids": jev["call_ids"]+second["call_ids"], "batch_id": bid, "proposed_filter": False,
                "issuer_conflict": bool(row["identity"]["issuer_verified"] and answers["relevance"]["choice"] == "unrelated")}
        reports.append({"id": bid, "item_ids": [r["id"] for r in items], "size": len(items),
                        "status": second["status"], "reason": second["reason"], "call_ids": second["call_ids"],
                        "elapsed_ms": second.get("elapsed_ms"),
                        "failed_items": failed, "valid_items": len(items)-failed,
                        "extra_returned_items": second.get("extra_returned_items", 0),
                        "unknown_returned_items": second.get("unknown_returned_items", 0)})
    return reports
