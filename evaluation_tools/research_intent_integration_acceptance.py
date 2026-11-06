"""Independent exposed-dev and loopback HTTP acceptance; no historical lock admission.

Importing this module and building a plan never load model settings or create stores.
Run the CLI in its own process: transport instrumentation is deliberately process-local.
"""

from collections import Counter
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from statistics import median
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from trading_companion.model import ModelCallError

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / '.agentbus/tmp'
PUBLIC = ROOT / 'data/evaluation/research-intent-v2.6-dev.jsonl'
REFERENCE = ROOT / 'tests/fixtures/research-intent-v268-public-replay.json'
HISTORICAL = ROOT / '.agentbus/evaluation-runs/research-intent-v2.6.8/public/dev/report.json'
NOTICE = '已暴露回归；不代表未见题、真实页面或正式切换资格'
KNOWN = ['strict_scope严格范围', 'operation操作分类', '期限理解', '知识误拒',
         'MACD输出错误尚未定位', '历史封存89/96未过门槛，不补跑']
_ACTIVE = ContextVar('integration_acceptance_transport', default=None)


def fingerprint(path):
    return sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def _message(question, *, needs=None, **options):
    return dict(action='message', question=question, needs=needs, **options)


def scenarios():
    """Public product wording, independent of sealed questions and model answers."""
    m = _message
    return [
        dict(id='company_prices', label='公司价位及交付后重算', steps=[
            m('分析平安银行'), m('平安银行建仓价位'), m('短线', needs='pending'),
            m('换成中线呢', needs='prices')]),
        dict(id='index_prices', label='指数价位及交付后重算', steps=[
            m('沪深300建仓价位'), m('短线', needs='pending'), m('换成中线呢', needs='prices')]),
        dict(id='scope', label='指标增删和严格范围', steps=[
            m('分析平安银行技术面，用EMA5和ATR14'), m('上一份分析补一项EMA13', needs='saved'),
            m('ATR这一项取消，EMA5照用', needs='saved'), m('只用EMA13', needs='saved')]),
        dict(id='profile_indicator', label='没有技术领域时追加指标', steps=[
            m('只研究平安银行公司资料'), m('上一份补EMA13', needs='saved')]),
        dict(id='refresh_explain', label='刷新默认范围及保存解释', steps=[
            m('分析平安银行近20天的EMA13'), m('刷新刚才那只', needs='saved'),
            m('解释上一份研究结论的依据', needs='saved')]),
        dict(id='risk', label='风险描述不授权价位', steps=[m('平安银行风险大吗，但不要价位')]),
        dict(id='knowledge', label='固定知识、通用知识和已知误拒', steps=[
            m('MACD怎么算的'), m('MACD怎么计算'), m('ETF是什么'), m('撤销委托一般怎么操作')]),
        dict(id='products', label='ETF研究边界和背景', steps=[
            m('研究沪深300ETF（510300）'), m('分析平安银行，ETF只作为背景')]),
        dict(id='screening', label='筛选、名单研究及修改', steps=[
            m('筛选PE低于20且换手率大于2%，按成交额降序选3只'), m('改成前2只', needs='shortlist'),
            m('研究名单第一只', needs='shortlist'), m('原来的筛选改成前3只', needs='shortlist')]),
        dict(id='history', label='回顾、历史、幂等及清空', steps=[
            m('分析平安银行'), dict(action='replay'), dict(action='result'), dict(action='history'),
            m('回顾我们刚才聊了什么', needs='saved'), m('你刚才给出的评级是什么', needs='saved'),
            m('查看上次结果', needs='saved'), m('取消'), dict(action='reset')]),
        dict(id='authorization', label='否决、换股和独立新任务', steps=[
            m('平安银行短线建仓价位'), m('换成中线，但不要价位', needs='prices'),
            m('分析招商银行公司资料'), m('另起一份平安银行技术研究，只看EMA13')]),
        dict(id='expiry', label='交付900秒边界', injection='clock', steps=[
            m('平安银行短线建仓价位'), dict(action='advance', seconds=900, needs='prices'),
            m('换成中线呢', needs='prices')]),
        dict(id='configuration', label='保存身份来源失配', injection='state', steps=[
            m('分析平安银行'), dict(action='source_mismatch', needs='saved'), m('继续研究上一只')]),
        dict(id='mode_mismatch', label='保存身份与价位的模式失配', injection='state', steps=[
            m('平安银行短线建仓价位'), dict(action='mode_mismatch', needs='prices'), m('换成中线呢')]),
        dict(id='pending_expiry', label='真实期限待办900秒边界', injection='clock', steps=[
            m('平安银行建仓价位'), dict(action='advance', seconds=900, needs='pending'), m('中线')]),
        dict(id='pending_priority', label='真实期限待办优先', steps=[
            m('平安银行短线建仓价位'), m('沪深300建仓价位'), m('中线', needs='pending')]),
        dict(id='pending_conflict', label='多待办与独立新任务', injection='state', steps=[
            m('平安银行建仓价位'), dict(action='capture_pending', needs='pending'),
            m('沪深300建仓价位'), dict(action='restore_pending', needs='pending'),
            m('中线', needs='conflict'), m('另起一份招商银行公司资料研究')]),
        dict(id='cancel', label='HTTP执行中取消', steps=[m('分析平安银行', cancel=True)]),
        dict(id='fallback_company', label='公司理解失败一次', injection='format', steps=[
            m('分析平安银行', fault=True)]),
        dict(id='fallback_technical', label='独立技术理解失败一次（公司资料能力关闭）', injection='format', technical=True, steps=[
            m('计算平安银行近30天EMA13', fault=True)]),
    ]


