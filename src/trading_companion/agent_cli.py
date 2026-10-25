"""Explicitly enabled terminal research; never print unchecked model drafts."""

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Callable

from trading_companion.agent import AgentTurnResult, run_natural_research_turn
from trading_companion.api_service import DATA_MODES, DataModeDependencyError, ensure_data_mode_dependencies
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.development_fixtures import FIXTURES, REFERENCE_DATE, ROOT, load_development_fixtures
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.local_env import load_model_environment
from trading_companion.research_report import indicator_label
from trading_companion.research_session import ResearchSession
from trading_companion.security_directory import SecurityDirectory, load_security_directory
from trading_companion.tool_model import MAX_USER_CHARACTERS, ToolCapableModel
from trading_companion.turn_control import TurnControl, workflow_turn_control


MAX_TURNS = 5
SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_LABELS = {
    "cutoff:previous_shanghai_day": "截止日取日期锚点的前一自然日，不推断交易日",
    "range:365_calendar_days": "默认区间为365个自然日",
    "relative_unit:30_days_per_month_365_per_year": "相对区间按每月30天、每年365天解释",
    "indicators:default_eight": "未指定指标，采用默认8项组合（7类指标）",
    "indicators:inherited_then_edited": "沿用原指标组合，仅替换本轮明确指定的一项",
    "indicators:inherited_then_updated": "沿用原组合，仅添加或删除本轮明确的一项指标",
    "dates:explicit_update": "使用本轮明确指定的日期变化，未指定的日期边界沿用原值",
}


@dataclass(frozen=True)
class ConversationData:
    mode: str
    directory_path: Path | None = None
    development_source: CsvDailyBarSource | None = None

    def __post_init__(self):
        if self.mode == "synthetic":
            source = self.development_source
            if (self.directory_path is not None or type(source) is not CsvDailyBarSource
                    or source.is_synthetic is not True or source.path != ROOT / "data" / FIXTURES[0][0]):
                raise ModelConfigurationError("Synthetic chat requires the pinned CSV and fixture directory")
        elif self.mode not in ("tencent", "akshare_eastmoney") or self.development_source is not None or not isinstance(self.directory_path, Path):
            raise ModelConfigurationError("Online chat requires a selected local directory without a CSV source")
        ensure_data_mode_dependencies(self.mode)

    @classmethod
    def from_options(cls, mode: str, directory_path: str | None = None):
        if mode == "synthetic" and directory_path is None:
            source, _ = load_development_fixtures()
            return cls(mode, development_source=source)
        if mode in ("tencent", "akshare_eastmoney"):
            return cls(mode, Path(directory_path) if directory_path else ROOT / "data/security_directory_live.json")
        raise ModelConfigurationError("Choose an explicit supported data mode")

    def load_directory(self, now: datetime) -> SecurityDirectory:
        """Re-read current local authority; never refresh over the network."""
        if self.mode == "synthetic":
            _, directory = load_development_fixtures()
        else:
            directory = load_security_directory(self.directory_path)
            directory.assert_usable(now.astimezone(SHANGHAI).date(), development=False)
        return directory


class _ConversationModel:
    """Consume a process-local call allowance before delegation, including failed calls."""

    def __init__(self, delegate: ToolCapableModel, limit: int):
        self.delegate = delegate
        self.limit = limit
        self.forwarded = 0
        self.blocked = 0

    def complete(self, messages, *, tools):
        if self.forwarded >= self.limit:
            self.blocked += 1
            raise ModelCallError("conversation_call_limit", "Terminal model-call allowance exhausted")
        self.forwarded += 1
        return self.delegate.complete(messages, tools=tools)


def _help(write: Callable[[str], None]) -> None:
    write("命令：/help 帮助；/last 查看上次成功结果；/reset 清除会话；/exit 退出。命令不调用模型。")
    write("支持完整单股票日线问题，以及待澄清时回复完整股票名称或代码。")
    write("已有研究后可明确替换一项指标，例如“把SMA3改成5日，其他不变”；将重新查询，15分钟内有效。")
    write("支持“这里的RSI为什么偏高”等已保存结果追问，最多1次模型调用、0次取数；“查看上次结果”不调用模型。")
    write("支持“RSI是什么”“解释一下MACD里的DIF和DEA”等指标定义，不调用模型或行情。")
    write("支持“换成中国平安”“把日期改成2026-09-21至2026-09-24”“添加ATR2”“删除RSI2”，其他条件保留并重新计算。")
    write("每条只修改一类条件；添加须明确参数，删除含糊时先澄清；换股票有歧义时回复完整名称或代码。")
    write("“改成5日”有多个指标候选时先澄清；只回复原指标标签（例如SMA3）可继续原修改。")
    write("支持“把日期改成最近一个月”“看最新的”；月按30自然日，最新仅截止上海前一自然日，不是实时行情；人工模式只重跑样例。")
    write("来源由启动参数--data固定，对话不能切源，失败不自动换源；需要换源请结束并按新配置重新启动。")
    write("暂不支持复合条件修改及开放知识问答；请用完整新问题。")
    write("等待股票选择时，如需改条件，请输入“新问题：”加完整问题；也可输入“取消”。")


