"""Normal local-server launcher with an explicitly selected offline development mode."""

import argparse
from datetime import timedelta, timezone
from pathlib import Path
import sys

from trading_companion.model import ModelConfigurationError
from trading_companion.local_env import load_model_environment
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.api_service import DATA_MODES, DataModeDependencyError, ensure_data_mode_dependencies


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Single-worker loopback research API, not a public deployment.")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--demo", action="store_true", help="Fixed offline fake model and pinned synthetic data")
    entry.add_argument("--provider", choices=("deepseek", "openai"), help="Default model provider (default: deepseek)")
    parser.add_argument("--data", choices=DATA_MODES,
                        help="Normal source (default: tencent); AKShare requires python -m pip install -e '.[data]'")
    parser.add_argument("--directory", help="Existing local security snapshot; Tencent/AKShare modes only")
    parser.add_argument("--market-context", help="Optional trusted local market-context JSON; never refreshed online")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--snapshot-db", help="Optional local SQLite evidence archive; excludes chat and model prose")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535")
    if args.demo and (args.data or args.directory or args.market_context or args.snapshot_db):
        parser.error("Demo uses pinned fixtures; do not specify data, directory or market context")
    if not args.demo:
        args.provider = args.provider or "deepseek"
        args.data = args.data or "tencent"
    if args.data == "synthetic" and args.directory:
        parser.error("Synthetic data does not accept a directory path")
    service = development_service = snapshot_store = None
    try:
        ensure_data_mode_dependencies("synthetic" if args.demo else args.data)
        import uvicorn
        from trading_companion.api import create_app, create_demo_service
        from trading_companion.api_service import ResearchApiService
        from trading_companion.development_fixtures import (
            REFERENCE_DATE, ROOT, load_development_fixtures, load_development_market_context,
        )
        from trading_companion.market_context import load_market_context
        if args.demo:
            service = create_demo_service()
        else:
            from trading_companion.model_registry import configured_models
            from trading_companion.security_directory import load_security_directory
            from datetime import datetime
            if args.data == "synthetic":
                source, _ = load_development_fixtures()
                loader = lambda _: load_development_fixtures()[1]
            else:
                source = None
                directory_path = Path(args.directory) if args.directory else ROOT / "data/security_directory_live.json"
                def loader(_):
                    # Routing may use stale names as a veto, never as research authority.
                    return load_security_directory(directory_path)
                directory = loader(datetime.now(timezone.utc))
                directory.assert_usable(datetime.now(timezone.utc).astimezone(
                    timezone(timedelta(hours=8))).date(), development=False)
            models = configured_models(default_provider=args.provider, loader=load_model_environment)
            if args.market_context:
                context_path = Path(args.market_context)
                context_loader = lambda _: load_market_context(context_path)
            elif source is not None:
                context_loader = lambda _: load_development_market_context()
            else:
                context_loader = None
            from trading_companion.company_research import ResearchDataService
            if args.snapshot_db:
                from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError
                try:
                    snapshot_store = ResearchSnapshotStore(Path(args.snapshot_db))
                except SnapshotStoreError:
                    raise ModelConfigurationError("The selected local snapshot archive is unavailable") from None
            service = ResearchApiService(model_factory=models[args.provider], model_factories=models, directory_loader=loader,
                provider=args.provider, data_mode=args.data, development_source=source,
                reference_date=REFERENCE_DATE if args.data == "synthetic" else None,
                market_context_loader=context_loader,
                product_data_service=ResearchDataService() if source is None else None, snapshot_store=snapshot_store)
            # A separate fixture-only service can never alter the normal service's source or budget.
            development_service = create_demo_service()
    except DataModeDependencyError:
        print("[data_dependency_missing] AKShare可选依赖未安装；先运行 python -m pip install -e '.[data]'。未读取模型配置或启动服务，不自动切换来源。", file=sys.stderr)
        return 2
    except ImportError:
        print("Install API dependencies first: python -m pip install -e '.[api,model]'", file=sys.stderr)
        return 2
    except (ModelConfigurationError, DataSourceError, DataValidationError, InvalidRequestError):
        if service is not None:
            service.close()
        if snapshot_store is not None:
            snapshot_store.close()
        print("[configuration_error] 服务端模型配置、本地目录或夹具不可用；不要发送密钥。", file=sys.stderr)
        print("正常模式不会自动退回测试数据。请检查 .env 与证券目录；需要离线演示时显式使用 --demo。", file=sys.stderr)
        return 2
    print("仅监听127.0.0.1、单进程；不下单，不保存聊天文件，不开放公网。")
    print("固定离线脚本 / 人工行情，不是自由问答模型。" if args.demo else
          f"正常服务默认模型：{args.provider}；发送研究问题后调用所选提供方。")
    if not args.demo:
        label = {"synthetic": "人工行情 / 非真实", "tencent": "腾讯不复权历史日线",
                 "akshare_eastmoney": "AKShare / 东方财富不复权历史日线"}[args.data]
        print(f"行情来源：{label}；由本次启动固定，失败不自动换源，对话输入不能切换。")
        print("网页 Dev Mode 可切换到独立的离线测试会话；不会调用真实模型或行情，也不会更改正常服务来源。")
    print("证券状态与日历仅为本地快照背景；未知不猜测、不据此下单，不自动刷新，也不发送给模型。")
    print(f"研究界面：http://127.0.0.1:{args.port}/；API文档：/docs；访问日志关闭，请不要共享会话访问令牌。")
    try:
        app = (create_app(service=service) if development_service is None
               else create_app(service=service, development_service=development_service))
        uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    finally:
        service.close()
        if development_service is not None:
            development_service.close()
        if snapshot_store is not None:
            snapshot_store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
