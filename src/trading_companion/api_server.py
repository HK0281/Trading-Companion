"""Explicit local-server launcher; no arguments show help and never read model credentials."""

import argparse
from datetime import timedelta, timezone
from pathlib import Path
import sys

from trading_companion.model import ModelConfigurationError
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.api_service import DATA_MODES, DataModeDependencyError, ensure_data_mode_dependencies


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Single-worker loopback research API, not a public deployment.")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--demo", action="store_true", help="Fixed offline fake model and pinned synthetic data")
    entry.add_argument("--provider", choices=("deepseek",), help="Explicitly enable potentially paid model calls")
    parser.add_argument("--data", choices=DATA_MODES,
                        help="Startup-pinned source; AKShare requires python -m pip install -e '.[data]'")
    parser.add_argument("--directory", help="Existing local security snapshot; Tencent/AKShare modes only")
    parser.add_argument("--market-context", help="Optional trusted local market-context JSON; never refreshed online")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535")
    if not args.demo and args.provider is None:
        if args.data or args.directory or args.market_context:
            parser.error("Select --demo or --provider deepseek first")
        parser.print_help()
        return 0
    if args.demo and (args.data or args.directory or args.market_context):
        parser.error("Demo uses pinned fixtures; do not specify data, directory or market context")
    if args.provider and args.data is None:
        parser.error("Explicitly choose --data synthetic, --data tencent or --data akshare_eastmoney")
    if args.data == "synthetic" and args.directory:
        parser.error("Synthetic data does not accept a directory path")
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
            from trading_companion.deepseek_model import DeepSeekConfig
            from trading_companion.deepseek_tool_model import DeepSeekToolModel
            from trading_companion.security_directory import load_security_directory
            from datetime import datetime
            if args.data == "synthetic":
                source, _ = load_development_fixtures()
                loader = lambda _: load_development_fixtures()[1]
            else:
                source = None
                directory_path = Path(args.directory) if args.directory else ROOT / "data/security_directory_live.json"
                def loader(now):
                    directory = load_security_directory(directory_path)
                    directory.assert_usable(now.astimezone(timezone(timedelta(hours=8))).date(), development=False)
                    return directory
                loader(datetime.now(timezone.utc))
            config = DeepSeekConfig.from_env()
            if args.market_context:
                context_path = Path(args.market_context)
                context_loader = lambda _: load_market_context(context_path)
            elif source is not None:
                context_loader = lambda _: load_development_market_context()
            else:
                context_loader = None
            service = ResearchApiService(model_factory=lambda: DeepSeekToolModel(config), directory_loader=loader,
                provider="deepseek", data_mode=args.data, development_source=source,
                reference_date=REFERENCE_DATE if args.data == "synthetic" else None,
                market_context_loader=context_loader)
    except DataModeDependencyError:
        print("[data_dependency_missing] AKShare可选依赖未安装；先运行 python -m pip install -e '.[data]'。未读取模型配置或启动服务，不自动切换来源。", file=sys.stderr)
        return 2
    except ImportError:
        print("Install API dependencies first: python -m pip install -e '.[api,model]'", file=sys.stderr)
        return 2
    except (ModelConfigurationError, DataSourceError, DataValidationError, InvalidRequestError):
        print("[configuration_error] 服务端模型配置、本地目录或夹具不可用；不要发送密钥。", file=sys.stderr)
        return 2
    print("仅监听127.0.0.1、单进程；不下单，不保存聊天文件，不开放公网。")
    print("固定离线脚本 / 人工行情，不是自由问答模型。" if args.demo else
          "已显式启用DeepSeek，可能计费；本轮问题、参数和指标证据会发送给该提供方。")
    if not args.demo:
        label = {"synthetic": "人工行情 / 非真实", "tencent": "腾讯不复权历史日线",
                 "akshare_eastmoney": "AKShare / 东方财富不复权历史日线"}[args.data]
        print(f"行情来源：{label}；由本次启动固定，失败不自动换源，对话输入不能切换。")
    print("证券状态与日历仅为本地快照背景；未知不猜测、不据此下单，不自动刷新，也不发送给模型。")
    print(f"研究界面：http://127.0.0.1:{args.port}/；API文档：/docs；访问日志关闭，请不要共享会话访问令牌。")
    uvicorn.run(create_app(service=service), host="127.0.0.1", port=args.port, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
