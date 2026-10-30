"""Model-first candidate extraction, grounded identity resolution and explicit defaults."""

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import re
from trading_companion.turn_trace import trace_call, trace_event

from trading_companion.agent_input import InputIssue, parse_indicator_specs, request_arguments
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.multi_contracts import (
    DEFAULT_INDICATORS, IndicatorSpec, MultiIndicatorRequest, validate_research_conditions,
)
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.research_objective import (
    ResearchObjective, extract_outlook, mask_outlook_expressions, parse_outlook_expression,
)
from trading_companion.tool_model import (
    MAX_USER_CHARACTERS, ToolCapableModel, ToolDefinition, ToolMessage, ToolModelResponse,
)


INTENT_TOOL_NAME = "interpret_research_question"
INTENT_TOOL = ToolDefinition(INTENT_TOOL_NAME,
    "Submit an interpretation, not permission to query prices. Copy instrument, date and indicator "
    "phrases verbatim from the user. Never replace a name with a remembered code, invent defaults, "
    "drop a second stock, or convert relative dates yourself. General single-stock analysis, including "
    "'分析一下中际旭创', needs no dates or indicators from the user: omitted fields use application defaults. "
    "Prediction/outlook requests mean conditional scenarios from historical daily bars, not future prices. "
    "Put past data windows in date_expression, future horizons (e.g. 下个月) in outlook_expression; "
    "never request clarification merely for omitted dates/indicators. Indicator definitions are knowledge. "
    "Orders, news/fundamental research, screening and price targets are unsupported.",
    {"type": "object", "additionalProperties": False,
     "required": ["intent", "instrument_mentions", "date_expression", "indicator_mentions", "clarification_reason"],
     "properties": {
         "intent": {"type": "string", "enum": ["research", "knowledge", "unsupported", "clarify"]},
         "instrument_mentions": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
         "date_expression": {"type": ["string", "null"]},
         "outlook_expression": {"type": ["string", "null"]},
         "indicator_mentions": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
         "clarification_reason": {"type": ["string", "null"],
                                  "enum": [None, "instrument", "date", "indicator", "request"]},
     }})

_DATE_CUE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}|最近|近期|过去|截至|截止|今天|今日|昨天|昨日|前天|明天|未来|"
    r"上周|本周|上个月|这个月|本月|去年|今年|明年|交易日|"
    r"近(?=[0-9一二两三四五六七八九十百几数半])|半年|半个月|近日|近来|"
    r"[0-9零一二两三四五六七八九十百几数半]+\s*个?\s*(?:自然日|天|日|周|个月|月|年)")
_ISO = r"([0-9]{4}-[0-9]{2}-[0-9]{2})"
_COUNT = r"[0-9零一二两三四五六七八九十百]+"
_DATE_RANGE = re.compile(_ISO + r"\s*(?:至|到|~|～)\s*" + _ISO)
_DATE_POINT = re.compile(r"(?:(?:截至|截止|到)\s*)?" + _ISO)
_DATE_RELATIVE = re.compile(r"(?:最近|近|过去)\s*(" + _COUNT + r")\s*"
                            r"(?:个)?\s*(自然日|天|日|周|月|年)")
_LOCAL_DATE = re.compile(
    r"(?<![0-9-])(?:" + _DATE_RANGE.pattern + "|" + _DATE_POINT.pattern + "|"
    + _DATE_RELATIVE.pattern + r"|(?:最近|近期)(?!\s*[0-9零一二两三四五六七八九十百几数半+＋－.点-]))(?![0-9-])")
_DATE_SUFFIX = re.compile(r"\s*(?:半|多|左右|以内|以上|以下|底|末|初|上旬|中旬|下旬|"
                          r"(?:之|以)?(?:前|后|内|外)|起|至|到|~|～)")