def public_inputs():
    from evaluation_tools import research_intent_v26_cases as cases
    from trading_companion.research_intent_v2_evaluation import prefix_library
    rows = cases.load_cases()
    old = [json.loads(line) for line in (ROOT / 'data/evaluation/research-intent-v2.jsonl').read_text(
        encoding='utf-8').splitlines()]
    prefixes = prefix_library([row for row in old if row['split'] == 'dev'])
    setups = cases.load_setups()
    return rows, prefixes, setups


def production_request(row, prefixes, setups):
    from evaluation_tools.research_intent_v26_cases import build_context
    from trading_companion.conversation_memory import TurnDigest
    from trading_companion.research_intent_context import prepare_intent_request
    # Neither target text nor evaluation metadata goes into the setup executor.
    state = dict(question='仅构建公开前置状态', context=deepcopy(row['context']), setup_id=row['setup_id'])
    ctx, _ = build_context(state, setups, prefixes)
    return prepare_intent_request(row['question'], session=ctx.session, directory=ctx.directory,
        now=ctx.now, reference_date=ctx.reference_date, source=ctx.source, development=ctx.development,
        digests=tuple(TurnDigest(q, 'other', 'completed') for q in ctx.history), quote_source=ctx.quote_source,
        company_enabled=ctx.company_enabled, workflow_enabled=ctx.workflow_enabled,
        conversation_memory=ctx.conversation_memory)


def historical_reference(rows, *, history=HISTORICAL, reference=REFERENCE):
    """Read only public reports/responses; never re-admit current code to old locks."""
    from trading_companion.research_intent_evaluation import field_scores
    from trading_companion.research_intent_evaluation_cases import GOLD_FIELDS
    try:
        value = json.loads(Path(history).read_text(encoding='utf-8'))
        expected = {r['id']: r for r in rows}
        observed = value['rows']
        if (value['plan']['contract_version'] != 'research-intent-v2.6.8'
                or value['authenticity'] != 'live_provider' or len(observed) != 64
                or {r['id'] for r in observed} != set(expected)
                or any(any(r[k] != expected[r['id']][k] for k in ('question', 'context', 'gold')) for r in observed)):
            raise ValueError('Historical public identity mismatch')
        return dict(kind='historical_live_public_dev', source=str(Path(history).relative_to(ROOT)),
            sha256_lf=fingerprint(history), scores=field_scores(observed, 'V2'),
            strategy=dict(timeout_seconds=45, max_attempts=2), notice='历史真实成绩，非本批运行')
    except FileNotFoundError:
        value = json.loads(Path(reference).read_text(encoding='utf-8'))
        saved = {r['id']: r for r in value['rows']}
        if len(saved) != 64 or set(saved) != {r['id'] for r in rows}:
            raise ValueError('Saved public reference identity mismatch')
        scoring = []
        for row in rows:
            original = saved[row['id']]
            if original['input'] != {k: row[k] for k in ('question', 'context', 'setup_id')}:
                raise ValueError('Saved public input mismatch')
            ref = original['reference']
            scoring.append(dict(gold=row['gold'], V2=dict(status='ok' if ref['status'] in ('local', 'checked') else ref['status'],
                fields={k: v for k, v in (ref['checked'] or {}).items() if k in GOLD_FIELDS})))
        return dict(kind='saved_response_reference', source=str(Path(reference).relative_to(ROOT)),
            sha256_lf=fingerprint(reference), scores=field_scores(scoring, 'V2'), notice='保存响应参考，非本批真实成绩')


def build_plan(*, suite='all', max_calls=None):
    if suite not in ('all', 'dev', 'products'):
        raise ValueError('Choose all, dev or products')
    if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
        raise ValueError('Use a positive explicit global call limit')
    from trading_companion import research_intent_v2 as intent
    from trading_companion.intent_rule_baseline import current_baseline
    from trading_companion.research_intent_context import IntentContextError
    rows, prefixes, setups = public_inputs()
    dev = []
    if suite != 'products':
        for row in rows:
            item = dict(id=row['id'], question=row['question'], max_calls=1)
            try:
                request = production_request(row, prefixes, setups)
                local = intent.local_preflight(row['question'], request.context)
                item.update(serialized_characters=request.serialized_characters,
                    request_utf8_bytes=sum(len(json.dumps(m.to_dict(), ensure_ascii=False).encode()) for m in request.messages)
                        + len(json.dumps(intent.TOOL.to_dict()).encode()),
                    path='local' if local else 'understanding', max_calls=0 if local else 1)
            except IntentContextError as error:
                item.update(path='context_fallback', reason=error.code, max_calls=0)
            dev.append(item)
    scripts = scenarios() if suite != 'dev' else []
    for script in scripts:
        for step in script['steps']:
            step['max_calls_per_entry'] = 6 if step['action'] == 'message' else 0
    messages = sum(s['action'] == 'message' for f in scripts for s in f['steps'])
    upper = sum(r['max_calls'] for r in dev) + 2 * 6 * messages
    # Transparent planning estimate, not a tokenizer result or a cost commitment.
    dev_estimate = sum(r.get('request_utf8_bytes', 0) for r in dev if r['max_calls'])
    return dict(kind='intent-integration-acceptance-plan-v1', title=NOTICE, suite=suite,
        rule_baseline=current_baseline()['rule_baseline_version'], candidate=intent.VERSION,
        guard=intent.GUARD_VERSION, input_sha256_lf=fingerprint(PUBLIC),
        source_sha256_lf={str(p.relative_to(ROOT)): fingerprint(p) for p in sorted([
            * (ROOT / 'src/trading_companion').glob('*.py'), Path(__file__),
            ROOT / 'scripts/evaluate_research_intent_integration.py',
            ROOT / 'evaluation_tools/research_intent_v26_cases.py',
            ROOT / 'evaluation_tools/research_intent_v25_cases.py'])},
        setup_sha256_lf={name: fingerprint(ROOT / name) for name in (
            'data/evaluation/research-intent-v2.jsonl', 'data/evaluation/research-intent-v2-setups.json',
            'data/evaluation/research-intent-v2.5-setups.json', 'data/evaluation/research-intent-v2.6-setups.json')},
        historical_reference=historical_reference(rows), dev_count=len(dev), dev=dev,
        product_scripts=scripts, product_messages_per_entry=messages, product_entries=['legacy', 'intent_v2'],
        product_actions_per_entry=sum(len(s['steps']) for s in scripts) - messages,
        theoretical_max_calls=upper, max_calls=max_calls, live_allowed=max_calls is not None,
        estimates=dict(method='UTF-8 bytes / 3 heuristic for dev; product input envelope only, not provider tokens',
            dev_input_tokens_approx=round(dev_estimate / 3), dev_output_token_cap=1500 * sum(r['max_calls'] for r in dev),
            product_input_tokens_envelope=20000 * 2 * 6 * messages,
            product_output_token_envelope=4096 * 2 * 6 * messages,
            notice='产品后续输入依赖真实取数和工具轮次；包络仅预算参考，不是供应商上下文上限或计费预测'),
        limits=dict(intent_seconds=20, intent_calls=1, intent_output=1500, turn_seconds=90,
                    turn_calls_at_most=6, observed_tokens=24000, session_calls=400),
        known_issues=KNOWN, paid_calls=0, live_enabled=False, eligible_for_switch=False)


