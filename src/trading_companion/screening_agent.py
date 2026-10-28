"""Natural screening intent with grounded numeric conditions and one bounded quote read."""

import re
import unicodedata
from datetime import datetime, timezone

from trading_companion.agent import AgentTurnResult, _controlled
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.screening import FIELDS, ScreeningService, render_screening_result
from trading_companion.screening_context import SavedScreening
from trading_companion.screening_intent import (SCREEN_TOOL, _OPERATORS, _plain_activity_limit,
    checked_screening_request, edited_screening_request, is_screening_request, _clauses, MissingScreeningField)
from trading_companion.tool_model import ToolMessage
from trading_companion.turn_control import checkpoint
from trading_companion.turn_trace import trace_call


def _preflight(question, *, directory, base):
    if (not 0 < len(question) <= 2000 or re.search(r"下单|自动买|自动卖|执行交易|https?://|忽略.*(?:规则|系统)", question)):
        return AgentTurnResult(code="screening_request_forbidden", message="可以筛选研究候选，不能执行交易或外部指令。", **base)
    if re.search(r"不要ST|排除ST|剔除ST|非ST|上市天数|上市时间|ROE|净利润|营收|金叉|[0-9]{4}[-年]|去年|历史|回测|截至", question, re.I):
        return AgentTurnResult(status="needs_clarification", code="screening_condition_not_supported",
            message="当前筛选使用当前报价样本，支持价格、涨跌幅、成交额、换手率、动态PE、PB和市值。财报、历史、ST状态与技术形态条件尚未接入筛选，请改用这些数值条件。", **base)
    if (re.search(r"或者|或是|任一|至少一个|其中一个|二选一|\bOR\b|\|\||(?:条件|满足|低于|高于|大于|小于|%|倍|[0-9])\s*或|排除|剔除|仅限|只看|行业|板块|科创板|创业板|主板|北交所|股息|低估|高估|静态|滚动|TTM|(?<!涨)跌幅|美元|港元|港币|美金|USD|HKD|EUR|万亿|亿万|ETF|基金|美股|港股|债券|期权|期货", question, re.I)
            or re.search(r"(?:选(?:出)?|找(?:出)?|前|展示|推荐)\s*[一二两三四五六七八九十]+\s*(?:只|支|名|个)", question)
            or question.count("按") > 1):
        return AgentTurnResult(status="needs_clarification", code="screening_condition_not_supported",
            message="当前支持各数值条件同时满足（AND）、明确双边区间及单字段排序；暂不支持板块、行业、排除条件或OR组合。数量请用阿拉伯数字；PE采用动态市盈率。", **base)
    try:
        groups, _, _ = _clauses(question, wrappers=True)
    except ValueError:
        groups = []
    mentions = directory.mentioned_spans(question) if directory is not None else ()
    explicit_universe = any(not any(a <= start and end <= b for a, b, _ in groups) and not (re.fullmatch(r"[0-9]{6}", question[start:end])
        and (re.search(r"(?:" + "|".join(_OPERATORS.values()) + r")\s*$", question[:start])
             or re.match(r"\s*(?:元|万元|亿元|股|倍|%|万|亿)", question[end:]))) for start, end in mentions)
    if explicit_universe or re.search(r"从.{1,100}(?:中|里面|之内)", question):
        return AgentTurnResult(status="needs_clarification", code="screening_universe_not_supported",
            message="当前数值筛选覆盖数据源的有界样本，尚不支持指定股票名单作为筛选范围。你可以直接比较这些股票，或去掉名单后筛选报价样本。", **base)


def _execute(question, *, session, model, directory, source, now, clock, research_source):
    question = unicodedata.normalize("NFKC", question)
    base = dict(stage="screening", input_mode="natural", user_question=question)
    issue = _preflight(question, directory=directory, base=base)
    if issue is not None:
        return issue
    if source is None:
        return AgentTurnResult(code="screening_source_unavailable", message="当前服务尚未配置报价筛选源。", **base)
    try:
        response = model.complete((ToolMessage("system",
            "Return one interpret_stock_screening call. Copy each explicit numerical condition as its exact phrase; "
            "never invent thresholds or drop conditions. Each atom has one field, operator and value. "
            "For a two-sided interval emit two atoms, copying the whole exact interval phrase for both. "
            "在10到20之间 / 介于10和20之间 includes both endpoints. Preserve symbolic < versus <=. Convert 万/亿 "
            "to CNY; percentage points stay e.g. 2 for 2%. Unsupported or ambiguous conditions use clarify. "
            "No conditions means an activity shortlist with application defaults price>0, amount descending. "
            "Default sort amount_cny desc, default limit 5. Sorting direction/limit must follow explicit user wording. "
            "Do not provide a potential score, return forecast, ticker, URL or trade action."), ToolMessage("user", question)), tools=(SCREEN_TOOL,))
        checkpoint()
        if response.status != "tool_calls" or len(response.tool_calls) != 1 or response.tool_calls[0].name != SCREEN_TOOL.name:
            raise ValueError("Invalid screening intent response")
        payload = response.tool_calls[0].arguments
        if type(payload) is not dict or payload.keys() != {"intent", "conditions", "sort_by", "sort_direction", "limit"}:
            raise ValueError("Invalid screening intent")
        activity_limit = _plain_activity_limit(question)
        if activity_limit is not None:
            # A model clarification cannot revoke the documented application defaults.
            payload = {"intent": "screen", "conditions": [], "sort_by": "amount_cny",
                       "sort_direction": "desc", "limit": activity_limit}
        if type(payload["limit"]) is not int or not 1 <= payload["limit"] <= 20:
            raise ValueError("Screening result count exceeds the agent's bound")
        if payload["intent"] != "screen":
            return AgentTurnResult(status="needs_clarification", code="screening_needs_conditions",
                message="请给出明确数值条件，例如：市盈率低于20、换手率大于2%，按成交额从高到低选5只。", model_calls_attempted=1, model_responses=(response,), **base)
        request, defaulted = checked_screening_request(question, payload)
        return _collect(request, defaulted=defaulted, responses=(response,), session=session,
            directory=directory, source=source, clock=clock, research_source=research_source, base=base)
    except (DataSourceError, DataValidationError):
        return AgentTurnResult(code="screening_data_unavailable", message="本轮报价快照未取得或未通过身份与时间校验，未生成筛选名单。请稍后重试。",
            model_calls_attempted=1, **base)
    except (InvalidRequestError, ValueError, TypeError, KeyError, ModelCallError, ModelConfigurationError, ModelRequestError):
        return AgentTurnResult(status="needs_clarification", code="screening_intent_invalid",
            message="筛选条件没有完整通过核对，未查询行情。请使用明确的字段、阈值和排序，例如：PE低于20且换手率大于2%，按成交额降序选5只。",
            model_calls_attempted=1, **base)


