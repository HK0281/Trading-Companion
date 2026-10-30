"""Offline evaluation of actual message() journeys; this is not a model-quality score."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.evaluation_common import EvaluationModelMeter, reported_usage
from trading_companion.request_routing import IntentKind
from trading_companion.trace_cli import TraceReader, TraceSummary, ROUTES
from trading_companion.turn_trace import JsonlTraceSink, _SAFE_EVENT_CODES
from trading_companion.workflow_evaluation_fixtures import (
    DATA_PROFILES, PHASES, SCRIPT_KEYS, NOW, REFERENCE_DATE, FixedSources, ScriptedWorkflowModel, fixture_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data/evaluation/research-api-v1.json"
B_DELTA = ROOT / "data/evaluation/research-api-b-delta-v1.json"
VERSION = "research-workflow-evaluation-v2"
OUTCOME_KEYS = frozenset(("route", "intent", "status", "category", "code"))
REQUIRED_KEYS = frozenset(("forbid_codes", "require_codes", "securities_if_answered", "no_collection", "no_model",
                           "coverage", "forbid_categories", "screening_rows"))
QUALITY_KEYS = frozenset(("answer_contains", "answer_origin_if_answered", "answer_status_if_answered"))
STATUSES = frozenset(("completed", "limited", "needs_clarification", "out_of_scope", "invalid_request", "failed", "cancelled"))
CATEGORIES = frozenset(("completed", "clarification", "rejected", "error"))
INTENTS = frozenset(item.value for item in IntentKind)
REFUSAL_CODES = frozenset(("product_trade_execution_forbidden", "product_unsafe_request", "product_request_out_of_scope"))


def _require(condition):
    if not condition:
        raise ValueError("Invalid versioned evaluation input")


def _strings(value, *, maximum=20, allowed=None):
    _require(type(value) is list and len(value) <= maximum
             and all(type(item) is str and 0 < len(item) <= 200 for item in value)
             and len(set(value)) == len(value) and (allowed is None or set(value) <= allowed))


def _matches(outcome, acceptable):
    return all(outcome.get(key) in acceptable[key] for key in OUTCOME_KEYS)


def load_corpus(path=CORPUS):
    path = Path(path)
    _require(path.stat().st_size <= 1_000_000)

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda value: _require(False))
    if path.resolve() == CORPUS.resolve():
        data = _apply_b_delta(data, raw)
    _require(type(data) is dict and data.keys() == {"schema_version", "version", "annotation_policy", "data_authenticity", "cases"}
             and data["schema_version"] == "research-api-corpus-v2" and data["version"] == "research-api-v1"
             and data["data_authenticity"] == "fixed_synthetic" and type(data["annotation_policy"]) is str
             and type(data["cases"]) is list and 30 <= len(data["cases"]) <= 50)
    seen = set()
    choices = {"route": ROUTES, "intent": INTENTS, "status": STATUSES, "category": CATEGORIES, "code": _SAFE_EVENT_CODES}
    for case in data["cases"]:
        _require(type(case) is dict and case.keys() == {"id", "group", "description", "tags", "data_profile", "steps",
                                                      "modes", "mode_exclusions"}
                 and type(case["id"]) is str and re.fullmatch(r"[A-Z][0-9]{2}", case["id"]) is not None
                 and case["id"] not in seen and case["data_profile"] in DATA_PROFILES
                 and type(case["group"]) is str and 0 < len(case["group"]) <= 80
                 and type(case["description"]) is str and 0 < len(case["description"]) <= 300
                 and type(case["steps"]) is list and 1 <= len(case["steps"]) <= 10)
        seen.add(case["id"])
        _strings(case["modes"], maximum=3, allowed={"offline", "live-model", "live"})
        _require("offline" in case["modes"] and type(case["mode_exclusions"]) is dict
                 and case["mode_exclusions"].keys() == {"offline", "live-model", "live"} - set(case["modes"])
                 and all(type(reason) is str and 0 < len(reason) <= 500 for reason in case["mode_exclusions"].values()))
        _strings(case["tags"])
        _require(bool(case["tags"]) and ("multi_turn" in case["tags"]) == (len(case["steps"]) > 1)
                 and (case["data_profile"] == "normal" or "fault_injected" in case["tags"]))
        for step in case["steps"]:
            _require(type(step) is dict and step.keys() == {"question", "history", "fixture_script", "expectation"}
                     and type(step["question"]) is str and 0 < len(step["question"].strip()) <= 2000
                     and type(step["history"]) is bool)
            script = step["fixture_script"]
            _require(type(script) is dict and script.keys() <= SCRIPT_KEYS and "phases" in script)
            # Repeated answer phases are valid (a bounded repair), unlike duplicate identity mentions.
            _require(type(script["phases"]) is list and len(script["phases"]) <= 6
                     and all(type(item) is str and item in PHASES for item in script["phases"]))
            for key in ("mentions", "kinds", "indicators"):
                if key in script:
                    _strings(script[key], maximum=12)
            _require("date_expression" not in script or script["date_expression"] is None
                     or type(script["date_expression"]) is str and len(script["date_expression"]) <= 120)
            if "screening" in script:
                payload = script["screening"]
                _require(type(payload) is dict and payload.keys() == {"intent", "conditions", "sort_by", "sort_direction", "limit"}
                         and payload["intent"] in ("screen", "clarify") and type(payload["conditions"]) is list
                         and len(payload["conditions"]) <= 10 and type(payload["limit"]) is int and 1 <= payload["limit"] <= 20
                         and payload["sort_by"] in ("amount_cny", "pb", "pe_dynamic", "price")
                         and payload["sort_direction"] in ("asc", "desc"))
                for atom in payload["conditions"]:
                    _require(type(atom) is dict and atom.keys() == {"field", "operator", "value", "phrase"}
                             and atom["field"] in ("pe_dynamic", "pb", "price", "turnover_percent")
                             and atom["operator"] in ("lt", "lte", "gt", "gte", "eq")
                             and type(atom["value"]) in (int, float) and abs(atom["value"]) <= 10**12
                             and type(atom["phrase"]) is str and 0 < len(atom["phrase"]) <= 200)
            expectation = step["expectation"]
            _require(type(expectation) is dict and expectation.keys() == {"required", "acceptable", "quality", "offline_expected"})
            required = expectation["required"]
            _require(type(required) is dict and required.keys() <= REQUIRED_KEYS)
            for key in ("forbid_codes", "require_codes", "securities_if_answered", "forbid_categories"):
                if key in required:
                    _strings(required[key], allowed=_SAFE_EVENT_CODES if key.endswith("codes") else
                             CATEGORIES if key == "forbid_categories" else None)
            _require(not (set(required.get("forbid_codes", [])) & set(required.get("require_codes", []))))
            for key in ("no_collection", "no_model"):
                _require(key not in required or type(required[key]) is bool)
            quality = expectation["quality"]
            _require(type(quality) is dict and quality.keys() <= QUALITY_KEYS)
            if "answer_contains" in quality:
                _strings(quality["answer_contains"])
            _require("answer_origin_if_answered" not in quality or quality["answer_origin_if_answered"] in ("model", "application"))
            _require("answer_status_if_answered" not in quality or quality["answer_status_if_answered"] in ("accepted", "fallback"))
            if "screening_rows" in required:
                _require(type(required["screening_rows"]) is int and 0 <= required["screening_rows"] <= 20)
            if "coverage" in required:
                _require(type(required["coverage"]) is list and len(required["coverage"]) <= 15)
                for item in required["coverage"]:
                    _require(type(item) is dict and {"instrument_id", "kind", "status"} <= item.keys()
                             and item.keys() <= {"instrument_id", "kind", "status", "code"}
                             and re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", item["instrument_id"]) is not None
                             and item["kind"] in ("profile", "financial", "technical", "news", "announcements")
                             and item["status"] in ("available", "limited", "unavailable", "empty")
                             and ("code" not in item or item["code"] in _SAFE_EVENT_CODES))
            alternatives, exact = expectation["acceptable"], expectation["offline_expected"]
            _require(type(exact) is dict and exact.keys() == OUTCOME_KEYS
                     and all(type(exact[key]) is str and exact[key] in choices[key] for key in OUTCOME_KEYS)
                     and type(alternatives) is list and 1 <= len(alternatives) <= 5)
            for alternative in alternatives:
                _require(type(alternative) is dict and alternative.keys() == OUTCOME_KEYS | {"reason"}
                         and type(alternative["reason"]) is str and 0 < len(alternative["reason"]) <= 500)
                for key in OUTCOME_KEYS:
                    _strings(alternative[key], maximum=8, allowed=choices[key])
                    _require(bool(alternative[key]))
            _require(any(_matches(exact, value) for value in alternatives)
                     and exact["code"] not in required.get("forbid_codes", [])
                     and exact["category"] not in required.get("forbid_categories", []))
    effective = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return data, sha256(effective if path.resolve() == CORPUS.resolve() else raw).hexdigest()


def b_delta_descriptor():
    raw = B_DELTA.read_bytes().replace(b"\r\n", b"\n")
    delta = json.loads(raw.decode("utf-8"))
    return {"parent_sha256": delta["parent_sha256_lf"], "delta_version": delta["version"],
            "delta_sha256": sha256(raw).hexdigest(), "rule_baseline_version": delta["rule_baseline_version"]}


def _apply_b_delta(data, raw):
    delta = json.loads(B_DELTA.read_text(encoding="utf-8"))
    _require(type(delta) is dict and delta.keys() == {"version", "rule_baseline_version", "parent_sha256_lf", "changes"}
             and delta["version"] == "research-api-b-delta-v1"
             and delta["rule_baseline_version"] == "contextual-routing-b-v1"
             and delta["parent_sha256_lf"] == sha256(raw).hexdigest()
             and type(delta["changes"]) is list and len(delta["changes"]) == 2)
    case = next(case for case in data["cases"] if case["id"] == "C05")
    for index, change in zip((2, 3), delta["changes"]):
        _require(type(change) is dict and change.keys() == {"case_id", "step", "old", "new", "reason"}
                 and change["case_id"] == "C05" and change["step"] == index
                 and type(change["reason"]) is str and bool(change["reason"])
                 and case["steps"][index - 1] == change["old"])
        case["steps"][index - 1] = deepcopy(change["new"])
    return data


def _codes(value):
    if type(value) is dict:
        own = [value["code"]] if type(value.get("code")) is str else []
        return own + [code for item in value.values() for code in _codes(item)]
    if type(value) is list:
        return [code for item in value for code in _codes(item)]
    return []


def _observe(result, trace, *, fixed=True):
    company, parameters = result.get("company_research") or {}, result.get("parameters") or {}
    conversation, knowledge = result.get("conversation") or {}, result.get("knowledge") or {}
    securities = [item["instrument_id"] for item in company.get("securities", [])]
    if not securities and "instrument_id" in parameters:
        securities = [parameters["instrument_id"]]
    raw_status = result.get("status", "failed")
    category = {"completed": "completed", "limited": "completed", "needs_clarification": "clarification",
                "out_of_scope": "rejected", "invalid_request": "rejected"}.get(raw_status, "error")
    if result.get("code") in REFUSAL_CODES:
        category = "rejected"
    return {"route": trace["route"] if trace else "unrouted", "intent": trace["intent"] if trace else None,
            "status": raw_status, "category": category, "code": result.get("code", "other"),
            "stage": result.get("stage"), "answer": result.get("answer"), "securities": securities,
            "coverage": company.get("coverage", []), "answer_status": company.get("status", knowledge.get("status")),
            "answer_origin": company.get("origin", conversation.get("origin", knowledge.get("origin"))),
            "validation_scope": result.get("validation_scope"),
            "codes": sorted(set(_codes(result) + [event["code"] for event in trace["event_records"]] if trace else _codes(result))),
            "screening_rows": len(result["screening"]["rows"]) if result.get("screening") else None,
            "company_research": (_fixed_authenticity(company) if fixed else company) if company else None,
            "report": _fixed_authenticity(result.get("report")) if fixed else result.get("report"), "conversation": conversation or None,
            "knowledge": knowledge or None}


def _fixed_authenticity(value):
    """Source names still identify parsers; their injected content is always artificial."""
    if type(value) is dict:
        return {key: "fixed_synthetic" if key == "data_authenticity" else _fixed_authenticity(item) for key, item in value.items()}
    if type(value) is list:
        return [_fixed_authenticity(item) for item in value]
    return value


def grade_step(step, observed, *, calls, collections):
    """Keep safety, acceptable outcomes and quality distinct; offline requires all."""
    expectation, checks, quality_checks = step["expectation"], {}, {}
    required, codes = expectation["required"], set(observed["codes"])
    quality = expectation["quality"]
    checks["forbidden_codes_absent"] = not codes.intersection(required.get("forbid_codes", []))
    checks["required_codes_present"] = set(required.get("require_codes", [])) <= codes
    checks["required_category"] = observed["category"] not in required.get("forbid_categories", [])
    if required.get("no_model"):
        checks["no_model"] = calls == 0
    if required.get("no_collection"):
        checks["no_collection"] = None if collections is None else collections == 0
    if "securities_if_answered" in required and observed["category"] == "completed":
        checks["securities"] = observed["securities"] == required["securities_if_answered"]
    if observed["category"] == "completed":
        for key in ("answer_origin", "answer_status"):
            if key + "_if_answered" in quality:
                quality_checks[key] = observed.get(key) == quality[key + "_if_answered"]
    for index, item in enumerate(required.get("coverage", [])):
        checks["coverage_" + str(index)] = any(all(candidate.get(key) == value for key, value in item.items() if key != "instrument_id")
            for company in observed["coverage"] if company["instrument_id"] == item["instrument_id"]
            for candidate in company["sections"])
    if "answer_contains" in quality:
        quality_checks["display"] = all(text in (observed["answer"] or "") for text in quality["answer_contains"])
    if "screening_rows" in required:
        checks["screening_rows"] = observed["screening_rows"] == required["screening_rows"]
    hard = [name for name, passed in checks.items() if passed is False]
    acceptable = any(_matches(observed, value) for value in expectation["acceptable"])
    exact = all(observed.get(key) == value for key, value in expectation["offline_expected"].items())
    return {"required_checks": checks, "hard_requirement_violations": hard,
            "unverified_requirements": [name for name, passed in checks.items() if passed is None],
            "quality_checks": quality_checks,
            "quality_requirement_failures": [name for name, passed in quality_checks.items() if not passed],
            "acceptable_outcome": acceptable, "outside_acceptable": not acceptable,
            "offline_expected_matched": exact}


def _read_trace(directory):
    reader = TraceReader()
    rows = list(reader.read(reader.directory(directory)))
    return rows, reader


def _revision():
    try:
        process = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, timeout=5, check=False)
        value = process.stdout.decode("ascii").strip()
        if process.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return None


def _dirty():
    try:
        process = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT,
                                 capture_output=True, timeout=5, check=False)
        return bool(process.stdout) if process.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _unrun(case, repeat, index, step, code):
    return {"case_id": case["id"], "repeat": repeat, "step": index, "tags": case["tags"],
            "question": step["question"], "expectation": deepcopy(step["expectation"]),
            "status": "not_run", "code": code, "observed": None, "trace": None,
            "hard_requirement_violations": [], "quality_checks": None, "quality_requirement_failures": [],
            "outside_acceptable": None, "offline_expected_matched": None}


def _run(selected, repeat, workers, meter, model, trace_root, plan):
    from trading_companion.workflow_evaluation_runtime import live_sources, collection_attempts
    mode = plan["mode"]
    fixed = mode != "live"
    results, case_reports, all_rows = [], [], []
    diagnostics = {}
    for iteration in range(1, repeat + 1):
        for case in selected:
            directory = trace_root / (case["id"] + "-" + str(iteration))
            directory.mkdir(parents=True)
            seen, case_results = set(), []
            sources = None
            try:
                context = FixedSources(case["data_profile"], workers) if fixed else live_sources(plan)
                with context as sources:
                    service = ResearchApiService(model_factory=lambda: meter, directory_loader=lambda now: sources.directory,
                        provider=plan["provider"], data_mode="tencent" if fixed else plan["data_source"],
                        reference_date=REFERENCE_DATE if fixed else None,
                        product_data_service=sources.company, screening_source=sources.quotes,
                        wall_clock=(lambda: NOW) if fixed else None, trace_sink=JsonlTraceSink(directory),
                        trace_mode="development" if fixed else "normal")
                    try:
                        credentials = service.create_session()
                        for index, step in enumerate(case["steps"], 1):
                            if meter.forwarded >= meter.limit:
                                case_results.append(_unrun(case, iteration, index, step, "evaluation_budget_exhausted"))
                                continue
                            if mode == "offline":
                                model.start_step(step["fixture_script"])
                            before_calls, before_forwarded = meter.attempts, meter.forwarded
                            before_reads = sources.http.calls + sources.daily_reads if fixed else 0
                            framework = []
                            try:
                                result = service.message(credentials["session_id"], credentials["access_token"], uuid4().hex,
                                                         step["question"], history=step["history"])["result"]
                            except ApiProblem as error:
                                result = {"status": "failed", "code": error.code if error.code in _SAFE_EVENT_CODES else "other"}
                            except Exception:
                                framework.append("evaluation_step_error")
                                result = {"status": "failed", "code": "other"}
                            rows, reader = _read_trace(directory)
                            fresh = [row for row in rows if row["run_id"] not in seen]
                            trace = fresh[0] if len(fresh) == 1 and not any(reader.diagnostics.values()) else None
                            if trace is None:
                                framework.append("trace_incomplete")
                            else:
                                seen.add(trace["run_id"])
                                all_rows.append(trace)
                            # Preserve diagnostics once, rather than counting earlier lines on every step.
                            diagnostics[str(directory.name)] = (dict(reader.diagnostics), reader.valid_turns,
                                                                 reader.bytes_read, reader.lines_read)
                            observed = _observe(result, trace, fixed=fixed)
                            collections = sources.http.calls + sources.daily_reads - before_reads if fixed else collection_attempts(trace)
                            grade = grade_step(step, observed, calls=meter.attempts - before_calls,
                                               collections=collections)
                            if mode != "offline":
                                grade["offline_expected_matched"] = None
                            if mode == "offline" and (model.failed or model.remaining):
                                framework.append("fixture_script_mismatch")
                            if fixed and sources.fault.failed:
                                framework.append("fixture_deadline_error")
                            serialized_trace = None if trace is None else {**trace, "started_at": trace["started_at"].isoformat()}
                            case_results.append({"case_id": case["id"], "repeat": iteration, "step": index,
                                "tags": case["tags"], "question": step["question"], "expectation": deepcopy(step["expectation"]),
                                "data_authenticity": plan["data_authenticity"], "observed": observed, **grade,
                                "collection_attempts": collections,
                                "accounting": {"attempts": meter.attempts - before_calls,
                                               "forwarded": meter.forwarded - before_forwarded,
                                               "ledger": deepcopy(meter.ledger[before_forwarded:])},
                                "status": "passed" if not framework and not grade["hard_requirement_violations"]
                                          and not grade["unverified_requirements"]
                                          and not grade["quality_requirement_failures"]
                                          and grade["acceptable_outcome"] and (mode != "offline" or grade["offline_expected_matched"]) else "failed",
                                "framework_errors": framework, "trace": serialized_trace})
                    finally:
                        service.close()
                    frozen = {path.name: path.read_bytes() for path in directory.glob("*.jsonl")}
                # Late HTTP work has exited by now. A finished trace must remain unchanged.
                if frozen != {path.name: path.read_bytes() for path in directory.glob("*.jsonl")} or fixed and sources.fault.failed:
                    for item in case_results:
                        if item["status"] != "not_run":
                            item["status"] = "failed"
                            item["framework_errors"].append("late_fixture_or_trace_error")
            except Exception:
                for index in range(len(case_results) + 1, len(case["steps"]) + 1):
                    case_results.append(_unrun(case, iteration, index, case["steps"][index - 1], "evaluation_case_initialization_error"))
                for item in case_results:
                    if item["status"] != "not_run":
                        item["status"] = "failed"
                        item["framework_errors"].append("evaluation_case_cleanup_error")
            results.extend(case_results)
            case_reports.append({"case_id": case["id"], "repeat": iteration, "description": case["description"],
                "steps": len(case["steps"]), "tags": case["tags"], "data_profile": case["data_profile"],
                "passed": all(item["status"] == "passed" for item in case_results),
                "trace_directory": str(directory), "transport": sources.http.report() if sources and fixed else None,
                "daily_reads": sources.daily_reads if sources and fixed else None})
    summary, reader = TraceSummary(), TraceReader()
    for values, valid, read_bytes, lines in diagnostics.values():
        reader.valid_turns += valid
        reader.bytes_read += read_bytes
        reader.lines_read += lines
        for key, value in values.items():
            reader.diagnostics[key] += value
    for row in all_rows:
        summary.add(row)
    return results, case_reports, summary.report(reader)


def evaluate_workflows(*, mode="offline", selected_cases=None, repeat=1, collection_workers=3,
                       model_call_limit=None, trace_dir=None, provider=None, model=None, prices=None,
                       directory=None, data_source="tencent", assumed_input_tokens=None, assumed_output_tokens=None):
    from trading_companion.workflow_evaluation_runtime import make_plan, PlanError, invalid_plan, build_real_model, model_network_boundary
    from trading_companion.evaluation_pricing import actual_cost
    from trading_companion.workflow_evaluation_metrics import evaluation_metrics
    from trading_companion.workflow_evaluation_reporting import manual_review
    base = {"version": VERSION, "steps": [], "cases": [], "language_quality": "requires_manual_review"}
    try:
        plan, selected, price_data = make_plan(mode=mode, selected_cases=selected_cases, repeat=repeat,
            collection_workers=collection_workers, model_call_limit=model_call_limit, provider=provider, model=model,
            prices=prices, directory=directory, data_source=data_source, trace_dir=trace_dir,
            assumed_input_tokens=assumed_input_tokens, assumed_output_tokens=assumed_output_tokens)
    except PlanError as error:
        return {**base, **error.result()}
    except (ValueError, TypeError, OSError, KeyError, UnicodeError, RecursionError):
        return {**base, **invalid_plan()}
    if mode != "offline":
        print(json.dumps({"evaluation_plan": plan}, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
    total, allowance = plan["planned_steps"], plan["model_call_limit"]
    output = {**base, **{key: plan[key] for key in ("mode", "corpus", "provider", "model", "data_authenticity",
              "selected_cases", "repeat", "collection_workers", "planned_steps", "model_call_limit",
              "excluded_cases", "not_applicable_to_mode", "not_applicable_steps")}, "plan": plan,
              "evaluation_scope": "fixed_script_contracts_not_model_quality" if mode == "offline" else "model_workflow_evaluation_not_investment_performance",
              "real_model_requested": mode != "offline", "real_data_requested": mode == "live",
              "fixtures": fixture_manifest() if mode != "live" else None,
              "code_revision": _revision(), "working_tree_dirty": _dirty(),
              "evaluation_implementation_sha256": sha256(b"".join(
                  (Path(__file__).parent / name).read_bytes().replace(b"\r\n", b"\n") for name in
                  ("workflow_evaluation.py", "workflow_evaluation_runtime.py", "workflow_evaluation_metrics.py",
                   "workflow_evaluation_reporting.py", "evaluation_common.py", "evaluation_pricing.py"))).hexdigest(),
              "evaluated_at": datetime.now(timezone.utc).isoformat(),
              "traces_retained": trace_dir is not None, "trace_directory": None}
    credential_attempts, network_attempts, error_code, meter = [], [], None, None

    def denied(*args, **kwargs):
        credential_attempts.append(True)
        raise RuntimeError("Offline evaluation cannot load model credentials")
    try:
        from scripts.verify import clean_environment, offline_network
        with ExitStack() as stack:
            if mode == "offline":
                stack.enter_context(clean_environment())
                stack.enter_context(offline_network())
                stack.enter_context(patch("trading_companion.local_env.load_model_environment", denied))
                stack.enter_context(patch("trading_companion.local_env._read_local_values", denied))
            elif mode == "live-model":
                network_attempts = stack.enter_context(model_network_boundary(plan["provider"]))
            delegate = ScriptedWorkflowModel() if mode == "offline" else build_real_model(plan)
            meter = EvaluationModelMeter(delegate, allowance)
            if trace_dir is None:
                trace_root = Path(stack.enter_context(TemporaryDirectory(prefix="workflow-evaluation-")))
            else:
                trace_root = Path(trace_dir).absolute() / uuid4().hex
                trace_root.mkdir(parents=True)
                output["trace_directory"] = str(trace_root)
            steps, cases, trace_summary = _run(selected, repeat, collection_workers, meter, delegate, trace_root, plan)
            if trace_dir is None:
                for case in cases:
                    case["trace_directory"] = None
            output.update(steps=steps, cases=cases, trace_summary=trace_summary)
    except Exception:
        output.update(code="evaluation_offline_boundary_or_io_error" if mode == "offline"
                      else "evaluation_runtime_initialization_or_boundary_error")
        error_code = output["code"]
    if not output["steps"]:
        output["steps"] = [_unrun(case, iteration, index, step, error_code or "evaluation_initialization_error")
                           for iteration in range(1, repeat + 1) for case in selected for index, step in enumerate(case["steps"], 1)]
    steps = output["steps"]
    counts = {name: sum(item["status"] == name for item in steps) for name in ("passed", "failed", "not_run")}
    success = counts["passed"] == total and not credential_attempts and not network_attempts and error_code is None
    output.update(status="passed" if success else "failed",
        code=error_code or ("evaluation_offline_credential_violation" if credential_attempts else
             "evaluation_source_network_violation" if network_attempts else
             "scenario_expectations_met" if success else "scenario_expectations_not_met"),
        credential_reads_attempted=len(credential_attempts), source_network_attempts_blocked=len(network_attempts),
        summary={"planned": total, "executed": total - counts["not_run"], **counts,
                 "hard_requirement_violations": sum(bool(item["hard_requirement_violations"]) for item in steps),
                 "outside_acceptable": sum(item["outside_acceptable"] is True for item in steps),
                 "quality_requirement_failures": sum(bool(item["quality_requirement_failures"]) for item in steps),
                 "trace_missing": sum(item["status"] != "not_run" and item["trace"] is None for item in steps)},
        accounting={"forwarded": meter.forwarded if meter else 0, "attempts": meter.attempts if meter else 0,
                    "blocked": meter.blocked if meter else 0, "token_usage": reported_usage(meter.usage if meter else [])},
        cost=actual_cost(price_data, plan["provider"], plan["model"], meter.ledger if meter else []))
    output["metrics"] = evaluation_metrics(output)
    output["manual_review"] = manual_review(output)
    return output
