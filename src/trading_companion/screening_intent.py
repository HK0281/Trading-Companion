"""Grounded numeric screening intent, separated from turn execution."""

import re
import unicodedata
from collections import Counter
from dataclasses import replace
from decimal import Decimal

from trading_companion.screening import FIELDS, ScreeningCondition, ScreeningRequest
from trading_companion.tool_model import ToolDefinition


ALIASES = {"price": ("股价", "价格", "最新价"), "change_percent": ("涨跌幅", "涨幅", "跌幅"),
    "amount_cny": ("成交额",), "turnover_percent": ("换手率",), "pe_dynamic": ("动态市盈率", "动态PE", "市盈率", "PE", "市盈"),
    "pb": ("市净率", "PB", "市净"), "market_cap_cny": ("总市值", "市值"),
    "float_market_cap_cny": ("流通市值",)}
_OPERATORS = {"lt": r"低于|小于|(?<![<>=])<(?![=])", "lte": r"不高于|不超过|不大于|小于等于|至多|<=|≤",
    "gt": r"高于|大于|超过|(?<![<>=])>(?![=])", "gte": r"不低于|不小于|大于等于|至少|>=|≥", "eq": r"等于|(?<![<>=])=(?![=])"}
SCREEN_TOOL = ToolDefinition("interpret_stock_screening", "Extract only explicit numeric conditions from the question.", {
    "type": "object", "properties": {
        "intent": {"type": "string", "enum": ["screen", "clarify"]},
        "conditions": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
            "field": {"type": "string", "enum": list(FIELDS)}, "operator": {"type": "string", "enum": list(_OPERATORS)},
            "value": {"type": "number"}, "phrase": {"type": "string"}},
            "required": ["field", "operator", "value", "phrase"], "additionalProperties": False}},
        "sort_by": {"type": "string", "enum": list(FIELDS)}, "sort_direction": {"type": "string", "enum": ["asc", "desc"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
    "required": ["intent", "conditions", "sort_by", "sort_direction", "limit"], "additionalProperties": False})


def is_screening_request(question):
    if type(question) is str:
        # A displayed-list reference is not a request to collect another quote sample.
        question = re.sub(r"筛选(?:名单|结果)(?=中|里|的|第|前)", "", question)
    return type(question) is str and bool(re.search(
        r"筛选|选股|选出|挑选|(?:选|推荐|找).{0,8}(?:股票|个股|潜力股|候选股)|"
        r"(?:哪些|什么)(?:股票|个股).{0,8}值得关注|值得关注的(?:股票|个股)", question)) \
        and not bool(re.search(r"如何|怎么|怎样|方法|原理|教程|是什么", question))


def _plain_activity_limit(question):
    """Only a complete, filter-free request can receive application-owned defaults."""
    wording = question.strip().rstrip("。.!！?？").strip()
    if re.fullmatch(r"(?:哪些|什么)(?:股票|个股)值得关注|值得关注的(?:股票|个股)", wording):
        return 5
    match = re.fullmatch(r"(?:(?:请|帮我|给我|麻烦|请你|能否|可以)\s*)*"
        r"(?:(?:选(?:出)?|推荐|找|挑选|筛选)\s*(?:(?:几个|几只|一些|几支)|(?P<count>[0-9]{1,2})\s*(?:只|支|个))?"
        r"\s*(?:股票|个股|潜力股|候选股)|选股)(?:吧|吗)?", wording)
    return int(match["count"]) if match and match["count"] else 5 if match else None


def _field_mentions(text):
    matches = [(match.start(), match.end(), kind) for kind, aliases in ALIASES.items() for alias in aliases
               for match in re.finditer((r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])")
                   if alias.isascii() else re.escape(alias), text, re.I)]
    return [(start, end, kind) for start, end, kind in matches if not any(
        other_start <= start and end <= other_end and (start, end) != (other_start, other_end)
        for other_start, other_end, _ in matches)]


def _mentioned_fields(text):
    return {kind for _, _, kind in _field_mentions(text)}


