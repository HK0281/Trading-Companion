"""Normal local-server launcher with an explicitly selected offline development mode."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from trading_companion.model import ModelConfigurationError
from trading_companion.local_env import load_model_environment
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.api_service import DATA_MODES, DataModeDependencyError, ensure_data_mode_dependencies


_SHANGHAI = timezone(timedelta(hours=8))
_DIRECTORY_REFRESH_HINT = (
    r"请手动运行：.\.venv\Scripts\python.exe -B -m trading_companion.security_directory_refresh --live"
    "（自选目录请追加 --output 指定同一路径）。"
)


class _StartupError(Exception):
    """Only fixed application messages may cross the startup error boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code)


def _prepare_startup_directory(path: Path, *, refresh_stale: bool) -> None:
    """Refresh only absent/dated snapshots; never refresh from the runtime loader."""
    from trading_companion.security_directory import load_security_directory

    directory = None
    reference_date = datetime.now(_SHANGHAI).date()
    try:
        try:
            directory = load_security_directory(path)
        except DataSourceError:
            try:
                path.stat()
            except FileNotFoundError:
                raise _StartupError("directory_missing", "证券名单缺失；" + _DIRECTORY_REFRESH_HINT) from None
            except OSError:
                pass
            raise _StartupError("directory_unavailable", "证券名单无法读取，请检查文件访问权限。") from None
        except (DataValidationError, InvalidRequestError):
            raise _StartupError("directory_invalid", "证券名单格式无效，请检查本地名单文件。") from None
        try:
            directory.assert_usable(reference_date, development=False)
        except (DataValidationError, InvalidRequestError):
            if not directory.is_fixture and not 0 <= (
                    reference_date - directory.retrieved_at.astimezone(_SHANGHAI).date()).days <= 7:
                raise _StartupError("directory_stale", "证券名单已过期或日期在未来；" + _DIRECTORY_REFRESH_HINT) from None
            raise _StartupError("directory_invalid", "证券名单格式无效或属于人工测试名单，不能用于正常研究。") from None
    except _StartupError as error:
        if not refresh_stale or error.code not in ("directory_missing", "directory_stale"):
            raise
        reason = ("证券名单缺失" if directory is None else
                  f"证券名单已过期或日期在未来（取于 {directory.retrieved_at.astimezone(_SHANGHAI).date()}）")
        print(f"{reason}，正在刷新证券名单（来源：交易所）…", flush=True)
        try:
            from trading_companion.security_directory_refresh import refresh_security_directory
            refresh_security_directory(path, reference_date=reference_date)
            directory = load_security_directory(path)
            directory.assert_usable(datetime.now(_SHANGHAI).date(), development=False)
        except (DataSourceError, DataValidationError, InvalidRequestError, OSError):
            raise _StartupError("directory_refresh_failed",
                "自动刷新证券名单失败（网络、交易所页面或本地文件不可用），请稍后重试；"
                + _DIRECTORY_REFRESH_HINT) from None
        print(f"证券名单刷新成功：{len(directory.securities)} 条，取于 "
              f"{directory.retrieved_at.astimezone(_SHANGHAI).date()}。", flush=True)