_INDICATOR_CUE = re.compile(
    r"(?<![0-9.+＋－-])[+＋－-]?(?:[0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百]+)\s*(?:日|天)\s*"
    r"(?:指数移动平均线|简单移动平均线|均线|EMA|SMA|RSI|ATR|ADX|VWMA)(?!\s*(?:[（(]|[+＋－-]?[0-9]))|"
    r"(?<![A-Za-z])(?:MACD|BOLL|SMA|EMA|RSI|ATR|ADX|VWMA|OBV|VOLUME)(?![A-Za-z])"
    r"(?:\s*(?:[（(][^）)]*(?:[）)]|$)|[+＋－-]?[0-9]+(?:\.[0-9]+)?))?|"
    r"KDJ|指数移动平均线|简单移动平均线|布林带|布林线|均线|量能|成交量", re.IGNORECASE)
_UNSUPPORTED_CAPABILITY = re.compile(
    r"下单|自动交易|代我交易|执行交易|帮我(?:买|卖)|替我(?:买|卖)|"
    r"新闻|财报|基本面|公告|估值|营收|市盈率|资产负债|现金流|"
    r"目标价|目标价格|止盈|止损|涨到多少|跌到多少|收益率|胜率|"
    r"选股|筛选股票|推荐股票")
_RESEARCH_CUE = re.compile(r"分析|研究|预测|展望|走势|趋势|怎么看|怎么样|如何|后市|看看|看一下|看下")
_SUPPORTED_WORDS = re.compile("|".join(re.escape(value) for value in sorted((
    "请帮我", "能不能", "可不可以", "帮我", "麻烦", "我想", "你觉得", "你看", "给我", "请", "一下",
    "分析", "研究", "预测", "展望", "看看", "看一下", "看一看", "看下", "判断", "评估",
    "走势", "趋势", "表现", "怎么看", "怎么样", "如何", "后市", "方向", "技术面", "技术指标",
    "详细", "综合", "整体", "股票", "证券", "这只", "A股", "日线", "K线", "指标", "均线",
    "关注", "只看", "结合", "参考", "以及", "并且", "然后", "的", "和", "与", "来", "对", "从",
), key=len, reverse=True)))


@trace_call("check", "intent_shape")
def _bounded_research_shape(question, draft):
    """Repair a model's spurious clarification only for a fully checked, limited request shape."""
    if not draft["instrument_mentions"] or not _RESEARCH_CUE.search(question):
        return False
    phrases = [*draft["instrument_mentions"], *draft["indicator_mentions"]]
    phrases.extend(value for value in (draft["date_expression"], draft.get("outlook_expression")) if value)
    remaining = mask_outlook_expressions(_masked(question, _spans(question, phrases)))
    remaining = _SUPPORTED_WORDS.sub("", remaining)
    if re.fullmatch(r"[\s，,。.!！?？、;；:：]*", remaining) is not None:
        return True
    # Only a missing/partial date may repair a date clarification, never another task category.
    if draft["intent"] not in ("research", "clarify") or draft["clarification_reason"] not in (None, "date"):
        return False
    historical = mask_outlook_expressions(_masked(question, _spans(
        question, [*draft["instrument_mentions"], *draft["indicator_mentions"]])))
    local = _local_date_expression(historical)
    if local is None:
        return False
    remaining = _SUPPORTED_WORDS.sub("", _masked(historical, _spans(historical, [local])))
    return re.fullmatch(r"[\s，,。.!！?？、;；:：]*", remaining) is not None


@dataclass(frozen=True)
class ResearchConditions:
    """Checked conditions without a guessed stock; the original date anchor is retained."""

    start_date: date
    as_of_date: date
    indicators: tuple[IndicatorSpec, ...]
    source: str
    reference_date: date
    applied_defaults: tuple[str, ...] = ()
    objective: ResearchObjective = ResearchObjective()

    def __post_init__(self):
        if (self.source not in ("tencent", "akshare_eastmoney", "csv")
                or type(self.reference_date) is not date or type(self.objective) is not ResearchObjective):
            raise InvalidRequestError("Invalid research-condition configuration")
        object.__setattr__(self, "indicators", validate_research_conditions(
            self.start_date, self.as_of_date, self.indicators))
        if self.as_of_date >= self.reference_date:
            raise InputIssue("needs_clarification", "completed_daily_bars_required",
                             "当前只支持上海当日之前的已完成日线，请明确一个过去的截止日期。")

    def for_security(self, instrument_id: str) -> MultiIndicatorRequest:
        return MultiIndicatorRequest(instrument_id, self.start_date, self.as_of_date,
                                     self.indicators, source=self.source)

    def to_dict(self) -> dict[str, object]:
        return {"start_date": self.start_date.isoformat(), "as_of_date": self.as_of_date.isoformat(),
                "indicators": [{"kind": spec.kind, **spec.parameters} for spec in self.indicators],
                "source": self.source, "reference_date": self.reference_date.isoformat(),
                "applied_defaults": list(self.applied_defaults), "objective": self.objective.to_dict()}