def isolated_path(path):
    target = Path(path).resolve()
    if (not TEMP.resolve().is_relative_to(ROOT.resolve())
            or target == TEMP.resolve() or not target.is_relative_to(TEMP.resolve())):
        raise ValueError('Acceptance output must be a new directory beneath .agentbus/tmp')
    return target


def new_run_directory(path):
    path = isolated_path(path)
    # Never reuse stores, traces or a partial prior run.
    path.mkdir(parents=True, exist_ok=False)
    return path


def usage_observation(payload):
    raw = payload.get('usage') if type(payload) is dict else None
    raw = raw if type(raw) is dict else {}
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
    value = {k: v if type(v := raw.get(k)) is int and v >= 0 else None for k in keys}
    details = raw.get('prompt_tokens_details')
    nested = details.get('cached_tokens') if type(details) is dict else None
    nested = nested if type(nested) is int and nested >= 0 else None
    value['cached_tokens'] = nested
    p, c, t = (value[k] for k in keys[:3])
    value['complete'] = None not in (p, c, t) and p + c == t
    hit, miss = value['prompt_cache_hit_tokens'], value['prompt_cache_miss_tokens']
    value['cache_consistent'] = (p is not None and hit is not None and miss is not None and hit + miss == p
                               and (nested is None or nested == hit))
    value['unknown_fields'] = [k for k in keys if value[k] is None]
    return value


class CallLimitReached(ModelCallError):
    def __init__(self):
        super().__init__('integration_call_limit', 'Acceptance global call limit reached')


class TransportObserver:
    """One conservative reservation per real delegate call, including failed retries."""
    def __init__(self, maximum):
        if type(maximum) is not int or maximum < 1:
            raise ValueError('An explicit positive global limit is required')
        self.maximum, self.events, self.lock = maximum, [], Lock()
        self.data_requests = []
        self.started = Event()

    @property
    def exhausted(self):
        with self.lock:
            return len(self.events) >= self.maximum

    def call(self, delegate, config, body):
        with self.lock:
            if len(self.events) >= self.maximum:
                raise CallLimitReached()
            event = dict(attempt=len(self.events) + 1, requested_model=config.model,
                         wire_started=False, response_received=False, usage=None)
            self.events.append(event)
        token, start = _ACTIVE.set(event), monotonic()
        try:
            payload = delegate(config, body)
            event.update(response_received=True, usage=usage_observation(payload))
            returned = payload.get('model') if type(payload) is dict else None
            event['returned_model'] = returned if type(returned) is str and len(returned) <= 128 and returned.isprintable() else None
            return payload
        except Exception:
            event['failure'] = 'transport_failed'  # Never stringify provider errors/configuration.
            raise
        finally:
            event['elapsed_ms'] = round(1000 * (monotonic() - start))
            _ACTIVE.reset(token)

    @contextmanager
    def installed(self):
        from unittest.mock import patch
        from trading_companion import deepseek_model as text, deepseek_tool_model as tool
        from trading_companion import turn_trace
        delegate, trace = text._post_chat, text.trace_provider_request
        source_identity = turn_trace._source_identity

        def observed(config, body):
            return self.call(delegate, config, body)

        def wire(*args, **kwargs):
            event = _ACTIVE.get()
            if event is not None:
                event['wire_started'] = True
                self.started.set()
            return trace(*args, **kwargs)

        def source(operation, function, args):
            identity = source_identity(operation, function, args)
            current = turn_trace._CURRENT.get()
            if current is not None and operation in ('daily_source', 'daily_bars', 'index_daily',
                                                      'company_section', 'screening_snapshot'):
                owner = getattr(function, '__self__', None)
                request = (args[1] if operation == 'company_section' and len(args) > 1 else
                           args[0] if operation == 'daily_source' and args else
                           getattr(owner, 'request', owner))
                entry = dict(run_id=current.run_id, operation=operation,
                    source=identity[0], access=identity[1], request=request_scope(request),
                    section=args[2] if operation == 'company_section' and len(args) > 2 else None)
                with self.lock:
                    self.data_requests.append(entry)
            return identity

        with patch.object(text, '_post_chat', observed), patch.object(tool, '_post_chat', observed), \
                patch.object(text, 'trace_provider_request', wire), patch.object(turn_trace, '_source_identity', source):
            yield self


