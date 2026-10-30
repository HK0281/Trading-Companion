"""Whole-message, one-dimension updates over application-confirmed conditions."""

from dataclasses import dataclass, replace
from datetime import date, timedelta
import re
import unicodedata

from trading_companion.agent_input import InputIssue
from trading_companion.errors import InvalidRequestError
from trading_companion.research_edit import indicator_candidates, parse_indicator_instance
from trading_companion.research_intent import ResearchConditions, _dates
from trading_companion.multi_contracts import MultiIndicatorRequest


_ISO = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_VERB = r"(?:改成|改为|换成|换为|调整为)"
_DATE_TARGET = r"(?:日期区间|日期|时间范围|研究区间|区间|开始日期|起始日期|截止日期|结束日期|截止日|起始日)"
_CUE = re.compile(r"添加|增加|加上|删除|移除|去掉|不要再看|"
                  r"^(?:(?:请帮我|请|帮我)\s*)?(?:换成|换为|改成|改看)|"
                  r"(?:股票|证券|研究对象|" + _DATE_TARGET + r")\s*(?:改|换|调)")
_DATE_FLAGS = {"cutoff:previous_shanghai_day", "range:365_calendar_days",
               "date_expression:local_recognition",
               "relative_unit:30_days_per_month_365_per_year", "dates:explicit_update",
               "dates:relative_update", "dates:refresh_latest_previous_day", "refresh:same_dates"}
_RELATIVE = r"(?:最近|近|过去)\s*[0-9零一二两三四五六七八九十百]+\s*(?:个)?\s*(?:自然日|天|日|周|月|年)"
_REFRESH = {"看最新的", "更新到最新", "刷新行情", "刷新日线", "更新行情", "更新到最新日线"}


def is_condition_update(question: str) -> bool:
    """Catch supported and incomplete mutations before fresh-query defaults or follow-ups."""
    text = unicodedata.normalize("NFKC", question).strip()
    return bool(_CUE.search(text) or re.match(r"(?:(?:请帮我|请|帮我)\s*)?(?:看最新的|更新到最新|刷新行情|刷新日线|更新行情)", text))


def _issue(code: str, message: str) -> InputIssue:
    return InputIssue("needs_clarification", code, message)


@dataclass(frozen=True)
class ConditionUpdate:
    kind: str
    value: str


def parse_condition_update(question: str) -> ConditionUpdate:
    """Consume the entire command, never a valid prefix with an ignored second task."""
    text = unicodedata.normalize("NFKC", question).strip()
    if not text or not all(char.isprintable() for char in text):
        raise _issue("invalid_condition_update", "修改句不能为空或包含控制字符；没有执行。")
    text = re.sub(r"[。.!！]$", "", text).strip()
    text = re.sub(r"\s*[,;]\s*(?:其他|其它|其余)(?:都)?不变$", "", text).strip()
    text = re.sub(r"^(?:请帮我|请|帮我)\s*", "", text)
    if text in _REFRESH:
        return ConditionUpdate("refresh", "previous_shanghai_day")
    forms = (
        ("relative_range", r"(?:(?:把|将)?\s*(?:日期区间|日期|时间范围|研究区间|区间)\s*)?" + _VERB
         + r"\s*(" + _RELATIVE + r")"),
        ("range", r"(?:把|将)?\s*(?:日期区间|日期|时间范围|研究区间|区间)\s*" + _VERB +
         r"\s*(" + _ISO + r"\s*(?:至|到|~)\s*" + _ISO + r")"),
        ("start", r"(?:把|将)?\s*(?:开始日期|起始日期|起始日)\s*" + _VERB + r"\s*(" + _ISO + r")"),
        ("cutoff", r"(?:把|将)?\s*(?:截止日期|结束日期|截止日)\s*" + _VERB + r"\s*(" + _ISO + r")"),
        ("instrument", r"(?:(?:把|将)?\s*(?:股票|证券|研究对象)\s*" + _VERB +
         r"|换成|换为|改成|改看)\s*([\w.* -]{2,40})"),
        ("add", r"(?:添加|增加|加上)\s*(.+)"),
        ("remove", r"(?:删除|移除|去掉)\s*(.+)"),
    )
    for kind, pattern in forms:
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match:
            value = match[1].strip()
            if kind == "instrument" and value.upper() in (
                    "腾讯", "东方财富", "东财", "AKSHARE", "今天", "昨天", "最新", "最近"):
                raise _issue("condition_update_not_supported",
                    "这不是股票修改；来源由启动配置固定。日期请用明确修改句，刷新请单独说“刷新行情”，不是实时查询。")
            if kind == "instrument" and re.match(
                    r"(?:SMA|EMA|RSI|ATR|MACD|BOLL|VOLUME|ADX|OBV|VWMA|DIF|DEA)(?:$|[0-9(])|均线|布林带|量能",
                    value, re.IGNORECASE):
                raise _issue("condition_update_not_supported",
                    "这看起来是指标，不是股票名称；请明确原指标及替换值，例如“把RSI2改成14日”，没有查询。")
            # Stock names are data, not a place to hide another instruction without punctuation.
            if kind == "instrument" and re.search(
                    r"买入|卖出|下单|然后|并且|另外|忽略|改成|换成|添加|删除|为什么|走势|预测|新闻|目标价|止盈|止损", value):
                break
            return ConditionUpdate(kind, value)
    raise _issue("condition_update_not_supported",
        "请一次明确修改一类条件，例如“换成中国平安”“把日期改成2026-09-21至2026-09-24”"
        "“添加ATR2”“删除RSI2”“改成最近一个月”或“刷新行情”；不会忽略附加要求，最新只指上海前一自然日截止的历史日线，不是实时行情。")