@dataclass(frozen=True)
class PreparedResearchInput:
    status: str = "failed"
    code: str = "not_started"
    message: str = "输入尚未理解。"
    stage: str = "understanding"
    intent: str | None = None
    request: MultiIndicatorRequest | None = field(default=None, repr=False)
    security: Security | None = None
    candidates: tuple[Security, ...] = ()
    directory: SecurityDirectory | None = field(default=None, repr=False)
    applied_defaults: tuple[str, ...] = ()
    model_response: ToolModelResponse | None = field(default=None, repr=False)
    model_calls_attempted: int = 0
    failure_code: str | None = None
    conditions: ResearchConditions | None = field(default=None, repr=False)
    clarification_resumable: bool = False
    user_question: str = field(default="", repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status, "code": self.code, "message": self.message, "stage": self.stage,
            "intent": self.intent, "security": self.security.to_dict() if self.security else None,
            "candidates": [item.to_dict() for item in self.candidates[:5]],
            "candidate_count": len(self.candidates), "candidates_truncated": len(self.candidates) > 5,
            "directory": self.directory.metadata() if self.directory else None,
            "applied_defaults": list(self.applied_defaults),
            "confirmed_parameters": request_arguments(self.request) if self.request else None,
            "model_calls_attempted": self.model_calls_attempted, "failure_code": self.failure_code,
            "clarification_resumable": self.clarification_resumable,
            "pending_parameters": self.conditions.to_dict()
            if self.clarification_resumable and self.conditions else None,
        }


def _spans(question: str, phrases: list[str]) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for phrase in phrases
            for match in re.finditer(re.escape(phrase), question)]


def _masked(question: str, spans: list[tuple[int, int]]) -> str:
    characters = list(question)
    for start, end in spans:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _count(text: str) -> int:
    """Handle explicit integer counts, not ambiguous Chinese numeric shorthand."""
    if text.isascii() and text.isdigit():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    total, pending, previous_unit = 0, None, 1000
    for character in text:
        if character in ("十", "百"):
            unit = 10 if character == "十" else 100
            if unit >= previous_unit:
                raise InvalidRequestError("Invalid Chinese count units")
            total += (1 if pending is None else pending) * unit
            pending, previous_unit = None, unit
        elif character in digits:
            if pending not in (None, 0):
                raise InvalidRequestError("Ambiguous Chinese digit sequence")
            pending = digits[character]
        else:
            raise InvalidRequestError("Unsupported integer count")
    if previous_unit == 100 and pending and "零" not in text.split("百")[-1]:
        raise InvalidRequestError("Ambiguous abbreviated Chinese count")
    return total + (pending or 0)


def _normalize_null_strings(arguments: dict) -> tuple[dict, tuple[str, ...]]:
    """Accept only fixed null spellings in nullable string fields, without mutating model output."""
    normalized = dict(arguments)
    count = 0
    for key in ("date_expression", "outlook_expression", "clarification_reason"):
        value = normalized.get(key)
        if type(value) is str and value.strip().casefold() in ("", "null", "none", "无"):
            normalized[key] = None
            count += 1
    return normalized, (f"model_null_string_normalized:{count}",) if count else ()


