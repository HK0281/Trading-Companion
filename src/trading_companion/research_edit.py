"""Bounded whole-message indicator replacements, not arbitrary conversational edits."""

import re
import unicodedata

from trading_companion.agent_input import InputIssue, parse_indicator_specs
from trading_companion.errors import InvalidRequestError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest


_EDIT_CUE = re.compile(r"改成|改为|改看|改到|修改|更改|调整|替换|换成|换为|其他不变|其它不变|其余不变|沿用|上一轮|上次|刚才|窗口|周期")
_EDIT = re.compile(
    r"(?:(?:请帮我|请|帮我)\s*)?(?:把|将)?\s*(.+?)\s*"
    r"(?:改成|改为|换成|换为|调整为)\s*(.+?)"
    r"(?:\s*[,;]\s*(?:其他|其它|其余)(?:都)?不变)?\s*[。.!！]?\s*", re.IGNORECASE)
_KINDS = {
    "SMA": ("sma",), "简单移动平均线": ("sma",), "均线": ("sma", "ema"),
    "EMA": ("ema",), "指数移动平均线": ("ema",), "RSI": ("rsi",),
    "ATR": ("atr",), "VOLUME": ("volume",), "量能": ("volume",), "成交量": ("volume",),
    "BOLL": ("boll",), "布林带": ("boll",), "布林线": ("boll",), "MACD": ("macd",),
}
_WINDOW_NAMES = {name: kinds[0] for name, kinds in _KINDS.items() if len(kinds) == 1
                 and kinds[0] not in ("macd", "boll")}
_WINDOW_NAMES["均线"] = "sma"
EDIT_EXAMPLE = "把SMA3改成5日，其他不变"


def is_indicator_edit(question: str) -> bool:
    """Route incomplete edit-like messages away from fresh-request defaults."""
    return bool(_EDIT_CUE.search(unicodedata.normalize("NFKC", question)))


def _issue(code: str, message: str) -> InputIssue:
    return InputIssue("needs_clarification", code, message)


def parse_indicator_instance(text: str) -> IndicatorSpec:
    """Read one explicit instance; shared by replacement, addition and removal."""
    match = re.fullmatch(r"([0-9]+)\s*(?:日|天)\s*(.+)", text)
    if match and match[2].upper() in _WINDOW_NAMES:
        return IndicatorSpec(_WINDOW_NAMES[match[2].upper()], window=int(match[1]))
    specs = parse_indicator_specs(text)
    if len(specs) != 1:
        raise InvalidRequestError("Exactly one indicator instance is required")
    return specs[0]


def indicator_candidates(request: MultiIndicatorRequest, text: str) -> tuple[IndicatorSpec, ...]:
    """Generic names may match several instances; callers must not choose the first."""
    kinds = _KINDS.get(text.upper())
    return tuple(spec for spec in request.indicators if spec.kind in kinds) if kinds else tuple(
        spec for spec in request.indicators if spec == parse_indicator_instance(text))


def indicator_selection_label(spec: IndicatorSpec) -> str:
    """An explicit instance users can copy into a short clarification reply."""
    values = spec.parameters
    if spec.kind == "macd":
        return "MACD({fast},{slow},{signal})".format(**values)
    if spec.kind == "boll":
        return "BOLL({window},{stddev_multiplier})".format(**values)
    return f"{spec.kind.upper()}{values['window']}"


def indicator_selection(question: str) -> IndicatorSpec | None:
    """Read a whole short, explicit answer; do not discard additional clauses."""
    text = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！").strip()
    if not text or len(text) > 80 or not all(char.isprintable() for char in text):
        return None
    text = re.sub(r"^(?:我指的是|选择|就选|选)\s*", "", text)
    try:
        return parse_indicator_instance(text)
    except (InvalidRequestError, ValueError, OverflowError):
        return None


