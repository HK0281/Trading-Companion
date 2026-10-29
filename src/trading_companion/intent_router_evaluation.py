"""Standalone classification experiments: preview, reviewed data, dev lock, then holdout."""

from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import random
import sys
from time import monotonic

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.evaluation_pricing import DEFAULT_PRICES, MODEL_ID, load_prices, estimate_cost, price_for
from trading_companion.local_env import load_evaluation_key_environment
from trading_companion.news_triage_evaluation import network_boundary, TIMEOUT_SCOPE
from trading_companion.typesafe_jev import JevConfig, ClassifierError, require
from trading_companion.intent_router_task import load_corpus, input_state, digest, read_json, definition_digest, frozen_sources, LABELS
from trading_companion.intent_router_baseline import rule_classify, protected_reason
from trading_companion.intent_router_models import JevIntent, DeepSeekIntent, fixture_post, TEMPLATE
from trading_companion.intent_router_metrics import metrics, choose_threshold, cutover, scores, ratio, paired
from trading_companion.workflow_evaluation_runtime import PlanError, check_output_root

SOURCE_FILES = ("intent_router_task.py", "intent_router_models.py", "intent_router_baseline.py", "intent_router_evaluation.py",
                "intent_router_metrics.py", "intent_router_reporting.py", "classification_metrics.py", "typesafe_jev.py",
                "evaluation_pricing.py", "news_triage_evaluation.py", "news_triage_models.py", "local_env.py")


def need(condition, code, detail):
    if not condition:
        raise PlanError(code, detail)