def _candidate(response: ToolModelResponse, question: str) -> tuple[dict, tuple[str, ...]]:
    if (response.status != "tool_calls" or len(response.tool_calls) != 1
            or response.tool_calls[0].name != INTENT_TOOL_NAME):
        raise InputIssue("failed", "invalid_intent_response", "模型未返回完整、可校验的请求理解结果；没有查询行情。")
    arguments = response.tool_calls[0].arguments
    required = {"intent", "instrument_mentions", "date_expression", "indicator_mentions", "clarification_reason"}
    if (required - arguments.keys() or arguments.keys() - (required | {"outlook_expression"})):
        raise InputIssue("failed", "invalid_intent_response", "请求理解字段不符合约定；没有查询行情。")
    arguments, defaults = _normalize_null_strings(arguments)
    if (arguments["intent"] not in ("research", "knowledge", "unsupported", "clarify")
            or arguments["clarification_reason"] not in (None, "instrument", "date", "indicator", "request")):
        raise InputIssue("failed", "invalid_intent_response", "请求理解类别不符合约定；没有查询行情。")
    for key in ("instrument_mentions", "indicator_mentions"):
        phrases = arguments[key]
        if type(phrases) is not list or len(phrases) > 12 or any(
            type(phrase) is not str or not phrase.strip() or len(phrase) > 80 for phrase in phrases):
            raise InputIssue("failed", "invalid_intent_response", "请求理解中的名称或指标字段无效。")
    expressions = [arguments["date_expression"], arguments.get("outlook_expression")]
    if any(value is not None and (type(value) is not str or not value.strip() or len(value) > 120)
           for value in expressions):
        raise InputIssue("failed", "invalid_intent_response", "请求理解中的日期或展望字段无效。")
    copied = [*arguments["instrument_mentions"], *arguments["indicator_mentions"]]
    copied.extend(value for value in expressions if value is not None)
    if any(phrase not in question for phrase in copied):
        raise InputIssue("needs_clarification", "ungrounded_intent",
                         "模型提取了原问题中不存在的名称或条件；请重述研究问题，没有使用模型记忆补码。")
    return arguments, defaults


def _local_date_expression(question: str) -> str | None:
    """Find one whole historical expression; never choose among dates or ignore leftovers."""
    matches = list(_LOCAL_DATE.finditer(question))
    if len(matches) != 1:
        return None
    match = matches[0]
    if _DATE_SUFFIX.match(question, match.end()) or _DATE_CUE.search(_masked(question, [match.span()])):
        return None
    return match[0]


def _dates(expression: str | None, remaining_question: str, reference: date) -> tuple[date, date, tuple[str, ...]]:
    uncovered = remaining_question if expression is None else remaining_question.replace(
        expression, " " * len(expression), 1)
    text = expression.strip() if expression is not None else ""
    partial = expression is not None and not (text in ("最近", "近期") or _DATE_RANGE.fullmatch(text)
        or _DATE_POINT.fullmatch(text) or _DATE_RELATIVE.fullmatch(text))
    uncovered_cue = _DATE_CUE.search(uncovered)
    if uncovered_cue or partial and _DATE_CUE.search(remaining_question):
        local = _local_date_expression(remaining_question)
        if local is None and not uncovered_cue:
            return _parse_dates(expression, reference)
        if local is None:
            code = "date_not_grounded" if expression is None else "conflicting_dates"
            message = ("原问题含时间要求，但未能完整识别；请明确时间范围。" if expression is None
                       else "发现未被完整理解的时间条件，请明确一个研究区间。")
            raise InputIssue("needs_clarification", code, message)
        with trace_event("repair", "local_date_expression") as event:
            start, cutoff, defaults = _parse_dates(local, reference)
            event.result(True)
        return start, cutoff, (*defaults, "date_expression:local_recognition")
    return _parse_dates(expression, reference)


