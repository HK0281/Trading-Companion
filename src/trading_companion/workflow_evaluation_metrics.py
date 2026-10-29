"""One set of denominators for all steps and overlapping tags; missing detail stays unknown."""

from collections import Counter, defaultdict

from trading_companion.trace_cli import TraceReader, TraceSummary


CATEGORIES = ("completed", "clarification", "rejected", "error")
ANSWER_CHECKS = frozenset(("knowledge_answer", "product_answer", "research_response", "workflow_response"))
PASS_CODES = frozenset(("ok", "answer_ready", "product_research_answered", "response_evidence_checked", "workflow_response_evidence_checked"))


def ratio(numerator, denominator, *, unknown=0):
    return {"numerator": numerator, "denominator": denominator, "unknown": unknown,
            "value": numerator / denominator if denominator else None}


def detail(step):
    trace = step.get("trace")
    return trace is not None and not trace["truncated"] and not trace["missing_parents"]


def events(step, *, kind=None, operations=None):
    return [event for event in (step.get("trace") or {}).get("event_records", [])
            if (kind is None or event["kind"] == kind) and (operations is None or event["operation"] in operations)]


def expects_answer(step):
    expected = step["expectation"]
    return expected["offline_expected"]["category"] == "completed" and "answer_origin_if_answered" in expected["quality"]


def mechanical_checks(steps):
    eligible = [step for step in steps if expects_answer(step)]
    counts = Counter(dict.fromkeys(("passed", "failed", "not_executed", "unknown", "attempts", "rejected_attempts"), 0))
    for step in eligible:
        checks = events(step, kind="check", operations=ANSWER_CHECKS)
        counts["attempts"] += len(checks)
        counts["rejected_attempts"] += sum(event["status"] in ("rejected", "failed", "stopped") for event in checks)
        if not detail(step):
            state = "not_executed" if step["status"] == "not_run" else "unknown"
        elif not checks:
            state = "not_executed"
        else:
            last = checks[-1]
            state = "passed" if last["status"] == "ok" and last["code"] in PASS_CODES else "failed"
            observed = step.get("observed") or {}
            if observed.get("answer_origin") != "model" or observed.get("answer_status") == "fallback":
                state = "failed"
        counts[state] += 1
    return {"scope": "joint_citation_and_numeric_mechanical_checks_not_semantic_support",
            "applicable": len(eligible), **counts,
            "pass_rate": ratio(counts["passed"], len(eligible), unknown=counts["unknown"] + counts["not_executed"])}


def repair_metrics(steps):
    result = {}
    for label, operation in (("rule_correction", "intent_rule"), ("model_rewrite", "model_rewrite")):
        applicable = [step for step in steps if (expects_answer(step) if label == "model_rewrite" else
                      step["expectation"]["offline_expected"]["intent"] == "technical_research")]
        entered, unknown, attempts = 0, 0, 0
        for step in steps:
            matches = [event for event in events(step, kind="repair", operations={operation}) if event["entered"]]
            # A skipped rule is not a correction; the trace records rule_applied explicitly.
            matches = [event for event in matches if operation != "intent_rule" or event["code"] == "rule_applied"]
            attempts += len(matches)
            entered += bool(matches)
            unknown += int(not detail(step) and not matches)
        result[label] = {"affected_steps": entered, "entered_attempts": attempts,
                         "all_planned": ratio(entered, len(steps), unknown=unknown),
                         "applicable": ratio(sum(bool([e for e in events(step, kind="repair", operations={operation})
                              if e["entered"] and (operation != "intent_rule" or e["code"] == "rule_applied")]) for step in applicable),
                              len(applicable), unknown=sum(not detail(step) and not any(e["entered"] and
                                  (operation != "intent_rule" or e["code"] == "rule_applied") for e in
                                  events(step, kind="repair", operations={operation})) for step in applicable)),
                         "applicability": "planned_model_answer_steps" if label == "model_rewrite" else "planned_technical_entry_steps"}
    return result


def source_metrics(steps):
    company = [step for step in steps if step["expectation"]["offline_expected"]["intent"] == "company_research"
               or step["expectation"]["offline_expected"]["intent"] == "screening_followup"
               and "answer_status_if_answered" in step["expectation"]["quality"]]
    counts = Counter(dict.fromkeys(("partial_deadline", "all_missing", "ordinary_source_failure", "whole_turn_stop",
                                  "unknown", "entered_collection", "artificial_fault_steps", "natural_partial_deadline",
                                  "deadline_without_verified_partial"), 0))
    for step in company:
        observed = step.get("observed") or {}
        artificial = "fault_injected" in step["tags"]
        counts["artificial_fault_steps"] += artificial
        entered = step.get("collection_attempts")
        counts["entered_collection"] += entered is not None and entered > 0
        counts["whole_turn_stop"] += any(code in observed.get("codes", []) for code in ("turn_deadline_exceeded", "turn_cancelled"))
        if not detail(step) or entered is None:
            counts["unknown"] += 1
            continue
        sections = [section for item in observed.get("coverage", []) for section in item["sections"]]
        deadline = any(section["code"] == "research_data_deadline_exceeded" for section in sections)
        retained = bool((observed.get("company_research") or {}).get("evidence"))
        partial = (deadline and retained and any(section["status"] in ("available", "limited") for section in sections)
                   and any(section["status"] == "unavailable" for section in sections)
                   and "未取得" in (observed.get("answer") or ""))
        counts["partial_deadline"] += partial
        counts["deadline_without_verified_partial"] += deadline and not partial
        counts["natural_partial_deadline"] += partial and not artificial
        counts["all_missing"] += bool(sections) and not retained and not any(s["status"] in ("available", "limited") for s in sections)
        counts["ordinary_source_failure"] += any(e["status"] == "failed" for e in events(step, kind="source"))
    return {"planned_company_steps": len(company), **counts,
            "partial_per_planned_company": ratio(counts["partial_deadline"], len(company), unknown=counts["unknown"]),
            "partial_per_entered_collection": ratio(counts["partial_deadline"], counts["entered_collection"], unknown=counts["unknown"]),
            "natural_partial_per_planned_company": ratio(counts["natural_partial_deadline"],
                len(company) - counts["artificial_fault_steps"], unknown=sum(not detail(s) for s in company if "fault_injected" not in s["tags"])),
            "scope": "artificial_faults_separate_not_real_source_timeout_performance"}