def implementation_digest():
    return digest({f: sha256((Path(__file__).parent/f).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for f in SOURCE_FILES})


def failure(error):
    return error.result() if isinstance(error, PlanError) else {"status": "invalid_request", "code": "intent_input_invalid",
                                                                "detail": "题库、价格、锁文件或评估输入无效。"}


def compatible(reference, plan):
    other = reference["plan"]
    return (reference["version"] == "intent-router-evaluation-v1" and reference["status"] == "completed"
        and other["suite"] == "paraphrase" and other["repeat"] == 1
        and all(other[k] == plan[k] for k in ("mode", "models", "classifiers", "timeout_seconds", "definition_sha256", "implementation_sha256", "prices_sha256"))
        and other.get('actual_rule_source_sha256_lf') == plan['actual_rule_source_sha256_lf']
        and other["corpus"]["corpus_sha256"] == plan["corpus"]["corpus_sha256"]
        and other["corpus"]["split_sha256"] == plan["corpus"]["split_sha256"])


def load_lock(path, plan):
    lock = read_json(path)
    need(lock["version"] == "intent-router-threshold-lock-v1" and lock["selection_sha256"] == digest(lock["selection"]),
         "intent_lock_invalid", "阈值锁格式或摘要不符。")
    selection = lock["selection"]
    source = read_json(selection["source_report"], 10_000_000)
    need(lock["mode"] == plan["mode"] and digest(source) == selection["source_report_sha256"]
         and compatible(source,plan) and source["plan"]["split"] == "dev",
         "intent_lock_incompatible", "阈值锁必须来自同题库、模型、提示和实现的完整开发集报告。")
    validate_dev_report(source)
    need(selection["threshold"] == choose_threshold([r for r in source["rows"] if r["case_kind"] == "semantic"])
         and selection["models"] == source["plan"]["models"]
         and selection["actual_models"] == source["metrics"]["providers"]["jev"]["actual_models"],
         "intent_lock_invalid", "锁内阈值或模型与开发报告不符。")
    if plan["mode"] == "live":
        need(lock.get("review_status") == "approved" and lock.get("reviewer") == "claude",
             "intent_lock_not_reviewed", "真实留出运行前须由 Claude 复核并锁定阈值。")
    return lock


def validate_reference_report(report, split):
    """Require every unique frozen question, rather than trusting a completed flag."""
    rows, manifest = load_corpus()
    expected = {r["id"]: r for r in rows if r["split"] == split}
    actual = [r for r in report["rows"] if r["case_kind"] == "semantic"]
    need(report["version"] == "intent-router-evaluation-v1" and report["status"] == "completed"
         and report["plan"]["suite"] == "paraphrase" and report["plan"]["split"] == split
         and report["plan"]["repeat"] == 1
         and report["plan"]["corpus"]["corpus_sha256"] == manifest["corpus_sha256"]
         and len(actual) == len(report["rows"]) == len(expected) and {r["id"] for r in actual} == expected.keys(),
         "intent_reference_invalid", "参考报告必须完整、每题一次，不能缺题、重复或混入其他划分。")
    for row in actual:
        need(all(row[k] == v for k,v in expected[row["id"]].items()) and row["repeat"] == 1,
             "intent_reference_invalid", "参考结果与冻结题库不符。")
        for provider in report["plan"]["classifiers"]:
            result = row["results"][provider]
            confidence = result.get("confidence")
            need(result["status"] == "ok" and result.get("attempted") is True and result.get("prediction") in LABELS
                 and (provider != "jev" or type(confidence) in (int,float) and isfinite(confidence) and 0 <= confidence <= 1),
                 "intent_reference_invalid", "参考报告存在失败、未尝试或非法分类结果。")


def validate_dev_report(report):
    need(report["plan"]["split"] == "dev" and "jev" in report["plan"]["classifiers"],
         "intent_dev_report_invalid", "只可从完整144条、单次、含Jev的开发集实验导出锁。")
    validate_reference_report(report, "dev")


def export_lock(report_path, output):
    report = read_json(report_path, 10_000_000)
    validate_dev_report(report)
    selection = {"source_report": str(Path(report_path).resolve()), "source_report_sha256": digest(report),
        "threshold": choose_threshold([r for r in report["rows"] if r["case_kind"] == "semantic"]),
        "models": report["plan"]["models"], "actual_models": report["metrics"]["providers"]["jev"]["actual_models"],
        "selection_rule": "accuracy>=.95;n>=40;coverage>=.4;Wilson_lower>=.85;max_coverage_then_higher_threshold"}
    lock = {"version": "intent-router-threshold-lock-v1", "mode": report["plan"]["mode"], "selection": selection,
            "selection_sha256": digest(selection), "review_status": "draft", "reviewer": None}
    target = Path(output)
    check_output_root(target.parent)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(lock,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    return lock


def make_plan(*, mode="offline", classifiers=None, split="dev", suite="paraphrase", repeat=1, call_limit=None,
              prices=None, deepseek_model=None, timeout_seconds=5, assumed_input_tokens=None,
              assumed_output_tokens=None, threshold_lock=None, reference_report=None, report_dir=None):
    need(mode in ("offline","live") and split in ("dev","holdout") and suite in ("paraphrase","context-ablation","legacy"),
         "intent_mode_invalid", "请选 offline/live、dev/holdout 及支持的套件。")
    need(suite != "legacy", "intent_legacy_deferred", "旧59/105输入一致率套件按#157后置；本版仅交付主线和消融，不声称该项已实现。")
    need(type(repeat) is int and 1 <= repeat <= 3 and type(timeout_seconds) in (int,float)
         and isfinite(timeout_seconds) and 1 <= timeout_seconds <= 15, "intent_budget_invalid", "重复次数1..3，超时1..15秒。")
    need(suite != "context-ablation" or repeat == 1, "intent_ablation_repeat_invalid", "成对消融每题只运行一次，避免重复扩大样本。")
    chosen = ["jev","deepseek"] if classifiers is None and mode == "offline" else classifiers
    need(type(chosen) in (list,tuple) and 1 <= len(chosen) <= 2 and len(set(chosen)) == len(chosen)
         and all(type(c) is str and c in ("jev","deepseek") for c in chosen), "intent_classifiers_invalid", "live须显式选择jev/deepseek且不重复。")
    chosen = [c for c in ("jev","deepseek") if c in chosen]
    need(mode != "live" or call_limit is not None and prices is not None, "intent_live_options_required", "live须显式给调用上限和价格文件。")
    need((mode == "live" and "deepseek" in chosen and type(deepseek_model) is str and MODEL_ID.fullmatch(deepseek_model))
         or deepseek_model is None and (mode == "offline" or "deepseek" not in chosen),
         "intent_deepseek_model_invalid", "live选DeepSeek须指定模型；其他情况不得给该参数。")
    for value in (assumed_input_tokens,assumed_output_tokens):
        need(value is None or type(value) is int and 0 <= value <= 1_000_000, "intent_tokens_invalid", "token假设须为0..1000000整数或省略。")
    check_output_root(report_dir)
    corpus, manifest = load_corpus()
    selected = [r for r in corpus if r["split"] == split and (suite != "context-ablation" or r["ablation_pair"] is not None)]
    boundary = [r for r in corpus if r["case_kind"] == "protected"]
    # Reject mislabeled admission records before constructing either model.
    need(all(protected_reason(r) is None for r in selected) and all(protected_reason(r) is not None for r in boundary),
         "intent_admission_invalid", "语义题含保护请求或保护题缺少归属。")
    total = len(selected)*repeat*len(chosen)
    limit = total if call_limit is None else call_limit
    need(type(limit) is int and 1 <= limit <= total, "intent_call_limit_invalid", f"call-limit须为1..{total}。")
    price_data, price_sha = load_prices(DEFAULT_PRICES if prices is None else prices)
    models = {c: "jev-latest" if c == "jev" else deepseek_model or "offline-deepseek-v1" for c in chosen}
    suites = {}
    for name,count in (("dev",144),("holdout",96),("ablation-dev",24),("ablation-holdout",24),("legacy-upper-bound",164)):
        suites[name] = {"questions": count, "requests_upper_bound": count*len(chosen)*repeat,
            "implemented": name != "legacy-upper-bound", "cost_by_provider": {
                c: estimate_cost(price_data,"typesafe" if c=="jev" else c,models[c],count*repeat,assumed_input_tokens,assumed_output_tokens) for c in chosen}}
    plan = {"version": "intent-router-plan-v1", "status": "planned", "mode":mode, "suite":suite, "split":split,
        "classifiers":chosen, "models":models, "repeat":repeat, "seed":42, "corpus":manifest,
        "definition_sha256":definition_digest(), "implementation_sha256":implementation_digest(), "deepseek_template":TEMPLATE,
        "actual_rule_source_sha256_lf":frozen_sources(),
        "prices_sha256":price_sha, "price_entries":{c:price_for(price_data,"typesafe" if c=="jev" else c,models[c]) for c in chosen},
        "planned_questions":len(selected)*repeat, "unique_questions":len(selected), "worst_case_requests":total,"call_limit":limit,
        "suite_preview":suites, "all_suites_requests_upper_bound":sum(v["requests_upper_bound"] for v in suites.values()),
        "selected_cost_preview":{c:estimate_cost(price_data,"typesafe" if c=="jev" else c,models[c],min(limit,len(selected)*repeat),assumed_input_tokens,assumed_output_tokens) for c in chosen},
        "timeout_seconds":timeout_seconds,"timeout_scope":TIMEOUT_SCOPE,"threshold_lock":None,"reference_sha256":None,
        "input_view":"no_latest" if suite == "context-ablation" else "full",
        "data_scope":"仅人工问题与固定布尔/类别状态；不发历史、证券资料、规则答案或标注。",
        "scoring_scope":"语义路由标签不是最终执行结果；规则执行器能否自行澄清未判定。",
        "legacy_status":"deferred_per_157_not_implemented"}
    if threshold_lock is not None:
        need(split == "holdout", "intent_lock_unexpected", "阈值锁只用于留出集。")
        plan["threshold_lock"] = load_lock(threshold_lock,plan)
    if reference_report is not None:
        reference = read_json(reference_report, 10_000_000)
        need(suite == "context-ablation" and compatible(reference,plan) and reference["plan"]["split"] == split,
             "intent_reference_incompatible", "消融须引用同条件同划分的完整主实验。")
        validate_reference_report(reference, split)
        originals = {r["id"]:r for r in reference["rows"] if r["case_kind"] == "semantic"}
        need(set(originals) >= {r["id"] for r in selected} and all(all(originals[r["id"]][k]==v for k,v in r.items()) for r in selected),
             "intent_reference_incompatible", "消融参考缺题或与题库不符。")
        plan["reference_sha256"] = digest(reference)
    else:
        reference = None
    return plan, selected, boundary, price_data, reference


def build_plan(**kwargs):
    try:
        return make_plan(**kwargs)[0]
    except (OSError, ValueError, TypeError, KeyError, ClassifierError, RecursionError) as error:
        return failure(error)


def build_models(plan):
    result = {}
    for c in plan["classifiers"]:
        fake = plan["mode"] == "offline"
        key = "offline-not-a-key" if fake else load_evaluation_key_environment("TYPESAFE_API_KEY" if c=="jev" else "DEEPSEEK_API_KEY").get("TYPESAFE_API_KEY" if c=="jev" else "DEEPSEEK_API_KEY", "")
        result[c] = (JevIntent(JevConfig(key,timeout_seconds=plan["timeout_seconds"]),post=fixture_post(c) if fake else None) if c=="jev" else
                     DeepSeekIntent(DeepSeekConfig(plan["models"][c],key,timeout_seconds=plan["timeout_seconds"]),post=fixture_post(c) if fake else None))
    return result


def ablation_summary(rows, reference, classifiers):
    originals = {r["id"]:r for r in reference["rows"] if r["case_kind"] == "semantic"}
    full = [originals[r["id"]] for r in rows]
    result = {}
    for c in classifiers:
        pairs = {}
        for row in rows:
            pairs.setdefault(row["ablation_pair"],[]).append(row)
        both = sum(len(group)==2 and all(r["results"][c].get("prediction")==r["gold"] and r["results"][c]["status"]=="ok" for r in group) for group in pairs.values())
        models_full = sorted({r["results"][c].get("model") for r in full if r["results"][c].get("model")})
        models_ablated = sorted({r["results"][c].get("model") for r in rows if r["results"][c].get("model")})
        same_models = bool(models_full) and models_full == models_ablated
        strata = {}
        for kind in ("label_changes", "label_invariant"):
            groups = [g for g in pairs.values() if (len({r["gold"] for r in g}) > 1) == (kind == "label_changes")]
            subset = [r for g in groups for r in g]
            original_subset = [originals[r["id"]] for r in subset]
            strata[kind] = {"pairs":len(groups), "questions":len(subset), "full":scores(original_subset,c),
                "without_latest":scores(subset,c),
                "accuracy_change":scores(subset,c)["accuracy"]["value"]-scores(original_subset,c)["accuracy"]["value"] if same_models and subset else None,
                "both_ends_correct_full":ratio(sum(all(originals[r["id"]]["results"][c]["status"]=="ok"
                    and originals[r["id"]]["results"][c].get("prediction")==r["gold"] for r in g) for g in groups),len(groups)),
                "both_ends_correct_without_latest":ratio(sum(all(r["results"][c]["status"]=="ok"
                    and r["results"][c].get("prediction")==r["gold"] for r in g) for g in groups),len(groups))}
        result[c] = {"full":scores(full,c), "without_latest":scores(rows,c), "both_ends_correct":ratio(both,len(pairs)),
            "pair_strata":strata,
            "model_identity_matches_reference":same_models, "actual_models_full":models_full,"actual_models_without_latest":models_ablated,
            "accuracy_change": scores(rows,c)["accuracy"]["value"]-scores(full,c)["accuracy"]["value"] if same_models else None,
            "ask_rate_full":ratio(sum(r["results"][c].get("prediction")=="clarify" and r["results"][c]["status"]=="ok" for r in full),len(full)),
            "ask_rate_without_latest":ratio(sum(r["results"][c].get("prediction")=="clarify" and r["results"][c]["status"]=="ok" for r in rows),len(rows)),
            "scope":"preselected_pairs_information_ablation_not_a_new_holdout_policy_search"}
    return result


def evaluate(**kwargs):
    try:
        plan, selected, boundary, prices, reference = make_plan(**kwargs)
        if plan["mode"] == "live":
            print(json.dumps(plan,ensure_ascii=False,allow_nan=False),file=sys.stderr,flush=True)
            need(plan["corpus"]["annotation_status"] == "reviewed", "intent_annotations_not_reviewed", "题库草稿禁止真实运行，须Claude逐条复核。")
            need(plan["repeat"] == 1, "intent_live_repeat_invalid", "正式真实实验每题一次，重复实验另立版本。")
            need(plan["split"] != "holdout" or plan["threshold_lock"] is not None, "intent_lock_required", "真实留出集须先锁定已复核的开发阈值。")
        need(plan["suite"] != "context-ablation" or reference is not None, "intent_reference_required", "消融运行须先保存完整主实验并提供reference-report。")
    except (OSError, ValueError, TypeError, KeyError, ClassifierError, RecursionError) as error:
        return failure(error)
    rows, schedule, framework, denied = [], [], None, []
    for iteration in range(1,plan["repeat"]+1):
        for item in selected:
            row = {**item,"repeat":iteration,"results":{}}
            start=monotonic()
            row["results"]["rules"] = {**rule_classify(item),"elapsed_ms":(monotonic()-start)*1000}
            for c in plan["classifiers"]:
                row["results"][c] = {"status":"not_run","code":"call_limit","attempted":False,"prediction":None,
                    "confidence":None,"model":None,"usage":None,"elapsed_ms":None}
                schedule.append((len(rows),c))
            rows.append(row)
    random.Random(42).shuffle(schedule)
    attempts=0
    try:
        with ExitStack() as stack:
            if plan["mode"] == "offline":
                from scripts.verify import clean_environment, offline_network
                stack.enter_context(clean_environment()); stack.enter_context(offline_network())
            else:
                denied=stack.enter_context(network_boundary(plan["classifiers"]))
            models=build_models(plan)
            for index,c in schedule:
                if attempts >= plan["call_limit"]:
                    continue
                attempts+=1
                result=rows[index]["results"][c]
                result.update(attempted=True,status="failed",code="classifier_internal_error")
                start=monotonic()
                try:
                    result.update(models[c].classify(input_state(rows[index],ablated=plan["suite"]=="context-ablation")),status="ok",code="ok")
                except ClassifierError as error:
                    result.update(error.metadata,code=error.code)
                except Exception:
                    framework="classifier_internal_error"
                result["elapsed_ms"]=(monotonic()-start)*1000
    except Exception:
        framework="evaluation_boundary_or_initialization_error"
    partial=bool(framework or denied or any(r["results"][c]["status"]!="ok" for r in rows for c in plan["classifiers"]))
    threshold=plan["threshold_lock"]["selection"]["threshold"] if plan["threshold_lock"] else None
    if plan["split"] == "dev" and "jev" in plan["classifiers"]:
        threshold=choose_threshold(rows)
    report={"version":"intent-router-evaluation-v1","status":"partial" if partial else "completed",
        "code":framework or ("partial_results" if partial else "completed"),"plan":plan,"synthetic":True,
        "scope":"simulated_protocols_not_model_performance" if plan["mode"]=="offline" else "synthetic_corpus_not_production_quality",
        "evaluated_at":datetime.now(timezone.utc).isoformat(),"accounting":{"forwarded":attempts,"limit":plan["call_limit"],"network_violations":len(denied)},
        "protected":{"n":len(boundary),"model_calls":0,"items":[{"id":r["id"],"reason":protected_reason(r)} for r in boundary]},
        "rows":rows,"execution_order":[{"id":rows[i]["id"],"classifier":c,"repeat":rows[i]["repeat"]} for i,c in schedule]}
    report["metrics"]=metrics(rows,plan["classifiers"],plan["models"],prices,simulated=plan["mode"]=="offline",threshold=threshold,split=plan["split"])
    report["ablation"]=ablation_summary(rows,reference,plan["classifiers"]) if reference else None
    report["disagreements"]=[{"id":r["id"],"question":r["question"],"context":r["context"],"gold":r["gold"],
        "predictions":{c:r["results"][c].get("prediction") for c in ("rules",*plan["classifiers"])}}
        for r in rows if len({r["results"][c].get("prediction") for c in ("rules",*plan["classifiers"])})>1]
    report["cutover"]=cutover(report)
    return report
