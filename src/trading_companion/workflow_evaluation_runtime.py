"""Side-effect-free plans precede credential loading and explicit model/source wiring."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from pathlib import Path
import socket
from unittest.mock import patch

from trading_companion.evaluation_pricing import DEFAULT_PRICES, MODEL_ID, load_prices, estimate_cost, price_for


MODES = ("offline", "live-model", "live")
ROOT = Path(__file__).resolve().parents[2]


class PlanError(ValueError):
    def __init__(self, code, detail, *, exclusions=()):
        self.code, self.detail, self.exclusions = code, detail, list(exclusions)
        super().__init__(code)

    def result(self):
        return {"status": "invalid_request", "code": self.code, "detail": self.detail,
                "excluded_cases": self.exclusions}


def invalid_plan():
    return PlanError("evaluation_input_invalid", "无法校验评估输入，请检查参数、语料及本地配置。").result()


def require(condition, *, code, detail):
    if not condition:
        raise PlanError(code, detail)


def check_output_root(path):
    if path is not None:
        message = "trace-dir/report-dir 必须是有效目录；目标及已有父路径不能是文件。"
        require(isinstance(path, (str, Path)) and "\0" not in str(path),
                code="evaluation_output_directory_invalid", detail=message)
        try:
            target = Path(path).absolute()
            valid = all(not ancestor.exists() or ancestor.is_dir() for ancestor in (target, *target.parents))
        except (OSError, ValueError):
            valid = False
        require(valid, code="evaluation_output_directory_invalid", detail=message)


def make_plan(*, mode="offline", selected_cases=None, repeat=1, collection_workers=3, model_call_limit=None,
              provider=None, model=None, prices=None, directory=None, data_source="tencent",
              assumed_input_tokens=None, assumed_output_tokens=None, trace_dir=None):
    from trading_companion.workflow_evaluation import load_corpus, b_delta_descriptor, technical_delta_descriptor
    require(type(mode) is str and mode in MODES, code="evaluation_mode_invalid",
            detail="mode 必须是 offline、live-model 或 live。")
    require(type(repeat) is int and 1 <= repeat <= 3, code="evaluation_repeat_out_of_range",
            detail="repeat 必须是 1..3 之间的整数。")
    require(type(collection_workers) is int and 1 <= collection_workers <= 3,
            code="evaluation_collection_workers_out_of_range", detail="collection-workers 必须是 1..3 之间的整数。")
    try:
        corpus, digest = load_corpus()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PlanError("evaluation_corpus_invalid", "评估语料无法读取或结构无效，请检查版本及场景标注。") from None
    excluded = [{"case_id": case["id"], "steps": len(case["steps"]) * repeat,
                 "reason": case["mode_exclusions"][mode]} for case in corpus["cases"] if mode not in case["modes"]]
    ids = [case["id"] for case in corpus["cases"]]
    requested = [case["id"] for case in corpus["cases"] if mode in case["modes"]] if selected_cases is None else selected_cases
    require(type(requested) in (tuple, list) and bool(requested)
            and all(type(value) is str and value in ids for value in requested) and len(set(requested)) == len(requested),
            code="evaluation_case_selection_invalid", detail="case 必须是语料中有效且不重复的场景 ID，选择不能为空。")
    incompatible = [entry for entry in excluded if entry["case_id"] in requested]
    if incompatible:
        raise PlanError("evaluation_cases_not_applicable_to_mode", "case 包含当前 mode 不适用的场景，原因见 excluded_cases。",
                        exclusions=incompatible)
    selected = [case for case in corpus["cases"] if case["id"] in requested]
    total = repeat * sum(len(case["steps"]) for case in selected)
    allowance = 6 * total if model_call_limit is None else model_call_limit
    require(type(allowance) is int and 1 <= allowance <= 6 * total,
            code="evaluation_model_call_limit_out_of_range",
            detail=f"model-call-limit 必须是 1..{6 * total} 之间的整数（6 × {total} 个计划步骤）。")
    check_output_root(trace_dir)
    for name, assumption in (("assumed-input-tokens", assumed_input_tokens), ("assumed-output-tokens", assumed_output_tokens)):
        require(assumption is None or type(assumption) is int and 0 <= assumption <= 1_000_000,
                code="evaluation_assumed_tokens_invalid", detail=f"{name} 必须省略或为 0..1000000 之间的整数。")
    require(type(data_source) is str and data_source in ("tencent", "akshare_eastmoney"),
            code="evaluation_data_source_invalid", detail="data-source 必须是 tencent 或 akshare_eastmoney。")
    if mode == "offline":
        require(provider is None and model is None and directory is None and data_source == "tencent",
                code="evaluation_offline_options_invalid",
                detail="offline 不接受 provider、model 或 directory，data-source 必须保留 tencent。")
    else:
        require(provider in ("deepseek", "openai"), code="evaluation_provider_invalid",
                detail="真实模式必须显式指定 provider，取值为 deepseek 或 openai。")
        require(type(model) is str and MODEL_ID.fullmatch(model) is not None, code="evaluation_model_invalid",
                detail="真实模式必须显式指定 model：1..128 个字母、数字、点、下划线或连字符，以字母或数字开头。")
        require(prices is not None, code="evaluation_price_file_invalid", detail="真实模式必须用 prices 显式指定价格文件。")
        require(model_call_limit is not None, code="evaluation_model_call_limit_required",
                detail=f"真实模式必须显式指定 model-call-limit，范围为 1..{6 * total}（6 × {total} 个计划步骤）。")
        require(mode == "live" or directory is None and data_source == "tencent",
                code="evaluation_live_model_source_options_invalid",
                detail="live-model 使用固定资料，不接受 directory，data-source 必须保留 tencent。")
    try:
        price_data, price_digest = load_prices(DEFAULT_PRICES if prices is None else prices)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PlanError("evaluation_price_file_invalid",
                        "prices 文件无法读取或价格配置无效；请检查 evaluation-prices-v1 结构、单价及核对元数据。") from None
    plan = {"version": "workflow-evaluation-plan-v1", "status": "planned", "mode": mode,
            "corpus": {"version": corpus["version"], "sha256": digest, **b_delta_descriptor(),
                       'rule_baseline_version': technical_delta_descriptor()['rule_baseline_version'],
                       'technical_delta': technical_delta_descriptor()},
            "selected_cases": [case["id"] for case in selected], "selected_scenarios": len(selected),
            "planned_steps": total, "repeat": repeat, "collection_workers": collection_workers,
            "excluded_cases": excluded, "not_applicable_to_mode": len(excluded),
            "not_applicable_steps": sum(entry["steps"] for entry in excluded),
            "selection": "all_mode_compatible" if selected_cases is None else "explicit_cases",
            "provider": "fake" if mode == "offline" else provider,
            "model": "fake-tools-v1" if mode == "offline" else model,
            "data_source": data_source, "data_authenticity": "live_sources" if mode == "live" else "fixed_synthetic",
            "price_config_sha256": price_digest, "price": price_for(price_data, provider, model),
            "model_call_limit": allowance, "worst_case_model_calls": 6 * total,
            "scripted_model_calls": sum(len(step["fixture_script"]["phases"]) for case in selected for step in case["steps"]) * repeat
                                    if mode == "offline" else None,
            "cost_preview": estimate_cost(price_data, provider, model, allowance, assumed_input_tokens, assumed_output_tokens),
            "directory": None, "directory_sha256": None, "directory_metadata": None}
    if mode == "live":
        from trading_companion.api_service import ensure_data_mode_dependencies
        from trading_companion.security_directory import load_security_directory
        from trading_companion.errors import DataSourceError
        try:
            ensure_data_mode_dependencies(data_source)
        except (OSError, ValueError, DataSourceError):
            raise PlanError("evaluation_live_directory_or_dependency_invalid",
                            "data-source 的本地依赖不可用；akshare_eastmoney 需要安装项目的 data 可选依赖。") from None
        try:
            path = Path(directory) if directory is not None else ROOT / "data/security_directory_live.json"
            snapshot = load_security_directory(path)
            plan.update(directory=str(path.absolute()), directory_sha256=sha256(path.read_bytes()).hexdigest(),
                        directory_metadata=snapshot.metadata())
        except (OSError, ValueError, TypeError, DataSourceError):
            raise PlanError("evaluation_live_directory_or_dependency_invalid",
                            "directory 无法读取或不是合法证券目录；请检查本地快照配置。") from None
        today = datetime.now(timezone(timedelta(hours=8))).date()
        try:
            snapshot.assert_usable(today, development=False)
        except (ValueError, OverflowError):
            if snapshot.is_fixture:
                raise PlanError("evaluation_live_directory_or_dependency_invalid",
                                "directory 是人工夹具，不能用于 live；请提供真实交易所名单快照。") from None
            retrieved = snapshot.retrieved_at.isoformat()
            raise PlanError("evaluation_live_directory_stale",
                            f"directory retrieved_at={retrieved}；快照须在当前北京时间日期 {today.isoformat()} 前 0..7 天内。"
                            "请运行 python -m trading_companion.security_directory_refresh --live 刷新。") from None
    return plan, selected, price_data


def build_plan(**kwargs):
    try:
        return make_plan(**kwargs)[0]
    except PlanError as error:
        return error.result()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return invalid_plan()


def build_real_model(plan):
    from trading_companion.local_env import load_model_environment
    from trading_companion.model_registry import _factory
    provider = plan["provider"]
    values = dict(load_model_environment(provider=provider))
    values[provider.upper() + "_MODEL"] = plan["model"]
    model = _factory(provider, values)()
    require(model.config.model == plan["model"], code="evaluation_model_configuration_mismatch",
            detail="模型适配器的配置与显式 model 不一致，已停止执行。")
    return model


@contextmanager
def model_network_boundary(provider):
    """Permit only the selected adapter's synchronous POST; source/DNS attempts stay visible."""
    import requests
    from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL
    from trading_companion.openai_tool_model import OPENAI_CHAT_URL
    allowed, denied = ContextVar("evaluation_model_transport", default=False), []
    endpoint = {"deepseek": DEEPSEEK_CHAT_URL, "openai": OPENAI_CHAT_URL}[provider]
    post = requests.post

    def guarded(function):
        def call(*args, **kwargs):
            if not allowed.get():
                denied.append(True)
                raise OSError("Evaluation forbids source network access")
            return function(*args, **kwargs)
        return call

    def model_post(url, *args, **kwargs):
        if url != endpoint:
            denied.append(True)
            raise OSError("Evaluation forbids this endpoint")
        token = allowed.set(True)
        try:
            return post(url, *args, **kwargs)
        finally:
            allowed.reset(token)
    with patch("requests.post", model_post), patch("socket.getaddrinfo", guarded(socket.getaddrinfo)), \
            patch.object(socket.socket, "connect", guarded(socket.socket.connect)), \
            patch.object(socket.socket, "connect_ex", guarded(socket.socket.connect_ex)), \
            patch.object(socket.socket, "sendto", guarded(socket.socket.sendto)):
        yield denied


@contextmanager
def live_sources(plan):
    from trading_companion.security_directory import load_security_directory
    from trading_companion.collection_execution import CollectionExecutor
    from trading_companion.company_research import ResearchDataService
    from trading_companion.screening_source import TencentQuoteSnapshotSource
    from types import SimpleNamespace
    path = Path(plan["directory"])
    require(sha256(path.read_bytes()).hexdigest() == plan["directory_sha256"],
            code="evaluation_live_directory_changed", detail="directory 在计划预览后发生变化，请重新校验计划。")
    directory = load_security_directory(path)
    directory.assert_usable(datetime.now(timezone(timedelta(hours=8))).date(), development=False)
    pool = CollectionExecutor()
    try:
        yield SimpleNamespace(directory=directory,
            company=ResearchDataService(executor=pool, max_workers=plan["collection_workers"]),
            quotes=TencentQuoteSnapshotSource(executor=pool, max_workers=plan["collection_workers"]))
    finally:
        pool.close(wait=True)


def collection_attempts(trace):
    if trace is None or trace["truncated"] or trace["missing_parents"]:
        return None
    return sum(event["kind"] == "source" and event["entered"] and event["operation"] != "directory"
               for event in trace["event_records"])