def request_scope(request):
    """Only public execution parameters, never repr/asdict of a source or configuration."""
    from datetime import date
    from trading_companion.multi_contracts import IndicatorSpec
    result = {}
    instrument = getattr(request, 'instrument_id', None)
    if type(instrument) is str and len(instrument) <= 12:
        result['instrument_id'] = instrument
    for key in ('start_date', 'end_date', 'as_of_date', 'as_of'):
        value = getattr(request, key, None)
        if type(value) in (datetime, date):
            result[key] = value.isoformat()
    indicators = getattr(request, 'indicators', None)
    if type(indicators) is tuple and all(type(s) is IndicatorSpec for s in indicators):
        result['indicators'] = [dict(kind=s.kind, parameters=dict(s.parameters)) for s in indicators]
    return result or None


class FaultOnce:
    """Explicit test adapter; does not spend a provider request on a synthetic error."""
    def __init__(self, delegate):
        self.delegate, self.armed, self.injected = delegate, False, 0

    def complete(self, messages, *, tools):
        if self.armed:
            self.armed = False
            self.injected += 1
            raise ModelCallError('tool_arguments_json_invalid', 'Injected acceptance failure')
        return self.delegate.complete(messages, tools=tools)


def token_summary(runtime):
    runtime = runtime or {}
    observed = runtime.get('observed_tokens')
    complete = runtime.get('usage_complete_for_attempts') is True
    return dict(observed_tokens=observed, margin=24000 - observed if type(observed) is int else None,
                reliable=complete, threshold_reached=observed >= 24000 if type(observed) is int else None)


def replay_dev(plan, model, observer):
    from trading_companion import research_intent_v2 as intent
    from trading_companion.research_intent_context import IntentContextError
    from trading_companion.research_intent_evaluation import field_scores
    from trading_companion.turn_control import TurnControl, TurnLimits
    rows, prefixes, setups = public_inputs()
    results = []
    for row in rows:
        observation = dict(status='not_measured', reason='global_call_limit', fields={})
        start, offset = monotonic(), len(observer.events)
        try:
            request = production_request(row, prefixes, setups)
            local = intent.local_preflight(row['question'], request.context)
            if local is not None:
                observation = dict(status='ok', path='local', fields=local.fields())
            elif not observer.exhausted:
                control = TurnControl(TurnLimits(timeout_seconds=20, model_calls=1))
                control.start()
                try:
                    run = intent.understand_active(row['question'], model=model, context=request.context, control=control)
                    observation = dict(status=run.status, reason=run.code, path='understanding',
                        fields=run.intent.fields() if run.intent else {}, corrections=list(run.corrections))
                finally:
                    stopped = control.finish()
                observation['runtime'] = control.to_dict()
                if stopped:
                    observation.update(status='failed', reason=stopped, fields={})
        except IntentContextError as error:
            observation.update(status='context_fallback', reason=error.code)
        except Exception:
            observation.update(status='failed', reason='acceptance_execution_failed')
        results.append(dict(id=row['id'], question=row['question'], gold=row['gold'], V2=observation,
            elapsed_ms=round((monotonic() - start) * 1000), transmissions=deepcopy(observer.events[offset:])))
    return dict(title='公开64题已暴露回归', denominator=64, rows=results,
                scores=field_scores(results, 'V2'), historical=plan['historical_reference'])


def load_live_models():
    """Only this explicit live branch reads the standard local .env/environment loader."""
    from trading_companion.local_env import load_model_environment
    from trading_companion.model_registry import configured_models, configured_intent_model
    values = load_model_environment(provider='deepseek')
    loader = lambda *, provider: values if provider == 'deepseek' else {}
    models = configured_models(default_provider='deepseek', loader=loader)
    return models['deepseek'], configured_intent_model(loader=loader)


@contextmanager
def live_service(directory, *, enabled, answer_factory, intent_factory, technical=False):
    from trading_companion.api_service import ResearchApiService
    from trading_companion.company_research import ResearchDataService
    from trading_companion.market_context_live import ExchangeMarketContextSource
    from trading_companion.memory_store import SqliteConversationStore
    from trading_companion.screening_source import configured_quote_source
    from trading_companion.security_directory import load_security_directory
    from trading_companion.snapshot_store import ResearchSnapshotStore
    from trading_companion.turn_trace import JsonlTraceSink
    directory = new_run_directory(directory)
    # No refresh or source fallback: startup validation is read-only.
    snapshot = ROOT / 'data/security_directory_live.json'
    directory_value = load_security_directory(snapshot)
    directory_value.assert_usable(datetime.now(timezone(timedelta(hours=8))).date(), development=False)
    clock = [timedelta()]
    captured = []
    sink = JsonlTraceSink(directory / 'trace')
    write = sink.write

    def record(value):
        captured.append(deepcopy(value))
        write(value)

    sink.write = record
    with ExitStack() as stack:
        memory = SqliteConversationStore(directory / 'conversations.sqlite3')
        stack.callback(memory.close)
        archive = ResearchSnapshotStore(directory / 'snapshots.sqlite3')
        stack.callback(archive.close)
        service = ResearchApiService(model_factory=answer_factory, provider='deepseek', data_mode='tencent',
            directory_loader=lambda _: directory_value,
            product_data_service=None if technical else ResearchDataService(max_workers=3),
            screening_source=configured_quote_source('tencent', 'standard', max_workers=3),
            market_background_source=ExchangeMarketContextSource(), conversation_store=memory,
            conversation_context=True, snapshot_store=archive, trace_sink=sink,
            wall_clock=lambda: datetime.now(timezone.utc) + clock[0], intent_model_factory=intent_factory,
            research_intent_v2_enabled=enabled)
        stack.callback(service.close)
        yield service, clock, captured


