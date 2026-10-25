"""Per-session identity, edit bases and detached results; not transcript memory."""

from dataclasses import dataclass, field, replace
from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from threading import Lock
import unicodedata

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.agent_input import InputIssue, request_arguments
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_memory import SavedResearch, ResultLookup
from trading_companion.research_report import build_research_report
from trading_companion.research_edit import (
    indicator_selection, indicator_selection_label, replace_indicator, window_edit_candidates,
)
from trading_companion.research_intent import PreparedResearchInput, ResearchConditions
from trading_companion.research_objective import parse_outlook_expression
from trading_companion.research_update import parse_condition_update, update_conditions
from trading_companion.security_directory import Security, SecurityDirectory, SecurityMatch
from trading_companion.tool_model import MAX_USER_CHARACTERS


PENDING_LIFETIME = timedelta(minutes=15)
CONFIRMED_LIFETIME = timedelta(minutes=15)


def selection_match(reply: str, directory: SecurityDirectory) -> SecurityMatch:
    """Consume the whole short reply; never extract a name and discard extra clauses."""
    text = unicodedata.normalize("NFKC", reply).strip().rstrip("。.!！").strip()
    if not text or len(text) > 80 or any(not character.isprintable() for character in text):
        return SecurityMatch("not_found", "not_a_short_selection", ())
    direct = directory.lookup(text)
    if direct.status != "not_found":
        return direct
    wrapper = re.fullmatch(r"(?:我指的是|我说的是|就选|选择|选|是)\s*(.+)", text)
    return directory.lookup(wrapper[1]) if wrapper else direct


@dataclass(frozen=True)
class PendingResearch:
    conditions: ResearchConditions
    candidates: tuple[Security, ...]
    created_at: datetime
    development_source: CsvDailyBarSource | None = field(default=None, repr=False)
    user_question: str = field(default="", repr=False)

    @property
    def expires_at(self) -> datetime:
        return self.created_at + PENDING_LIFETIME


@dataclass(frozen=True)
class ConfirmedResearch:
    request: MultiIndicatorRequest
    conditions: ResearchConditions
    security: Security
    confirmed_at: datetime
    development_source: CsvDailyBarSource | None = field(default=None, repr=False)

    @property
    def expires_at(self) -> datetime:
        return self.confirmed_at + CONFIRMED_LIFETIME


@dataclass(frozen=True)
class PendingIndicatorEdit:
    request: MultiIndicatorRequest
    security: Security
    window: int
    candidates: tuple[IndicatorSpec, ...]
    created_at: datetime
    development_source: CsvDailyBarSource | None = field(default=None, repr=False)

    @property
    def expires_at(self) -> datetime:
        return self.created_at + PENDING_LIFETIME