def summarize_steps(steps):
    total = len(steps)
    executed = sum(step["status"] != "not_run" for step in steps)
    route, intent, unknown = 0, 0, 0
    matrix = {expected: dict.fromkeys(CATEGORIES, 0) for expected in CATEGORIES}
    unmapped, alternative, groups = 0, 0, defaultdict(list)
    trace_summary, reader = TraceSummary(), TraceReader()
    for step in steps:
        groups[(step["case_id"], step["repeat"])].append(step)
        observed = step.get("observed")
        if not observed or not step.get("trace"):
            unknown += 1
        else:
            choices = step["expectation"]["acceptable"]
            route += any(observed["route"] in choice["route"] for choice in choices)
            intent += any(observed["intent"] in choice["intent"] for choice in choices)
        if step.get("trace"):
            trace_summary.add(step["trace"])
            reader.valid_turns += 1
        if observed and observed["category"] in CATEGORIES:
            expected = step["expectation"]["offline_expected"]["category"]
            matrix[expected][observed["category"]] += 1
            alternative += observed["category"] != expected and step.get("acceptable_outcome") is True
        elif step["status"] != "not_run":
            unmapped += 1
    trace = trace_summary.report(reader)
    answers = [step for step in steps if expects_answer(step)]
    fallback = sum((step.get("observed") or {}).get("answer_origin") == "application"
                   or (step.get("observed") or {}).get("answer_status") == "fallback" for step in answers)
    return {"planned_steps": total, "execution_coverage": ratio(executed, total),
            "step_success": ratio(sum(step["status"] == "passed" for step in steps), total),
            "scenario_success": ratio(sum(all(step["status"] == "passed" for step in items) for items in groups.values()), len(groups)),
            "route_accuracy": ratio(route, total, unknown=unknown), "intent_accuracy": ratio(intent, total, unknown=unknown),
            "route_accuracy_executed": ratio(route, executed, unknown=unknown - (total - executed)),
            "intent_accuracy_executed": ratio(intent, executed, unknown=unknown - (total - executed)),
            "hard_requirement_violations": sum(bool(s["hard_requirement_violations"]) for s in steps),
            "outside_acceptable": sum(s["outside_acceptable"] is True for s in steps),
            "quality_requirement_failures": sum(bool(s["quality_requirement_failures"]) for s in steps),
            "unverified_requirement_steps": sum(bool(s.get("unverified_requirements")) for s in steps),
            "result_categories": {"expected_by_actual": matrix, "not_run": total - executed,
                                  "unmapped_executed": unmapped, "accepted_alternative_categories": alternative},
            "raw_statuses": dict(Counter((s.get("observed") or {}).get("status", "not_run") for s in steps)),
            "limited_steps": sum((s.get("observed") or {}).get("status") == "limited" for s in steps),
            "joint_evidence_checks": mechanical_checks(steps), "repairs": repair_metrics(steps),
            "fallback": {"affected_steps": fallback, "all_planned": ratio(fallback, total),
                         "applicable": ratio(fallback, len(answers), unknown=sum((s.get("observed") or {}).get("answer_origin")
                                                  not in ("model", "application") for s in answers))},
            "source_deadline": source_metrics(steps),
            "latency_coverage": ratio(trace["overall"]["total_elapsed_ms"]["n"], total),
            "trace": {key: trace[key] for key in ("overall", "models", "event_summaries", "details")}}


def evaluation_metrics(report):
    steps = report["steps"]
    return {"version": "workflow-metrics-v1", "overall": summarize_steps(steps),
            "run_integrity": {"status": report["status"], "code": report["code"],
                              "source_network_attempts_blocked": report["source_network_attempts_blocked"],
                              "credential_reads_attempted": report["credential_reads_attempted"]},
            "by_tag": {tag: summarize_steps([step for step in steps if tag in step["tags"]])
                       for tag in sorted({tag for step in steps for tag in step["tags"]})},
            "not_applicable_to_mode": report["not_applicable_to_mode"],
            "not_applicable_steps": report["not_applicable_steps"],
            "denominator_policy": "planned_selected_compatible_steps_including_failures_and_not_run; overlapping_tags_not_additive"}