def _grounded_condition(item, question):
    if type(item) is not dict or item.keys() != {"field", "operator", "value", "phrase"}:
        raise ValueError("Invalid numeric screening condition")
    field, operator, value, phrase = (item[key] for key in ("field", "operator", "value", "phrase"))
    if (field not in FIELDS or operator not in _OPERATORS or type(phrase) is not str or not phrase
            or phrase not in question or type(value) not in (int, float)):
        raise ValueError("Screening condition is not copied from the question")
    if _mentioned_fields(phrase) != {field}:
        raise ValueError("Screening field is not grounded")
    # Choose the longest matching operator so '不超过' cannot become '超过'.
    candidates = [(match.start(), len(match.group()), kind) for kind, pattern in _OPERATORS.items()
                  for match in re.finditer(pattern, phrase)]
    if not candidates or max(candidates, key=lambda candidate: candidate[1])[2] != operator:
        raise ValueError("Screening operator is not grounded")
    operation_start, operation_length, _ = max(candidates, key=lambda candidate: candidate[1])
    numbers = list(re.finditer(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*(亿|万|千)?", phrase))
    if len(numbers) != 1:
        raise ValueError("Use a single threshold per condition")
    match = numbers[0]
    if (not any(end <= operation_start for _, end, _ in _field_mentions(phrase))
            or match.start() < operation_start + operation_length
            or phrase[operation_start + operation_length:match.start()].strip()):
        raise ValueError("Use field, operator, then numeric threshold in that order")
    suffix = phrase[match.end():].strip()
    unit = FIELDS[field][1]
    unit_pattern = {"CNY": r"(?:元|人民币|元人民币|人民币元|CNY|RMB)?",
                    "CNY/share": r"(?:元(?:/股)?|人民币|CNY|RMB)?",
                    "percent": r"%?", "ratio": r"倍?"}[unit]
    if ((unit in ("percent", "ratio") and match[2] is not None)
            or re.fullmatch(unit_pattern + r"(?:的(?:股票|个股)?)?", suffix, re.I) is None):
        raise ValueError("Screening threshold has an unsupported unit or trailing expression")
    expected = float(Decimal(match[1]) * {None: 1, "千": 1000, "万": 10000, "亿": 100000000}[match[2]])
    if value != expected:
        raise ValueError("Screening threshold changed during interpretation")
    return {key: item[key] for key in ("field", "operator", "value")}


# Every accepted token consumes original text; unknown clauses cannot disappear.
_FIELD = re.compile("|".join(sorted((
    (r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])") if alias.isascii() else re.escape(alias)
    for aliases in ALIASES.values() for alias in aliases), key=len, reverse=True)), re.I)
_OP = re.compile("|".join(_OPERATORS[kind] for kind in ("lte", "gte", "eq", "lt", "gt")))
_VALUE = re.compile(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*(亿|万|千)?\s*"
                    r"(元人民币|人民币元|人民币|元/股|CNY|RMB|元|%|倍)?", re.I)
_JOIN = re.compile(r"\s*(?:并且|而且|且|并|和|与|AND|[,，;；、])\s*", re.I)
_LIMIT = re.compile(r"(?:选(?:出)?|找(?:出)?|前|展示|推荐)\s*([0-9]+)\s*(?:只|支|名|个)")
_POLITE = re.compile(r"\s*(?:(?:请你|请|帮我|给我|麻烦|能否|可以)\s*)*")
_STOCK = re.compile(r"(?:的)?(?:股票|个股|潜力股|候选股)|的|吧|吗|[。.!！?？]")
_ACTION = re.compile(r"筛选(?:出)?|选股|选出|挑选(?:出)?|推荐|找出|找|选")
_EDIT_PREFIX = re.compile(r"(?:在)?(?:原(?:来)?的?)?筛选条件(?:上|中)?[，,:：\s]*")
_ADD = re.compile(r"再加(?:上)?|加上|再增加|增加|新增")
_DIRECTION = re.compile(r"升序|从低到高|从小到大|降序|从高到低|从大到小")


def _field_at(text, position):
    match = _FIELD.match(text, position)
    if match is None:
        raise ValueError("Expected one supported screening field")
    return next(iter(_mentioned_fields(match.group()))), match.end()


def _value_at(text, position, field):
    match = _VALUE.match(text, position)
    if match is None:
        raise ValueError("Expected a signed numeric threshold")
    multiplier, unit = match[2], (match[3] or "").upper()
    allowed = {"CNY": ("", "元", "人民币", "元人民币", "人民币元", "CNY", "RMB"),
               "CNY/share": ("", "元", "元/股", "人民币", "CNY", "RMB"),
               "percent": ("", "%"), "ratio": ("", "倍")}[FIELDS[field][1]]
    if unit not in allowed or multiplier is not None and FIELDS[field][1] in ("percent", "ratio"):
        raise ValueError("Threshold units do not match the field")
    number = Decimal(match[1]) * {None: 1, "千": 1000, "万": 10000, "亿": 100000000}[multiplier]
    value = float(number)
    # ScreeningCondition also rejects infinities and booleans.
    ScreeningCondition(field, "eq", value)
    return value, match.end(), multiplier


def _operator_at(text, position):
    match = _OP.match(text, position)
    if match is None:
        raise ValueError("Expected one explicit comparison")
    kind = next(kind for kind, pattern in _OPERATORS.items() if re.fullmatch(pattern, match.group()))
    return kind, match.end()


def _skip_space(text, position):
    return position + len(text[position:]) - len(text[position:].lstrip())


def _predicate(text, position, field):
    """Parse field-owned thresholds without inventing another occurrence of the field."""
    position = _skip_space(text, position)
    interval = re.match(r"(?:在|介于)\s*", text[position:])
    if interval:
        lower, end, lower_scale = _value_at(text, position + interval.end(), field)
        join = re.match(r"\s*(?:到|至|和|与|~)\s*", text[end:])
        if join is None:
            raise ValueError("Expected a range separator")
        upper, end, upper_scale = _value_at(text, end + join.end(), field)
        closing = re.match(r"\s*之间", text[end:])
        if closing is None or lower > upper or (lower_scale is None) != (upper_scale is None):
            raise ValueError("Reversed, ambiguous or mixed-scale interval")
        return (ScreeningCondition(field, "gte", lower), ScreeningCondition(field, "lte", upper)), end + closing.end()
    operator, end = _operator_at(text, position)
    value, end, _ = _value_at(text, _skip_space(text, end), field)
    conditions = (ScreeningCondition(field, operator, value),)
    join = _JOIN.match(text, end)
    if join and _OP.match(text, join.end()):
        second, next_position = _operator_at(text, join.end())
        value, end, _ = _value_at(text, _skip_space(text, next_position), field)
        conditions += (ScreeningCondition(field, second, value),)
    return conditions, end


def _group_at(text, position):
    if _FIELD.match(text, position):
        field, end = _field_at(text, position)
        return _predicate(text, end, field)
    # Chained symbolic inequalities must contain two sides of one field. A
    # single reversed comparison remains unsupported rather than being flipped.
    leading = _VALUE.match(text, position)
    if leading is None:
        raise ValueError("Expected a numeric screening condition")
    first, end = _operator_at(text, _skip_space(text, leading.end()))
    if re.fullmatch(r"[<>]=?|[≤≥]", text[leading.end():end].strip()) is None:
        raise ValueError("Use symbols for a chained inequality")
    field, end = _field_at(text, _skip_space(text, end))
    lower, _, _ = _value_at(text, position, field)
    second_start = _skip_space(text, end)
    second, end = _operator_at(text, second_start)
    if re.fullmatch(r"[<>]=?|[≤≥]", text[second_start:end]) is None:
        raise ValueError("Use two symbolic boundaries")
    upper, end, _ = _value_at(text, _skip_space(text, end), field)
    inverse = {"lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte"}
    if (first not in inverse or second not in inverse
            or (inverse[first] in ("gt", "gte")) == (second in ("gt", "gte"))):
        raise ValueError("Both boundaries must point into the interval")
    return (ScreeningCondition(field, inverse[first], lower), ScreeningCondition(field, second, upper)), end


def _sort_at(text, position):
    field, end = _field_at(text, _skip_space(text, position + 1))
    end = _skip_space(text, end)
    direction = _DIRECTION.match(text, end)
    end = direction.end() if direction else end
    suffix = re.match(r"\s*(?:排序|排列)", text[end:])
    return field, ("asc" if direction and direction.group() in ("升序", "从低到高", "从小到大") else "desc"), (end + suffix.end() if suffix else end)


def _clauses(text, *, wrappers=False):
    """Return exact source spans, plus at most one sort and one result count."""
    position = _POLITE.match(text).end() if wrappers else 0
    groups, sort, limit = [], None, None
    while (position := _skip_space(text, position)) < len(text):
        join = _JOIN.match(text, position)
        if join:
            position = join.end()
            if position == len(text):
                raise ValueError("A trailing conjunction has no condition")
            continue
        count = _LIMIT.match(text, position)
        if count:
            if limit is not None or not 1 <= int(count[1]) <= 20:
                raise ValueError("Use one result count between 1 and 20")
            limit, position = int(count[1]), count.end()
        elif text[position] == "按":
            if sort is not None:
                raise ValueError("Use a single sorting field")
            field, direction, position = _sort_at(text, position)
            sort = field, direction
        elif wrappers and (word := _ACTION.match(text, position) or _STOCK.match(text, position)):
            position = word.end()
        else:
            start = position
            conditions, position = _group_at(text, position)
            groups.append((start, len(text[:position].rstrip()), conditions))
    return groups, sort, limit


def _consistent(conditions):
    """Reject empty intersections before any quote side effect."""
    for field in FIELDS:
        items = [item for item in conditions if item.field == field]
        equals = {item.value for item in items if item.operator == "eq"}
        lower = [(item.value, item.operator == "gt") for item in items if item.operator in ("gt", "gte")]
        upper = [(item.value, item.operator == "lt") for item in items if item.operator in ("lt", "lte")]
        if len(equals) > 1:
            raise ValueError("Contradictory equality constraints")
        if equals:
            lower.append((next(iter(equals)), False))
            upper.append((next(iter(equals)), False))
        if lower and upper:
            low, low_strict = max(lower)
            high, high_strict = min(upper, key=lambda item: (item[0], not item[1]))
            if low > high or low == high and (low_strict or high_strict):
                raise ValueError("Empty screening interval")


def checked_screening_request(question, payload):
    activity_limit = _plain_activity_limit(question)
    if activity_limit is not None:
        return ScreeningRequest((ScreeningCondition("price", "gt", 0),), limit=activity_limit), True
    groups, sort, limit = _clauses(question, wrappers=True)
    conditions = tuple(item for _, _, items in groups for item in items)
    if type(payload["conditions"]) is not list or len(payload["conditions"]) > 12:
        raise ValueError("Invalid condition array")
    remaining = Counter(conditions)
    for item in payload["conditions"]:
        if type(item) is not dict or item.keys() != {"field", "operator", "value", "phrase"}:
            raise ValueError("Invalid condition payload")
        condition = ScreeningCondition(item["field"], item["operator"], item["value"])
        phrase = item["phrase"]
        if type(phrase) is not str or not phrase or phrase not in question:
            raise ValueError("Condition phrase must be copied verbatim")
        candidates = [(start, end, items) for start, end, items in groups if condition in items]
        if not candidates or remaining[condition] <= 0:
            raise ValueError("Invented, changed or duplicated condition")
        # Two-sided shorthand binds both atoms to the whole original phrase;
        # it never fabricates a second field mention absent from the question.
        if not any(question[start:end] in phrase for start, end, _ in candidates):
            raise ValueError("The phrase does not cover its original numeric boundary")
        if all(len(items) == 1 for _, _, items in candidates):
            _grounded_condition(item, question)
        remaining[condition] -= 1
    if any(remaining.values()):
        raise ValueError("A numeric boundary was omitted")
    if (payload["sort_by"], payload["sort_direction"]) != (sort or ("amount_cny", "desc")) or payload["limit"] != (limit or 5):
        raise ValueError("Sort or count changed during interpretation")
    _consistent(conditions)
    defaulted = not conditions
    return ScreeningRequest(conditions or (ScreeningCondition("price", "gt", 0),),
        sort_by=payload["sort_by"], sort_direction=payload["sort_direction"], limit=payload["limit"]), defaulted


def _starts_predicate(text, position):
    position = _skip_space(text, position)
    operator = _OP.match(text, position)
    interval = re.match(r"(?:在|介于)\s*", text[position:])
    end = operator.end() if operator else position + interval.end() if interval else None
    return end is not None and _VALUE.match(text, _skip_space(text, end)) is not None


def is_screening_edit(question, *, session):
    """Recognize anchored mutation syntax, independently of whether a list exists."""
    if type(question) is not str:
        return False
    text = unicodedata.normalize("NFKC", question).strip()
    position = _POLITE.match(text).end()
    marker = _EDIT_PREFIX.match(text, position)
    if marker:
        return True
    add = _ADD.match(text, position)
    try:
        if add:
            start = _skip_space(text, add.end())
            if _FIELD.match(text, start):
                _, end = _field_at(text, start)
                return _starts_predicate(text, end)
            # A valid symbolic interval can start with a number instead of a field.
            _group_at(text, start)
            return True
        if text.startswith("把", position):
            _, end = _field_at(text, _skip_space(text, position + 1))
            mutation = re.match(r"\s*(?:改为|改成)\s*", text[end:])
            return mutation is not None and _starts_predicate(text, end + mutation.end())
        if text.startswith("按", position):
            _, end = _field_at(text, _skip_space(text, position + 1))
            end = _skip_space(text, end)
            # '按市盈率分析某公司' describes research, not a sort edit.
            return bool(_DIRECTION.match(text, end) or re.match(r"排序|排列", text[end:]) or _LIMIT.match(text, end))
    except ValueError:
        pass
    return False


class MissingScreeningField(ValueError):
    """A complete replacement targets a field absent from the saved conditions."""

    def __init__(self, field, predicate):
        super().__init__("The replacement field is absent from the screening conditions")
        self.field, self.predicate = field, predicate


def edited_screening_request(question, original):
    """One bounded mutation family per turn; untouched request fields are inherited."""
    text = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！?？").strip()
    position = _POLITE.match(text).end()
    marker = _EDIT_PREFIX.match(text, position)
    position = marker.end() if marker else position
    add = _ADD.match(text, position)
    if add:
        groups, sort, limit = _clauses(text[add.end():].strip())
        if not groups or sort is not None or limit is not None:
            raise ValueError("Add numeric conditions in a separate turn")
        request = replace(original, conditions=original.conditions + tuple(item for _, _, items in groups for item in items))
    elif text.startswith("把", position):
        field, end = _field_at(text, _skip_space(text, position + 1))
        mutation = re.match(r"\s*(?:改为|改成)\s*", text[end:])
        if mutation is None:
            raise ValueError("Replace an existing field explicitly")
        predicate_start = end + mutation.end()
        conditions, end = _predicate(text, predicate_start, field)
        if text[end:].strip():
            raise ValueError("Unconsumed condition edit")
        if not any(item.field == field for item in original.conditions):
            raise MissingScreeningField(field, text[predicate_start:end].strip())
        updated, inserted = [], False
        for item in original.conditions:
            if item.field != field:
                updated.append(item)
            elif not inserted:
                updated.extend(conditions)
                inserted = True
        request = replace(original, conditions=tuple(updated))
    else:
        groups, sort, limit = _clauses(text[position:])
        if groups or sort is None:
            raise ValueError("Use an explicit sort edit")
        request = replace(original, sort_by=sort[0], sort_direction=sort[1], limit=limit or original.limit)
    _consistent(request.conditions)
    if request == original:
        raise ValueError("The edit made no change")
    return request