def _parse_dates(expression: str | None, reference: date) -> tuple[date, date, tuple[str, ...]]:
    """The shared date semantics for verbatim model expressions and local recognition."""
    defaults = []
    cutoff = reference - timedelta(days=1)
    if expression is None:
        return cutoff - timedelta(days=364), cutoff, ("cutoff:previous_shanghai_day", "range:365_calendar_days")
    text = expression.strip()
    match = _DATE_RANGE.fullmatch(text)
    if match:
        return date.fromisoformat(match[1]), date.fromisoformat(match[2]), ()
    match = _DATE_POINT.fullmatch(text)
    if match:
        cutoff = date.fromisoformat(match[1])
        return cutoff - timedelta(days=364), cutoff, ("range:365_calendar_days",)
    if text in ("最近", "近期"):
        return cutoff - timedelta(days=364), cutoff, ("cutoff:previous_shanghai_day", "range:365_calendar_days")
    match = _DATE_RELATIVE.fullmatch(text)
    if match:
        number = _count(match[1])
        unit = match[2]
        days = number * {"自然日": 1, "天": 1, "日": 1, "周": 7, "月": 30, "年": 365}[unit]
        if not 1 <= days <= 366:
            raise InvalidRequestError("Relative range is outside the product limit")
        defaults.append("cutoff:previous_shanghai_day")
        if unit in ("月", "年"):
            defaults.append("relative_unit:30_days_per_month_365_per_year")
        return cutoff - timedelta(days=days - 1), cutoff, tuple(defaults)
    raise InputIssue("needs_clarification", "unsupported_date_expression",
                     "当前仅支持明确历史日期、最近若干自然日/周/月/年或默认近期；今天、未来及交易日区间需另行明确。")


def _indicator(phrase: str) -> tuple[IndicatorSpec, ...]:
    text = phrase.strip()
    aliases = {"MACD": "macd", "RSI": "rsi", "BOLL": "boll", "ATR": "atr",
               "EMA": "ema", "VOLUME": "volume", "布林带": "boll", "布林线": "boll",
               "量能": "volume", "成交量": "volume", "指数移动平均线": "ema",
               "ADX": "adx", "VWMA": "vwma", "OBV": "obv"}
    if text.upper() in aliases:
        return (IndicatorSpec(aliases[text.upper()]),)
    if text in ("均线", "简单移动平均线", "SMA", "sma"):
        return (IndicatorSpec("sma", window=20), IndicatorSpec("sma", window=60))
    match = re.fullmatch(r"(" + _COUNT + r")\s*(?:日|天)\s*"
                         r"(指数移动平均线|简单移动平均线|均线|EMA|SMA|RSI|ATR|ADX|VWMA)", text, re.IGNORECASE)
    if match:
        name = match[2].upper()
        kind = {"指数移动平均线": "ema", "简单移动平均线": "sma", "均线": "sma"}.get(name, name.lower())
        return (IndicatorSpec(kind, window=_count(match[1])),)
    return parse_indicator_specs(text)


def indicator_text(question):
    """Relative day ranges belong to dates, not an adjacent indicator window."""
    return re.sub(r'(?:最近|近|过去)\s*' + _COUNT + r'\s*(?:个)?\s*(?:自然日|天|日)',
                  lambda m: ' ' * len(m[0]), question)


def _ground_indicator_mentions(question, phrases):
    """Recover only overlapping indicator spans affected by a relative day boundary."""
    local = list(_INDICATOR_CUE.finditer(indicator_text(question)))
    dates = list(_DATE_RELATIVE.finditer(question))
    result = []
    for phrase in phrases:
        spans = _spans(question, [phrase])
        replacements = [m[0] for m in local for start, end in spans
                        if m.start() < end and m.end() > start
                        and any(d.start() < end and d.end() > start for d in dates)]
        result.extend(replacements or [phrase])
    return list(dict.fromkeys(result))


def _indicators(phrases: list[str], remaining_question: str) -> tuple[tuple[IndicatorSpec, ...], tuple[str, ...]]:
    spans = _spans(remaining_question, phrases)
    if any(not any(start <= match.start() and match.end() <= end for start, end in spans)
           for match in _INDICATOR_CUE.finditer(indicator_text(remaining_question))):
        raise InputIssue("needs_clarification", "indicator_not_grounded",
                         "原问题中的指标或周期未被完整识别，不能静默忽略；请明确所需指标。")
    if not phrases:
        return DEFAULT_INDICATORS, ("indicators:default_eight",)
    return tuple(spec for phrase in phrases for spec in _indicator(phrase)), ()


