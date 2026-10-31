"""Controlled single-index research and expiring, identity-isolated continuation."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from uuid import uuid4

from trading_companion.agent_input import InputIssue
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.index_answer import synthesize_index_answer, public_index, checked_answer, PARTIAL_NOTICE
from trading_companion.index_contracts import IndexResearchRequest, IndexResearchResult, DEFAULTS, VOLUME_KINDS
from trading_companion.index_source import TencentIndexSource, synthetic_index_bars
from trading_companion.market_scope import market_selection, market_reply_code, index_continuation
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.product_scope import _positive_scope, _INDICATOR_CUE, _indicator
from trading_companion.research_edit import replace_indicator, is_indicator_edit
from trading_companion.research_intent import _dates, _DATE_CUE, historical_date_inputs, _normalize_null_strings
from trading_companion.research_objective import extract_outlook, mask_outlook_expressions
from trading_companion.tool_model import ToolDefinition, ToolMessage, ToolModelResponse, MAX_USER_CHARACTERS
from trading_companion.turn_control import checkpoint, reserve_analysis
from trading_companion.turn_trace import trace_call

HISTORY = ('查看上次结果', '查看上一次结果', '上次研究结果')
INTENT_NAME = 'interpret_index_research'
INTENT_TOOL = ToolDefinition(INTENT_NAME,
    'Extract the explicit historical date/window from this index question VERBATIM, or null when omitted. '
    'Bare 最近/近期/近来/近日/目前/现在 use the application default: return null. '
    'Keep quantified periods such as 最近一个月 or 近20日 verbatim. '
    'Future observation horizons are not historical windows. Identity, indicators, defaults and source are controlled by the application.',
    {'type': 'object', 'additionalProperties': False, 'required': ['date_expression'],
     'properties': {'date_expression': {'type': ['string', 'null']}}})


@dataclass(frozen=True)
class SavedIndexResearch:
    result: IndexResearchResult
    answer: object
    saved_at: datetime
    development_source: object = None
    plan_notice: bool = False


@dataclass(frozen=True)
class PendingIndexResearch:
    question: str
    created_at: datetime
    source: str
    development_source: object


def owns_index_request(question, session=None, directory=None):
    if market_reply_code(question, directory=directory) is not None:
        return True
    kind = getattr(getattr(session, 'latest_research', None), 'kind', None)
    if session is not None and session.index_pending is not None:
        from trading_companion.routing_context import independent_task
        if not independent_task(question, directory):
            from trading_companion.indicator_knowledge import parse_knowledge_question
            from trading_companion.open_knowledge import is_general_knowledge_question
            return (type(question) is str and question.strip().rstrip('。.!！') not in
                    (*HISTORY, '取消', '取消分析', '取消研究', '算了', '新对话', '重新开始', '清空会话')
                    and parse_knowledge_question(question) is None
                    and not is_general_knowledge_question(question, directory=directory))
    return (type(question) is str and question.strip().rstrip('。.!！') in HISTORY and kind == 'index'
            or index_continuation(question, latest_kind=kind, directory=directory))


def _reply(code, message, *, status='needs_clarification'):
    from trading_companion.agent import AgentTurnResult
    return AgentTurnResult(status=status, code=code, message=message, stage='clarification', input_mode='natural')


def _saved(session, *, source, development_source, now, history):
    saved = session.index_saved
    if saved is None:
        return None
    if (not saved.saved_at <= now < saved.saved_at + timedelta(minutes=15)
            or saved.result.request.source != source or saved.development_source != development_source):
        session.index_saved = None
        return None
    try:
        saved.result.__post_init__()
        if not history and (not session._subject_current
                or getattr(session.latest_research, 'kind', None) != 'index'
                or session.latest_research.scope != saved.result.request):
            return None
    except (ValueError, TypeError, AttributeError):
        session.index_saved = None
        return None
    return saved


def _outcome(result, answer, *, responses=(), saved_at=None, plan_notice=False):
    from trading_companion.agent import AgentTurnResult
    answer = checked_answer(result, answer)
    view = public_index(result, answer, saved_at=saved_at, plan_notice=plan_notice)
    complete = answer.origin == 'model' and not answer.quality.partial and not any(s.kind in VOLUME_KINDS for s in result.request.indicators)
    return AgentTurnResult(status='completed' if complete else 'limited',
        code='index_research_completed' if answer.origin == 'model' else 'index_research_facts_only',
        stage='index_research', input_mode='natural',
        message=(PARTIAL_NOTICE + '，已保留通过核对的指数研判。' if answer.quality.partial else '指数研究已完成。')
            if answer.origin == 'model' else '已保留指数读数，暂未形成完整模型研判。',
        index_result=result, index_answer=answer, index_research=view,
        model_responses=responses, session_event='index_history' if saved_at else 'index_research')


def _indicators(question, previous=None):
    positive, excluded = _positive_scope(question)
    if 'technical' in excluded:
        raise InputIssue('out_of_scope', 'index_technical_excluded', '本批只支持指数技术研究；已按要求排除技术面，未查询行情。')
    specs = tuple(dict.fromkeys(spec for match in _INDICATOR_CUE.finditer(positive)
                               for spec in _indicator(match[0])))
    if not specs:
        specs = previous or DEFAULTS
    specs = tuple(s for s in specs if 'indicator:' + s.kind not in excluded)
    if not specs:
        raise InputIssue('needs_clarification', 'index_indicators_required', '请至少保留一个需要观察的技术指标。')
    return specs


def _execute(question, *, session, model, directory, source, development_source, now, reference_date, source_factory):
    from trading_companion.agent import _routing_reply
    from trading_companion.research_session import ResearchSession
    session = session or ResearchSession()
    if type(question) is not str or not question.strip() or len(question) > MAX_USER_CHARACTERS:
        return _reply('invalid_input', '请用不超过2000字符描述指数研究问题。')
    question = unicodedata.normalize('NFKC', question).strip()
    now = now or datetime.now(timezone.utc)
    if type(now) is not datetime or now.utcoffset() is None:
        raise ModelConfigurationError('Index research needs a trusted aware time')
    reference = reference_date or now.astimezone(timezone(timedelta(hours=8))).date()
    effective_source = 'csv' if development_source is not None else source
    history = question.rstrip('。.!！') in HISTORY
    # A configuration switch invalidates saved index data even if directory admission fails.
    saved = _saved(session, source=effective_source, development_source=development_source, now=now, history=history)
    try:
        directory.assert_usable(reference, development=development_source is not None)
    except (InvalidRequestError, DataValidationError):
        return _reply('directory_unusable', '当前本地证券目录不可用，未调用模型或查询行情。', status='failed')
    from trading_companion.routing_context import independent_task
    if (not history and sum(item is not None for item in (session.pending, session.pending_edit, session.product_pending, session.index_pending)) > 1
            and not independent_task(question, directory)):
        return _routing_reply('routing_state_conflict')
    if history:
        if saved is None:
            return _reply('index_saved_unavailable', '没有可查看的有效指数快照，可能已过期或配置已变化；未重新取数。')
        return _outcome(saved.result, saved.answer, saved_at=saved.saved_at, plan_notice=saved.plan_notice)
    index, defaulted, code = market_selection(question, directory=directory)
    if code is not None and code != 'index_identity_required':
        return _routing_reply(code)
    pending = session.index_pending
    if pending is not None and (not pending.created_at <= now < pending.created_at + timedelta(minutes=15)
            or pending.source != effective_source or pending.development_source != development_source):
        session.clear_pending()
        return _reply('index_clarification_expired', '原指数选择已过期或配置已变化，请重新提供完整研究问题。')
    if code == 'index_identity_required':
        if pending is not None and pending.question == question:
            return _routing_reply(code)
        session.begin_research()
        session.index_pending = PendingIndexResearch(question, now, effective_source, development_source)
        return _routing_reply(code)
    pending = session.index_pending
    if pending is not None:
        if (not pending.created_at <= now < pending.created_at + timedelta(minutes=15)
                or pending.source != effective_source or pending.development_source != development_source):
            session.clear_pending()
            return _reply('index_clarification_expired', '原指数选择已过期或配置已变化，请重新提供完整研究问题。')
        if index is not None and question.rstrip('。.!！') in (*index.aliases, index.instrument_id, index.symbol):
            if '科创板指数' in pending.question and index.instrument_id != '000688.SH':
                return _reply('index_selection_mismatch', '原问题中的科创板指数尚未确认；请明确选择科创50，或提出完整新研究。')
            question = pending.question.replace('科创板指数', index.name) if '科创板指数' in pending.question else index.name + '：' + pending.question
        elif index is None:
            return _reply('index_identity_required', '请回复一个完整指数名称；原问题和日期条件仍保留，或回复取消。')
    # A nameless follow-up may only use the latest current index, never another cache.
    continuation = index is None
    if continuation:
        if saved is None:
            return _reply('index_current_unavailable', '当前没有可续接的同一指数研究，请重新明确指数；未借用旧股票或旧指数资料。')
        index = saved.result.request.index
        from trading_companion.product_plan import _execution_requested, _INJECTION
        if _execution_requested(question) or _INJECTION.search(question):
            return _routing_reply('product_trade_execution_forbidden' if _execution_requested(question) else 'product_unsafe_request')
    if effective_source not in ('tencent', 'csv') or (effective_source == 'csv' and not getattr(development_source, 'is_synthetic', False)):
        session.begin_research()
        return _reply('index_source_unsupported', '指数研究目前只支持腾讯；当前来源未接入指数，未自动换源。', status='out_of_scope')
    from trading_companion.research_decision import wants_prices, excludes_plan
    plan_notice = (not excludes_plan(question) and (wants_prices(question)
        or bool(re.search(r'点位计划|情景计划|跌破观察位|入场参考|盈亏比', question))))
    if continuation and re.search(r'财报|基本面|新闻|公告|资金流|板块|成份股', question):
        return _reply('market_scope_unsupported', '本批只支持指数技术研究，尚无财报、新闻、资金流或成份股研究能力。', status='out_of_scope')
    # Explanations and a weekly view reuse the same daily evidence, with one narrative call.
    if continuation and not is_indicator_edit(question) and not re.search(r'刷新|最新|重新|最近|近\d|过去|\d{4}-|未来|短线|中线|长线', question):
        answer, responses = synthesize_index_answer(question, saved.result, model=model, directory=directory)
        return _outcome(saved.result, answer, responses=responses, saved_at=saved.saved_at, plan_notice=plan_notice or saved.plan_notice)
    responses = ()
    # Consume the subject before fallible interpretation. Explicit history stays detached.
    session.begin_research()
    try:
        if continuation and is_indicator_edit(question):
            request = replace_indicator(saved.result.request, question, request_factory=IndexResearchRequest)
            request = replace(request, benchmark_defaulted=saved.result.request.benchmark_defaulted, horizon=saved.result.request.horizon)
        else:
            response = model.complete((ToolMessage('system', 'Extract only the literal historical date_expression using interpret_index_research. Never infer an index code or future market data.'),
                                       ToolMessage('user', question)), tools=(INTENT_TOOL,))
            checkpoint()
            if (type(response) is not ToolModelResponse or response.status != 'tool_calls' or len(response.tool_calls) != 1
                    or response.tool_calls[0].name != INTENT_NAME or set(response.tool_calls[0].arguments) != {'date_expression'}):
                return _reply('index_intent_invalid', '未取得完整的指数日期理解，未查询行情。', status='failed')
            responses = (response,)
            arguments, _ = _normalize_null_strings(response.tool_calls[0].arguments)
            expression = arguments['date_expression']
            if expression is not None and (type(expression) is not str or not expression.strip() or len(expression) > 120):
                return _reply('index_intent_ungrounded', '日期条件不是原问题中的完整表达，未查询行情。')
            expression, date_text = historical_date_inputs(expression, mask_outlook_expressions(question))
            if expression is not None:
                expression = expression.strip()
                if expression not in question:
                    return _reply('index_intent_ungrounded', '日期条件不是原问题中的完整表达，未查询行情。')
            outlook = extract_outlook(question)
            start, end, _ = _dates(expression, date_text, reference)
            if continuation and expression is None and not _DATE_CUE.search(date_text) and not re.search(r'刷新|最新|重新', question):
                start, end = saved.result.request.start_date, saved.result.request.as_of_date
            if end >= reference:
                raise InvalidRequestError('Only completed historical index daily bars are supported')
            request = IndexResearchRequest(index.instrument_id, start, end, _indicators(question,
                saved.result.request.indicators if continuation else None), effective_source,
                benchmark_defaulted=defaulted if not continuation else saved.result.request.benchmark_defaulted,
                horizon=outlook.horizon if outlook is not None else saved.result.request.horizon if continuation else None)
        checkpoint()
        reserve_analysis()
        if effective_source == 'csv':
            bars = synthetic_index_bars(request)
        else:
            configured = source_factory(request) if source_factory else TencentIndexSource(request)
            bars = trace_call('source', 'index_daily', configured.read_daily_bars)
        checkpoint()
        result = IndexResearchResult(request, tuple(bars), now)
    except InputIssue as issue:
        return _reply(issue.code, str(issue), status=issue.status)
    except (InvalidRequestError, DataValidationError, DataSourceError, ValueError, OSError):
        return _reply('index_data_unavailable', '指数身份、日期或行情未通过检查；未使用旧研究或自动换源，请核对后重试。', status='failed')
    except (ModelCallError, ModelConfigurationError, ModelRequestError):
        return _reply('index_model_unavailable', '指数问题理解暂不可用，未查询行情。', status='failed')
    answer, written = synthesize_index_answer(question, result, model=model, directory=directory)
    checkpoint()
    session.index_saved = SavedIndexResearch(result, answer, now, development_source, plan_notice)
    session.stage_research_subject((), kind='index', source=effective_source,
        development=development_source is not None, now=now, scope=request)
    return _outcome(result, answer, responses=responses + written, plan_notice=plan_notice)


def run_index_research_turn(question, *, model, directory, session=None, source='tencent', development_source=None,
                            now=None, reference_date=None, control=None, source_factory=None):
    from trading_companion.agent import _controlled
    from trading_companion.routing_context import replaces_pending
    return _controlled(lambda checked, working: _execute(question, session=working, model=checked,
        directory=directory, source=source, development_source=development_source, now=now,
        reference_date=reference_date, source_factory=source_factory), model=model, session=session,
        control=control, default_calls=3, input_mode='natural', replace_pending=(replaces_pending(question, directory)
            and not (session is not None and session.index_pending is not None
                     and session.index_pending.question == question)))
