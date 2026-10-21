"""Bounded single-turn orchestration; checked facts and unchecked drafts stay separate."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import json
import re
import unicodedata

from trading_companion.agent_input import (
    InputIssue, parse_research_question, proposal_matches_request, request_arguments,
)
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.multi_contracts import MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_tool import (
    RESEARCH_TOOL_DEFINITION, TOOL_NAME, ResearchToolExecutor, ToolExecutionError,
)
from trading_companion.research_intent import PreparedResearchInput, prepare_research_question
from trading_companion.research_session import ResearchSession, selection_match
from trading_companion.security_directory import SecurityDirectory
from trading_companion.tool_model import MAX_USER_CHARACTERS, ToolCapableModel, ToolMessage, ToolModelResponse


@dataclass(frozen=True)
class AgentTurnResult:
    status: str = "failed"
    code: str = "not_started"
    message: str = "本轮尚未执行。"
    stage: str = "input"
    confirmed_request: MultiIndicatorRequest | None = field(default=None, repr=False)
    research_result: MultiIndicatorResult | None = field(default=None, repr=False)
    model_draft: str | None = field(default=None, repr=False)
    model_responses: tuple[ToolModelResponse, ...] = field(default=(), repr=False)
    model_calls_attempted: int = 0
    analyses_used: int = 0
    answer_validation: str = "not_performed"
    failure_code: str | None = None
    input_mode: str = "explicit"
    understanding: PreparedResearchInput | None = field(default=None, repr=False)
    session_event: str | None = None
    session_state: dict[str, object] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, object]:
        """Export explicitly; drafts and evidence are not sanitized production logs."""
        metadata = []
        for response in self.model_responses:
            payload = response.to_dict()
            metadata.append({key: payload[key] for key in (
                "status", "provider", "requested_model", "model", "is_mock", "usage",
            )})
        return {
            "status": self.status, "code": self.code, "message": self.message, "stage": self.stage,
            "confirmed_parameters": request_arguments(self.confirmed_request) if self.confirmed_request else None,
            "research_result": self.research_result.to_dict() if self.research_result else None,
            "model_draft": self.model_draft, "answer_validation": self.answer_validation,
            "model_responses": metadata, "model_calls_attempted": self.model_calls_attempted,
            "analyses_used": self.analyses_used, "failure_code": self.failure_code,
            "input_mode": self.input_mode,
            "understanding": self.understanding.to_dict() if self.understanding else None,
            "session_event": self.session_event, "session_state": self.session_state,
        }


def _finish(outcome: AgentTurnResult, status: str, code: str, message: str) -> AgentTurnResult:
    return replace(outcome, status=status, code=code, message=message)


def _has_research(outcome: AgentTurnResult) -> bool:
    result = outcome.research_result
    return result is not None and result.data_status == "ok" and result.synthesis_allowed


def _model_failure(outcome: AgentTurnResult, code: str) -> AgentTurnResult:
    outcome = replace(outcome, failure_code=code)
    if _has_research(outcome):
        return _finish(outcome, "limited", "explanation_unavailable",
                       "研究事实已保留，但本轮没有获得完整模型解释；可查看结构化结果，未重新取数。")
    return _finish(outcome, "failed", "model_unavailable", "本轮未能获得可用模型响应，没有生成行情分析。")


def _check_configuration(source: str, development_source: CsvDailyBarSource | None) -> None:
    if source not in ("tencent", "akshare_eastmoney"):
        raise ModelConfigurationError("Choose an explicit supported online source")
    if development_source is not None and (type(development_source) is not CsvDailyBarSource
                                           or type(development_source.is_synthetic) is not bool):
        raise ModelConfigurationError("Development CSV must be configured by trusted application code")


def run_research_turn(question: str, *, model: ToolCapableModel, source: str = "tencent",
                      development_source: CsvDailyBarSource | None = None) -> AgentTurnResult:
    """Preserve the explicit-format entry and its two-model-call upper bound."""
    _check_configuration(source, development_source)
    outcome = AgentTurnResult()
    try:
        request = parse_research_question(question, source="csv" if development_source else source)
    except InputIssue as issue:
        return _finish(outcome, issue.status, issue.code, str(issue))
    return _run_request(request, model=model, development_source=development_source, outcome=outcome)


def run_natural_research_turn(question: str, *, model: ToolCapableModel, directory: SecurityDirectory,
                              source: str = "tencent", development_source: CsvDailyBarSource | None = None,
                              reference_date: date | None = None, session: ResearchSession | None = None,
                              current_time: datetime | None = None) -> AgentTurnResult:
    """Keep stateless behavior; an optional application-owned session enables identity selection."""
    _check_configuration(source, development_source)
    if session is not None:
        if type(session) is not ResearchSession or type(directory) is not SecurityDirectory:
            raise ModelConfigurationError("Use a trusted per-conversation research session and directory")
        now = current_time if current_time is not None else datetime.now(timezone.utc)
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise ModelConfigurationError("Session time must be an aware trusted timestamp")
        reference = reference_date if reference_date is not None else now.astimezone(timezone(timedelta(hours=8))).date()
        if type(reference) is not date:
            raise ModelConfigurationError("Session reference date must be a trusted date")
        if not session._turn_lock.acquire(blocking=False):
            return AgentTurnResult(status="needs_clarification", code="session_busy", stage="clarification",
                                   message="本会话正在处理另一条消息，请等待该次处理结束；没有重复执行。",
                                   input_mode="natural", session_event="busy", session_state={"state": "busy"})
        try:
            return _run_session_turn(question, session=session, model=model, directory=directory, source=source,
                                     development_source=development_source, reference=reference, now=now)
        finally:
            session._turn_lock.release()
    prepared = prepare_research_question(
        question, model=model, directory=directory,
        source="csv" if development_source is not None else source,
        reference_date=reference_date, development=development_source is not None,
    )
    return _run_prepared(prepared, model=model, development_source=development_source)


def _run_prepared(prepared: PreparedResearchInput, *, model: ToolCapableModel,
                  development_source: CsvDailyBarSource | None) -> AgentTurnResult:
    """Use the same proposal binding and executor for initial and resumed requests."""
    outcome = AgentTurnResult(
        input_mode="natural", understanding=prepared, stage=prepared.stage,
        confirmed_request=prepared.request, model_calls_attempted=prepared.model_calls_attempted,
        model_responses=(prepared.model_response,) if prepared.model_response is not None else (),
        failure_code=prepared.failure_code,
    )
    if prepared.status != "ready":
        return _finish(outcome, prepared.status, prepared.code, prepared.message)
    return _run_request(prepared.request, model=model, development_source=development_source, outcome=outcome)


def _run_session_turn(question: str, *, session: ResearchSession, model: ToolCapableModel,
                      directory: SecurityDirectory, source: str,
                      development_source: CsvDailyBarSource | None, reference: date,
                      now: datetime) -> AgentTurnResult:
    def attach(outcome: AgentTurnResult, event: str) -> AgentTurnResult:
        return replace(outcome, session_event=event, session_state=session.to_dict())

    def reply(status: str, code: str, message: str, event: str) -> AgentTurnResult:
        return attach(AgentTurnResult(status=status, code=code, message=message,
                                      stage="clarification", input_mode="natural"), event)

    if type(question) is not str or not question.strip() or len(question) > MAX_USER_CHARACTERS:
        return reply("needs_clarification", "invalid_input", "请用不超过2000字符描述研究问题或选股回复。", "unchanged")
    command = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！").strip()
    if command in ("取消", "取消分析", "取消研究", "算了", "新对话", "重新开始", "清空会话"):
        session.clear()
        reset = command in ("新对话", "重新开始", "清空会话")
        return reply("cancelled", "session_reset" if reset else "research_cancelled",
                     "待澄清请求已清除；没有调用模型或查询行情。", "reset" if reset else "cancelled")
    new_question = re.fullmatch(r"新问题\s*[:：](.*)", question.strip(), re.DOTALL)
    if new_question:
        session.clear()
        question = new_question[1].strip()
    elif session.pending is not None:
        prepared = session.resume(question, directory=directory,
                                  source="csv" if development_source is not None else source,
                                  development_source=development_source, reference_date=reference, now=now)
        event = ("clarification_resumed" if prepared.status == "ready" else
                 "clarification_pending" if session.pending is not None else "clarification_invalidated")
        # resume() has already consumed a valid selection, even if downstream execution fails.
        return attach(_run_prepared(prepared, model=model, development_source=development_source), event)
    elif selection_match(question, directory).status != "not_found":
        return reply("needs_clarification", "no_pending_clarification",
                     "当前没有待补全的选股请求；请描述完整研究问题，例如“分析中国平安最近走势”。", "no_pending")
    prepared = prepare_research_question(
        question, model=model, directory=directory,
        source="csv" if development_source is not None else source,
        reference_date=reference, development=development_source is not None,
    )
    remembered = session.remember(prepared, now=now, development_source=development_source)
    outcome = _run_prepared(prepared, model=model, development_source=development_source)
    if remembered:
        outcome = replace(outcome, message=outcome.message + " 可直接回复完整名称或代码；请求15分钟内有效，也可回复“取消”。")
    return attach(outcome, "clarification_started" if remembered else "new_request")


def _run_request(request: MultiIndicatorRequest, *, model: ToolCapableModel,
                 development_source: CsvDailyBarSource | None, outcome: AgentTurnResult) -> AgentTurnResult:
    """Share execution policy; never reset an earlier interpretation's call count."""
    outcome = replace(outcome, confirmed_request=request, stage="proposal")
    messages = [
        ToolMessage("system", "You are a read-only A-share research assistant. The application has parsed "
                    "one explicit request, shown in the next user message. Propose exactly one "
                    "analyze_daily_indicators call with those parameters; never change stock, dates, "
                    "source or indicators. Do not pass paths, URLs or request IDs. After the result, "
                    "write a brief Chinese explanation grounded only in its facts and limitations. "
                    "Do not propose another call, invent unavailable values, infer tradability or "
                    "claim predictive certainty. Your explanation is an unchecked draft."),
        ToolMessage("user", json.dumps(request_arguments(request), separators=(",", ":"))),
    ]
    tools = (RESEARCH_TOOL_DEFINITION,)
    try:
        outcome = replace(outcome, model_calls_attempted=outcome.model_calls_attempted + 1)
        proposal = model.complete(messages, tools=tools)
        outcome = replace(outcome, model_responses=(*outcome.model_responses, proposal))
        if proposal.status != "tool_calls":
            if proposal.status == "complete":
                return _finish(outcome, "failed", "tool_call_not_proposed",
                               "模型没有提出研究调用；没有取得行情，不把直接生成的文字当作分析。")
            return _model_failure(outcome, "proposal_" + proposal.status)
        if len(proposal.tool_calls) != 1:
            return _finish(outcome, "failed", "multiple_calls_not_allowed", "本轮只允许一次分析；没有执行多调用批次。")
        call = proposal.tool_calls[0]
        if call.name != TOOL_NAME or not proposal_matches_request(call, request):
            return _finish(outcome, "failed", "proposal_request_mismatch",
                           "模型提议与已识别请求不一致，已阻止查询；没有擅自修改股票、日期、指标或来源。")

        outcome = replace(outcome, stage="execution")
        executor = ResearchToolExecutor(development_source=development_source)
        try:
            execution = executor.execute(proposal, messages=messages)
        finally:
            outcome = replace(outcome, analyses_used=executor.analyses_used)
        outcome = replace(outcome, research_result=execution.full_result)
        if execution.status != "executed":
            if _has_research(outcome):
                return _finish(outcome, "limited", "tool_view_unavailable",
                               "计算结果已保留，但工具视图未能完整返回；未截断证据或重新取数。")
            return _finish(outcome, "failed", "tool_execution_failed", "本轮工具未能完成有效研究，没有生成行情判断。")
        result = execution.full_result
        if result is None or result.data_status in ("source_error", "invalid_data"):
            return _finish(outcome, "failed", "research_data_failed", "本次数据读取或校验失败，没有用其他来源或旧结果补齐。")
        if not _has_research(outcome):
            return _finish(outcome, "limited", "research_not_ready",
                           "本次无匹配日线或没有足够的可解释指标；保留实际结果，不推断停牌原因或编造缺值。")

        outcome = replace(outcome, stage="explanation", model_calls_attempted=outcome.model_calls_attempted + 1)
        final = model.complete([*messages, proposal.as_message(), *execution.messages], tools=tools)
        outcome = replace(outcome, model_responses=(*outcome.model_responses, final))
        if final.status != "complete":
            return _model_failure(outcome, "unexpected_followup_call" if final.status == "tool_calls"
                                  else "final_" + final.status)
        outcome = replace(outcome, model_draft=final.text)
        if result.calculation_status != "complete":
            return _finish(outcome, "limited", "partial_research",
                           "研究已完成，部分指标字段不可用；有效事实与缺项均保留，模型解释仅为未校验草稿。")
        return _finish(outcome, "completed", "research_completed",
                       "研究计算及模型往返已完成；模型解释仍是未校验草稿，不代表回答质量已验收。")
    except ModelCallError as error:
        return _model_failure(outcome, error.code)
    except ModelRequestError:
        return _model_failure(outcome, "model_request_invalid")
    except ModelConfigurationError:
        return _model_failure(outcome, "model_configuration_error")
    except ToolExecutionError:
        return _finish(outcome, "failed", "tool_execution_failed", "本轮工具执行失败；不会自动重试或重置额度。")