@contextmanager
def loopback_client(service):
    """Serve production routes over a bound loopback socket, never a public listener."""
    import socket
    import httpx
    import uvicorn
    from trading_companion.api import create_app
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        server = uvicorn.Server(uvicorn.Config(create_app(service=service), host='127.0.0.1',
            port=sock.getsockname()[1], workers=1, access_log=False, log_config=None, log_level='critical'))
        failed = Event()

        def serve():
            try:
                server.run(sockets=[sock])
            except BaseException:
                failed.set()  # Do not print exception values or a configuration traceback.

        thread = Thread(target=serve, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started:
                if failed.is_set() or not thread.is_alive() or monotonic() > deadline:
                    raise RuntimeError('Loopback acceptance server unavailable')
                failed.wait(0.01)
            with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}',
                              timeout=100, trust_env=False) as client:
                yield client
        finally:
            server.should_exit = True
            thread.join(timeout=100)
            if thread.is_alive():
                raise RuntimeError('Loopback acceptance server did not stop')


def saved_scope(session):
    """Read program state only; this is evidence of saved scope, not proof of fresh IO."""
    latest = session.latest_research
    if latest is None:
        return None
    if latest.kind == 'index' and session.index_saved is not None:
        plan = session.index_saved.result.request
    elif session.product_saved is not None:
        plan = session.product_saved.plan
    elif session.confirmed is not None:
        plan = session.confirmed.request
    else:
        return None
    value = dict(subjects=[s.instrument_id for s in getattr(plan, 'securities', ())]
                 or [getattr(plan, 'instrument_id', None)],
        dates={k: str(getattr(plan, k)) for k in ('start_date', 'end_date', 'as_of', 'as_of_date')
               if getattr(plan, k, None) is not None},
        research_kinds=list(getattr(plan, 'kinds', ('technical',))),
        indicators=[dict(kind=s.kind, parameters=dict(s.parameters)) for s in getattr(plan, 'indicators', ())],
        excluded=list(getattr(getattr(plan, 'scope', None), 'excluded', ())),
        checked_prices=getattr(plan, 'checked_prices', getattr(plan, 'scenario_requested', None)),
        horizon=getattr(getattr(plan, 'objective', None), 'horizon', getattr(plan, 'horizon', None)),
        applied_defaults=list(getattr(plan, 'applied_defaults', ())))
    return value


def prerequisite(session, name):
    if name is None:
        return True
    if name == 'saved':
        return session.latest_research is not None and session._subject_current
    if name == 'prices':
        # Legacy does not register the new delivered-price continuation marker.
        plan = getattr(session.product_saved, 'plan', None)
        index = getattr(getattr(session.index_saved, 'result', None), 'request', None)
        from trading_companion.decision_horizon import permits_prices, requires_prices
        return (session._delivered_prices is not None
                or plan is not None and requires_prices(plan) and permits_prices(plan)
                or index is not None and index.scenario_requested)
    if name == 'pending':
        return session.product_pending is not None or session.index_pending is not None or session.pending is not None
    if name == 'shortlist':
        return session.screening_saved is not None
    if name == 'conflict':
        return session.product_pending is not None and session.index_pending is not None
    raise ValueError('Unknown acceptance prerequisite')


def _message_http(client, path, headers, payload, *, cancel, observer):
    if not cancel:
        return client.post(path + '/messages', headers=headers, json=payload), None
    observer.started.clear()
    returned, failed = [], []

    def send():
        try:
            returned.append(client.post(path + '/messages', headers=headers, json=payload))
        except Exception:
            failed.append(True)

    thread = Thread(target=send, daemon=True)
    thread.start()
    started = observer.started.wait(5)
    response = client.post(path + '/cancel', headers=headers, json=dict(request_id=payload['request_id']))
    thread.join(100)
    if thread.is_alive() or failed or not returned:
        raise RuntimeError('Acceptance message unavailable after cancellation')
    return returned[0], dict(wire_observed_before_cancel=started, http_status=response.status_code,
                            notice='取消与真实回包有竞争；未命中活动请求时不计取消通过')


