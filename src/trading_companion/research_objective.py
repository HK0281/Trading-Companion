"""Bounded research purposes, separate from historical market-data requests."""

from dataclasses import dataclass
import re
import unicodedata

from trading_companion.agent_input import InputIssue
from trading_companion.errors import InvalidRequestError


MAX_OUTLOOK_DAYS = 366
_COUNT = r"[+\-]?(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+|半)"
_PREFIX = r"(?:未来|接下来|今后|往后|后续)\s*(?:的)?\s*"
_UNIT = r"(?:个?交易日|自然日|天|日|个?星期|个?礼拜|周|个月|月|年)"
_DYNAMIC = _PREFIX + _COUNT + r"\s*" + _UNIT
_DYNAMIC_FULL = re.compile(_PREFIX + r"(" + _COUNT + r")\s*(" + _UNIT + r")")
_FIXED = {
    "下周": "下周", "下星期": "下周", "下个星期": "下周", "下一个星期": "下周",
    "下个月": "下个月", "下月": "下个月", "下一个月": "下个月",
    "明天": "明天", "明日": "明天", "后天": "后天",
    "短线": "短线", "短期": "短线", "中线": "中线", "中期": "中线",
}
_GENERIC = ("接下来一段时间", "未来一段时间", "接下来", "未来", "今后", "往后", "后续", "后市")
_UNSUPPORTED = ("明后年", "明年", "后年", "下个季度", "下季度", "下半年", "长期", "长线")
# Include range/qualifier tails so an unsupported suffix cannot be silently discarded.
_TAIL = (r"(?:\s*(?:至|到|[-~～])\s*" + _COUNT + r"\s*" + _UNIT + r")?"
         r"(?:半|多|左右|以内|以上|以下)?")
_SCAN = re.compile(_DYNAMIC + _TAIL + "|" + "|".join(
    re.escape(value) for value in sorted((*_FIXED, *_GENERIC, *_UNSUPPORTED), key=len, reverse=True)))
_QUANTIFIED_PREFIX = re.compile(_PREFIX + r"(?=[\d零〇一二两三四五六七八九十百半几数若干+\-])")
_OUTLOOK_ACTION = re.compile(r"预测|展望|后市|接下来|未来|今后|往后|后续")
# A recognized substring must cover the whole temporal cue. The negative lookbehind
# leaves the ordinary verb modifier in "分析一下下周" outside the time expression.
_FIXED_CUE = re.compile(
    r"(?<!一)下(?:\s*下)*\s*(?:" + _COUNT + r"\s*)?(?:个\s*)?(?:星\s*期|周|月|季度|半年)|"
    r"(?:大\s*)?(?:明|后){1,3}\s*(?:天|日)|(?:超|极)?(?:短|中|长){1,2}\s*(?:线|期)")
_TEMPORAL_SUFFIX = re.compile(
    r"\s*(?:[\d零〇一二两三四五六七八九十百半]|底|末|初|上旬|中旬|下旬|"
    r"中(?=的|走势|行情|$)|(?:之|以)?(?:前|后|内|外)|左右|起|至|到|[-~～])")


def _issue(code="unsupported_outlook_horizon"):
    return InputIssue("needs_clarification", code,
        "可以基于已完成日线讨论未来的条件性走势，但请使用一个明确期限，"
        "例如下周、下个月、未来30天或未来20个交易日；不推导具体到期日。")