def window_edit_candidates(request: MultiIndicatorRequest, question: str):
    """Recognize only a whole window command; ambiguity is persisted by the session."""
    text = unicodedata.normalize("NFKC", question).strip()
    if not text or not all(char.isprintable() for char in text):
        return None
    generic = re.fullmatch(
        r"(?:(?:请帮我|请|帮我)\s*)?(?:(?:把|将)?\s*(?:窗口|周期)\s*)?"
        r"(?:改成|改为|换成|换为|调整为)\s*([0-9]+)\s*(?:日|天)"
        r"(?:\s*[,;]\s*(?:其他|其它|其余)(?:都)?不变)?\s*[。.!！]?", text)
    if generic:
        return int(generic[1]), tuple(spec for spec in request.indicators if "window" in spec.parameters)
    match = _EDIT.fullmatch(text)
    if match is None:
        return None
    target, replacement = (part.strip() for part in match.groups())
    window = re.fullmatch(r"([0-9]+)\s*(?:日|天)?", replacement)
    if not window or target.upper() not in _KINDS:
        return None
    return int(window[1]), indicator_candidates(request, target)


def is_window_edit(question: str) -> bool:
    """Keep target-less numeric window edits out of the stock-update parser."""
    text = unicodedata.normalize("NFKC", question).strip()
    return re.match(r"(?:(?:请帮我|请|帮我)\s*)?(?:改成|改为|换成|换为|调整为)\s*[0-9]+\s*(?:日|天)", text) is not None


def replace_indicator(request: MultiIndicatorRequest, question: str) -> MultiIndicatorRequest:
    """Change one existing instance; preserve every other field and allocate a fresh run ID."""
    text = unicodedata.normalize("NFKC", question).strip()
    match = _EDIT.fullmatch(text) if text and all(char.isprintable() for char in text) else None
    if match is None:
        raise _issue("indicator_edit_not_supported",
                     "本步仅支持完整的一项指标替换，例如“" + EDIT_EXAMPLE +
                     "”；附带股票、日期、来源或其他要求时，请用“新问题：”给出完整条件，不会忽略额外要求。")
    target_text, replacement_text = (part.strip() for part in match.groups())
    try:
        candidates = indicator_candidates(request, target_text)
        if not candidates:
            raise _issue("indicator_edit_target_missing", "上一轮没有该指标实例；本步只替换已有指标，不自动添加。")
        if len(candidates) != 1:
            raise _issue("indicator_edit_ambiguous",
                         "上一轮有多条符合描述的均线或指标，请重述完整修改句并指定原参数，例如“把SMA20改成5日，其他不变”。")
        target = candidates[0]
        window = re.fullmatch(r"([0-9]+)\s*(?:日|天)?", replacement_text)
        if window:
            if "window" not in target.parameters:
                raise _issue("indicator_edit_ambiguous", "MACD包含多个周期，请明确完整参数，例如MACD(2,3,2)。")
            replacement = IndicatorSpec(target.kind, **{**target.parameters, "window": int(window[1])})
        else:
            replacement = parse_indicator_instance(replacement_text)
        if replacement.kind != target.kind:
            raise _issue("indicator_kind_change_not_supported", "本步只修改同类指标的参数；更换指标种类请提供完整新问题。")
        if replacement == target:
            raise _issue("indicator_edit_unchanged", "参数与上一轮相同，未重复查询；请明确需要修改的参数。")
        if replacement in request.indicators:
            raise _issue("indicator_edit_duplicate", "修改后会与已有指标重复；没有静默合并或删除，请明确新的指标组合。")
        indicators = tuple(replacement if spec == target else spec for spec in request.indicators)
        # Construct a new request so evidence from the old run cannot share its namespace.
        return MultiIndicatorRequest(request.instrument_id, request.start_date, request.as_of_date,
                                     indicators, source=request.source)
    except InputIssue:
        raise
    except (InvalidRequestError, ValueError, OverflowError):
        raise _issue("invalid_indicator_edit",
                     "指标实例、参数或附加要求不符合本步规则；未修改原条件或查询行情。请重述完整的一项替换；"
                     "修改股票、日期或来源请用“新问题：”提供完整问题。") from None