def update_conditions(conditions: ResearchConditions, update: ConditionUpdate, *,
                      instrument_id: str, reference_date: date) -> ResearchConditions:
    """Merge one change, preserve unrelated fields, and reuse domain validation."""
    start, cutoff, specs = conditions.start_date, conditions.as_of_date, conditions.indicators
    flags = conditions.applied_defaults
    try:
        if update.kind in ("range", "start", "cutoff", "relative_range", "refresh"):
            if update.kind == "refresh":
                days = (cutoff - start).days + 1
                cutoff = reference_date - timedelta(days=1)
                start = cutoff - timedelta(days=days - 1)
            elif update.kind == "relative_range":
                start, cutoff, relative_flags = _dates(update.value, update.value, reference_date)
            elif update.kind == "range":
                left, right = re.split(r"\s*(?:至|到|~)\s*", update.value)
                start, cutoff = date.fromisoformat(left), date.fromisoformat(right)
            elif update.kind == "start":
                start = date.fromisoformat(update.value)
            else:
                cutoff = date.fromisoformat(update.value)
            unchanged = (start, cutoff) == (conditions.start_date, conditions.as_of_date)
            if unchanged and update.kind != "refresh":
                raise _issue("condition_update_unchanged", "日期没有变化，未重复查询。")
            flags = tuple(item for item in flags if item not in _DATE_FLAGS)
            if update.kind == "refresh":
                flags += ("dates:refresh_latest_previous_day", "cutoff:previous_shanghai_day")
                if unchanged:
                    flags += ("refresh:same_dates",)
            elif update.kind == "relative_range":
                flags += (*relative_flags, "dates:relative_update")
            else:
                flags += ("dates:explicit_update",)
        elif update.kind == "add":
            added = parse_indicator_instance(update.value)
            if added in specs:
                raise _issue("condition_update_duplicate", "该指标实例已存在，未重复添加或查询。")
            specs = (*specs, added)
        elif update.kind == "remove":
            request = MultiIndicatorRequest(instrument_id, start, cutoff, specs, source=conditions.source)
            matches = indicator_candidates(request, update.value)
            if not matches:
                raise _issue("condition_update_target_missing", "原组合没有该指标实例，没有查询。")
            if len(matches) != 1:
                raise _issue("condition_update_ambiguous", "匹配到多个指标，请重述完整删除句并明确参数，例如“删除SMA20”。")
            specs = tuple(spec for spec in specs if spec != matches[0])
            if not specs:
                raise _issue("condition_update_empty_indicators", "至少需要保留一个指标，未删除最后一项或应用默认组合。")
        elif update.kind != "instrument":
            raise _issue("condition_update_not_supported", "未识别的条件修改类型，没有执行。")
        if update.kind in ("add", "remove"):
            flags = tuple(item for item in flags if item not in (
                "indicators:default_eight", "indicators:inherited_then_edited", "indicators:inherited_then_updated"))
            flags += ("indicators:inherited_then_updated",)
        merged = replace(conditions, start_date=start, as_of_date=cutoff, indicators=specs,
                         applied_defaults=flags,
                         reference_date=reference_date if update.kind in ("range", "start", "cutoff", "relative_range", "refresh") else conditions.reference_date)
        # Validate current online authority even when the original relative-date anchor is preserved.
        replace(merged, reference_date=reference_date)
        return merged
    except InputIssue:
        raise
    except (InvalidRequestError, ValueError, OverflowError):
        raise _issue("invalid_condition_update", "新日期、指标实例或组合不符合研究限制；原条件未修改，没有查询行情。") from None
