"""Bounded research orchestration with fact reports and checked relation explanations."""

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
    RESEARCH_TOOL_DEFINITION, TOOL_NAME, ResearchToolExecutor, ToolExecution, ToolExecutionError,
)
from trading_companion.relation_explanation import (
    EXPLANATION_INSTRUCTION, ExplanationValidation, validate_indicator_explanation,
)
from trading_companion.research_intent import PreparedResearchInput, prepare_research_question
from trading_companion.research_edit import is_indicator_edit
from trading_companion.research_update import is_condition_update
from trading_companion.indicator_knowledge import KnowledgeAnswer, parse_knowledge_question
from trading_companion.research_followup import FollowupAnswer, answer_saved_question, is_followup_question, is_result_view
from trading_companion.research_session import ResearchSession, selection_match
from trading_companion.research_report import ResearchReport, build_research_report
from trading_companion.security_directory import SecurityDirectory
from trading_companion.tool_model import MAX_USER_CHARACTERS, ToolCapableModel, ToolMessage, ToolModelResponse
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn, checkpoint


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
    _explanation_execution: ToolExecution | None = field(default=None, repr=False)
    knowledge_answer: KnowledgeAnswer | None = field(default=None, repr=False)
    followup_answer: FollowupAnswer | None = field(default=None, repr=False)
    runtime: dict[str, object] | None = field(default=None, repr=False)

    @property
    def safe_answer(self) -> ResearchReport | None:
        """User-facing facts are independent of the unchecked model draft."""
        if self.research_result is None:
            return None
        return build_research_report(self.research_result, expected_request=self.confirmed_request)

    @property
    def explanation_validation(self) -> ExplanationValidation:
        """Recheck the current context; a stored success must not outlive its evidence."""
        if self.model_draft is None or self._explanation_execution is None:
            return ExplanationValidation()
        execution = replace(self._explanation_execution, full_result=self.research_result)
        return validate_indicator_explanation(self.model_draft, execution=execution,
                                              expected_request=self.confirmed_request)

    @property
    def display_answer(self) -> str | None:
        """Default display never includes the model's original text or rejected claims."""
        if self.knowledge_answer is not None:
            return self.knowledge_answer.text
        if self.followup_answer is not None:
            return self.followup_answer.text
        report = self.safe_answer
        if report is None:
            return None
        validation = self.explanation_validation
        if validation.status == "accepted" and report.status in ("complete", "limited"):
            return report.text + "\n\n" + validation.text
        if report.status in ("complete", "limited"):
            return report.text + "\n\n本轮未取得通过检查的指标解读，仅展示事实摘要。"
        return report.text

    def to_dict(self) -> dict[str, object]:
        """Export explicitly; drafts and evidence are not sanitized production logs."""
        metadata = []
        for response in self.model_responses:
            payload = response.to_dict()
            metadata.append({key: payload[key] for key in (
                "status", "provider", "requested_model", "model", "is_mock", "usage",
            )})
        report = self.safe_answer
        return {
            "status": self.status, "code": self.code, "message": self.message, "stage": self.stage,
            "confirmed_parameters": request_arguments(self.confirmed_request) if self.confirmed_request else None,
            "research_result": self.research_result.to_dict() if self.research_result else None,
            "model_draft": self.model_draft, "answer_validation": self.answer_validation,
            "answer_validation_scope": "free_text_semantics_not_performed",
            "model_draft_policy": "debug_only_unchecked_not_for_default_display",
            "safe_answer": report.to_dict() if report is not None else None,
            "explanation_validation": self.explanation_validation.to_dict(),
            "display_answer": self.display_answer,
            "model_responses": metadata, "model_calls_attempted": self.model_calls_attempted,
            "analyses_used": self.analyses_used, "failure_code": self.failure_code,
            "input_mode": self.input_mode,
            "understanding": self.understanding.to_dict() if self.understanding else None,
            "session_event": self.session_event, "session_state": self.session_state,
            "knowledge_answer": self.knowledge_answer.to_dict() if self.knowledge_answer is not None else None,
            "followup_answer": self.followup_answer.to_dict() if self.followup_answer is not None else None,
            "runtime": self.runtime,
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


def _stopped(outcome: AgentTurnResult, code: str) -> AgentTurnResult:
    cancelled = code == "turn_cancelled"
    # Only facts accepted before the stop survive; cancelled output is not displayed.
    outcome = replace(outcome, model_draft=None, followup_answer=None, knowledge_answer=None,
                      research_result=None if cancelled else outcome.research_result, failure_code=code)
    return _finish(outcome, "cancelled" if cancelled else "limited" if _has_research(outcome) else "failed",
                   code, "本轮已取消；没有自动重试，也未保存本轮结果。" if cancelled else
                   "本轮已达到运行限制；停止后续调度，迟到内容已丢弃。仅保留停止前核验的事实，未保存为会话结果。")


def _controlled(action, *, model, control, default_calls, input_mode, session=None):
    runtime = TurnControl(TurnLimits(model_calls=default_calls)) if control is None else control
    if type(runtime) is not TurnControl:
        raise ModelConfigurationError("Turn control must be supplied by trusted application code")
    if session is not None and not session._turn_lock.acquire(blocking=False):
        return AgentTurnResult(status="needs_clarification", code="session_busy", stage="clarification",
                               message="本会话正在处理另一条消息，请等待该次处理结束；没有重复执行。",
                               input_mode="natural", session_event="busy", session_state={"state": "busy"})
    try:
        runtime.start()
        working = session._fork_for_turn() if session is not None else None

        def publish(stopped):
            if session is None:
                return
            if not stopped:
                session._publish_turn(working)

        outcome = AgentTurnResult(input_mode=input_mode)
        with bind_turn(runtime):
            try:
                checkpoint()
                outcome = action(ControlledToolModel(model, runtime), working)
            except TurnStopped as stop:
                outcome = _stopped(outcome, stop.code)
            except BaseException:
                # Ctrl+C also invalidates a consumed base, but still propagates to the UI.
                runtime.cancel()
                runtime.finish(publish)
                raise
            stop_code = runtime.finish(publish)
        if stop_code is not None:
            outcome = _stopped(outcome, stop_code)
        metadata = runtime.to_dict()
        return replace(outcome, runtime=metadata,
                       model_calls_attempted=metadata["model_calls_attempted"], analyses_used=metadata["analyses_used"],
                       session_state=session.to_dict() if session is not None else outcome.session_state,
                       session_event="turn_stopped" if session is not None and stop_code else outcome.session_event)
    finally:
        if session is not None:
            session._turn_lock.release()


def run_research_turn(question: str, *, model: ToolCapableModel, source: str = "tencent",
                      development_source: CsvDailyBarSource | None = None,
                      control: TurnControl | None = None) -> AgentTurnResult:
    """Preserve the explicit-format entry and its two-model-call upper bound."""
    _check_configuration(source, development_source)
    return _controlled(lambda checked_model, _: _run_explicit(
        question, model=checked_model, source=source, development_source=development_source),
        model=model, control=control, default_calls=2, input_mode="explicit")


def _run_explicit(question, *, model, source, development_source):
    outcome = AgentTurnResult()
    try:
        request = parse_research_question(question, source="csv" if development_source else source)
    except InputIssue as issue:
        return _finish(outcome, issue.status, issue.code, str(issue))
    return _run_request(request, model=model, development_source=development_source, outcome=outcome)


def run_natural_research_turn(question: str, *, model: ToolCapableModel, directory: SecurityDirectory,
                              source: str = "tencent", development_source: CsvDailyBarSource | None = None,
                              reference_date: date | None = None, session: ResearchSession | None = None,
                              current_time: datetime | None = None,
                              control: TurnControl | None = None) -> AgentTurnResult:
    """Route finite knowledge and optional identity/edit/result session flows."""
    _check_configuration(source, development_source)
    if session is not None and type(session) is not ResearchSession:
        raise ModelConfigurationError("Use a trusted per-conversation research session")
    return _controlled(lambda checked_model, working: _run_natural(
        question, model=checked_model, directory=directory, source=source,
        development_source=development_source, reference_date=reference_date,
        session=working, current_time=current_time),
        model=model, control=control, default_calls=3, input_mode="natural", session=session)


def _run_natural(question, *, model, directory, source, development_source, reference_date, session, current_time):
    if session is not None:
        if type(session) is not ResearchSession or type(directory) is not SecurityDirectory:
            raise ModelConfigurationError("Use a trusted per-conversation research session and directory")
        now = current_time if current_time is not None else datetime.now(timezone.utc)
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise ModelConfigurationError("Session time must be an aware trusted timestamp")
        reference = reference_date if reference_date is not None else now.astimezone(timezone(timedelta(hours=8))).date()
        if type(reference) is not date:
            raise ModelConfigurationError("Session reference date must be a trusted date")
        return _run_session_turn(question, session=session, model=model, directory=directory, source=source,
                                 development_source=development_source, reference=reference, now=now)
    if type(question) is str and 0 < len(question) <= MAX_USER_CHARACTERS:
        knowledge = parse_knowledge_question(question)
        if knowledge is not None:
            return AgentTurnResult(status="completed", code="indicator_knowledge_answered", stage="knowledge",
                                   input_mode="natural", message="仅解释项目维护的指标定义；未查询行情。", knowledge_answer=knowledge)
        if is_followup_question(question) and not is_condition_update(question):
            return AgentTurnResult(status="needs_clarification", code="no_saved_result", input_mode="natural",
                                   message="无状态入口不能查看上一轮结果，请使用同一会话并先完成研究。")
    if (type(question) is str and len(question) <= MAX_USER_CHARACTERS
            and (is_indicator_edit(question) or is_condition_update(question))):
        return AgentTurnResult(status="needs_clarification", code="no_confirmed_research", input_mode="natural",
                               message="无状态入口不能继承上一轮条件，请提供完整研究问题或使用同一会话。")
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

    def execute(prepared: PreparedResearchInput, event: str) -> AgentTurnResult:
        checkpoint()
        if prepared.status == "ready":
            # Consume the previous base before side effects, including failures and interruptions.
            session.begin_research()
        outcome = _run_prepared(prepared, model=model, development_source=development_source)
        # A stopped turn cannot stage new result or clarification state for publication.
        if outcome.failure_code in ("turn_cancelled", "turn_deadline_exceeded", "model_call_budget_exhausted",
                                    "analysis_budget_exhausted", "token_budget_exhausted"):
            return attach(outcome, "turn_stopped")
        report = outcome.safe_answer
        if _has_research(outcome) and report is not None and report.status in ("complete", "limited"):
            session.remember_confirmed(prepared, now=now, development_source=development_source)
            session.remember_result(prepared, outcome.research_result, now=now, development_source=development_source)
        return attach(outcome, event)

    if type(question) is not str or not question.strip() or len(question) > MAX_USER_CHARACTERS:
        return reply("needs_clarification", "invalid_input", "请用不超过2000字符描述研究问题或选股回复。", "unchanged")
    command = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！").strip()
    if command in ("取消", "取消分析", "取消研究", "算了", "新对话", "重新开始", "清空会话"):
        reset = command in ("新对话", "重新开始", "清空会话")
        if reset:
            session.clear()
        else:
            session.clear_pending()
        return reply("cancelled", "session_reset" if reset else "research_cancelled",
                     "会话条件已清除；没有调用模型或查询行情。" if reset else
                     "待澄清请求已清除；没有调用模型或查询行情，原有已验证研究条件不变。",
                     "reset" if reset else "cancelled")
    knowledge = parse_knowledge_question(question)
    if knowledge is not None:
        return attach(AgentTurnResult(status="completed", code="indicator_knowledge_answered", stage="knowledge",
            input_mode="natural", message="仅解释项目维护的指标定义；未查询行情。", knowledge_answer=knowledge), "knowledge")
    if is_result_view(question) or (session.pending is None and is_followup_question(question)
                                   and not is_condition_update(question)):
        lookup = session.saved_result(directory=directory, source="csv" if development_source is not None else source,
            development_source=development_source, reference_date=reference, now=now, allow_history=is_result_view(question))
        if lookup.status != "ready":
            return reply(lookup.status, lookup.code, lookup.message, "result_followup_blocked")
        run = answer_saved_question(question, snapshot=lookup.snapshot, model=model, directory=directory,
                                    historical=not lookup.is_current or is_result_view(question))
        answer = run.answer
        return attach(AgentTurnResult(status="completed" if answer.status == "accepted" else answer.status,
            code=answer.code, message="本轮只处理已保存结果；没有重新取数。", stage="followup", input_mode="natural",
            followup_answer=answer, model_calls_attempted=run.model_calls_attempted,
            model_responses=(run.model_response,) if run.model_response is not None else (), failure_code=run.failure_code),
            "result_followup")
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
        return execute(prepared, event)
    elif is_condition_update(question):
        prepared = session.prepare_condition_update(
            question, directory=directory, source="csv" if development_source is not None else source,
            development_source=development_source, reference_date=reference, now=now)
        remembered = session.remember(prepared, now=now, development_source=development_source)
        return execute(prepared, "condition_update_pending" if remembered else
                       "condition_update" if prepared.status == "ready" else "condition_update_blocked")
    elif is_indicator_edit(question):
        prepared = session.prepare_indicator_edit(
            question, directory=directory, source="csv" if development_source is not None else source,
            development_source=development_source, reference_date=reference, now=now)
        return execute(prepared, "indicator_edit" if prepared.status == "ready" else "indicator_edit_blocked")
    elif selection_match(question, directory).status != "not_found":
        return reply("needs_clarification", "no_pending_clarification",
                     "当前没有待补全的选股请求；请描述完整研究问题，例如“分析中国平安最近走势”。", "no_pending")
    prepared = prepare_research_question(
        question, model=model, directory=directory,
        source="csv" if development_source is not None else source,
        reference_date=reference, development=development_source is not None,
    )
    remembered = session.remember(prepared, now=now, development_source=development_source)
    outcome = execute(prepared, "clarification_started" if remembered else "new_request")
    if remembered:
        outcome = replace(outcome, message=outcome.message + " 可直接回复完整名称或代码；请求15分钟内有效，也可回复“取消”。")
    return outcome


def _run_request(request: MultiIndicatorRequest, *, model: ToolCapableModel,
                 development_source: CsvDailyBarSource | None, outcome: AgentTurnResult) -> AgentTurnResult:
    """Share execution policy; never reset an earlier interpretation's call count."""
    outcome = replace(outcome, confirmed_request=request, stage="proposal")
    messages = [
        ToolMessage("system", "You are a read-only A-share research assistant. The application has parsed "
                    "one explicit request, shown in the next user message. Propose exactly one "
                    "analyze_daily_indicators call with those parameters; never change stock, dates, "
                    "source or indicators. Do not pass paths, URLs or request IDs in tool arguments. "
                    + EXPLANATION_INSTRUCTION),
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
        executor = ResearchToolExecutor(development_source=development_source, request_id=request.request_id)
        try:
            execution = executor.execute(proposal, messages=messages)
        finally:
            outcome = replace(outcome, analyses_used=executor.analyses_used)
        checkpoint()
        outcome = replace(outcome, research_result=execution.full_result, _explanation_execution=execution)
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
        validation = outcome.explanation_validation
        if validation.status != "accepted":
            outcome = replace(outcome, failure_code=validation.code)
            return _finish(outcome, "limited", "partial_research" if result.calculation_status != "complete"
                           else "explanation_rejected",
                           "研究事实与缺项已保留，但模型提交的指标解读未通过本轮证据检查；仅展示事实摘要，未重试。")
        if result.calculation_status != "complete":
            return _finish(outcome, "limited", "partial_research",
                           "部分指标字段不可用；有效事实、缺项及通过检查的指标关系已保留，不生成收益预测。")
        return _finish(outcome, "completed", "research_completed",
                       "研究事实和所选指标关系已核对；展示文本由程序生成，不代表预测能力或自由文本语义已验收。")
    except TurnStopped as stop:
        return _stopped(outcome, stop.code)
    except ModelCallError as error:
        return _model_failure(outcome, error.code)
    except ModelRequestError:
        return _model_failure(outcome, "model_request_invalid")
    except ModelConfigurationError:
        return _model_failure(outcome, "model_configuration_error")
    except ToolExecutionError:
        return _finish(outcome, "failed", "tool_execution_failed", "本轮工具执行失败；不会自动重试或重置额度。")
