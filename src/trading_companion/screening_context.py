"""Immutable, short-lived displayed shortlists and deterministic identity selection."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
import unicodedata

from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.screening import ScreeningRequest, ScreeningResult
from trading_companion.security_directory import Security, SecurityDirectory


LIFETIME = timedelta(minutes=15)
_NUMBER = r"[0-9一二两三四五六七八九十]+"
_ORDINAL = re.compile(r"第\s*(" + _NUMBER + r")\s*(?:只|个)?")
_LEADING = re.compile(r"^(?:(?:请|帮我|麻烦|请你)\s*)*(?:(?:分析|研究|比较|看看|看一下|看|选择|就选|选|把)\s*|"
                      r"按\s*(?:基本面|技术面|估值|新闻|公告|市盈率|PE|PB)\s*(?:分析|研究|比较)\s*)?", re.I)
_LIST = re.compile(r"(?:(?:刚才|刚刚|上次|本次|这次)的?)?(?:筛选(?:结果|名单)?|候选名单|名单)(?:中|里)?的?\s*")
_REFERENCE = re.compile(_ORDINAL.pattern + r"|前\s*" + _NUMBER + r"\s*只")
_LIST_REFERENCE = re.compile(r"筛选名单|候选名单|名单中|名单里|筛选结果(?:中|里)")
_PRONOUN = re.compile(r"它们|它|这两只|这三只|这只|这些|全部")
_TAIL_REFERENCE = re.compile(r"(?<!其)它们?|这两只|这三只|这只|这些|全部")
_EXTRA_SUBJECT = re.compile(
    r"(?:并且|然后|以及|同时|另外|再|还|并|和|与|及|[,，;；])\s*"
    r"(?:分析|研究|比较|看看|看一下|看|介绍)"
    r"(?!\s*(?:[其这它的]|基本面|技术面|技术指标|新闻|公告|财报|财务|营收|利润|风险|走势|趋势|日线|周线|量价|公司背景|业务))")
_JOIN = re.compile(r"\s*(?:和|与|及|、|,|，)\s*")
_RESEARCH_CLAUSE = re.compile(
    r"只|仅|不要|不看|无需|不需要|未来|接下来|后续|最近|近期|截至|截止|[0-9]{4}[-年]|"
    r"(?:分析|研究|比较|看看|看一下|看)?(?:基本面|技术面|技术指标|新闻|公告|财报|财务|营收|利润|"
    r"风险|其它风险|其他风险|走势|趋势|日线|周线|量价|SMA|EMA|RSI|MACD|BOLL|ATR|ADX|OBV|VWMA)", re.I)


def _reference_boundary(text, end, *, allow_join=False):
    """A stock reference must end before a qualifier, not inside '第一个交易日'."""
    tail = text[end:].lstrip()
    return (not tail or re.match(r"[。.!！?？,，;；:：]|的|呢|股票|个股|证券|公司", tail) is not None
            or _RESEARCH_CLAUSE.match(tail) is not None
            or allow_join and _JOIN.match(tail) is not None)


def _additional_reference(text):
    return (_LIST_REFERENCE.search(text) is not None or any(
        _stock_ordinal(text, match) for match in _REFERENCE.finditer(text)))


def _stock_ordinal(text, match):
    return match is not None and (match.group().rstrip().endswith("只")
                                  or _reference_boundary(text, match.end(), allow_join=True))


def _count(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", text):
        left, right = text.split("十")
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return 0


@dataclass(frozen=True)
class SavedScreening:
    saved_at: datetime
    request: ScreeningRequest
    securities: tuple[Security, ...]
    quote_source: object = field(repr=False, compare=False)
    research_source: str = "tencent"
    is_fixture: bool = False

    def __post_init__(self):
        if (type(self.saved_at) is not datetime or self.saved_at.tzinfo is None or self.saved_at.utcoffset() is None
                or type(self.request) is not ScreeningRequest or self.request.limit > 20
                or type(self.securities) is not tuple or len(self.securities) > self.request.limit
                or any(type(item) is not Security for item in self.securities)
                or len({item.instrument_id for item in self.securities}) != len(self.securities)
                or self.quote_source is None or self.research_source not in ("tencent", "akshare_eastmoney", "csv")
                or type(self.is_fixture) is not bool):
            raise DataValidationError("Invalid saved screening context")

    @property
    def expires_at(self):
        return self.saved_at + LIFETIME

    @classmethod
    def checked(cls, result, *, directory, quote_source, now, research_source="tencent"):
        if type(result) is not ScreeningResult or type(directory) is not SecurityDirectory:
            raise DataValidationError("Only checked screening results can create a shortlist")
        securities = []
        for index, row in enumerate(result.rows, 1):
            match = directory.lookup(row["instrument_id"])
            if (row["sample_position"] != index or match.status != "resolved"
                    or match.candidates[0].name != row["name"]):
                raise DataValidationError("Displayed shortlist identity or order changed")
            securities.append(match.candidates[0])
        if result.source["data_authenticity"] != ("synthetic" if directory.is_fixture else "vendor_snapshot"):
            raise DataValidationError("Shortlist authenticity does not match its identity directory")
        return cls(now, result.request, tuple(securities), quote_source, research_source, directory.is_fixture)

    def invalid_reason(self, *, directory, quote_source, source, development, now):
        if not self.saved_at <= now < self.expires_at:
            return "screening_list_expired", "筛选名单已超过15分钟或时间异常，请重新筛选；未查询研究数据。"
        if (quote_source is not self.quote_source or source != self.research_source
                or development != self.is_fixture or directory.is_fixture != self.is_fixture):
            return "screening_list_configuration_changed", "数据来源或模式已变化，请重新筛选；未沿用旧名单。"
        try:
            directory.assert_usable(now.astimezone(timezone(timedelta(hours=8))).date(), development=development)
            for security in self.securities:
                match = directory.lookup(security.instrument_id)
                if match.status != "resolved" or match.candidates[0] != security:
                    return "screening_list_identity_changed", "名单证券在当前目录中已变化，请重新筛选；未查询研究数据。"
        except (DataValidationError, InvalidRequestError):
            return "screening_list_directory_unusable", "当前证券目录不可用，请先恢复目录再重新筛选；未查询研究数据。"
        return None


@dataclass(frozen=True)
class ScreeningSelection:
    code: str
    message: str
    securities: tuple[Security, ...] = ()
    effective_question: str = ""
    invalidate: bool = False

    @property
    def ready(self):
        return bool(self.securities)


def is_screening_followup(question, *, session, directory, now=None, quote_source=None, source=None, development=False):
    """Routing is only a hint; all authorization is rechecked inside the controlled turn."""
    if type(question) is not str:
        return False
    text = unicodedata.normalize("NFKC", question).strip()
    start = _LEADING.match(text).end()
    marker = _LIST.match(text, start)
    position = marker.end() if marker else start
    reference = _REFERENCE.match(text, position)
    if _LIST_REFERENCE.search(text) or _stock_ordinal(text, reference):
        return True
    saved = session.screening_saved
    if saved is None:
        return False
    # An unqualified pronoun follows the current research, not an older shortlist.
    pronoun = _PRONOUN.match(text, position)
    if (session.product_saved is None and session.confirmed is None and pronoun is not None
            and _reference_boundary(text, pronoun.end())):
        return True
    # A complete named question can independently authorize ordinary research even
    # when an older shortlist is unusable. Ordinal references cannot do that.
    if now is not None and saved.invalid_reason(directory=directory, quote_source=quote_source,
                                                source=source, development=development, now=now):
        return False
    spans = directory.mentioned_spans(text)
    if not spans:
        return False
    selected_ids = {item.instrument_id for item in saved.securities}
    return all((match := directory.lookup(text[start:end])).status == "resolved"
               and match.candidates[0].instrument_id in selected_ids for start, end in spans)


def select_screening(question, *, saved, directory, quote_source, source, development, now):
    """Replace only a fully consumed identity expression; preserve every research qualifier."""
    if saved is None:
        return ScreeningSelection("screening_list_unavailable", "当前没有可用的筛选名单，请先筛选，或直接提供股票名称研究。")
    reason = saved.invalid_reason(directory=directory, quote_source=quote_source, source=source,
                                  development=development, now=now)
    if reason:
        return ScreeningSelection(*reason, invalidate=True)
    if not saved.securities:
        return ScreeningSelection("screening_list_empty", "最近一次筛选没有可展示候选，请修改完整筛选条件后重试。")
    if (type(question) is not str or not 0 < len(question) <= 2000
            or any(not char.isprintable() and char not in "\n\r\t" for char in question)):
        return ScreeningSelection("screening_selection_invalid", "请明确选择名单内的股票，并保留完整研究要求。")
    text = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！?？").strip()
    start = _LEADING.match(text).end()
    marker = _LIST.match(text, start)
    position = marker.end() if marker else start
    securities = []
    error = ""
    prefix = re.match(r"前\s*(" + _NUMBER + r")\s*只", text[position:])
    pronoun = _PRONOUN.match(text, position)
    if prefix:
        count = _count(prefix[1])
        if not 1 <= count <= len(saved.securities):
            error = "选择的序号超出本次已展示名单。"
        else:
            securities = list(saved.securities[:count])
        position += prefix.end()
    elif pronoun:
        token = pronoun.group()
        if token in ("它", "这只"):
            if len(saved.securities) == 1:
                securities = list(saved.securities)
            else:
                error = "名单中有多只股票，请明确名称或序号，不默认选择第一只。"
        elif token in ("这两只", "这三只") and len(saved.securities) != {"这两只": 2, "这三只": 3}[token]:
            error = "请明确是哪几只，例如“前两只”或“第1和第3只”。"
        else:
            securities = list(saved.securities)
        position = pronoun.end()
    else:
        while True:
            ordinal = _ORDINAL.match(text, position)
            if ordinal:
                index = _count(ordinal[1])
                if not 1 <= index <= len(saved.securities):
                    error = "选择的序号超出本次已展示名单。"
                    break
                security = saved.securities[index - 1]
                position = ordinal.end()
            else:
                spans = [(left, right) for left, right in directory.mentioned_spans(text) if left == position]
                if not spans:
                    error = "没有完整识别名单选择，请使用名称、代码或明确序号。"
                    break
                _, end = max(spans, key=lambda span: span[1])
                match = directory.lookup(text[position:end])
                if match.status != "resolved" or match.candidates[0] not in saved.securities:
                    error = "选择的股票不在本次展示名单中，或名称尚不明确；请提供完整研究问题。"
                    break
                security, position = match.candidates[0], end
            securities.append(security)
            join = _JOIN.match(text, position)
            if not join:
                break
            # A comma before a research clause is punctuation, not a second identity.
            next_position = join.end()
            follows_identity = (_ORDINAL.match(text, next_position) or any(
                left == next_position for left, _ in directory.mentioned_spans(text)))
            if not follows_identity and join.group().strip() in (",", "，"):
                if not _RESEARCH_CLAUSE.match(text, next_position):
                    error = "逗号后的要求没有完整识别，请明确名单对象和研究范围。"
                break
            position = next_position
    tail = text[position:]
    if (not error and (_additional_reference(tail) or _TAIL_REFERENCE.search(tail) or _EXTRA_SUBJECT.search(tail)
                       or directory.mentioned_spans(tail) or re.match(r"\s*(?:和|与|或|及|、)", tail)
                       or re.search(r"(?:其它|其他)(?:股票|公司|证券)", tail))):
        error = "选择表达存在未处理的对象或冲突，请明确名称或序号后重述完整要求。"
    if not error and len(securities) > 3:
        error = "每次最多研究三只股票，请明确选择最多三只，不会自动截断名单。"
    if not error and len(set(securities)) != len(securities):
        error = "选择中有重复股票，请明确不同的名单对象。"
    if error or not securities:
        return ScreeningSelection("screening_selection_needs_clarification", error or "请明确名单对象。")
    introduction = re.sub(r"(?:选择|就选|选|把)\s*$", "", text[:start])
    effective = introduction + "、".join(item.instrument_id for item in securities) + tail
    if not re.match(r"(?:请|帮我|麻烦|请你)*(?:分析|研究|比较|看看|看一下|看|按)", effective):
        effective = "分析" + effective
    return ScreeningSelection("screening_selection_ready", "已核对筛选名单中的研究对象。", tuple(securities), effective)