def _run_browser_server(app, *, port: int) -> None:
    """Open the UI only after this server has successfully bound its loopback port."""
    import asyncio
    import webbrowser
    import uvicorn

    url = f"http://127.0.0.1:{port}/"

    class BrowserServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if not self.started:
                return
            try:
                # Browser startup must not block the loop serving the first page request.
                opened = await asyncio.to_thread(webbrowser.open, url, new=2)
            except Exception:
                opened = False
            if not opened:
                print(f"自动打开浏览器失败，请手动访问：{url}", file=sys.stderr)

    server = BrowserServer(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, access_log=False))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    # Match Uvicorn's failure exit if startup stopped before binding a listener.
    if not server.started:
        raise SystemExit(3)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Single-worker loopback research API, not a public deployment.")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--demo", action="store_true", help="Fixed offline fake model and pinned synthetic data")
    entry.add_argument("--provider", choices=("deepseek", "openai"), help="Default model provider (default: deepseek)")
    parser.add_argument("--data", choices=DATA_MODES,
                        help="Normal source (default: tencent); AKShare requires python -m pip install -e '.[data]'")
    parser.add_argument("--directory", help="Existing local security snapshot; Tencent/AKShare modes only")
    parser.add_argument("--refresh-stale-directory", action="store_true",
                        help="Refresh missing/stale default directory at startup only (enabled by start.cmd)")
    parser.add_argument("--market-context", help="Optional trusted local market-context JSON; never refreshed online")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=False,
                        help="Open the local UI after successful startup (enabled by start.cmd)")
    parser.add_argument("--snapshot-db", help="Optional local SQLite evidence archive; excludes chat and model prose")
    parser.add_argument("--memory-db", help="Local SQLite conversation history and earlier-turn digests (enabled by start.cmd)")
    parser.add_argument("--no-memory", action="store_true",
                        help="Disable conversation history, including the default enabled by start.cmd")
    parser.add_argument("--trace-dir", help="Opt-in bounded local run metadata; excludes questions, answers and credentials")
    parser.add_argument("--no-trace", action="store_true",
                        help="Disable metadata tracing, including the default enabled by start.cmd")
    parser.add_argument("--collection-workers", type=int, choices=(1, 2, 3), default=3,
                        help="Company/Tencent quote I/O width sharing three slots; Eastmoney stays serial (default: 3)")
    parser.add_argument("--screening-data", choices=("tencent", "eastmoney"), default="tencent",
                        help="Explicit bounded quote-screening source; no failure fallback (default: tencent)")
    parser.add_argument("--screening-coverage", choices=("standard", "expanded"), default="standard",
                        help="Expanded quote sample: up to 1000 records, more requests, same deadline")
    args = parser.parse_args(argv)
    if args.no_trace:
        args.trace_dir = None
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535")
    if args.demo and (args.data or args.directory or args.market_context or args.snapshot_db or args.memory_db):
        parser.error("Demo uses pinned fixtures; do not specify data, directory or market context")
    if args.no_memory:
        args.memory_db = None
    if not args.demo:
        args.provider = args.provider or "deepseek"
        args.data = args.data or "tencent"
    if args.data == "synthetic" and args.directory:
        parser.error("Synthetic data does not accept a directory path")
    if (args.demo or args.data == "synthetic") and args.screening_coverage != "standard":
        parser.error("Synthetic mode uses fixed fixtures, not expanded live sampling")
    service = development_service = snapshot_store = memory_store = None
    try:
        ensure_data_mode_dependencies("synthetic" if args.demo else args.data)
        import uvicorn
        from trading_companion.api import create_app, create_demo_service
        from trading_companion.api_service import ResearchApiService
        from trading_companion.development_fixtures import (
            REFERENCE_DATE, ROOT, load_development_fixtures, load_development_market_context,
        )
        from trading_companion.market_context import load_market_context
        from trading_companion.turn_trace import JsonlTraceSink
        trace_sink = (JsonlTraceSink(Path(args.trace_dir), warning=lambda: print(
            "[trace_unavailable] 本地运行记录暂不可写；研究回复不受影响。", file=sys.stderr)) if args.trace_dir else None)
        if args.demo:
            service = create_demo_service(trace_sink=trace_sink, trace_mode="demo")
        else:
            from trading_companion.model_registry import configured_models
            from trading_companion.security_directory import load_security_directory
            if args.data == "synthetic":
                source, _ = load_development_fixtures()
                loader = lambda _: load_development_fixtures()[1]
            else:
                source = None
                directory_path = Path(args.directory) if args.directory is not None else ROOT / "data/security_directory_live.json"
                def loader(_):
                    # Routing may use stale names as a veto, never as research authority.
                    return load_security_directory(directory_path)
                _prepare_startup_directory(directory_path,
                    refresh_stale=args.refresh_stale_directory and args.directory is None)
            models = configured_models(default_provider=args.provider, loader=load_model_environment)
            if args.market_context:
                context_path = Path(args.market_context)
                context_loader = lambda _: load_market_context(context_path)
            elif source is not None:
                context_loader = lambda _: load_development_market_context()
            else:
                context_loader = None
            from trading_companion.company_research import ResearchDataService
            from trading_companion.market_context_live import ExchangeMarketContextSource
            from trading_companion.screening_source import configured_quote_source
            screening_source = configured_quote_source(args.screening_data, args.screening_coverage,
                max_workers=args.collection_workers) if source is None else None
            background_source = ExchangeMarketContextSource() if source is None and not args.market_context else None
            if args.snapshot_db:
                from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError
                try:
                    snapshot_store = ResearchSnapshotStore(Path(args.snapshot_db))
                except SnapshotStoreError:
                    raise _StartupError("snapshot_store_unavailable",
                        "本地证据快照库不可用，请检查 --snapshot-db 指定的文件及访问权限。") from None
            if args.memory_db:
                from trading_companion.memory_store import MemoryStoreError, SqliteConversationStore
                try:
                    memory_store = SqliteConversationStore(Path(args.memory_db))
                except MemoryStoreError:
                    raise _StartupError("memory_store_unavailable",
                        "本地对话记录库不可用，请检查 --memory-db 指定的文件及访问权限，或使用 --no-memory。") from None
            service = ResearchApiService(model_factory=models[args.provider], model_factories=models, directory_loader=loader,
                provider=args.provider, data_mode=args.data, development_source=source,
                reference_date=REFERENCE_DATE if args.data == "synthetic" else None,
                market_context_loader=context_loader,
                product_data_service=ResearchDataService(max_workers=args.collection_workers) if source is None else None,
                snapshot_store=snapshot_store,
                screening_source=screening_source, market_background_source=background_source, trace_sink=trace_sink,
                conversation_store=memory_store, conversation_context=memory_store is not None)
            # A separate fixture-only service can never alter the normal service's source or budget.
            development_service = create_demo_service(trace_sink=trace_sink)
    except DataModeDependencyError:
        print("[data_dependency_missing] AKShare可选依赖未安装；先运行 python -m pip install -e '.[data]'。未读取模型配置或启动服务，不自动切换来源。", file=sys.stderr)
        return 2
    except ImportError:
        print("Install API dependencies first: python -m pip install -e '.[api,model]'", file=sys.stderr)
        return 2
    except (_StartupError, ModelConfigurationError, DataSourceError, DataValidationError, InvalidRequestError) as error:
        if service is not None:
            service.close()
        if development_service is not None:
            development_service.close()
        if snapshot_store is not None:
            snapshot_store.close()
        if memory_store is not None:
            memory_store.close()
        if isinstance(error, _StartupError):
            code, message = error.code, error.message
        elif isinstance(error, ModelConfigurationError):
            fields = "OPENAI_API_KEY 和 OPENAI_MODEL" if args.provider == "openai" else "DEEPSEEK_API_KEY 和 DEEPSEEK_MODEL"
            code, message = "model_configuration_error", f"模型配置缺失或无效，请检查 .env 或环境变量中的 {fields}；不要发送密钥。"
        else:
            code, message = "configuration_error", "服务端本地配置或夹具不可用，请检查启动配置；不要发送密钥。"
        print(f"[{code}] {message}", file=sys.stderr)
        print("正常模式不会自动退回测试数据；需要离线演示时显式使用 --demo。", file=sys.stderr)
        return 2
    print("仅监听127.0.0.1、单进程；不下单，不开放公网。")
    if memory_store is not None:
        print("对话记录保存在本机 SQLite 文件中，可在网页历史列表中删除；使用 --no-memory 可关闭。")
    elif not args.demo:
        print("对话记录未开启：不保存聊天内容，也不向模型提供前几轮摘要；start.cmd 默认开启。")
    if trace_sink is not None:
        print("本地运行记录已开启：仅运行元数据，有容量上限；不保存问题、答案、密钥或令牌。")
    print("固定离线脚本 / 人工行情，不是自由问答模型。" if args.demo else
          f"正常服务默认模型：{args.provider}；发送研究问题后调用所选提供方。")
    if not args.demo:
        label = {"synthetic": "人工行情 / 非真实", "tencent": "腾讯不复权历史日线",
                 "akshare_eastmoney": "AKShare / 东方财富不复权历史日线"}[args.data]
        print(f"行情来源：{label}；由本次启动固定，失败不自动换源，对话输入不能切换。")
        print("网页 Dev Mode 可切换到独立的离线测试会话；不会调用真实模型或行情，也不会更改正常服务来源。")
    print("公司研究可读取交易所当前板块与上市背景，交易规则按生效日期解释；历史状态、停牌和交易日历缺失保持未知。")
    print(f"研究界面：http://127.0.0.1:{args.port}/；API文档：/docs；访问日志关闭，请不要共享会话访问令牌。")
    try:
        app = (create_app(service=service) if development_service is None
               else create_app(service=service, development_service=development_service))
        if args.open_browser:
            _run_browser_server(app, port=args.port)
        else:
            uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    finally:
        service.close()
        if development_service is not None:
            development_service.close()
        if snapshot_store is not None:
            snapshot_store.close()
        if memory_store is not None:
            memory_store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