def _collect(request, *, defaulted, responses, session, directory, source, clock, research_source, base):
    session.begin_research()
    checkpoint()
    snapshot = trace_call("source", "screening_snapshot", source.read_snapshot, directory)
    checkpoint()
    result = ScreeningService(clock=clock).screen(snapshot, request)
    text = render_screening_result(result)
    if defaulted:
        text = "未指定数值条件，本轮先按成交额列出活跃研究候选。\n\n" + text
    session.screening_saved = SavedScreening.checked(result, directory=directory, quote_source=source,
                                                    now=clock(), research_source=research_source)
    return AgentTurnResult(status="completed" if result.rows else "limited", code=result.code,
        message=text, screening_result=result, screening_defaulted=defaulted,
        model_responses=responses, **base)


def run_screening_turn(question, *, session, model, directory, source, now, control=None, clock=None,
                       research_source="tencent"):
    return _controlled(lambda checked, working: _execute(question, session=working, model=checked, directory=directory,
        source=source, now=now, clock=clock or (lambda: datetime.now(timezone.utc)), research_source=research_source),
        model=model, session=session, control=control, default_calls=1, input_mode="natural")


def _execute_edit(question, *, session, directory, source, clock, research_source, development):
    question = unicodedata.normalize("NFKC", question)
    base = dict(stage="screening", input_mode="natural", user_question=question)
    saved = session.screening_saved
    if saved is None:
        return AgentTurnResult(status="needs_clarification", code="screening_list_unavailable",
            message="当前没有可用的筛选条件，请先给出完整筛选请求；未调用模型或查询报价。", **base)
    reason = saved.invalid_reason(directory=directory, quote_source=source, source=research_source,
                                  development=development, now=clock())
    if reason:
        session.clear_screening()
        return AgentTurnResult(status="needs_clarification", code=reason[0], message=reason[1], **base)
    # The full edit parser rejects identities/unknown text; no partial model
    # interpretation and no source access occur for an incomplete mutation.
    if not 0 < len(question) <= 2000:
        return AgentTurnResult(status="needs_clarification", code="screening_edit_invalid",
            message="请缩短本轮筛选条件修改；没有查询报价。", **base)
    try:
        request = edited_screening_request(question, saved.request)
    except MissingScreeningField as issue:
        label = FIELDS[issue.field][0]
        return AgentTurnResult(status="needs_clarification", code="screening_edit_target_missing",
            message=f"当前筛选条件中没有{label}，不能替换。"
                    f"如需新增，请说“再加{label}{issue.predicate}”；未修改原条件或查询报价。", **base)
    except (ValueError, TypeError, KeyError):
        return AgentTurnResult(status="needs_clarification", code="screening_edit_invalid",
            message="筛选条件修改没有完整通过核对，未查询报价。请每次明确增加数值条件、替换一个已有字段，"
                    "或修改排序与数量；例如“再加换手率大于2%”“把PE改为低于15”“按市净率升序选3只”。"
                    "未修改原条件，不接受冲突、重复、无变化或未知附加要求。", **base)
    try:
        return _collect(request, defaulted=False, responses=(), session=session, directory=directory,
            source=source, clock=clock, research_source=research_source, base=base)
    except (DataSourceError, DataValidationError):
        return AgentTurnResult(code="screening_data_unavailable",
            message="本轮报价快照未取得或未通过校验，未生成新名单；旧名单已失效。请重新给出完整筛选请求，不会自动换源。", **base)


def run_screening_edit_turn(question, *, session, model, directory, source, now, control=None, clock=None,
                            research_source="tencent", development=False):
    return _controlled(lambda _, working: _execute_edit(question, session=working, directory=directory,
        source=source, clock=clock or (lambda: datetime.now(timezone.utc)), research_source=research_source,
        development=development), model=model, session=session, control=control, default_calls=1, input_mode="natural")