def run_script(script, *, client, service, clock, captured, observer, enabled):
    binding = client.post('/v1/sessions', json={})
    binding.raise_for_status()
    credentials = binding.json()
    sid = credentials['session_id']
    headers = {'Authorization': 'Bearer ' + credentials['access_token']}
    path = '/v1/sessions/' + sid
    entry = service._entries[sid]
    fault = FaultOnce(entry.intent_model) if enabled else None
    if fault:
        entry.intent_model = fault
    initial_clock, pending, last_payload, observations = clock[0], None, None, []
    try:
        for number, step in enumerate(script['steps'], 1):
            item = dict(step=number, action=step['action'], question=step.get('question'),
                        injection=script.get('injection'), status='not_measured')
            observations.append(item)
            if not prerequisite(entry.session, step.get('needs')):
                item['reason'] = 'prerequisite_failed'
                continue
            if observer.exhausted:
                item['reason'] = 'global_call_limit'
                continue
            before, start, offset, traces = (entry.session._publication_sequence, monotonic(),
                                             len(observer.events), len(captured))
            injected_before = fault.injected if fault else 0
            try:
                action = step['action']
                result, response, cancellation = None, None, None
                if action == 'message':
                    payload = dict(request_id=uuid4().hex, question=step['question'])
                    last_payload = payload
                    if step.get('fault') and fault:
                        fault.armed = True
                    response, cancellation = _message_http(client, path, headers, payload,
                        cancel=step.get('cancel', False), observer=observer)
                elif action == 'replay':
                    if last_payload is None:
                        item['reason'] = 'no_request_to_replay'
                        continue
                    response = client.post(path + '/messages', headers=headers, json=last_payload)
                elif action == 'result':
                    response = client.get(path + '/result', headers=headers)
                elif action == 'history':
                    response = client.get('/v1/conversations')
                elif action == 'reset':
                    response = client.post(path + '/reset', headers=headers, json={})
                elif action == 'advance':
                    clock[0] += timedelta(seconds=step['seconds'])
                elif action == 'source_mismatch':
                    entry.session.latest_research = replace(entry.session.latest_research, source='akshare_eastmoney')
                elif action == 'mode_mismatch':
                    if entry.session.latest_research is not None:
                        entry.session.latest_research = replace(entry.session.latest_research, development=True)
                    if entry.session._delivered_prices is not None:
                        entry.session._delivered_prices = replace(entry.session._delivered_prices, development=True)
                elif action == 'capture_pending':
                    pending = entry.session.product_pending
                    if pending is None:
                        item['reason'] = 'company_pending_missing'
                        continue
                elif action == 'restore_pending':
                    if pending is None or entry.session.index_pending is None:
                        item['reason'] = 'two_real_pending_states_not_available'
                        continue
                    entry.session.product_pending = pending
                else:
                    raise ValueError('Unknown HTTP acceptance action')
                if response is not None:
                    item['http_status'] = response.status_code
                    if response.status_code >= 400:
                        item.update(status='failed', reason='http_error')
                    else:
                        body = response.json()
                        result = body.get('result') if type(body) is dict else None
                        item.update(status='observed', replayed=body.get('replayed') if type(body) is dict else None)
                else:
                    item.update(status='injected', reason='test_only_state_or_clock')
                if result is not None:
                    # Whitelist product output; never persist bindings, headers or service configuration.
                    item['result'] = {k: result[k] for k in ('status', 'code', 'answer', 'message',
                        'model_calls_attempted', 'runtime', 'parameters', 'company_research',
                        'index_research', 'report', 'screening', 'clarification_options') if k in result}
                    item['tokens'] = token_summary(result.get('runtime'))
                item['cancellation'] = cancellation
                if cancellation is not None:
                    item['cancel_exercised'] = bool(cancellation['wire_observed_before_cancel'] and result
                        and result.get('code') == 'turn_cancelled')
            except Exception:
                item.update(status='failed', reason='acceptance_step_failed')
            finally:
                if fault:
                    fault.armed = False
                item.update(elapsed_ms=round(1000 * (monotonic() - start)),
                    observed_at=datetime.now(timezone.utc).isoformat(),
                    transmissions=deepcopy(observer.events[offset:]), trace=deepcopy(captured[traces:]),
                    injection_exercised=(fault.injected > injected_before) if step.get('fault') and fault else None)
                run_ids = {t['run_id'] for t in item['trace']}
                item['actual_data_requests'] = deepcopy([e for e in observer.data_requests if e['run_id'] in run_ids])
                new_publication = entry.session._publication_sequence > before
                item['actual_scope'] = saved_scope(entry.session) if new_publication else None
                item['saved_scope_reference'] = saved_scope(entry.session)
                item['scope_status'] = 'new_program_publication' if new_publication else 'no_new_publication'
                screen = (item.get('result') or {}).get('screening')
                if (screen is not None and step['action'] == 'message' and not item.get('replayed')
                        and item['result']['status'] in ('completed', 'limited')):
                    item['actual_scope'] = dict(task='screening', request=screen['request'],
                        subjects=[r['instrument_id'] for r in screen['rows']], coverage=screen['coverage'])
                    item['scope_status'] = 'new_screening_result'
                item['fallback_reasons'] = [s['code'] for t in captured[traces:] for s in t.get('intent_stages', [])
                    if s['stage'] in ('context', 'understanding') and s['code'] not in ('ok', 'intent_v2_checked')]
                item['checks'] = product_checks(script['id'], step, item, enabled=enabled)
        return dict(id=script['id'], entry='intent_v2' if enabled else 'legacy',
                    injection=script.get('injection'), rows=observations)
    finally:
        clock[0] = initial_clock
        client.delete(path, headers=headers)


def product_checks(identifier, step, item, *, enabled):
    """Observable invariants only; None means the required evidence is missing."""
    result, scope = item.get('result') or {}, item.get('actual_scope')
    calls = item.get('transmissions', [])
    data = market_events(item.get('trace', []))
    trace_complete = bool(item.get('trace')) and all(not t.get('truncated') for t in item['trace'])
    checks = dict(http_success=200 <= item['http_status'] < 300 if 'http_status' in item else None)
    if step['action'] in ('replay', 'result', 'history', 'reset'):
        checks['zero_provider_calls'] = not calls
    if step['action'] == 'replay':
        checks['idempotent_replay'] = item.get('replayed') is True
    if identifier in ('knowledge', 'refresh_explain') and (identifier == 'knowledge' or '解释' in step.get('question', '')):
        checks.update(no_market_data=not data if trace_complete else None,
                      accepted_answer=result.get('status') in ('completed', 'limited'))
    if identifier == 'risk':
        company = result.get('company_research') or {}
        checks['risk_coverage_visible'] = bool(company.get('risk_scenarios') or company.get('coverage'))
        checks['no_price_authority'] = scope.get('checked_prices') is not True if scope else None
    if identifier in ('scope', 'profile_indicator') and step.get('needs') == 'saved':
        specs = scope.get('indicators', []) if scope else []
        checks['new_scope_published'] = scope is not None
        checks['no_price_authority'] = scope.get('checked_prices') is not True if scope else None
        if identifier == 'profile_indicator':
            checks['profile_and_technical'] = set(scope['research_kinds']) == {'profile', 'technical'} if scope else None
        if 'ATR这一项取消' in step.get('question', ''):
            checks['atr_removed'] = all(s['kind'] != 'atr' for s in specs) if scope else None
            checks['ema5_preserved'] = any(s['kind'] == 'ema' and s['parameters'].get('window') == 5 for s in specs) if scope else None
        if step.get('question') == '只用EMA13':
            checks['only_ema13'] = specs == [dict(kind='ema', parameters=dict(window=13))] if scope else None
    if identifier in ('company_prices', 'index_prices') and step.get('needs') == 'prices':
        checks['medium_recalculation'] = scope.get('horizon') == '未来40个交易日' if scope else False
    if identifier == 'expiry' and step['action'] == 'message' and step.get('needs') == 'prices' and enabled:
        checks['expired_delivery_not_used'] = scope is None or scope.get('checked_prices') is not True
    if identifier in ('configuration', 'mode_mismatch', 'pending_expiry') and item['step'] == 3 and enabled:
        checks['invalid_state_not_reused'] = scope is None
    if identifier == 'pending_conflict' and step.get('needs') == 'conflict':
        checks['conflict_clarified'] = result.get('status') == 'needs_clarification'
    if step.get('fault') and enabled:
        stages = [s for t in item.get('trace', []) for s in t.get('intent_stages', [])]
        checks['failure_injected_once'] = item.get('injection_exercised') is True
        checks['legacy_fallback_once'] = sum(s['stage'] == 'legacy' for s in stages) == 1
        checks['fallback_completed'] = result.get('status') in ('completed', 'limited')
        model_ids = [e['event_id'] for t in item.get('trace', []) for e in t.get('models', {}).get('events', [])]
        checks['no_data_before_injected_failure'] = (all(e['event_id'] > min(model_ids) for e in data)
            if trace_complete and model_ids else None)
    if step.get('cancel'):
        checks['active_cancellation_exercised'] = item.get('cancel_exercised') is True
        checks['no_late_publication'] = scope is None
    return checks