def _integer(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total, pending, previous_unit = 0, None, 1000
    for character in text:
        if character in ("十", "百"):
            unit = 10 if character == "十" else 100
            if unit >= previous_unit:
                raise _issue()
            total += (1 if pending is None else pending) * unit
            pending, previous_unit = None, unit
        elif character in digits:
            if pending not in (None, 0):
                raise _issue()
            pending = digits[character]
        else:
            raise _issue()
    if previous_unit == 100 and pending and "零" not in text.split("百")[-1]:
        raise _issue()
    return total + (pending or 0)


def _canonical_horizon(expression):
    if type(expression) is not str or not expression.strip() or len(expression) > 80:
        raise _issue()
    text = unicodedata.normalize("NFKC", expression).strip()
    if text in _FIXED:
        return _FIXED[text]
    if text in _GENERIC:
        return None
    match = _DYNAMIC_FULL.fullmatch(text)
    if match is None:
        raise _issue()
    number_text, unit = match.groups()
    if number_text == "半" and unit == "年":
        return "未来6个月"
    number = _integer(number_text)
    if "交易日" in unit:
        if not 1 <= number <= 366:
            raise _issue()
        return f"未来{number}个交易日"
    multiplier, canonical_unit = ({"自然日": (1, "天"), "天": (1, "天"), "日": (1, "天"),
        "周": (7, "周"), "星期": (7, "周"), "个星期": (7, "周"),
        "礼拜": (7, "周"), "个礼拜": (7, "周"), "月": (30, "个月"),
        "个月": (30, "个月"), "年": (365, "年")}[unit])
    if not 1 <= number * multiplier <= MAX_OUTLOOK_DAYS:
        raise _issue()
    return f"未来{number}{canonical_unit}"


@dataclass(frozen=True)
class ResearchObjective:
    """A narrative objective, never authority to fetch future bars or create a price target."""

    kind: str = "analysis"
    horizon: str | None = None

    def __post_init__(self):
        if self.kind not in ("analysis", "outlook", "decision") or (self.kind == "analysis" and self.horizon is not None):
            raise InvalidRequestError("Invalid research objective")
        if self.kind == "decision" and self.horizon is None:
            raise InvalidRequestError("Decision objective requires an explicit canonical horizon")
        if self.horizon is not None:
            try:
                canonical = _canonical_horizon(self.horizon)
            except InputIssue as error:
                raise InvalidRequestError("Invalid research outlook horizon") from error
            if canonical != self.horizon:
                raise InvalidRequestError("Research horizon must be canonical")

    def to_dict(self):
        return {"kind": self.kind, "horizon": self.horizon}


def parse_outlook_expression(expression: str) -> ResearchObjective:
    """Parse one complete bounded horizon; no calendar dates or future prices are invented."""
    return ResearchObjective("outlook", _canonical_horizon(expression))


def _outlook_matches(question):
    if type(question) is not str or len(question) > 2000:
        raise _issue()
    matches = tuple(_SCAN.finditer(question))
    for cue in _FIXED_CUE.finditer(question):
        if not any(item.start() <= cue.start() and cue.end() <= item.end() for item in matches):
            raise _issue()
    for prefix in _QUANTIFIED_PREFIX.finditer(question):
        if not any(item.start() <= prefix.start() and prefix.end() < item.end() for item in matches):
            raise _issue()
    horizons = []
    for match in matches:
        horizon = _canonical_horizon(match[0])
        if horizon is not None and _TEMPORAL_SUFFIX.match(question[match.end():]):
            raise _issue()
        horizons.append(horizon)
    distinct = {value for value in horizons if value is not None}
    if len(distinct) > 1:
        raise _issue("conflicting_outlook_horizons")
    return matches, next(iter(distinct)) if distinct else None


def extract_outlook(question: str) -> ResearchObjective | None:
    """Recognize a future purpose and one horizon; unsupported future clauses fail explicitly."""
    matches, horizon = _outlook_matches(question)
    return ResearchObjective("outlook", horizon) if matches or _OUTLOOK_ACTION.search(question) else None


def mask_outlook_expressions(question: str) -> str:
    """Hide only locally recognized future phrases before checking historical date leftovers."""
    matches, _ = _outlook_matches(question)
    characters = list(question)
    for match in matches:
        characters[match.start():match.end()] = " " * (match.end() - match.start())
    return "".join(characters)