def prepare_research_question(question: str, *, model: ToolCapableModel, directory: SecurityDirectory,
                              source: str = "tencent", reference_date: date | None = None,
                              development: bool = False) -> PreparedResearchInput:
    """One interpretation request, no prices; all executable parameters are checked locally."""
    if (type(directory) is not SecurityDirectory or source not in ("tencent", "akshare_eastmoney", "csv")
            or type(development) is not bool or (source == "csv" and not development)):
        raise ModelConfigurationError("Natural input needs a trusted directory and explicit source configuration")
    reference = reference_date if reference_date is not None else datetime.now(timezone(timedelta(hours=8))).date()
    if type(reference) is not date:
        raise ModelConfigurationError("Reference date must be supplied by trusted application code")
    result = PreparedResearchInput(directory=directory)
    if type(question) is not str or not question.strip() or len(question) > MAX_USER_CHARACTERS:
        return replace(result, status="needs_clarification", code="invalid_input", message="请用不超过2000字符描述一个研究问题。")
    result = replace(result, user_question=question)
    messages = [
        ToolMessage("system", "Understand the user's A-share question and submit exactly one "
                    "interpret_research_question call. You do not execute research. Extract all instrument "
                    "mentions verbatim, including any explicit codes, without choosing between ambiguous "
                    "names. Copy historical data time expressions into date_expression and any separate "
                    "future horizon into outlook_expression, and copy explicit indicators including periods. "
                    "A broad single-stock analysis or conditional outlook is research; dates and indicators "
                    "can be omitted without clarification. Never turn a future horizon into a historical cutoff. "
                    "Use null/empty lists when omitted; defaults belong to the application. Do not add "
                    "fields, paths, URLs, providers or confidence scores. Shanghai reference date: " + reference.isoformat()),
        ToolMessage("user", question),
    ]
    try:
        result = replace(result, model_calls_attempted=1)
        response = model.complete(messages, tools=(INTENT_TOOL,))
        result = replace(result, model_response=response)
        draft, normalization_defaults = _candidate(response, question)
        result = replace(result, applied_defaults=normalization_defaults)
        draft = {**draft, 'indicator_mentions': _ground_indicator_mentions(question, draft['indicator_mentions'])}
        result = replace(result, intent=draft["intent"])
        if _UNSUPPORTED_CAPABILITY.search(question):
            return replace(result, status="out_of_scope", code="unsupported_intent",
                           message="当前可基于历史日线做单股技术分析与条件性展望；尚无新闻、财报、选股或目标价工具，也不执行交易。")
        if draft["intent"] == "knowledge":
            return replace(result, status="out_of_scope", code="knowledge_entry_not_connected",
                           message="已识别为知识问题，不查询行情；当前仅支持明确的指标定义问法，例如“RSI是什么”，不生成开放知识回答。")
        bounded_shape = _bounded_research_shape(question, draft)
        if draft["intent"] == "unsupported" and not bounded_shape:
            return replace(result, status="out_of_scope", code="unsupported_intent",
                           message="当前入口仅支持只读单股票日线研究，不支持交易执行、新闻选股或目标价。")
        instrument_clarification = draft["clarification_reason"] == "instrument"
        model_clarification = draft["intent"] == "clarify" or draft["clarification_reason"] is not None
        if model_clarification and not instrument_clarification and not bounded_shape:
            raise InputIssue("needs_clarification", "intent_needs_clarification",
                             "研究目标或条件尚不明确，请补充希望研究的股票、时间或指标；暂不查询行情。")
        selected_sources = re.findall(r"(?:数据源\s*(?:为|是|[:：])?|使用|采用|用)\s*"
                                      r"(腾讯|东方财富|东财|AKShare)", question, re.IGNORECASE)
        if any(("tencent" if name == "腾讯" else "akshare_eastmoney") != source for name in selected_sources):
            raise InputIssue("needs_clarification", "source_selection_mismatch",
                             "你指定的来源与当前入口配置不同；需显式切换来源，不能忽略要求或静默换源。")
        result = replace(result, stage="resolution")
        directory.assert_usable(reference, development=development)
        mentions = draft["instrument_mentions"]
        spans = _spans(question, mentions)
        if any(not any(start <= known_start and known_end <= end for start, end in spans)
               for known_start, known_end in directory.mentioned_spans(question)):
            raise InputIssue("needs_clarification", "instrument_not_grounded",
                             "原问题中还有未被完整识别的证券名称或代码，请明确本次研究对象。")
        remaining = _masked(question, spans)
        indicators, indicator_defaults = _indicators(draft["indicator_mentions"], remaining)
        date_question = _masked(remaining, _spans(remaining, draft["indicator_mentions"]))
        historical_expression = draft["date_expression"]
        if historical_expression is not None and extract_outlook(historical_expression) is not None:
            # Compatibility: older interpreters put one future phrase in the old date field.
            if mask_outlook_expressions(historical_expression).strip():
                raise InputIssue("needs_clarification", "conflicting_dates",
                                 "请将历史数据区间与未来展望期限分别说明；没有把未来时间当作行情日期。")
            historical_expression = None
        if draft.get("outlook_expression") is not None:
            parse_outlook_expression(draft["outlook_expression"])
        objective = extract_outlook(date_question) or ResearchObjective()
        historical_question = mask_outlook_expressions(date_question)
        start, cutoff, defaults = _dates(historical_expression, historical_question, reference)
        conditions = ResearchConditions(start, cutoff, indicators, source, reference,
                                        (*normalization_defaults, *defaults, *indicator_defaults), objective)
        result = replace(result, conditions=conditions, applied_defaults=conditions.applied_defaults)
        if not mentions:
            result = replace(result, clarification_resumable=True)
            raise InputIssue("needs_clarification", "missing_instrument", "你希望研究哪只A股？可以直接提供股票名称。")
        matches = [directory.lookup(mention) for mention in mentions]
        candidates = tuple(sorted({item.instrument_id: item for match in matches for item in match.candidates}.values(),
                                  key=lambda item: item.instrument_id))
        result = replace(result, candidates=candidates)
        unique_identity = all(match.status == "resolved" for match in matches) and len(candidates) == 1
        if model_clarification and not instrument_clarification and not unique_identity:
            raise InputIssue("needs_clarification", "intent_needs_clarification",
                             "研究目标或证券尚未明确，请补充希望研究的股票；暂不查询行情。")
        if not unique_identity or (instrument_clarification and not bounded_shape):
            # Multiple distinct mentions require a full single-stock restatement, not slot filling.
            result = replace(result, clarification_resumable=len(set(mentions)) == 1)
            choices = "、".join(f"{item.name}（{item.instrument_id}）" for item in candidates[:5])
            message = ("你指的是哪只股票？目录候选：" + choices + "。请明确证券名称。" if choices
                       else "当前目录未匹配到该证券，请补充完整名称或代码；不会凭模型记忆补码。")
            raise InputIssue("needs_clarification", "instrument_needs_clarification", message)
        security = candidates[0]
        result = replace(result, security=security, stage="parameters")
        request = conditions.for_security(security.instrument_id)
        return trace_call("repair", "intent_rule", replace, result, status="ready", code="research_input_ready", request=request, intent="research",
                       message="证券身份及研究参数已校验；默认值和实际区间将随结果展示。")
    except InputIssue as issue:
        return replace(result, status=issue.status, code=issue.code, message=str(issue))
    except DataValidationError:
        return replace(result, status="failed", code="directory_unusable",
                       message="证券目录已过期、时间异常或仅为测试夹具；未授权在线分析，也不会自动刷新。")
    except (ModelCallError, ModelRequestError, ModelConfigurationError) as error:
        return replace(result, status="failed", code="intent_model_unavailable",
                       failure_code=error.code if isinstance(error, ModelCallError) else "model_request_or_configuration_error",
                       message="本轮未取得有效的请求理解结果，没有查询行情。")
    except (InvalidRequestError, ValueError, OverflowError):
        return replace(result, status="needs_clarification", code="invalid_research_parameters",
                       message="日期、指标或所选来源的证券范围不符合限制，请明确条件；不会静默修改。")
