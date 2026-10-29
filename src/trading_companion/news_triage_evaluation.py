"""Sequential synthetic headline experiment; plans precede all credential access."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import random
import socket
import sys
from time import monotonic
from datetime import datetime, timezone
from unittest.mock import patch

from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL, DeepSeekConfig
from trading_companion.evaluation_pricing import DEFAULT_PRICES, MODEL_ID, estimate_cost, load_prices, price_for
from trading_companion.model import ModelConfigurationError
from trading_companion.local_env import load_evaluation_key_environment
from trading_companion.news_triage import (
    RULE_VERSION, TASK_VERSION, definition_digest, input_state, load_corpus, rule_classify,
)
from trading_companion.news_triage_models import DEEPSEEK_TEMPLATE, DeepSeekTriage, JevTriage, fixture_post
from trading_companion.news_triage_metrics import metrics
from trading_companion.typesafe_jev import ClassifierError, JEV_URL, JevConfig
from trading_companion.workflow_evaluation_runtime import PlanError, check_output_root

TIMEOUT_SCOPE = "单次请求超时；DNS解析、慢速分块等极端情况不保证绝对墙钟上限；不保证远端停止计费。"
RULE_BASELINE_CAVEAT = "规则与题库由同一作者编写，规则分数可能偏高；本轮修题未改变规则，未按规则分数或模型输出挑选标题。"


def need(condition, code, detail):
    if not condition:
        raise PlanError(code, detail)


def make_plan(*, mode="offline", classifiers=None, selected_items=None, repeat=1, call_limit=None,
              prices=None, deepseek_model=None, timeout_seconds=15, assumed_input_tokens=None,
              assumed_output_tokens=None, report_dir=None):
    need(mode in ("offline", "live"), "triage_mode_invalid", "mode 必须是 offline 或 live。")
    need(type(repeat) is int and 1 <= repeat <= 3, "triage_repeat_invalid", "repeat 必须为 1..3。")
    need(type(timeout_seconds) in (int, float) and isfinite(timeout_seconds) and 1 <= timeout_seconds <= 60,
         "triage_timeout_invalid", "timeout-seconds 必须为 1..60 的有限数。")
    chosen = ["jev", "deepseek"] if classifiers is None and mode == "offline" else classifiers
    need(type(chosen) in (list, tuple) and 1 <= len(chosen) <= 2
         and all(type(c) is str and c in ("jev", "deepseek") for c in chosen) and len(set(chosen)) == len(chosen),
         "triage_classifiers_invalid", "live 必须显式选择不重复的 classifier：jev、deepseek。")
    chosen = [c for c in ("jev", "deepseek") if c in chosen]
    corpus, manifest = load_corpus()
    ids = [r["id"] for r in corpus]
    requested = ids if selected_items is None else selected_items
    need(type(requested) in (list, tuple) and bool(requested) and all(type(i) is str and i in ids for i in requested)
         and len(set(requested)) == len(requested), "triage_items_invalid", "item 必须是题库中不重复的有效编号，不能为空。")
    selected = [r for r in corpus if r["id"] in requested]
    total = len(selected) * repeat * len(chosen)
    limit = total if call_limit is None else call_limit
    need(type(limit) is int and 1 <= limit <= total, "triage_call_limit_invalid", f"call-limit 必须为 1..{total}。")
    need(mode != "live" or call_limit is not None and prices is not None,
         "triage_live_options_required", "live 必须显式给出 call-limit 和 prices。")
    need((mode == "live" and "deepseek" in chosen and type(deepseek_model) is str and MODEL_ID.fullmatch(deepseek_model))
         or deepseek_model is None and (mode == "offline" or "deepseek" not in chosen),
         "triage_deepseek_model_invalid", "live 选择 deepseek 时须给 deepseek-model；其他情况不得给该参数。")
    for value in (assumed_input_tokens, assumed_output_tokens):
        need(value is None or type(value) is int and 0 <= value <= 1_000_000,
             "triage_token_assumption_invalid", "用量假设必须省略或为 0..1000000 的整数。")
    check_output_root(report_dir)
    price_data, price_sha = load_prices(DEFAULT_PRICES if prices is None else prices)
    models = {"jev": "jev-latest", "deepseek": deepseek_model or "offline-deepseek-v1"}
    implementation = ("typesafe_jev.py", "news_triage.py", "news_triage_models.py", "news_triage_evaluation.py",
                      "news_triage_metrics.py", "classification_metrics.py", "news_triage_reporting.py", "evaluation_pricing.py", "local_env.py")
    plan = {"version": "news-triage-plan-v1", "status": "planned", "mode": mode, "classifiers": chosen,
            "models": {c: models[c] for c in chosen}, "corpus": manifest, "task_version": TASK_VERSION,
            "definition_sha256": definition_digest(), "rule_version": RULE_VERSION,
            "rule_baseline_caveat": RULE_BASELINE_CAVEAT,
            "deepseek_template": DEEPSEEK_TEMPLATE, "seed": 42,
            "rule_source_sha256": sha256((Path(__file__).parent / "news_triage.py").read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "selected_items": [r["id"] for r in selected], "repeat": repeat, "planned_titles": len(selected)*repeat,
            "worst_case_requests": total, "call_limit": limit, "timeout_seconds": timeout_seconds,
            "timeout_scope": TIMEOUT_SCOPE, "prices_sha256": price_sha,
            "price_entries": {c: price_for(price_data, "typesafe" if c == "jev" else c, models[c]) for c in chosen},
            "implementation_sha256": sha256(b"".join((Path(__file__).parent / f).read_bytes().replace(b"\r\n", b"\n")
                                                       for f in implementation)).hexdigest(),
            "cost_preview": {c: estimate_cost(price_data, "typesafe" if c == "jev" else c, models[c],
                                               min(limit, len(selected)*repeat), assumed_input_tokens, assumed_output_tokens)
                             for c in chosen},
            "data_scope": "合成标题和虚构公司；不传正文、链接、用户数据；语气不是交易信号。",
            "live_annotation_ready": manifest["annotation_status"] == "reviewed"}
    return plan, selected, price_data


def invalid(error=None):
    if isinstance(error, PlanError):
        return error.result()
    return {"status": "invalid_request", "code": "triage_input_invalid", "detail": "题库、标注、价格或本地输入无效。"}


def build_plan(**kwargs):
    try:
        return make_plan(**kwargs)[0]
    except (OSError, ValueError, TypeError, KeyError, ClassifierError, RecursionError) as error:
        return invalid(error)


def build_models(plan):
    models = {}
    for c in plan["classifiers"]:
        fake = plan["mode"] == "offline"
        post = fixture_post(c) if fake else None
        if c == "jev":
            key = "offline-not-a-key" if fake else load_evaluation_key_environment("TYPESAFE_API_KEY").get("TYPESAFE_API_KEY", "")
            config = JevConfig(key, timeout_seconds=plan["timeout_seconds"])
            models[c] = JevTriage(config, post=post)
        else:
            key = "offline-not-a-key" if fake else load_evaluation_key_environment("DEEPSEEK_API_KEY").get("DEEPSEEK_API_KEY", "")
            config = DeepSeekConfig(plan["models"][c], key,
                                    timeout_seconds=plan["timeout_seconds"])
            models[c] = DeepSeekTriage(config, post=post)
    return models


@contextmanager
def network_boundary(classifiers):
    """Only a selected, synchronous adapter POST may open network connections."""
    import requests
    permitted = ContextVar("triage_network_permission", default=False)
    denied = []
    endpoints = {JEV_URL if c == "jev" else DEEPSEEK_CHAT_URL for c in classifiers}
    original_post = requests.post

    def guard(function):
        def call(*args, **kwargs):
            if not permitted.get():
                denied.append(True)
                raise ClassifierError("network_forbidden")
            return function(*args, **kwargs)
        return call

    def post(url, *args, **kwargs):
        if url not in endpoints or kwargs.get("allow_redirects") is not False or kwargs.get("stream") is not True:
            denied.append(True)
            raise ClassifierError("network_forbidden")
        token = permitted.set(True)
        try:
            return original_post(url, *args, **kwargs)
        finally:
            permitted.reset(token)
    with patch("requests.post", post), patch("socket.getaddrinfo", guard(socket.getaddrinfo)), \
            patch.object(socket.socket, "connect", guard(socket.socket.connect)), \
            patch.object(socket.socket, "connect_ex", guard(socket.socket.connect_ex)), \
            patch.object(socket.socket, "sendto", guard(socket.socket.sendto)):
        yield denied


def evaluate(**kwargs):
    try:
        plan, corpus, prices = make_plan(**kwargs)
        if plan["mode"] == "live":
            print(json.dumps(plan, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
            need(plan["live_annotation_ready"], "triage_annotations_not_reviewed", "题库仍为草稿；须先由 Claude 逐条复核。")
    except (OSError, ValueError, TypeError, KeyError, ClassifierError, RecursionError) as error:
        return invalid(error)
    rows, schedule = [], []
    for repeat in range(1, plan["repeat"]+1):
        for item in corpus:
            start = monotonic()
            rules = rule_classify(input_state(item))
            row = {**item, "repeat": repeat, "results": {"rules": {"status": "ok", "code": "ok", "answers": rules,
                    "elapsed_ms": (monotonic()-start)*1000, "attempted": False, "model": RULE_VERSION}}}
            for c in plan["classifiers"]:
                row["results"][c] = {"status": "not_run", "code": "call_limit", "answers": None,
                                     "elapsed_ms": None, "attempted": False, "model": None, "usage": None}
                schedule.append((len(rows), c))
            rows.append(row)
    random.Random(42).shuffle(schedule)
    attempts, denied, framework_error = 0, [], None
    try:
        with ExitStack() as stack:
            if plan["mode"] == "offline":
                from scripts.verify import clean_environment, offline_network
                stack.enter_context(clean_environment())
                stack.enter_context(offline_network())
            else:
                denied = stack.enter_context(network_boundary(plan["classifiers"]))
            models = build_models(plan)
            for index, c in schedule:
                if attempts >= plan["call_limit"]:
                    continue
                attempts += 1
                result = rows[index]["results"][c]
                result.update(attempted=True, status="failed", code="classifier_internal_error")
                start = monotonic()
                try:
                    result.update(models[c].classify(input_state(rows[index])), status="ok", code="ok")
                except ClassifierError as error:
                    result.update(error.metadata, code=error.code)
                except Exception:
                    framework_error = "classifier_internal_error"
                result["elapsed_ms"] = (monotonic()-start)*1000
    except ModelConfigurationError:
        framework_error = "classifier_configuration_invalid"
    except Exception:
        framework_error = "evaluation_boundary_or_initialization_error"
    if framework_error:
        for row in rows:
            for c in plan["classifiers"]:
                if not row["results"][c]["attempted"]:
                    row["results"][c]["code"] = framework_error
    partial = bool(framework_error or denied or any(r["results"][c]["status"] != "ok" for r in rows for c in plan["classifiers"]))
    result = {"version": "news-triage-evaluation-v1", "status": "partial" if partial else "completed",
              "code": framework_error or ("network_forbidden" if denied else "partial_results" if partial else "completed"),
              "evaluated_at": datetime.now(timezone.utc).isoformat(), "plan": plan, "synthetic": True,
              "scope": "offline_fixture_contracts_not_model_performance" if plan["mode"] == "offline" else "synthetic_headline_experiment_not_production_performance",
              "accounting": {"forwarded": attempts, "limit": plan["call_limit"], "network_violations": len(denied)},
              "execution_order": [{"id": rows[i]["id"], "repeat": rows[i]["repeat"], "classifier": c} for i, c in schedule],
              "rows": rows}
    result["metrics"] = metrics(rows, plan["classifiers"], plan["models"], prices, simulated=plan["mode"] == "offline")
    result["disagreements"] = [{"id": r["id"], "repeat": r["repeat"], "question": q,
                                "predictions": {c: (r["results"][c].get("answers") or {}).get(q, {}).get("choice")
                                                for c in ("rules", *plan["classifiers"])}}
                               for r in rows for q in ("relevance", "event_type", "tone")
                               if len({(r["results"][c].get("answers") or {}).get(q, {}).get("choice")
                                       for c in ("rules", *plan["classifiers"])}) > 1]
    return result
