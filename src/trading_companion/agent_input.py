"""Conservative first-turn input grammar; ambiguous text never authorizes research."""

from datetime import date, datetime, timedelta, timezone
import re

from trading_companion.akshare_adapter import to_akshare_symbol
from trading_companion.errors import InvalidRequestError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.tencent_adapter import to_tencent_symbol
from trading_companion.tool_model import MAX_USER_CHARACTERS, ToolCall


QUESTION_EXAMPLE = "分析 000001.SZ，2026-09-21 至 2026-09-25，指标：SMA3、SMA20、VOLUME3"
_QUESTION = re.compile(
    r"\s*(?:请)?分析\s*([0-9]{6}\.(?:SH|SZ|BJ))\s*[,，;；]?\s*"
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*(?:至|到|~|～)\s*"
    r"([0-9]{4}-[0-9]{2}-[0-9]{2})\s*[,，;；]?\s*指标\s*[:：]\s*(.+?)\s*[。！!]?\s*",
    re.IGNORECASE,
)
_WINDOW = re.compile(r"(SMA|EMA|RSI|ATR|ADX|VWMA|VOLUME|量能|成交量)\s*(?:\(\s*([0-9]+)\s*\)|([0-9]+))",
                     re.IGNORECASE)
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"


class InputIssue(ValueError):
    """A fixed user-facing issue, without echoing the original question."""

    def __init__(self, status: str, code: str, message: str):
        self.status, self.code = status, code
        super().__init__(message)


def _indicator_tokens(text: str) -> list[str]:
    """Split top-level separators without splitting MACD/BOLL parameter lists."""
    text = text.translate(str.maketrans({"（": "(", "）": ")", "，": ",", "；": ";"}))
    tokens, start, depth = [], 0, 0
    for index, character in enumerate(text):
        if character == "(":
            depth += 1
            if depth > 1:
                raise InvalidRequestError("Nested indicator parentheses are unsupported")
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise InvalidRequestError("Unbalanced indicator parentheses")
        elif character in ",、;" and depth == 0:
            tokens.append(text[start:index].strip())
            start = index + 1
    tokens.append(text[start:].strip())
    if depth or any(not token for token in tokens):
        raise InvalidRequestError("Provide complete, separated indicator specifications")
    return tokens


def _indicator(text: str) -> IndicatorSpec:
    if text.upper() in ("OBV", "OBV()"):
        return IndicatorSpec("obv")
    match = _WINDOW.fullmatch(text)
    if match:
        name = match[1].lower()
        return IndicatorSpec("volume" if name in ("量能", "成交量") else name,
                             window=int(match[2] or match[3]))
    match = re.fullmatch(r"MACD\s*\(\s*([0-9]+)\s*,\s*([0-9]+)\s*,\s*([0-9]+)\s*\)",
                         text, re.IGNORECASE)
    if match:
        return IndicatorSpec("macd", fast=int(match[1]), slow=int(match[2]), signal=int(match[3]))
    match = re.fullmatch(r"BOLL\s*\(\s*([0-9]+)\s*,\s*(" + _NUMBER + r")\s*\)",
                         text, re.IGNORECASE)
    if match:
        return IndicatorSpec("boll", window=int(match[1]), stddev_multiplier=float(match[2]))
    raise InvalidRequestError("Use explicit supported indicator notation")


def parse_indicator_specs(text: str) -> tuple[IndicatorSpec, ...]:
    """Reuse explicit indicator notation without requiring a complete input sentence."""
    return tuple(_indicator(token) for token in _indicator_tokens(text))


def parse_research_question(question: str, *, source: str) -> MultiIndicatorRequest:
    """Recognize one explicit request, not arbitrary natural-language intent."""
    if type(question) is not str or not question.strip() or len(question) > MAX_USER_CHARACTERS:
        raise InputIssue("needs_clarification", "invalid_input",
                         "请提供不超过2000字符的单股票历史日线问题，不能省略关键参数。")
    if re.search(r"下单|撤单|买入|卖出|券商|\b(?:order|broker)\b", question, re.IGNORECASE):
        raise InputIssue("out_of_scope", "trading_not_supported", "本程序只做只读研究，不具备下单、撤单或资金操作权限。")
    match = _QUESTION.fullmatch(question)
    if match is None:
        raise InputIssue("needs_clarification", "explicit_request_required",
                         "请明确一只股票的完整代码、起止日期和指标。简称、裸代码或含歧义的句子不会触发查询。示例："
                         + QUESTION_EXAMPLE)
    instrument, start_text, cutoff_text, indicators_text = match.groups()
    instrument = instrument.upper()
    try:
        (to_tencent_symbol if source == "tencent" else to_akshare_symbol)(instrument)
    except InvalidRequestError:
        raise InputIssue("out_of_scope", "unsupported_instrument",
                         "该股票代码不在当前所选数据源支持范围内；不会自动换源。") from None
    try:
        request = MultiIndicatorRequest(
            instrument, date.fromisoformat(start_text), date.fromisoformat(cutoff_text),
            parse_indicator_specs(indicators_text), source=source,
        )
    except (InvalidRequestError, ValueError, OverflowError):
        raise InputIssue("needs_clarification", "invalid_research_parameters",
                         "日期、区间长度或指标参数不符合范围，请检查；程序不会改日期、换周期或静默删除指标。") from None
    if source != "csv" and request.as_of_date >= datetime.now(timezone(timedelta(hours=8))).date():
        raise InputIssue("needs_clarification", "completed_daily_bars_required",
                         "当前在线来源只分析上海当日之前的日线，请明确一个已过去的截止日期。")
    return request


def request_arguments(request: MultiIndicatorRequest) -> dict[str, object]:
    arguments = {
        "instrument_id": request.instrument_id, "start_date": request.start_date.isoformat(),
        "as_of_date": request.as_of_date.isoformat(),
        "indicators": [{"kind": spec.kind, **spec.parameters} for spec in request.indicators],
    }
    if request.source != "csv":
        arguments["source"] = request.source
    return arguments


def proposal_matches_request(call: ToolCall, request: MultiIndicatorRequest) -> bool:
    """Bind execution to parsed parameters, including the trusted source choice."""
    arguments, expected = call.arguments, request_arguments(request)
    if arguments.keys() != expected.keys():
        return False
    if any(arguments[key] != value for key, value in expected.items() if key != "indicators"):
        return False
    raw = arguments["indicators"]
    if type(raw) is not list or len(raw) != len(request.indicators):
        return False
    try:
        specs = [IndicatorSpec(item["kind"], **{key: value for key, value in item.items() if key != "kind"})
                 for item in raw if type(item) is dict and "kind" in item]
    except (InvalidRequestError, TypeError, ValueError):
        return False
    return sorted(spec.indicator_id for spec in specs) == sorted(spec.indicator_id for spec in request.indicators)