def market_events(traces):
    return [e for t in traces for e in t.get('events', []) if e.get('kind') == 'source' and e.get('entered')
            and e.get('operation') not in ('directory', 'knowledge_search')]


def run_products(plan, *, folder, answer_factory, intent_factory, observer,
                 service_factory=live_service, client_factory=loopback_client):
    results = unmeasured_products(plan)
    entries = {(r['id'], r['entry']): r for r in results}
    # Compare the same flow back-to-back before spending calls on another flow.
    for script in plan['product_scripts']:
        for enabled, label in ((False, 'legacy'), (True, 'intent_v2')):
            entry = entries[script['id'], label]
            if observer.exhausted:
                entry['reason'] = 'global_call_limit'
                continue
            try:
                with service_factory(folder / label / script['id'], enabled=enabled, answer_factory=answer_factory,
                                     intent_factory=intent_factory, technical=script.get('technical', False)) as (service, clock, captured):
                    with client_factory(service) as client:
                        value = run_script(script, client=client, service=service, clock=clock,
                            captured=captured, observer=observer, enabled=enabled)
                        entry.update(value)
            except Exception:
                entry['reason'] = 'acceptance_service_or_script_unavailable'
    return results


def unmeasured_products(plan, *, label=None):
    return [dict(id=s['id'], entry=entry, injection=s.get('injection'),
        company_enabled=not s.get('technical', False), rows=[
        dict(step=i, action=step['action'], question=step.get('question'), status='not_measured',
             reason='not_run') for i, step in enumerate(s['steps'], 1)])
        for entry in ([label] if label else ['legacy', 'intent_v2']) for s in plan['product_scripts']]


def unmeasured_dev(plan):
    from trading_companion.research_intent_evaluation import field_scores
    rows, _, _ = public_inputs()
    result = [dict(id=r['id'], question=r['question'], gold=r['gold'],
                   V2=dict(status='not_measured', reason='not_run', fields={})) for r in rows]
    return dict(title='公开64题已暴露回归', denominator=64, rows=result,
                scores=field_scores(result, 'V2'), historical=plan['historical_reference'])


def _cost(event, prices):
    """Prices are operator supplied per requested model, in currency per million tokens."""
    price = prices.get(event['requested_model']) if type(prices) is dict else None
    usage = event.get('usage') or {}
    if (not price or not usage.get('complete') or not usage.get('cache_consistent')
            or any(type(price.get(k)) not in (float, int) or not isfinite(price[k]) or price[k] < 0
                   for k in ('input_hit', 'input_miss', 'output'))
            or type(price.get('currency')) is not str or not price['currency'].isalpha()):
        return None
    return dict(currency=price['currency'], amount=sum(usage[u] * price[p] for u, p in (
        ('prompt_cache_hit_tokens', 'input_hit'), ('prompt_cache_miss_tokens', 'input_miss'),
        ('completion_tokens', 'output'))) / 1_000_000)


def transmission_totals(events):
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens',
            'prompt_cache_miss_tokens', 'cached_tokens')
    result = {}
    for key in keys:
        known = [(e.get('usage') or {}).get(key) for e in events]
        result[key] = dict(total=sum(known) if all(type(v) is int for v in known) else None,
            known_partial=sum(v for v in known if type(v) is int),
            unknown_attempts=sum(v is None for v in known))
    result['all_attempt_usage_consistent'] = all((e.get('usage') or {}).get('complete') is True for e in events)
    return result


def product_comparison(products):
    by_id = {(s['id'], s['entry']): s for s in products}
    rows = []
    for identifier in dict.fromkeys(s['id'] for s in products):
        old, new = (by_id.get((identifier, label), {}).get('rows', []) for label in ('legacy', 'intent_v2'))
        for i in range(max(len(old), len(new))):
            a, b = (group[i] if i < len(group) else {} for group in (old, new))
            av, bv = (x.get('tokens', {}).get('observed_tokens') for x in (a, b))
            rows.append(dict(script=identifier, step=i + 1,
                legacy_ms=a.get('elapsed_ms'), intent_v2_ms=b.get('elapsed_ms'),
                legacy_observed_tokens=av, intent_v2_observed_tokens=bv,
                observed_token_delta=bv - av if type(av) is int and type(bv) is int else None,
                usage_reliable=all(x.get('tokens', {}).get('reliable') is True for x in (a, b)),
                legacy_status=a.get('status'), intent_v2_status=b.get('status'),
                legacy_scope=a.get('actual_scope'), intent_v2_scope=b.get('actual_scope')))
    return rows