def _display_turn(result: AgentTurnResult, write: Callable[[str], None]) -> None:
    """Only application text and validated relations belong in the terminal display."""
    write(f"[{result.status}/{result.code}] {result.message}")
    prepared = result.understanding
    if prepared is not None:
        if result.confirmed_request is not None and prepared.security is not None:
            write(f"已确认股票：{prepared.security.name}（{prepared.security.instrument_id}）。")
        conditions = prepared.conditions
        if conditions is not None:
            write(f"已校验条件：{conditions.start_date} 至 {conditions.as_of_date}；指标："
                  + "、".join(indicator_label(spec) for spec in conditions.indicators) + "。")
            for default in conditions.applied_defaults:
                if default in DEFAULT_LABELS:
                    write("默认/日期口径：" + DEFAULT_LABELS[default] + "。")
        if result.status == "needs_clarification" and prepared.candidates:
            write("候选：" + "；".join(item.name + "（" + item.instrument_id + "）"
                                   for item in prepared.candidates[:5]))
            if len(prepared.candidates) > 5:
                write("仅显示前5项，请用完整名称或代码明确股票，不支持按序号选择。")
    conversation = result.conversation_answer if result.workflow_trace is not None else None
    answer = "\n\n".join(conversation["paragraphs"]) if conversation is not None else result.display_answer
    if answer is not None:
        write(answer)
    write(f"本轮：模型尝试{result.model_calls_attempted}次，分析{result.analyses_used}次。")
    if result.runtime is not None:
        runtime = result.runtime
        write(f"运行控制：{runtime['elapsed_ms']}毫秒；已观察token累计{runtime['observed_tokens']}；"
              + ("用量完整一致。" if runtime["usage_complete_for_attempts"] else "用量缺失或不完整，不能推算完整成本。"))


