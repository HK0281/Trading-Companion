"""Natural screening intent with grounded numeric conditions and one bounded quote read."""

import re
import unicodedata
from datetime import datetime, timezone

from trading_companion.agent import AgentTurnResult, _controlled
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.screening import FIELDS, ScreeningRequest, ScreeningService, render_screening_result
from trading_companion.tool_model import ToolDefinition, ToolMessage
from trading_companion.turn_control import checkpoint


ALIASES = {"price": ("股价", "价格", "最新价"), "change_percent": ("涨跌幅", "涨幅", "跌幅"),
    "amount_cny": ("成交额",), "turnover_percent": ("换手率",), "pe_dynamic": ("市盈率", "PE", "市盈"),
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
    return type(question) is str and bool(re.search(r"筛选|选股|选出|挑选|推荐.{0,8}(?:股票|个股)|找.{0,8}(?:股票|潜力股)", question)) \
        and not bool(re.search(r"如何|怎么|怎样|方法|原理|教程|是什么", question))


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
    expected = float(match[1]) * {None: 1, "千": 1000, "万": 10000, "亿": 100000000}[match[2]]
    if value != expected:
        raise ValueError("Screening threshold changed during interpretation")
    return {key: item[key] for key in ("field", "operator", "value")}


def _execute(question, *, session, model, directory, source, now, clock):
    question = unicodedata.normalize("NFKC", question)
    base = dict(stage="screening", input_mode="natural", user_question=question)
    if (not 0 < len(question) <= 2000 or re.search(r"下单|自动买|自动卖|执行交易|https?://|忽略.*(?:规则|系统)", question)):
        return AgentTurnResult(code="screening_request_forbidden", message="可以筛选研究候选，不能执行交易或外部指令。", **base)
    if re.search(r"不要ST|排除ST|剔除ST|非ST|上市天数|上市时间|ROE|净利润|营收|金叉|[0-9]{4}[-年]|去年|历史|回测|截至", question, re.I):
        return AgentTurnResult(status="needs_clarification", code="screening_condition_not_supported",
            message="当前筛选使用当前报价样本，支持价格、涨跌幅、成交额、换手率、动态PE、PB和市值。财报、历史、ST状态与技术形态条件尚未接入筛选，请改用这些数值条件。", **base)
    if (re.search(r"或者|或是|任一|至少一个|其中一个|二选一|\bOR\b|\|\||(?:条件|满足|低于|高于|大于|小于|%|倍|[0-9])\s*或|介于|之间|[0-9]\s*(?:到|至|~|-)\s*[0-9]|排除|剔除|仅限|只看|行业|板块|科创板|创业板|主板|北交所|股息|低估|高估|静态|滚动|TTM|(?<!涨)跌幅|美元|港元|港币|美金|USD|HKD|EUR|万亿|亿万|ETF|基金|美股|港股|债券|期权|期货", question, re.I)
            or re.search(r"(?:选(?:出)?|找(?:出)?|前|展示|推荐)\s*[一二两三四五六七八九十]+\s*(?:只|支|名|个)", question)
            or question.count("按") > 1):
        return AgentTurnResult(status="needs_clarification", code="screening_condition_not_supported",
            message="当前支持各数值条件同时满足（AND），并按一个数值字段排序；暂不支持板块、行业、排除条件、区间或OR组合。数量请用阿拉伯数字；PE采用动态市盈率。", **base)
    mentions = directory.mentioned_spans(question) if directory is not None else ()
    explicit_universe = any(not (re.fullmatch(r"[0-9]{6}", question[start:end])
        and (re.search(r"(?:" + "|".join(_OPERATORS.values()) + r")\s*$", question[:start])
             or re.match(r"\s*(?:元|万元|亿元|股|倍|%|万|亿)", question[end:]))) for start, end in mentions)
    if explicit_universe or re.search(r"从.{1,100}(?:中|里面|之内)", question):
        return AgentTurnResult(status="needs_clarification", code="screening_universe_not_supported",
            message="当前数值筛选覆盖数据源的有界样本，尚不支持指定股票名单作为筛选范围。你可以直接比较这些股票，或去掉名单后筛选报价样本。", **base)
    if source is None:
        return AgentTurnResult(code="screening_source_unavailable", message="当前服务尚未配置报价筛选源。", **base)
    try:
        response = model.complete((ToolMessage("system",
            "Return one interpret_stock_screening call. Copy each explicit numerical condition as its exact phrase; "
            "never invent thresholds or drop conditions. Each phrase has one field, operator and value. Convert 万/亿 "
            "to CNY; percentage points stay e.g. 2 for 2%. Unsupported, ambiguous or range conditions use clarify. "
            "No conditions means an activity shortlist with application defaults price>0, amount descending. "
            "Default sort amount_cny desc, default limit 5. Sorting direction/limit must follow explicit user wording. "
            "Do not provide a potential score, return forecast, ticker, URL or trade action."), ToolMessage("user", question)), tools=(SCREEN_TOOL,))
        checkpoint()
        if response.status != "tool_calls" or len(response.tool_calls) != 1 or response.tool_calls[0].name != SCREEN_TOOL.name:
            raise ValueError("Invalid screening intent response")
        payload = response.tool_calls[0].arguments
        if type(payload) is not dict or payload.keys() != {"intent", "conditions", "sort_by", "sort_direction", "limit"}:
            raise ValueError("Invalid screening intent")
        if payload["intent"] != "screen":
            return AgentTurnResult(status="needs_clarification", code="screening_needs_conditions",
                message="请给出明确数值条件，例如：市盈率低于20、换手率大于2%，按成交额从高到低选5只。", model_calls_attempted=1, model_responses=(response,), **base)
        conditions = [_grounded_condition(item, question) for item in payload["conditions"]]
        operator_count = len(re.findall("|".join(f"(?:{_OPERATORS[kind]})" for kind in ("lte", "gte", "eq", "lt", "gt")), question))
        if operator_count != len(conditions):
            raise ValueError("A numeric condition was omitted or duplicated")
        sort_match = re.search(r"按([^，,。;；]{1,30})", question)
        if sort_match:
            if _mentioned_fields(sort_match[1]) != {payload["sort_by"]}:
                raise ValueError("Sort field changed")
            expected_direction = "asc" if re.search(r"升序|从低到高|从小到大", sort_match[1]) else "desc"
            if payload["sort_direction"] != expected_direction:
                raise ValueError("Sort direction changed")
        elif payload["sort_by"] != "amount_cny" or payload["sort_direction"] != "desc":
            raise ValueError("Unrequested sort choice")
        limits = re.findall(r"(?:选(?:出)?|找(?:出)?|前|展示|推荐)\s*([0-9]+)\s*(?:只|支|名|个)", question)
        if (limits and (len(set(limits)) != 1 or payload["limit"] != int(limits[0]))
                or not limits and payload["limit"] != 5):
            raise ValueError("Result limit changed")
        expected_fields = {item["field"] for item in conditions}
        if sort_match:
            expected_fields.add(payload["sort_by"])
        if not _mentioned_fields(question) <= expected_fields:
            raise ValueError("A requested screening field was omitted")
        defaulted = not conditions
        if defaulted:
            if re.search(r"[<>≤≥]|低于|高于|大于|小于|不超过|至少|不低于|不高于|等于|介于|到|之间", question):
                raise ValueError("Explicit thresholds were omitted")
            conditions = [{"field": "price", "operator": "gt", "value": 0}]
        request = ScreeningRequest.from_dict({**{key: payload[key] for key in ("sort_by", "sort_direction", "limit")},
                                               "conditions": conditions})
        session.begin_research()
        checkpoint()
        snapshot = source.read_snapshot(directory)
        checkpoint()
        result = ScreeningService(clock=clock).screen(snapshot, request)
        text = render_screening_result(result)
        if defaulted:
            text = "未指定数值条件，本轮先按成交额列出活跃研究候选。\n\n" + text
        return AgentTurnResult(status="completed" if result.rows else "limited", code=result.code,
            message=text, screening_result=result, screening_defaulted=defaulted,
            model_calls_attempted=1, model_responses=(response,), **base)
    except (DataSourceError, DataValidationError):
        return AgentTurnResult(code="screening_data_unavailable", message="本轮报价快照未取得或未通过身份与时间校验，未生成筛选名单。请稍后重试。",
            model_calls_attempted=1, **base)
    except (InvalidRequestError, ValueError, TypeError, KeyError, ModelCallError, ModelConfigurationError, ModelRequestError):
        return AgentTurnResult(status="needs_clarification", code="screening_intent_invalid",
            message="筛选条件没有完整通过核对，未查询行情。请使用明确的字段、阈值和排序，例如：PE低于20且换手率大于2%，按成交额降序选5只。",
            model_calls_attempted=1, **base)


def run_screening_turn(question, *, session, model, directory, source, now, control=None, clock=None):
    return _controlled(lambda checked, working: _execute(question, session=working, model=checked, directory=directory,
        source=source, now=now, clock=clock or (lambda: datetime.now(timezone.utc))),
        model=model, session=session, control=control, default_calls=1, input_mode="natural")
