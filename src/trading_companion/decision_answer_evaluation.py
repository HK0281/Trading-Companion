"""Twenty frozen questions through the real chat entry; offline scores are contracts only."""
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import html
import json
import re
from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from trading_companion.answer_validation import normalize_horizon
from trading_companion.api_service import ResearchApiService, ensure_data_mode_dependencies
from trading_companion.decision_demo import scripted_narrative
from trading_companion.evaluation_common import EvaluationModelMeter, reported_usage
from trading_companion.evaluation_pricing import (
    DEFAULT_PRICES, MODEL_ID, actual_cost, estimate_cost, load_prices, price_for, unique_object,
)
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.research_decision import RATINGS
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.security_directory import Security, SecurityDirectory, load_security_directory
from trading_companion.tool_model import ToolCall, ToolModelResponse
from trading_companion.turn_trace import JsonlTraceSink, _SAFE_EVENT_CODES
from trading_companion.workflow_evaluation import _read_trace, _revision, _dirty, _fixed_authenticity
from trading_companion.workflow_evaluation_fixtures import FixedSources, NOW, REFERENCE_DATE, ScriptedWorkflowModel, fixture_manifest
from trading_companion.workflow_evaluation_metrics import ratio
from trading_companion.workflow_evaluation_runtime import (
    PlanError, require, check_output_root, build_real_model, live_sources, model_network_boundary,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'data/evaluation/decision-answer-v1.jsonl'
MANIFEST = CORPUS.with_suffix('.manifest.json')
VERSION = 'decision-answer-evaluation-v1'
GROUPS = ('buy', 'sell', 'outlook', 'stop')
CASE_KEYS = {'id', 'group', 'security_name', 'instrument_id', 'industry', 'question', 'expected_horizon',
             'setup_questions', 'requires_batch_b', 'review_tags', 'intent_note'}


def load_corpus(path=CORPUS, manifest_path=MANIFEST):
    path, manifest_path = Path(path), Path(manifest_path)
    if path.stat().st_size > 200_000 or manifest_path.stat().st_size > 20_000:
        raise ValueError('Evaluation corpus too large')
    raw = path.read_bytes().replace(b'\r\n', b'\n')
    parse = lambda text: json.loads(text, object_pairs_hook=unique_object,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    cases = [parse(line) for line in raw.decode('utf-8').splitlines()]
    manifest = parse(manifest_path.read_text(encoding='utf-8'))
    valid = (len(cases) == 20 and manifest.get('version') == 'decision-answer-manifest-v1'
             and manifest.get('corpus_version') == 'decision-answer-v1'
             and manifest.get('corpus_sha256_lf') == sha256(raw).hexdigest()
             and [manifest.get(k) for k in ('questions', 'main_denominator', 'minimum_formed',
                    'maximum_calls_per_question', 'maximum_total_calls')] == [20, 20, 18, 6, 120])
    require(valid, code='decision_corpus_invalid', detail='20题语料摘要、版本或预设阈值无效。')
    for index, case in enumerate(cases, 1):
        require(type(case) is dict and case.keys() == CASE_KEYS and case['id'] == f'D{index:02}'
                and case['group'] in GROUPS and type(case['requires_batch_b']) is bool
                and type(case['setup_questions']) is list and len(case['setup_questions']) <= 1
                and case['requires_batch_b'] == bool(case['setup_questions'])
                and type(case['review_tags']) is list and 1 <= len(case['review_tags']) <= 8
                and all(type(v) is str and 0 < len(v) <= 2000 for v in
                    [case[k] for k in ('question','security_name','instrument_id','industry','expected_horizon','intent_note')]
                    + case['setup_questions'] + case['review_tags'])
                and re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', case['instrument_id']) is not None,
                code='decision_corpus_invalid', detail='题目字段、对象或上下文标记无效。')
    require(dict(Counter(c['group'] for c in cases)) == manifest.get('groups') == dict.fromkeys(GROUPS, 5)
            and [c['id'] for c in cases if c['requires_batch_b']] == manifest.get('requires_batch_b'),
            code='decision_corpus_invalid', detail='分组或B批依赖标记与冻结题库不符。')
    return cases, manifest


def _decision_fixture_manifest(cases):
    descriptor = fixture_manifest()
    descriptor['base_fixture_sha256'] = descriptor.pop('sha256')
    identities = {c['instrument_id']: Security(c['instrument_id'], c['security_name']) for c in cases}
    descriptor.update(version='decision-fixed-data-v1',
        securities=[s.to_dict() for s in identities.values()],
        decision_implementation_sha256=sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest())
    descriptor['sha256'] = sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return descriptor


def make_plan(*, mode='offline', provider=None, model=None, model_call_limit=None, prices=None,
              directory=None, data_source='tencent', collection_workers=3,
              assumed_input_tokens=None, assumed_output_tokens=None, trace_dir=None):
    require(mode in ('offline','live-model','live'), code='decision_mode_invalid', detail='请选择offline、live-model或live。')
    cases, manifest = load_corpus()
    require(type(collection_workers) is int and 1 <= collection_workers <= 3,
            code='decision_workers_invalid', detail='采集并发必须是1..3。')
    allowance = 120 if model_call_limit is None else model_call_limit
    require(type(allowance) is int and 1 <= allowance <= 120, code='decision_budget_invalid', detail='总调用额度必须是1..120。')
    require(data_source in ('tencent','akshare_eastmoney'), code='decision_source_invalid', detail='请选择已支持的行情来源。')
    check_output_root(trace_dir)
    if mode == 'offline':
        require(provider is None and model is None and directory is None and data_source == 'tencent',
                code='decision_offline_options_invalid', detail='离线模式不接受真实模型或来源配置。')
    else:
        require(provider == 'deepseek' and type(model) is str and MODEL_ID.fullmatch(model) is not None,
                code='decision_model_invalid', detail='真实模式须明确指定provider=deepseek和model。')
        require(prices is not None and model_call_limit is not None, code='decision_live_limits_required',
                detail='真实模式须显式提供prices与model-call-limit，先预览再运行。')
        require(mode == 'live' or directory is None and data_source == 'tencent',
                code='decision_fixed_source_options_invalid', detail='live-model只使用人工资料，不接受真实来源选项。')
    price_data, price_digest = load_prices(DEFAULT_PRICES if prices is None else prices)
    cost = estimate_cost(price_data, provider, model, allowance, assumed_input_tokens, assumed_output_tokens)
    plan = dict(version='decision-answer-plan-v1', status='planned', mode=mode,
        provider='fake' if mode == 'offline' else provider, model='decision-contract-fake-v1' if mode == 'offline' else model,
        corpus_version=manifest['corpus_version'], corpus_sha256=manifest['corpus_sha256_lf'], questions=deepcopy(cases),
        main_denominator=20, minimum_formed=18, maximum_calls_per_question=6, model_call_limit=allowance,
        worst_case_model_calls=120, planned_setup_turns=sum(len(c['setup_questions']) for c in cases),
        typical_calls_with_one_answer_repair=3*sum(1+len(c['setup_questions']) for c in cases),
        call_scope='includes_setup_intent_tools_answer_and_at_most_one_answer_repair_no_outer_retries',
        questions_with_setup=[c['id'] for c in cases if c['setup_questions']],
        fixture=_decision_fixture_manifest(cases) if mode!='live' else None,
        collection_workers=collection_workers, data_source=data_source,
        data_authenticity='live_sources' if mode == 'live' else 'fixed_synthetic',
        sources=['local_pinned_directory', data_source, 'eastmoney_company_financial_announcements', 'sina_news']
                if mode == 'live' else ['artificial_directory_company_transport_daily_bars'],
        max_output_tokens=1500, price_config_version=price_data['version'], price_config_sha256=price_digest, price=price_for(price_data, provider, model),
        cost_preview=cost, directory=None, directory_sha256=None, directory_metadata=None)
    if mode == 'live':
        ensure_data_mode_dependencies(data_source)
        path = Path(directory) if directory is not None else ROOT/'data/security_directory_live.json'
        snapshot = load_security_directory(path)
        snapshot.assert_usable(datetime.now(timezone(timedelta(hours=8))).date(), development=False)
        matches = [snapshot.lookup(c['security_name']) for c in cases]
        require(all(m.status=='resolved' and len(m.candidates)==1 and m.candidates[0].instrument_id==c['instrument_id']
                    for c,m in zip(cases,matches)),
                code='decision_directory_missing_security', detail='真实目录必须将20题的名称或已知别名唯一解析到预设证券代码。')
        plan.update(directory=str(path.absolute()), directory_sha256=sha256(path.read_bytes()).hexdigest(),
                    directory_metadata=snapshot.metadata())
    return plan, cases, price_data


def build_plan(**kwargs):
    try:
        return make_plan(**kwargs)[0]
    except PlanError as error:
        return error.result()
    except Exception:
        return dict(status='invalid_request', code='decision_plan_invalid', detail='题库、价格或本地目录配置无效；未加载密钥或联网。')


class DecisionFixedSources(FixedSources):
    """Reuse actual vendor parsers and daily calculations with this corpus's artificial identities."""
    def __init__(self, cases, workers):
        super().__init__('normal', workers)
        identities = {c['instrument_id']: Security(c['instrument_id'], c['security_name']) for c in cases}
        self.directory = SecurityDirectory(tuple(identities.values()), 'decision_evaluation_synthetic', NOW, False)
        self.http.directory = self.directory
        self.http.symbols = {s.instrument_id[-2:].lower()+s.instrument_id[:6]: s for s in self.directory.securities}


class DecisionContractModel:
    """Only the offline fixture has a fixed bullish answer; no ratings are in the question corpus."""
    def __init__(self):
        self.case, self.seed, self.technical = None, False, ScriptedWorkflowModel()

    def start(self, case, seed=False):
        self.case, self.seed = case, seed
        if seed:
            self.technical.start_step(dict(phases=['technical_intent','technical_tool','technical_answer'],
                                           mentions=[case['security_name']], indicators=['SMA3'], date_expression='最近'))

    def complete(self, messages, *, tools):
        if self.seed:
            return self.technical.complete(messages, tools=tools)
        if tools and tools[0].name == PRODUCT_INTENT_TOOL_NAME:
            question = messages[-1].content
            return ToolModelResponse('tool_calls', tool_calls=(ToolCall('decision_eval_intent', PRODUCT_INTENT_TOOL_NAME,
                dict(intent='research', instrument_mentions=[self.case['security_name']] if self.case['security_name'] in question else [],
                     research_kinds=['technical'] if '只看技术面' in question else [], date_expression=None,
                     outlook_expression=None, wants_risk=self.case['group']=='stop', clarification_reason=None)),))
        if tools and tools[0].name == INTENT_TOOL_NAME:
            # The current technical-only route cannot deliver a decision verdict. Keep that failure visible.
            return ToolModelResponse('tool_calls',tool_calls=(ToolCall('unsupported_decision',INTENT_TOOL_NAME,
                dict(intent='unsupported',instrument_mentions=[],date_expression=None,indicator_mentions=[],clarification_reason=None)),))
        if not tools and 'product-research-answer-v3' in messages[0].content:
            context = json.loads(next(m.content.split('\n',1)[1] for m in messages
                                     if m.role=='assistant' and m.content.startswith('UNTRUSTED_RESEARCH_CONTEXT\n')))
            return ToolModelResponse('complete', json.dumps(scripted_narrative(context), ensure_ascii=False))
        raise ValueError('Unexpected offline evaluation phase')


class TimedMeter(EvaluationModelMeter):
    def __init__(self, delegate, limit):
        super().__init__(delegate, limit)
        self.calls = []

    def complete(self, messages, *, tools):
        before, start, response = self.forwarded, perf_counter(), None
        try:
            response = super().complete(messages, tools=tools)
            return response
        finally:
            if self.forwarded > before:
                answer = bool(messages) and not tools and 'product-research-answer-v3' in messages[0].content
                paragraphs = None
                if answer and type(response) is ToolModelResponse and response.status=='complete':
                    try:
                        from trading_companion.product_narrative_answer import _parse
                        payload = _parse(response.text)
                        if type(payload.get('narratives')) is list and len(payload['narratives'])<=3:
                            paragraphs = len(payload['narratives'])
                    except (ValueError,TypeError,AttributeError,RecursionError):
                        pass
                self.calls.append(dict(elapsed_ms=round((perf_counter()-start)*1000, 3),
                    answer=answer, generated_paragraphs=paragraphs))


def grade_case(case, result):
    """Count a retained valid rating even when unrelated prose was deleted; facts never count."""
    company = result.get('company_research') or {}
    evidence = {e['id']: e['instrument_id'] for e in company.get('evidence', [])}
    def claim(c):
        return (type(c) is dict and type(c.get('text')) is str and bool(c['text'].strip())
                and type(c.get('evidence_ids')) is list and bool(c['evidence_ids'])
                and all(evidence.get(i) == case['instrument_id'] for i in c['evidence_ids']))
    decisions = company.get('decisions', [])
    d = decisions[0] if len(decisions) == 1 else {}
    rating = RATINGS.get(d.get('rating'))
    checks = dict(
        published=result.get('status') in ('completed','limited'),
        model_answer=company.get('version')=='product-research-answer-v3' and company.get('origin')=='model'
                     and company.get('status') in ('accepted','limited') and (company.get('quality') or {}).get('state') in ('complete','partial'),
        identity=[s['instrument_id'] for s in company.get('securities', [])]==[case['instrument_id']]
                 and d.get('instrument_id')==case['instrument_id'],
        horizon=normalize_horizon(d.get('horizon'))==normalize_horizon(case['expected_horizon']),
        accepted_rating=d.get('status')=='available' and d.get('code')=='decision_ready'
                        and bool(rating) and d.get('direction') in (rating[1] if rating else ()),
        usable_reasons=2 <= len(d.get('reasons', [])) <= 4 and all(claim(c) for c in d.get('reasons', []))
                       and claim(d.get('invalidation')))
    return dict(formed=all(checks.values()), checks=checks)


def _latency(values):
    values = sorted(v for v in values if v is not None)
    return dict(samples=len(values), median_ms=median(values) if values else None,
                p95_ms=values[max(0,ceil(.95*len(values))-1)] if values else None, method='median_and_nearest_rank_p95')


def summarize(cases):
    require(len(cases)==20 and len({c['id'] for c in cases})==20, code='decision_report_incomplete', detail='报告必须保留全部20题。')
    def group(rows):
        n = len(rows)
        return dict(questions=n, formed=sum(c['formed'] for c in rows),
                    formation_rate=ratio(sum(c['formed'] for c in rows), n),
                    reached_answer_rate=ratio(sum(bool(c.get('answer_stage_reached')) for c in rows), n),
                    not_run=sum(c['status']=='not_run' for c in rows))
    quality = [c['quality'] for c in cases if c.get('quality')]
    reached = [c for c in cases if c.get('answer_attempts',0)>0]
    attempts = sum(c.get('answer_attempts',0) for c in cases)
    unknown_structure = sum(c.get('answer_attempts',0) for c in reached if not c.get('quality'))
    repairs = [q for q in quality if q['repair_attempts'] > 0]
    failures = sum(q.get('structure_failures',0) for q in quality)
    formed = sum(c['formed'] for c in cases)
    count_fields = ('retained_paragraphs','generated_sentences','retained_sentences','deleted_sentences')
    counts = {k:sum(q[k] for q in quality if type(q.get(k)) is int) for k in count_fields}
    result = dict(**group(cases), target_minimum=18, target_met=formed>=18,
        groups={g:group([c for c in cases if c['group']==g]) for g in GROUPS},
        batch_b_groups={str(value).lower():group([c for c in cases if c['requires_batch_b']==value]) for value in (False,True)},
        generated_paragraphs=sum(c.get('generated_paragraphs',0) for c in cases),
        generated_paragraphs_unknown_attempts=sum(c.get('generated_paragraphs_unknown_attempts',0) for c in cases),
        **counts, quality_count_unknown={k:sum(type(q.get(k)) is not int for q in quality) for k in count_fields},
        quality_unavailable_questions=20-len(quality),
        sentence_deletion_rate=ratio(counts['deleted_sentences'],counts['generated_sentences']),
        repair_attempts=sum(q['repair_attempts'] for q in quality),
        repair_success_rate=ratio(sum(q['state']=='complete' for q in repairs), len(repairs)),
        partial_rate=ratio(sum(q['state']=='partial' for q in quality),20),
        turn_limited_rate=ratio(sum(c.get('turn_status')=='limited' for c in cases),20),
        fallback_rate=ratio(sum(q['state']=='fallback' for q in quality),20),
        structure_parse_failure_rate=ratio(failures,attempts,unknown=unknown_structure),
        structure_affected_question_rate=ratio(sum(q.get('structure_failures',0)>0 for q in quality),20),
        reasons=dict(Counter(r['code'] for q in quality for r in q['reasons'])),
        draft_rejection_reasons=dict(Counter(check['code'] for c in cases for check in c.get('answer_checks',[])
                                             if check['status']!='ok')),
        outcome_codes=dict(Counter(c['code'] for c in cases)),
        end_to_end_latency=_latency([c.get('elapsed_ms') for c in cases]),
        target_turn_latency=_latency([c.get('target_elapsed_ms') for c in cases]),
        model_call_latency=_latency([call['elapsed_ms'] for c in cases for call in c.get('calls',[])]))
    return result


def _send(service, credentials, question, directory, seen):
    start = perf_counter()
    try:
        result = service.message(credentials['session_id'], credentials['access_token'], uuid4().hex, question)['result']
    except Exception as error:
        code = getattr(error,'code',None)
        result = dict(status='failed', code=code if code in _SAFE_EVENT_CODES else 'evaluation_message_failed')
    elapsed = round((perf_counter()-start)*1000, 3)
    rows, reader = _read_trace(directory)
    fresh = [r for r in rows if r['run_id'] not in seen]
    trace = fresh[0] if len(fresh)==1 and not any(reader.diagnostics.values()) else None
    if trace:
        seen.add(trace['run_id'])
    return result, elapsed, trace


def _empty(case, code):
    return dict(**deepcopy(case), status='not_run', code=code, formed=False, quality=None, answer_attempts=0,
                generated_paragraphs=0, setup=[], calls=[], elapsed_ms=None)


def _run(cases, plan, meter, delegate, trace_root):
    output = []
    for case in cases:
        case_start = perf_counter()
        row = _empty(case,'evaluation_budget_exhausted')
        output.append(row)
        if meter.forwarded >= meter.limit:
            continue
        before = meter.forwarded
        per_case = EvaluationModelMeter(meter, 6)
        path = trace_root/case['id']
        try:
            path.mkdir(parents=True)
            with (DecisionFixedSources(cases,plan['collection_workers']) if plan['mode']!='live' else live_sources(plan)) as sources:
                fixed = plan['mode']!='live'
                service = ResearchApiService(model_factory=lambda:per_case, directory_loader=lambda _:sources.directory,
                    provider=plan['provider'], data_mode='tencent' if fixed else plan['data_source'],
                    reference_date=REFERENCE_DATE if fixed else None, product_data_service=sources.company,
                    wall_clock=(lambda:NOW) if fixed else None, trace_sink=JsonlTraceSink(path),
                    trace_mode='development' if fixed else 'normal')
                try:
                    credentials, seen = service.create_session(), set()
                    for question in case['setup_questions']:
                        if plan['mode']=='offline':
                            delegate.start(case, seed=True)
                        setup_before = meter.forwarded
                        result, elapsed, trace = _send(service,credentials,question,path,seen)
                        row['setup'].append(dict(question=question,status=result['status'],code=result['code'],
                            elapsed_ms=elapsed, model_calls=meter.forwarded-setup_before,
                            identity=(result.get('parameters') or {}).get('instrument_id'),
                            trace_complete=bool(trace and not trace['truncated'] and not trace['missing_parents']),
                            trace_run_id=trace['run_id'] if trace else None))
                    if meter.forwarded < meter.limit and per_case.forwarded < per_case.limit:
                        if plan['mode']=='offline':
                            delegate.start(case)
                        target_before = meter.forwarded
                        result, elapsed, trace = _send(service,credentials,case['question'],path,seen)
                        company = result.get('company_research') or {}
                        calls = meter.calls[target_before:]
                        quality = deepcopy(company.get('quality'))
                        row.update(status='executed', code=result['code'], turn_status=result['status'],
                            **grade_case(case,result), quality=quality, elapsed_ms=elapsed,
                            answer_stage_reached=bool(quality) or any(c['answer'] for c in calls),
                            answer_attempts=sum(c['answer'] for c in calls), target_calls=deepcopy(calls),
                            target_elapsed_ms=elapsed,
                            generated_paragraphs=sum(c['generated_paragraphs'] or 0 for c in calls if c['answer']),
                            generated_paragraphs_unknown_attempts=sum(c['answer'] and c['generated_paragraphs'] is None for c in calls),
                            answer=result.get('answer'), company_research=_fixed_authenticity(company) if fixed else company,
                            answer_checks=[dict(status=e['status'],code=e['code']) for e in (trace or {}).get('event_records',[])
                                           if e['kind']=='check' and e['operation']=='product_answer'],
                            route=trace['route'] if trace else None, trace_complete=bool(trace and not trace['truncated'] and not trace['missing_parents']),
                            trace_run_id=trace['run_id'] if trace else None)
                        context_ready = all(s['status'] in ('completed','limited') and s['identity']==case['instrument_id']
                                            for s in row['setup'])
                        row['checks']['setup_context'] = context_ready
                        row['formed'] &= context_ready
                finally:
                    service.close()
        except Exception:
            row.update(status='failed', code='evaluation_case_error', formed=False)
        row.update(calls=deepcopy(meter.calls[before:]), model_calls=meter.forwarded-before,
                   elapsed_ms=round((perf_counter()-case_start)*1000,3),
                   accounting=dict(attempts=per_case.attempts,blocked=per_case.blocked,ledger=deepcopy(meter.ledger[before:])))
    return output


def evaluate(**kwargs):
    base = dict(version=VERSION,cases=[],language_quality='requires_manual_review')
    try:
        plan,cases,prices = make_plan(**kwargs)
    except PlanError as error:
        return {**base,**error.result()}
    except Exception:
        return {**base,'status':'invalid_request','code':'decision_plan_invalid',
                'detail':'题库、价格或本地目录配置无效；未加载密钥或联网。'}
    output = dict(**base,plan=plan,code_revision=_revision(),working_tree_dirty=_dirty(),
        implementation_sha256=sha256(Path(__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest(),
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        evaluation_scope='offline_contracts_not_model_quality' if plan['mode']=='offline' else 'real_model_answers_not_investment_performance',
        real_model_requested=plan['mode']!='offline',real_data_requested=plan['mode']=='live')
    meter, error_code, denied_network, credential_attempts = None, None, [], []
    try:
        from scripts.verify import clean_environment, offline_network
        with ExitStack() as stack:
            if plan['mode']=='offline':
                stack.enter_context(clean_environment())
                stack.enter_context(offline_network())
                def denied(*args,**kw):
                    credential_attempts.append(True)
                    raise RuntimeError('No offline credentials')
                for name in ('load_model_environment','_read_local_values'):
                    stack.enter_context(patch('trading_companion.local_env.'+name,denied))
            elif plan['mode']=='live-model':
                denied_network = stack.enter_context(model_network_boundary(plan['provider']))
            delegate = DecisionContractModel() if plan['mode']=='offline' else build_real_model(plan)
            meter = TimedMeter(delegate,plan['model_call_limit'])
            trace_dir = kwargs.get('trace_dir')
            trace_root = Path(stack.enter_context(TemporaryDirectory(prefix='decision-evaluation-'))) if trace_dir is None else Path(trace_dir)/uuid4().hex
            output['cases'] = _run(cases,plan,meter,delegate,trace_root)
            output['trace_directory'] = str(trace_root.absolute()) if trace_dir else None
    except Exception:
        error_code = 'evaluation_initialization_or_boundary_error'
    if not output['cases']:
        output['cases'] = [_empty(c,error_code or 'evaluation_not_run') for c in cases]
    for row in output['cases']:
        row['cost'] = actual_cost(prices,plan['provider'],plan['model'],row.get('accounting',{}).get('ledger',[]))
    output.update(metrics=summarize(output['cases']),
        accounting=dict(forwarded=meter.forwarded if meter else 0,attempts=meter.attempts if meter else 0,
                        blocked=meter.blocked if meter else 0,token_usage=reported_usage(meter.usage if meter else [])),
        cost=actual_cost(prices,plan['provider'],plan['model'],meter.ledger if meter else []))
    if denied_network:
        error_code = 'evaluation_source_network_violation'
    if credential_attempts:
        error_code = 'evaluation_offline_credential_violation'
    complete = all(c['status']=='executed' and c.get('trace_complete') and all(s['trace_complete'] for s in c['setup'])
                   for c in output['cases'])
    output.update(status='passed' if complete and not error_code and output['metrics']['target_met'] else 'failed',
                  code=error_code or ('evaluation_incomplete' if not complete else
                       'decision_target_met' if output['metrics']['target_met'] else 'decision_target_not_met'))
    output['quality_target_claim'] = 'not_evaluated_offline' if plan['mode']=='offline' else 'mechanical_target_requires_manual_review'
    output['offline_credential_attempts'] = len(credential_attempts)
    output['blocked_source_network_attempts'] = len(denied_network)
    return output


def write_report(report,root):
    check_output_root(root)
    destination = Path(root)/('decision-'+uuid4().hex)
    destination.mkdir(parents=True,exist_ok=False)
    (destination/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')
    def cell(value):
        return html.escape(str(value)).replace('|','&#124;').replace('\n','<br>')
    lines = ['# 决策回答20题评估','',f"模式：{report['plan']['mode']}；主分母固定20；形成 {report['metrics']['formed']}/20，目标至少18。",
             '离线脚本不代表真实模型质量；自动通过仍需人工核对证据关联、措辞和条件。','',
             '| 题号 | 组别 | 依赖B | 状态/结果码 | 有效结论 | 问题 |','| --- | --- | --- | --- | --- | --- |']
    for c in report['cases']:
        lines.append('| '+' | '.join(cell(v) for v in (c['id'],c['group'],c['requires_batch_b'],c['status']+'/'+c['code'],c['formed'],c['question']))+' |')
    lines += ['', '## 指标', '', '```json',json.dumps(report['metrics'],ensure_ascii=False,indent=2),'```','',
              '## 人工复核','', '每题检查结论是否回应问题、证据是否相关、是否过度断言、条件能否理解。未运行和失败题保留。','']
    review = []
    for c in report['cases']:
        lines += [f"### {c['id']}：{cell(c['question'])}",'',cell(c.get('answer') or '未形成可显示的研究回答。'),'']
        review.append(dict(case_id=c['id'],question=c['question'],formed=c['formed'],requires_batch_b=c['requires_batch_b'],
            answer=c.get('answer'),company_research=c.get('company_research'),
            review=dict(relevance=None,evidence_support=None,wording=None,conditions=None,notes='')))
    (destination/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    (destination/'manual-review.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in review),encoding='utf-8')
    return str(destination.absolute())