def run_conversation(*, model: ToolCapableModel, data: ConversationData, provider: str = "deepseek",
                     read_line: Callable[[str], str] | None = None,
                     write_line: Callable[[str], None] | None = None,
                     clock: Callable[[], datetime] | None = None, workflow: bool = False) -> int:
    """Own one identity/edit session, not a transcript or general conversation memory."""
    if type(workflow) is not bool:
        raise ModelConfigurationError("Workflow selection must be a trusted boolean")
    read = input if read_line is None else read_line
    write = print if write_line is None else write_line
    now = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    session = ResearchSession()
    meter = _ConversationModel(model, MAX_TURNS * 3)
    turns = analyses = 0
    in_flight = False
    active_control = None
    exit_code = 0
    write("Trading Companion：只读单股票日线研究；不下单、不管理资金、不承诺收益。")
    if provider == "fake":
        write(("离线固定脚本演示" if data.mode == "synthetic" else "固定模型脚本，行情来源仍以所选模式为准")
              + "，不是语言模型，不接受自由输入，也不代表真实模型评估。")
    else:
        write("已显式选择DeepSeek；每条研究输入可能产生付费模型请求，不自动重试。")
        write("本轮问题、校验后的参数及紧凑指标证据会发送到DeepSeek；不发送完整聊天历史，密钥只用于接口认证。")
    if data.mode == "synthetic":
        write("人工样例 / 非真实行情。平安银行仅5条日线、目录4项身份；日期锚点固定为2026-09-26。")
        write("输入示例：分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2。")
    else:
        label = "腾讯" if data.mode == "tencent" else "AKShare / 东方财富"
        write(f"行情：{label}不复权历史日线；目录仅从本地读取并在每条研究输入前重查，不自动刷新。")
        write("输入示例：分析平安银行最近一个月的走势，关注SMA3和RSI2。")
    write(f"本次启动最多处理{MAX_TURNS}条非空问题，最多转发{MAX_TURNS * 3}次模型调用、执行{MAX_TURNS * (2 if workflow else 1)}次分析；"
          "空行和本地命令不占问题次数。")
    if workflow:
        write("每轮共享90秒协作截止时间、最多6次模型/6次工具/2次分析，以及24,000已观察token停止阈值；不是硬中断或费用上限。")
        write("整次启动的15次模型总额仍生效；先达到的上限会停止继续调用，不保证每条问题都能用满单轮额度。")
    else:
        write("每轮共享90秒协作截止时间、最多3次模型/1次分析，以及24,000已观察token停止阈值；不是硬中断或费用上限。")
    write("同步等待期间可按Ctrl+C退出；本终端没有后台/cancel命令，迟到响应不得保存为会话结果。")
    _help(write)
    try:
        while turns < MAX_TURNS:
            try:
                question = read("你> ")
            except EOFError:
                write("输入结束，退出会话。")
                break
            command = question.strip()
            if not command:
                continue
            if command == "/exit":
                break
            if command == "/help":
                _help(write)
                continue
            if command == "/reset":
                session.clear()
                write("待澄清请求及已保存研究条件已清除；结果快照也已清除；没有调用模型或查询行情，额度不重置。")
                continue
            if command == "/last":
                question = "查看上次结果"
            elif command.startswith("/"):
                write("未知本地命令；输入/help查看支持的命令，没有发送给模型。")
                continue
            else:
                turns += 1
            if len(question) > MAX_USER_CHARACTERS:
                write("问题超过2000字符，没有调用模型；本条已计入输入次数。")
                continue
            timestamp = now()
            try:
                directory = data.load_directory(timestamp)
            except (DataSourceError, DataValidationError, InvalidRequestError):
                write("[directory_unusable] 本地目录或人工夹具不可用；已停止。请显式修复或刷新后重启，不自动取新目录。")
                exit_code = 1
                break
            in_flight = True
            active_control = workflow_turn_control() if workflow else TurnControl()
            result = run_natural_research_turn(
                question, model=meter, directory=directory, session=session,
                source="tencent" if data.mode == "synthetic" else data.mode,
                development_source=data.development_source, current_time=timestamp,
                reference_date=REFERENCE_DATE if data.mode == "synthetic" else None,
                control=active_control, workflow=workflow,
            )
            in_flight = False
            active_control = None
            analyses += result.analyses_used
            _display_turn(result, write)
            if meter.blocked:
                write("模型调用上限已触发，停止本次会话；没有转发超限请求。")
                exit_code = 1
                break
        else:
            write("已达到本次启动的问题次数上限，会话结束。")
    except KeyboardInterrupt:
        if active_control is not None:
            active_control.cancel()
        write("\n收到中断，会话结束；不自动重试或续跑。")
        exit_code = 130
    finally:
        session.clear()
        write(f"会话统计：已计入{turns}条问题，转发{meter.forwarded}次模型调用，已确认{analyses}次分析。")
        if in_flight:
            write("被中断一轮的分析次数尚未回传，可能已取数；上述分析计数仅含已返回的轮次。")
        write("待选状态及研究条件已清除；结果快照也已清除；本入口不保存聊天、模型草稿或报告文件。")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only terminal research; no arguments only show help.")
    entry = parser.add_mutually_exclusive_group()
    entry.add_argument("--demo", action="store_true", help="Run two fixed offline turns, not free-form fake chat")
    entry.add_argument("--provider", choices=("deepseek",), help="Explicitly enable potentially paid model calls")
    parser.add_argument("--data", choices=DATA_MODES,
                        help="Startup-pinned mode; AKShare requires python -m pip install -e '.[data]'")
    parser.add_argument("--directory", help="Trusted local snapshot; Tencent/AKShare modes only, never auto-refreshed")
    args = parser.parse_args(argv)
    if args.demo and (args.data is not None or args.directory is not None):
        parser.error("--demo uses fixed artificial inputs; do not supply data or directory options")
    if args.provider is None and not args.demo:
        if args.data is not None or args.directory is not None:
            parser.error("Select --demo or explicitly select --provider deepseek")
        parser.print_help()
        return 0
    if args.provider and args.data is None:
        parser.error("Explicitly choose --data synthetic, --data tencent or --data akshare_eastmoney")
    if args.data == "synthetic" and args.directory is not None:
        parser.error("Synthetic mode uses a pinned fixture directory only")
    try:
        data = ConversationData.from_options("synthetic" if args.demo else args.data, args.directory)
        data.load_directory(datetime.now(timezone.utc))
        if args.demo:
            from trading_companion.session_agent_demo import build_demo_model
            questions = iter(("看看平安最近一个月的走势，只看SMA3和RSI2", "我指的是平安银行", "/exit"))

            def read_demo(prompt):
                question = next(questions)
                print(prompt + question)
                return question

            return run_conversation(model=build_demo_model(), data=data, provider="fake", read_line=read_demo)
        from trading_companion.research_response import RESPONSE_OUTPUT_TOKENS
        config = replace(DeepSeekConfig.from_env(load_model_environment()), max_output_tokens=RESPONSE_OUTPUT_TOKENS)
        model = DeepSeekToolModel(config)
    except DataModeDependencyError:
        print("[data_dependency_missing] AKShare可选依赖未安装；先运行 python -m pip install -e '.[data]'。未读取模型配置或查询行情，不自动切换来源。", file=sys.stderr)
        return 2
    except ModelConfigurationError:
        print("[model_configuration_error] 模型配置缺失或无效；检查项目根目录.env或当前终端的模型名和密钥，不要发送密钥。", file=sys.stderr)
        return 2
    except (DataSourceError, DataValidationError, InvalidRequestError):
        print("[directory_unusable] 本地目录或人工夹具不可用；未读取模型配置或发起网络请求。", file=sys.stderr)
        return 1
    return run_conversation(model=model, data=data, workflow=True)


if __name__ == "__main__":
    raise SystemExit(main())