class ResearchSession:
    """One trusted application-owned object per conversation; no process-global pending task."""

    def __init__(self):
        self._pending: PendingResearch | None = None
        self._pending_edit: PendingIndicatorEdit | None = None
        self._confirmed: ConfirmedResearch | None = None
        self._last_result: SavedResearch | None = None
        self._result_current = False
        self._turn_lock = Lock()
        self._turn_parent = None

    def _fork_for_turn(self):
        """Stage state privately while the original session lock is held by the Agent."""
        working = ResearchSession()
        # Conditions are immutable; saved-result access already returns detached copies.
        working._pending, working._confirmed, working._last_result = self._pending, self._confirmed, self._last_result
        working._pending_edit = self._pending_edit
        working._result_current = self._result_current
        working._turn_parent = self
        return working

    def _publish_turn(self, working):
        """Publish bounded local references atomically with respect to turn cancellation."""
        self._pending, self._confirmed, self._last_result = working._pending, working._confirmed, working._last_result
        self._pending_edit = working._pending_edit
        self._result_current = working._result_current

    @property
    def pending(self) -> PendingResearch | None:
        return self._pending

    @property
    def pending_edit(self) -> PendingIndicatorEdit | None:
        return self._pending_edit

    @property
    def confirmed(self) -> ConfirmedResearch | None:
        return self._confirmed

    def clear(self) -> None:
        self.clear_pending()
        self.clear_confirmed()
        self.clear_saved_result()
        if self._turn_parent is not None:
            self._turn_parent.clear()

    def clear_saved_result(self) -> None:
        self._last_result = None
        self._result_current = False

    def begin_research(self) -> None:
        """Old facts remain explicitly viewable, but cannot answer an implicit new-result question."""
        self.clear_pending()
        self.clear_confirmed()
        self._result_current = False
        if self._turn_parent is not None:
            # Consumption is immediate, but newly produced state remains staged until finish().
            self._turn_parent.clear_pending()
            self._turn_parent.begin_research()

    def remember_result(self, prepared: PreparedResearchInput, result: MultiIndicatorResult, *, now: datetime,
                        development_source: CsvDailyBarSource | None) -> bool:
        if prepared.request is None or prepared.security is None:
            return False
        snapshot = SavedResearch.checked(result, request=prepared.request, security=prepared.security,
                                         now=now, development_source=development_source)
        if snapshot is None:
            return False
        self._last_result, self._result_current = snapshot, True
        return True

    def saved_result(self, *, directory: SecurityDirectory, source: str,
                     development_source: CsvDailyBarSource | None, reference_date: date,
                     now: datetime, allow_history: bool = False) -> ResultLookup:
        saved = self._last_result
        if saved is None:
            return ResultLookup("needs_clarification", "no_saved_result", "当前没有可查看的可信研究结果，请先提供完整研究问题。")
        def invalid(code, message):
            if self._confirmed is not None and self._confirmed.request.request_id == saved.request.request_id:
                self.clear_confirmed()
            self.clear_saved_result()
            return ResultLookup("needs_clarification", code, message)
        if not saved.saved_at <= now < saved.expires_at:
            return invalid("saved_result_expired", "保存结果已超过15分钟或时间异常，已清除；请重新研究，未自动取数。")
        if source != saved.request.source or development_source != saved.development_source:
            return invalid("saved_result_configuration_changed", "当前数据配置与原结果不同，旧结果已清除；请重新研究。")
        try:
            directory.assert_usable(reference_date, development=development_source is not None)
            current = directory.lookup(saved.security.instrument_id)
            if current.status != "resolved" or current.candidates[0].name != saved.security.name:
                return invalid("saved_result_identity_changed", "当前目录中原证券已消失或更名，旧结果已清除；请重新确认。")
            if build_research_report(saved.result, expected_request=saved.request).status not in ("complete", "limited"):
                return invalid("saved_result_integrity_failed", "保存结果未通过当前检查，已清除；请重新研究。")
        except DataValidationError:
            return invalid("directory_unusable", "当前目录不可用；旧结果已清除，不自动刷新或查询行情。")
        if not self._result_current and not allow_history:
            return ResultLookup("needs_clarification", "no_current_result",
                "最近的新研究未取得可信结果，不将旧结果当作本轮结果；可明确输入“查看上次结果”，或重新研究。")
        return ResultLookup("ready", "saved_result_ready", "已校验保存结果；未重新取数。", deepcopy(saved), self._result_current)

    def clear_pending(self) -> None:
        self._pending = None
        self._pending_edit = None

    def clear_confirmed(self) -> None:
        self._confirmed = None
        self._pending_edit = None

    def remember_confirmed(self, prepared: PreparedResearchInput, *, now: datetime,
                           development_source: CsvDailyBarSource | None) -> bool:
        """The orchestrator calls this only after this run's fact report passes integrity checks."""
        if (prepared.status != "ready" or prepared.request is None or prepared.conditions is None
                or prepared.security is None):
            return False
        self._confirmed = ConfirmedResearch(prepared.request, prepared.conditions, prepared.security,
                                            now, development_source)
        return True

    def remember(self, prepared: PreparedResearchInput, *, now: datetime,
                 development_source: CsvDailyBarSource | None) -> bool:
        if not (prepared.status == "needs_clarification" and prepared.clarification_resumable
                and prepared.conditions is not None and prepared.request is None):
            return False
        self._pending_edit = None
        self._pending = PendingResearch(prepared.conditions, prepared.candidates, now, development_source,
                                        prepared.user_question)
        return True

    def to_dict(self) -> dict[str, object]:
        pending = self._pending
        confirmed = self._confirmed
        base = None if confirmed is None else {
            "parameters": request_arguments(confirmed.request), "request_id": confirmed.request.request_id,
            "security": confirmed.security.to_dict(), "conditions": confirmed.conditions.to_dict(),
            "confirmed_at": confirmed.confirmed_at.isoformat(), "expires_at": confirmed.expires_at.isoformat(),
        }
        memory = None if self._last_result is None else self._last_result.metadata(current=self._result_current)
        if self._pending_edit is not None:
            edit = self._pending_edit
            return {"state": "awaiting_indicator", "confirmed": base, "saved_result": memory, "pending": {
                "kind": "indicator_edit", "target_window": edit.window,
                "base_request_id": edit.request.request_id,
                "candidates": [indicator_selection_label(spec) for spec in edit.candidates],
                "created_at": edit.created_at.isoformat(), "expires_at": edit.expires_at.isoformat(),
            }}
        if pending is None:
            return {"state": "idle", "pending": None, "confirmed": base, "saved_result": memory}
        return {"state": "awaiting_instrument", "confirmed": base, "saved_result": memory, "pending": {
            "conditions": pending.conditions.to_dict(),
            "candidates": [item.to_dict() for item in pending.candidates[:5]],
            "candidate_count": len(pending.candidates),
            "candidates_truncated": len(pending.candidates) > 5,
            "created_at": pending.created_at.isoformat(), "expires_at": pending.expires_at.isoformat(),
        }}

    def _checked_edit_base(self, *, directory: SecurityDirectory, source: str,
                           development_source: CsvDailyBarSource | None, reference_date: date,
                           now: datetime) -> PreparedResearchInput:
        """Share current-authority checks across every supported condition mutation."""
        saved = self._confirmed
        result = PreparedResearchInput(intent="research", stage="parameters", directory=directory)
        if saved is None:
            return replace(result, status="needs_clarification", code="no_confirmed_research",
                           message="当前没有可继承的已验证研究条件，请先提供完整研究问题；没有查询行情。")
        if not saved.confirmed_at <= now < saved.expires_at:
            self.clear_confirmed()
            return replace(result, status="needs_clarification", code="research_context_expired",
                           message="研究条件已超过15分钟或时间异常，旧基准已清除；请提供完整研究问题。")
        if source != saved.request.source or development_source != saved.development_source:
            self.clear_confirmed()
            return replace(result, code="research_context_configuration_changed",
                           message="来源配置已变化，旧基准已清除；请提供完整研究问题，不自动换源。")
        try:
            directory.assert_usable(reference_date, development=development_source is not None)
            current = directory.lookup(saved.security.instrument_id)
            if (current.status != "resolved" or current.candidates[0].name != saved.security.name):
                self.clear_confirmed()
                return replace(result, code="research_context_identity_changed",
                               message="目录中的原证券已消失或更名，旧基准已清除；请重新确认完整研究问题。")
            if source != "csv" and saved.request.as_of_date >= reference_date:
                raise InvalidRequestError("Cutoff must still precede the current Shanghai date")
            # Reconstruct both contracts, rather than trusting a previously accepted object forever.
            conditions = ResearchConditions(saved.conditions.start_date, saved.conditions.as_of_date,
                saved.conditions.indicators, source, reference_date, saved.conditions.applied_defaults,
                saved.conditions.objective)
            conditions.for_security(saved.security.instrument_id)
            return replace(result, status="ready", code="checked_edit_base", request=saved.request,
                           conditions=saved.conditions, security=current.candidates[0],
                           applied_defaults=saved.conditions.applied_defaults)
        except InputIssue as issue:
            return replace(result, status=issue.status, code=issue.code, message=str(issue))
        except DataValidationError:
            self.clear_confirmed()
            return replace(result, code="directory_unusable",
                           message="当前证券目录不可用，旧基准已清除；请显式刷新后提供完整问题。")
        except InvalidRequestError:
            self.clear_confirmed()
            return replace(result, status="needs_clarification", code="invalid_research_context",
                           message="原条件不符合当前研究限制，旧基准已清除；不会改日期或换源。")

    def prepare_outlook(self, expression: str, *, question: str, directory: SecurityDirectory, source: str,
                        development_source: CsvDailyBarSource | None, reference_date: date,
                        now: datetime) -> PreparedResearchInput:
        """Revalidate inherited identity, then create a new research request, not a saved answer."""
        base = self._checked_edit_base(directory=directory, source=source, development_source=development_source,
                                       reference_date=reference_date, now=now)
        if base.status != "ready":
            return replace(base, user_question=question)
        try:
            objective = parse_outlook_expression(expression)
            conditions = replace(base.conditions, objective=objective, reference_date=reference_date)
            request = conditions.for_security(base.security.instrument_id)
            return replace(base, code="contextual_outlook_ready", conditions=conditions, request=request,
                           candidates=(base.security,), user_question=question,
                           message="沿用已确认股票及历史数据范围，重新取数并分析指定期限的条件情景；不查询未来行情。")
        except InputIssue as issue:
            return replace(base, status=issue.status, code=issue.code, request=None,
                           user_question=question, message=str(issue))

    def prepare_indicator_edit(self, question: str, *, directory: SecurityDirectory, source: str,
                               development_source: CsvDailyBarSource | None, reference_date: date,
                               now: datetime) -> PreparedResearchInput:
        """Resolve bounded edits without guessing a target or extending the trusted base."""
        base = self._checked_edit_base(directory=directory, source=source, development_source=development_source,
                                       reference_date=reference_date, now=now)
        if base.status != "ready":
            return base
        try:
            window_edit = window_edit_candidates(base.request, question)
            if window_edit is not None:
                window, candidates = window_edit
                if not candidates:
                    raise InputIssue("needs_clarification", "indicator_edit_target_missing",
                        "原组合没有可单独修改窗口的指标；MACD需要明确完整参数，没有查询行情。")
                if len(candidates) > 1:
                    self._pending = None
                    self._pending_edit = PendingIndicatorEdit(base.request, base.security, window, candidates,
                                                              now, development_source)
                    labels = "、".join(indicator_selection_label(spec) for spec in candidates)
                    return replace(base, status="needs_clarification", code="indicator_edit_ambiguous", request=None,
                        message=f"要把哪一个指标改成{window}日？请只回复一个原指标：{labels}。"
                                "原股票、日期和来源保持不变；待澄清请求15分钟内有效，也可回复“取消”。没有查询行情。")
                question = f"把{indicator_selection_label(candidates[0])}改成{window}日"
            request = replace_indicator(base.request, question)
            self._pending_edit = None
            return self._edited_input(base, request)
        except InputIssue as issue:
            return replace(base, status=issue.status, code=issue.code, request=None, message=str(issue))

    @staticmethod
    def _edited_input(base: PreparedResearchInput, request: MultiIndicatorRequest) -> PreparedResearchInput:
        defaults = tuple(item for item in base.applied_defaults if item not in (
            "indicators:default_eight", "indicators:inherited_then_updated"))
        defaults = tuple(dict.fromkeys((*defaults, "indicators:inherited_then_edited")))
        conditions = replace(base.conditions, indicators=request.indicators, applied_defaults=defaults)
        return replace(base, code="indicator_edit_ready", request=request,
            conditions=conditions, applied_defaults=defaults,
            message="仅修改已指定指标；股票、原起止日期及来源保持不变，将重新计算，不复用旧结果。")

    def resume_indicator_edit(self, reply: str, *, directory: SecurityDirectory, source: str,
                              development_source: CsvDailyBarSource | None, reference_date: date,
                              now: datetime) -> PreparedResearchInput:
        """Bind a short selection to the original edit and recheck its full authority."""
        pending = self._pending_edit
        result = PreparedResearchInput(intent="research", stage="clarification", directory=directory)
        if pending is None:
            return replace(result, status="needs_clarification", code="no_pending_indicator_edit",
                message="当前没有待补全的指标修改；请提供完整修改句，没有查询行情。")
        if not pending.created_at <= now < pending.expires_at:
            self.clear_pending()
            return replace(result, status="needs_clarification", code="indicator_edit_expired",
                message="待澄清指标修改已超过15分钟或时间异常，已清除；请重新描述完整研究问题。")
        base = self._checked_edit_base(directory=directory, source=source, development_source=development_source,
                                       reference_date=reference_date, now=now)
        if base.status != "ready":
            self.clear_pending()
            return base
        if (base.request != pending.request or base.security != pending.security
                or development_source != pending.development_source):
            self.clear_pending()
            return replace(base, status="needs_clarification", code="indicator_edit_context_changed", request=None,
                message="原研究基准、证券身份或数据配置已变化，待修改请求已清除；请重新描述完整问题。")
        selected = indicator_selection(reply)
        if selected not in pending.candidates:
            return replace(base, status="needs_clarification", code="indicator_edit_reply_not_specific", request=None,
                message=f"仍待确认要把哪个原指标改成{pending.window}日。请只回复"
                        + "、".join(indicator_selection_label(spec) for spec in pending.candidates)
                        + "中的一个；不会忽略额外条件或延长原请求有效期，也可回复“取消”。")
        # Consume the selection before any later model or data side effect.
        self.clear_pending()
        try:
            request = replace_indicator(base.request,
                f"把{indicator_selection_label(selected)}改成{pending.window}日")
            return self._edited_input(base, request)
        except InputIssue as issue:
            return replace(base, status=issue.status, code=issue.code, request=None, message=str(issue))

    def prepare_condition_update(self, question: str, *, directory: SecurityDirectory, source: str,
                                 development_source: CsvDailyBarSource | None, reference_date: date,
                                 now: datetime) -> PreparedResearchInput:
        """Merge one explicit change; unresolved stock identities use the existing pending flow."""
        base = self._checked_edit_base(directory=directory, source=source, development_source=development_source,
                                       reference_date=reference_date, now=now)
        if base.status != "ready":
            return base
        try:
            update = parse_condition_update(question)
            conditions = update_conditions(base.conditions, update,
                instrument_id=base.security.instrument_id, reference_date=reference_date)
            security = base.security
            if update.kind == "instrument":
                match = directory.lookup(update.value)
                if match.status != "resolved":
                    return replace(base, status="needs_clarification", code="condition_update_instrument_unresolved",
                        request=None, security=None, candidates=match.candidates, conditions=conditions,
                        clarification_resumable=True,
                        message="新股票尚未确认；已保留原日期、指标和来源，请回复候选的完整名称或代码。"
                                "没有候选时请补充完整名称或代码；也可回复“取消”，没有查询行情。")
                security = match.candidates[0]
                if security.instrument_id == base.security.instrument_id:
                    raise InputIssue("needs_clarification", "condition_update_unchanged", "研究股票没有变化，未重复查询。")
            request = conditions.for_security(security.instrument_id)
            code = "condition_update_ready"
            message = "已修改本轮明确的一类条件，其他条件保留；将使用新请求重新计算，不复用旧结果。"
            if update.kind == "relative_range":
                code = "relative_range_ready"
                message = "按固定自然日规则修改区间（月按30天、年按365天），截止上海前一自然日；股票、指标和来源不变，将重新计算。"
            elif update.kind == "refresh":
                code = "refresh_latest_ready"
                message = "已保留股票、指标和原自然日区间长度，截止上海前一自然日；这是历史日线刷新，不是实时行情，也不保证前一天为交易日。"
                if "refresh:same_dates" in conditions.applied_defaults:
                    message += "区间未变化，按明确刷新重新计算，不代表发现了新行情。"
                if development_source is not None and development_source.is_synthetic:
                    message += "当前为固定参考日期的合成样例，刷新只重跑样例，不查询真实市场。"
            message += f"研究区间：{conditions.start_date.isoformat()}至{conditions.as_of_date.isoformat()}。"
            return replace(base, code=code, request=request, security=security,
                candidates=(security,), conditions=conditions, applied_defaults=conditions.applied_defaults,
                message=message)
        except InputIssue as issue:
            return replace(base, status=issue.status, code=issue.code, request=None, message=str(issue))
        except (InvalidRequestError, DataValidationError):
            return replace(base, status="needs_clarification", code="invalid_condition_update", request=None,
                           message="修改后的证券或条件不符合当前数据源限制；没有换源、修改原基准或查询行情。")

    def resume(self, reply: str, *, directory: SecurityDirectory, source: str,
               development_source: CsvDailyBarSource | None, reference_date: date,
               now: datetime) -> PreparedResearchInput:
        """Revalidate current authority; consume the pending task before any model/price call."""
        pending = self._pending
        result = PreparedResearchInput(intent="research", stage="clarification", directory=directory)
        if pending is None:
            return replace(result, status="needs_clarification", code="no_pending_clarification",
                           message="当前没有待补全的选股请求，请提供完整研究问题。")
        if not pending.created_at <= now < pending.expires_at:
            self.clear_pending()
            return replace(result, status="needs_clarification", code="clarification_expired",
                           message="待澄清请求已过期或时间异常，请重新描述完整研究问题；没有查询行情。")
        if source != pending.conditions.source or development_source != pending.development_source:
            self.clear_pending()
            return replace(result, code="clarification_configuration_changed",
                           message="研究来源配置已改变，旧请求已清除；请重新描述完整问题。")
        try:
            directory.assert_usable(reference_date, development=development_source is not None)
        except DataValidationError:
            self.clear_pending()
            return replace(result, code="directory_unusable",
                           message="当前证券目录不可用，旧请求已清除；请显式刷新目录后重新提问。")
        result = replace(result, conditions=pending.conditions, candidates=pending.candidates,
                         applied_defaults=pending.conditions.applied_defaults, user_question=pending.user_question)
        if type(reply) is not str or not reply.strip() or len(reply) > MAX_USER_CHARACTERS:
            return replace(result, status="needs_clarification", code="invalid_input",
                           clarification_resumable=True,
                           message="请回复完整股票名称或代码，或取消当前请求。")
        match = selection_match(reply, directory)
        if match.status != "resolved":
            return replace(result, status="needs_clarification", code="clarification_reply_not_specific",
                           clarification_resumable=True,
                           message="请只回复完整股票名称或代码；额外条件不会被忽略。若要改日期、指标或开始新研究，"
                                   "请用“新问题：”给出完整问题，也可回复“取消”。")
        security = match.candidates[0]
        previous = {item.instrument_id: item for item in pending.candidates}
        if previous and security.instrument_id not in previous:
            return replace(result, status="needs_clarification", code="candidate_selection_mismatch",
                           clarification_resumable=True,
                           message="该证券不在原候选中，未将旧条件应用到另一只股票；请选原候选，或取消后重新提问。")
        if previous and previous[security.instrument_id].name != security.name:
            self.clear_pending()
            return replace(result, code="clarification_directory_changed",
                           message="目录中的证券名称已改变，旧请求已清除；请重新确认完整研究问题。")
        try:
            if source != "csv" and pending.conditions.as_of_date >= reference_date:
                raise InvalidRequestError("Cutoff no longer precedes the current Shanghai date")
            request = pending.conditions.for_security(security.instrument_id)
        except InvalidRequestError:
            self.clear_pending()
            return replace(result, status="needs_clarification", code="invalid_research_parameters",
                           message="该证券或日期不符合当前研究来源限制，旧请求已清除；不会自动换源。")
        self.clear_pending()
        return replace(result, status="ready", code="clarification_resolved", request=request, security=security,
                       candidates=(security,), message="股票已确认；沿用并展示此前日期、指标和来源，执行前已重新校验。")
