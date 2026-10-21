"""Per-session pending identity selection, not general chat memory or authentication."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import re
from threading import Lock
import unicodedata

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.research_intent import PreparedResearchInput, ResearchConditions
from trading_companion.security_directory import Security, SecurityDirectory, SecurityMatch
from trading_companion.tool_model import MAX_USER_CHARACTERS


PENDING_LIFETIME = timedelta(minutes=15)


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

    @property
    def expires_at(self) -> datetime:
        return self.created_at + PENDING_LIFETIME


class ResearchSession:
    """One trusted application-owned object per conversation; no process-global pending task."""

    def __init__(self):
        self._pending: PendingResearch | None = None
        self._turn_lock = Lock()

    @property
    def pending(self) -> PendingResearch | None:
        return self._pending

    def clear(self) -> None:
        self._pending = None

    def remember(self, prepared: PreparedResearchInput, *, now: datetime,
                 development_source: CsvDailyBarSource | None) -> bool:
        if not (prepared.status == "needs_clarification" and prepared.clarification_resumable
                and prepared.conditions is not None and prepared.request is None):
            return False
        self._pending = PendingResearch(prepared.conditions, prepared.candidates, now, development_source)
        return True

    def to_dict(self) -> dict[str, object]:
        pending = self._pending
        if pending is None:
            return {"state": "idle", "pending": None}
        return {"state": "awaiting_instrument", "pending": {
            "conditions": pending.conditions.to_dict(),
            "candidates": [item.to_dict() for item in pending.candidates[:5]],
            "candidate_count": len(pending.candidates),
            "candidates_truncated": len(pending.candidates) > 5,
            "created_at": pending.created_at.isoformat(), "expires_at": pending.expires_at.isoformat(),
        }}

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
            self.clear()
            return replace(result, status="needs_clarification", code="clarification_expired",
                           message="待澄清请求已过期或时间异常，请重新描述完整研究问题；没有查询行情。")
        if source != pending.conditions.source or development_source != pending.development_source:
            self.clear()
            return replace(result, code="clarification_configuration_changed",
                           message="研究来源配置已改变，旧请求已清除；请重新描述完整问题。")
        try:
            directory.assert_usable(reference_date, development=development_source is not None)
        except DataValidationError:
            self.clear()
            return replace(result, code="directory_unusable",
                           message="当前证券目录不可用，旧请求已清除；请显式刷新目录后重新提问。")
        result = replace(result, conditions=pending.conditions, candidates=pending.candidates,
                         applied_defaults=pending.conditions.applied_defaults)
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
            self.clear()
            return replace(result, code="clarification_directory_changed",
                           message="目录中的证券名称已改变，旧请求已清除；请重新确认完整研究问题。")
        try:
            if source != "csv" and pending.conditions.as_of_date >= reference_date:
                raise InvalidRequestError("Cutoff no longer precedes the current Shanghai date")
            request = pending.conditions.for_security(security.instrument_id)
        except InvalidRequestError:
            self.clear()
            return replace(result, status="needs_clarification", code="invalid_research_parameters",
                           message="该证券或日期不符合当前研究来源限制，旧请求已清除；不会自动换源。")
        self.clear()
        return replace(result, status="ready", code="clarification_resolved", request=request, security=security,
                       candidates=(security,), message="股票已确认；沿用并展示此前日期、指标和来源，执行前已重新校验。")