def report_summary(report):
    products = report.get('products', [])
    rows = [r for s in products for r in s['rows']]
    costs = [e.get('cost') for e in report['transmissions']]
    currencies = {c['currency'] for c in costs if c}
    return dict(reserved_attempts=len(report['transmissions']),
        wire_started=sum(e['wire_started'] for e in report['transmissions']),
        responses=sum(e['response_received'] for e in report['transmissions']),
        usage_incomplete=sum(not (e.get('usage') or {}).get('complete') for e in report['transmissions']),
        costs_unknown=sum(c is None for c in costs),
        known_costs={currency: sum(c['amount'] for c in costs if c and c['currency'] == currency) for currency in currencies},
        usage=transmission_totals(report['transmissions']),
        product_statuses=dict(Counter(r['status'] for r in rows)),
        latency={entry: dict(samples=len(times), p50_ms=median(times) if times else None,
                            slowest_ms=max(times) if times else None)
                 for entry in ('legacy', 'intent_v2')
                 for times in [[r['elapsed_ms'] for s in products if s['entry'] == entry for r in s['rows']
                                if 'elapsed_ms' in r and r['action'] == 'message']]},
        threshold_reached=sum(r.get('tokens', {}).get('threshold_reached') is True for r in rows),
        token_margin_unreliable=sum(r.get('tokens', {}).get('reliable') is not True for r in rows
                                    if r.get('action') == 'message'),
        checks=dict(Counter('passed' if c is True else 'failed' if c is False else 'not_measured'
                            for r in rows for c in r.get('checks', {}).values())),
        injected_failures=sum(r.get('injection_exercised') is True for r in rows))


def run_live(plan, *, output, prices=None):
    if plan.get('max_calls') is None:
        raise ValueError('--live requires an explicit positive --max-calls')
    observer = TransportObserver(plan['max_calls'])
    folder = new_run_directory(output)
    report = dict(kind='intent-integration-acceptance-report-v1', title=NOTICE, plan=plan,
        eligible_for_switch=False, live_requested=True, known_issues=KNOWN,
        dev=unmeasured_dev(plan) if plan['suite'] != 'products' else None,
        products=unmeasured_products(plan), transmissions=[],
        page_acceptance='未测：由Claude另验页面按钮、历史和取消交互', status='running')
    try:
        answer_factory, intent_factory = load_live_models()
        with observer.installed():
            if plan['suite'] != 'products':
                report['dev'] = replay_dev(plan, intent_factory(), observer)
            if plan['suite'] != 'dev':
                report['products'] = run_products(plan, folder=folder, answer_factory=answer_factory,
                                                  intent_factory=intent_factory, observer=observer)
        report['status'] = 'observations_recorded'
    except (Exception, KeyboardInterrupt):
        report.update(status='incomplete', failure='acceptance_interrupted_or_failed')
    finally:
        report['transmissions'] = deepcopy(observer.events)
        for event in report['transmissions']:
            event['cost'] = _cost(event, prices)
        costs = {e['attempt']: e['cost'] for e in report['transmissions']}
        groups = [e['rows'] for e in report['products']] + ([report['dev']['rows']] if report['dev'] else [])
        for rows in groups:
            for row in rows:
                for event in row.get('transmissions', []):
                    event['cost'] = costs[event['attempt']]
                row['usage'] = transmission_totals(row.get('transmissions', [])) if 'transmissions' in row else None
                row['model_counts'] = dict(reserved_attempts=len(row.get('transmissions', [])),
                    wire_started=sum(e['wire_started'] for e in row.get('transmissions', [])),
                    injected_failures=int(row.get('injection_exercised') is True),
                    production_attempts=sum(t.get('models', {}).get('attempted', 0) for t in row.get('trace', [])))
        report['summary'] = report_summary(report)
        report['paid_requests_started'] = report['summary']['wire_started']
        report['authenticity'] = 'live_provider_observation' if report['paid_requests_started'] else 'no_provider_observation'
        report['comparison'] = product_comparison(report['products'])
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (folder / 'report.md').write_text(markdown(report), encoding='utf-8')
    return report


def markdown(value):
    """Full structured detail stays available in JSON; the Markdown is user readable."""
    plan = value.get('plan', value)
    lines = ['# 接入验收：' + NOTICE, '',
        f"公开题：{plan['dev_count']}；每个入口产品消息：{plan['product_messages_per_entry']}；"
        f"理论调用上界：{plan['theoretical_max_calls']}；显式全局上限：{plan['max_calls']}", '',
        '默认只读；真实运行由Claude另列预算。估计token不等于供应商计费用量。', '',
        '## 公开dev原句', '']
    lines += [f"- {r['id']}：{r['question']}（{r['path']}，调用≤{r['max_calls']}）" for r in plan['dev']]
    lines += ['', '## 产品流程原句（旧入口、新入口各一遍）', '']
    for script in plan['product_scripts']:
        lines += [f"### {script['id']}：{script['label']}；注入={script.get('injection', '无')}", '']
        for i, step in enumerate(script['steps'], 1):
            lines.append(f"{i}. {step.get('question') or step['action']}；前置={step.get('needs')}；"
                         f"每入口调用≤{step['max_calls_per_entry']}")
        lines.append('')
    lines += ['## token估计与历史对照', '', '```json', json.dumps(dict(estimates=plan['estimates'],
        historical=plan['historical_reference']), ensure_ascii=False, indent=2), '```', '',
        '## 既有不足及未测', '', *['- ' + issue for issue in KNOWN],
        '- 页面按钮、历史显示、取消交互仍须人工验收；本工具不判正式切换。', '']
    if 'summary' in value:
        lines += ['## 本批结果', '', '```json', json.dumps(dict(status=value['status'], summary=value['summary'],
            comparison=value['comparison'], dev_scores=(value.get('dev') or {}).get('scores')),
            ensure_ascii=False, indent=2), '```', '', '逐轮用量、缓存、实际范围、回退和注入详情见同目录report.json。', '']
    return '\n'.join(lines) + '\n'
