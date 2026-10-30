"""Ten draft technical conversations through the real API, with measured source scope."""
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone, timedelta, date
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from uuid import uuid4

from trading_companion.api_service import ResearchApiService, ensure_data_mode_dependencies
from trading_companion.answer_diagnostics import AnswerDiagnostics, bind_diagnostics, check_diagnostics_root, diagnostic_location
from trading_companion.decision_answer_evaluation import TimedMeter, _send, _latency
from trading_companion.deepseek_model import ANSWER_OUTPUT_TOKENS, TOOL_OUTPUT_TOKENS
from trading_companion.evaluation_common import EvaluationModelMeter, reported_usage
from trading_companion.evaluation_pricing import DEFAULT_PRICES, MODEL_ID, actual_cost, estimate_cost, load_prices, price_for, unique_object
from trading_companion.product_research import collect_product_research
from trading_companion.security_directory import load_security_directory
from trading_companion.technical_answer_evaluation_checks import FAILURE_CODES, grade_turn
from trading_companion.technical_answer_evaluation_fixtures import TechnicalContractModel, TechnicalFixedSources, NOW, REFERENCE_DATE
from trading_companion.turn_trace import JsonlTraceSink
from trading_companion.workflow_evaluation import _revision, _dirty, _fixed_authenticity
from trading_companion.workflow_evaluation_metrics import ratio
from trading_companion.workflow_evaluation_runtime import (
    PlanError, require, check_output_root, build_real_model, live_sources, model_network_boundary,
)

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'data/evaluation/technical-answer-v1.jsonl'
MANIFEST = CORPUS.with_suffix('.manifest.json')
VERSION = 'technical-answer-evaluation-v1'
NOTICE = '草稿题库、需人工复核'
DOMAINS = {'technical', 'profile', 'financial', 'news', 'announcements', 'risk'}


def implementation_fingerprint():
    names = ('technical_answer_evaluation.py', 'technical_answer_evaluation_checks.py',
             'technical_answer_evaluation_fixtures.py', 'decision_answer_evaluation.py', 'workflow_evaluation_fixtures.py',
             'product_narrative_answer.py', 'product_indicator_edit.py', 'product_agent.py', 'product_plan.py',
             'product_scope.py', 'product_research.py', 'answer_validation.py', 'answer_numeric_bindings.py',
             'answer_claim_recovery.py', 'answer_technical_validation.py', 'research_intent.py',
             'deepseek_model.py', 'deepseek_tool_model.py', 'turn_trace.py')
    digest = sha256()
    for name in names:
        digest.update(name.encode('ascii') + b'\0')
        digest.update(Path(__file__).with_name(name).read_bytes().replace(b'\r\n', b'\n'))
    return digest.hexdigest()


def load_corpus(path=CORPUS, manifest_path=MANIFEST):
    path, manifest_path = Path(path), Path(manifest_path)
    require(path.stat().st_size <= 200000 and manifest_path.stat().st_size <= 20000,
            code='technical_corpus_invalid', detail='题库文件过大。')
    raw = path.read_bytes().replace(b'\r\n', b'\n')
    def parse(text):
        return json.loads(text, object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    cases = [parse(line) for line in raw.decode('utf-8').splitlines()]
    manifest = parse(manifest_path.read_text(encoding='utf-8'))
    require(len(cases) == 10 and manifest.get('version') == 'technical-answer-manifest-v1'
            and manifest.get('corpus_version') == 'technical-answer-v1'
            and manifest.get('corpus_sha256_lf') == sha256(raw).hexdigest()
            and manifest.get('review_status') == 'draft_requires_manual_review'
            and [manifest.get(k) for k in ('questions', 'turns', 'main_denominator', 'minimum_formed',
                 'maximum_scope_violations', 'maximum_calls_per_turn', 'maximum_total_calls')] == [10, 14, 10, 9, 0, 6, 84],
            code='technical_corpus_invalid', detail='10题语料摘要、草稿标记、版本或预设门槛无效。')
    for index, case in enumerate(cases, 1):
        require(type(case) is dict and case.keys() == {'id', 'security_name', 'instrument_id', 'tags', 'turns'}
                and case['id'] == f'T{index:02}' and type(case['security_name']) is str and bool(case['security_name'])
                and re.fullmatch(r'\d{6}\.(?:SH|SZ)', case['instrument_id']) is not None
                and type(case['tags']) is list and 1 <= len(case['tags']) <= 8
                and all(type(t) is str and 0 < len(t) < 100 for t in case['tags'])
                and type(case['turns']) is list and 1 <= len(case['turns']) <= 2,
                code='technical_corpus_invalid', detail='题目身份、标签或轮数无效。')
        for turn in case['turns']:
            require(type(turn) is dict and turn.keys() == {'question', 'strict_indicators', 'required_indicators',
                    'allowed_domains', 'requires_decision', 'expected_dates'}
                    and type(turn['question']) is str and 0 < len(turn['question']) <= 2000
                    and type(turn['requires_decision']) is bool
                    and all(type(turn[k]) is list and len(turn[k]) <= 12
                            and all(type(v) is str and 0 < len(v) < 100 for v in turn[k])
                            and len(set(turn[k])) == len(turn[k])
                            for k in ('strict_indicators', 'required_indicators', 'allowed_domains'))
                    and set(turn['allowed_domains']) <= DOMAINS,
                    code='technical_corpus_invalid', detail='问题或逐轮预期范围无效。')
            if turn['expected_dates'] is not None:
                require(type(turn['expected_dates']) is list and len(turn['expected_dates']) == 2
                        and all(type(d) is str and date.fromisoformat(d).isoformat() == d for d in turn['expected_dates'])
                        and turn['expected_dates'][0] <= turn['expected_dates'][1],
                        code='technical_corpus_invalid', detail='预期日期区间无效。')
    require(sum(len(c['turns']) for c in cases) == 14
            and [c['id'] for c in cases if len(c['turns']) > 1] == manifest.get('multi_turn_cases') == ['T06', 'T07', 'T08', 'T10']
            and len({c['instrument_id'] for c in cases}) == 10,
            code='technical_corpus_invalid', detail='多轮题或证券组合与清单不符。')
    return cases, manifest


def make_plan(*, mode='offline', provider=None, model=None, model_call_limit=None, prices=None,
              directory=None, data_source='tencent', collection_workers=3, assumed_input_tokens=None,
              assumed_output_tokens=None, trace_dir=None, diagnostics_dir=None):
    cases, manifest = load_corpus()
    require(mode in ('offline', 'live-model', 'live'), code='technical_mode_invalid', detail='请选择有效运行模式。')
    require(type(collection_workers) is int and collection_workers in (1, 2, 3),
            code='technical_workers_invalid', detail='采集并发必须是1..3。')
    allowance = 84 if model_call_limit is None else model_call_limit
    require(type(allowance) is int and 1 <= allowance <= 10000,
            code='technical_budget_invalid', detail='总调用额度必须是1..10000；实际运行另受14轮共84次上限约束。')
    require(data_source in ('tencent', 'akshare_eastmoney'), code='technical_source_invalid', detail='请选择支持的行情来源。')
    check_output_root(trace_dir)
    check_diagnostics_root(diagnostics_dir)
    if mode == 'offline':
        require(provider is None and model is None and directory is None and data_source == 'tencent',
                code='technical_offline_options_invalid', detail='离线模式不接受真实模型或来源配置。')
    else:
        require(provider == 'deepseek' and type(model) is str and MODEL_ID.fullmatch(model) is not None,
                code='technical_model_invalid', detail='真实模式须指定provider=deepseek和model。')
        require(prices is not None and model_call_limit is not None, code='technical_live_limits_required',
                detail='真实模式须显式提供prices与model-call-limit，先预览再运行。')
        require(mode == 'live' or directory is None and data_source == 'tencent',
                code='technical_fixed_source_options_invalid', detail='live-model使用人工资料，不接受真实来源配置。')
    price_data, price_digest = load_prices(DEFAULT_PRICES if prices is None else prices)
    budget = min(84, allowance)
    plan = dict(version='technical-answer-plan-v1', status='planned', notice=NOTICE, mode=mode,
        provider='fake' if mode == 'offline' else provider,
        model='technical-contract-fake-v1' if mode == 'offline' else model,
        corpus_version=manifest['corpus_version'], corpus_sha256=manifest['corpus_sha256_lf'],
        review_status=manifest['review_status'], questions=deepcopy(cases), main_denominator=10, turns=14,
        minimum_formed=9, maximum_scope_violations=0, maximum_calls_per_turn=6,
        maximum_calls_per_question={c['id']: 6 * len(c['turns']) for c in cases},
        requested_model_call_limit=allowance, model_call_limit=budget, worst_case_model_calls=84,
        max_output_tokens=ANSWER_OUTPUT_TOKENS,
        output_tokens_by_stage=dict(structured_answer=ANSWER_OUTPUT_TOKENS, tools=TOOL_OUTPUT_TOKENS),
        call_scope='all_setup_and_target_turns_including_repairs_no_outer_retries',
        collection_workers=collection_workers, data_source=data_source,
        data_authenticity='live_sources' if mode == 'live' else 'fixed_synthetic',
        directory=None, directory_sha256=None, directory_metadata=None,
        price_config_version=price_data['version'], price_config_sha256=price_digest,
        price=price_for(price_data, provider, model),
        cost_preview=estimate_cost(price_data, provider, model, budget, assumed_input_tokens, assumed_output_tokens),
        diagnostics_enabled=diagnostics_dir is not None,
        sources=['local_pinned_directory', data_source, 'eastmoney_company_financial_announcements', 'sina_news']
            if mode == 'live' else ['artificial_directory_company_transport_400_weekday_bars'],
        fixture=None if mode == 'live' else dict(version='technical-fixed-data-v1', reference_time=NOW.isoformat(),
            bars=400, authenticity='fixed_synthetic', implementation_sha256=implementation_fingerprint()))
    if mode == 'live':
        ensure_data_mode_dependencies(data_source)
        path = Path(directory) if directory else ROOT / 'data/security_directory_live.json'
        snapshot = load_security_directory(path)
        snapshot.assert_usable(datetime.now(timezone(timedelta(hours=8))).date(), development=False)
        matches = [snapshot.lookup(c['security_name']) for c in cases]
        require(all(m.status == 'resolved' and len(m.candidates) == 1 and m.candidates[0].instrument_id == c['instrument_id']
                    for c, m in zip(cases, matches)),
                code='technical_directory_missing_security', detail='真实目录须将10题名称唯一解析到预设代码。')
        plan.update(directory=str(path.absolute()), directory_sha256=sha256(path.read_bytes()).hexdigest(),
                    directory_metadata=snapshot.metadata())
    return plan, cases, price_data


def build_plan(**kwargs):
    try:
        return make_plan(**kwargs)[0]
    except PlanError as error:
        return error.result()
    except Exception:
        return dict(status='invalid_request', code='technical_plan_invalid', detail='题库、价格或目录配置无效；未加载密钥或联网。')


class CollectionAudit:
    """Record requests before forwarding, including attempts that fail without evidence."""
    def __init__(self, sources):
        self.sources, self.calls, self.observation = sources, [], None

    def __enter__(self):
        self.stack = ExitStack()
        collect_many = self.sources.company.collect_many
        def company(requests, kinds):
            records = [dict(instrument_id=request.instrument_id, kind=kind, status='attempted')
                       for request in requests for kind in kinds]
            self.calls.extend(records)
            try:
                result = collect_many(requests, kinds)
            except BaseException:
                for record in records:
                    record['status'] = 'failed'
                raise
            for record in records:
                record['status'] = 'returned'
            return result
        self.stack.enter_context(patch.object(self.sources.company, 'collect_many', side_effect=company))
        import trading_companion.product_research as research
        build_source = research._build_source
        def daily(request):
            self.calls.append(dict(instrument_id=request.instrument_id, kind='technical', status='attempted'))
            return build_source(request)
        self.stack.enter_context(patch.object(research, '_build_source', side_effect=daily))
        def collect(plan, **kwargs):
            self.observation = dict(indicator_ids=[s.indicator_id for s in plan.indicators], kinds=list(plan.kinds),
                instrument_ids=[s.instrument_id for s in plan.securities], technical_requests=[])
            bundle = collect_product_research(plan, **kwargs)
            self.observation['technical_requests'] = [[s.indicator_id for s in c.technical.request.indicators]
                for c in bundle.companies if c.technical is not None]
            return bundle
        self.stack.enter_context(patch('trading_companion.product_agent.collect_product_research', side_effect=collect))
        return self

    def __exit__(self, *args):
        self.stack.close()


def _empty_turn(expected, index, code='budget_exhausted'):
    return dict(index=index, question=expected['question'], phase='target', status='not_run', code=code,
                requires_decision=expected['requires_decision'],
                budget_exhausted=code == 'budget_exhausted', formed=False, passed=False, trace_complete=False,
                scope=dict(violations=[], unknown=['turn_not_executed'], passed=False), model_calls=0,
                generated_sentences=None, deleted_sentences=None, output_truncations=None,
                issue_counts_known=False, source_calls=None)


def _empty(case, code='budget_exhausted'):
    turns = [_empty_turn(t, i, code) for i, t in enumerate(case['turns'])]
    for turn in turns[:-1]:
        turn['phase'] = 'setup'
    return dict(id=case['id'], security_name=case['security_name'], instrument_id=case['instrument_id'],
                tags=case['tags'], status='not_run', code=code, turns=turns, formed=False, passed=False,
                model_calls=0, elapsed_ms=None, ledger=[])


class _TurnModel:
    """Keep one API session while resetting only the evaluation's per-turn allowance."""
    delegate = None

    def complete(self, messages, *, tools):
        return self.delegate.complete(messages, tools=tools)


def _run(cases, plan, meter, delegate, trace_root):
    rows = []
    for case in cases:
        row, start, before = _empty(case), perf_counter(), meter.forwarded
        rows.append(row)
        if meter.forwarded >= meter.limit:
            continue
        path = trace_root / case['id']
        try:
            path.mkdir(parents=True)
            with (TechnicalFixedSources(cases, plan['collection_workers']) if plan['mode'] != 'live' else live_sources(plan)) as sources:
                fixed = plan['mode'] != 'live'
                per_case = EvaluationModelMeter(meter, 6 * len(case['turns']))
                current = _TurnModel()
                service = ResearchApiService(model_factory=lambda: current, directory_loader=lambda _: sources.directory,
                    provider=plan['provider'], data_mode='tencent' if fixed else plan['data_source'],
                    reference_date=REFERENCE_DATE if fixed else None, product_data_service=sources.company,
                    wall_clock=(lambda: NOW) if fixed else None, trace_sink=JsonlTraceSink(path),
                    trace_mode='development' if fixed else 'normal')
                try:
                    credentials, seen = service.create_session(), set()
                    for index, expected in enumerate(case['turns']):
                        if meter.forwarded >= meter.limit or per_case.forwarded >= per_case.limit:
                            break
                        if plan['mode'] == 'offline':
                            delegate.start(case, index)
                        per_turn = EvaluationModelMeter(per_case, 6)
                        current.delegate = per_turn
                        forwarded, blocked = meter.forwarded, meter.blocked
                        with CollectionAudit(sources) as audit, diagnostic_location(case_id=case['id'], phase=row['turns'][index]['phase']):
                            result, elapsed, trace = _send(service, credentials, expected['question'], path, seen)
                        quality = deepcopy((result.get('company_research') or {}).get('quality') or {})
                        validation = next((e.get('validation', {}) for e in reversed((trace or {}).get('event_records', []))
                                           if e['kind'] == 'check' and e['operation'] == 'answer_validation'), {})
                        for key in ('output_truncation_version', 'output_truncations', 'issue_count_version', 'draft_issue_counts',
                                    'deduplicated_drivers', 'deduplicated_sentences', 'inline_binding_recoveries'):
                            if key in validation:
                                quality[key] = validation[key]
                        graded_result = deepcopy(result)
                        if graded_result.get('company_research'):
                            graded_result['company_research']['quality'] = quality
                        grade = grade_turn(case, expected, graded_result, audit.observation, audit.calls)
                        exhausted = bool(per_turn.blocked or per_case.blocked or meter.blocked > blocked
                            or result.get('code') in ('evaluation_budget_exhausted', 'model_call_budget_exhausted'))
                        script_complete = plan['mode'] != 'offline' or not delegate.remaining and not delegate.failed
                        row['turns'][index].update(grade, status='executed', code=result.get('code'),
                            turn_status=result.get('status'), budget_exhausted=exhausted,
                            elapsed_ms=elapsed, model_calls=meter.forwarded - forwarded,
                            trace_complete=bool(trace and not trace['truncated'] and not trace['missing_parents']),
                            trace_run_id=(trace or {}).get('run_id'),
                            route=dict(kind=trace['route'], intent=trace.get('intent')) if trace else None,
                            script_complete=script_complete, source_calls=deepcopy(audit.calls),
                            observation=deepcopy(audit.observation), quality=quality,
                            parameters=deepcopy(result.get('parameters')),
                            answer=result.get('answer'), company_research=_fixed_authenticity(result.get('company_research')) if fixed else result.get('company_research'))
                        row['turns'][index]['passed'] &= script_complete and not exhausted
                finally:
                    service.close()
            row.update(status='executed', code=row['turns'][-1]['code'])
        except Exception:
            row.update(status='failed', code='technical_case_error')
        row.update(formed=row['turns'][-1]['formed'] and all(t.get('checks', {}).get('identity')
                   and t.get('checks', {}).get('published') for t in row['turns'][:-1]),
                   passed=all(t['passed'] for t in row['turns']), model_calls=meter.forwarded - before,
                   elapsed_ms=round((perf_counter() - start) * 1000, 3), ledger=deepcopy(meter.ledger[before:]))
    return rows


def aggregate_turns(selected):
    known = [t for t in selected if type(t.get('generated_sentences')) is int and type(t.get('deleted_sentences')) is int]
    generated, deleted = (sum(t[key] for t in known) for key in ('generated_sentences', 'deleted_sentences'))
    unknown = len(selected) - len(known)
    return dict(generated_sentences=generated if not unknown else None,
        deleted_sentences=deleted if not unknown else None, known_generated_sentences=generated, known_deleted_sentences=deleted,
        sentence_deletion_rate=ratio(deleted, generated) if not unknown else None, unknown_turns=unknown,
        draft_failure_counts={phase: {code: sum(t['draft_failure_counts'][phase][code] for t in selected if t.get('issue_counts_known'))
            if all(t.get('issue_counts_known') for t in selected) else None for code in FAILURE_CODES}
            for phase in ('first_draft', 'repair_draft', 'all_drafts')},
        final_reason_counts={code: sum(t['final_reason_counts'][code] for t in selected)
            if all(type(t.get('final_reason_counts', {}).get(code)) is int for t in selected) else None for code in FAILURE_CODES},
        issue_counts_unknown_turns=sum(not t.get('issue_counts_known') for t in selected),
        output_truncations=sum(t['output_truncations'] for t in selected) if all(type(t.get('output_truncations')) is int for t in selected) else None,
        **{key: sum(t[key] for t in selected) if all(type(t.get(key)) is int for t in selected) else None
            for key in ('deduplicated_drivers', 'deduplicated_sentences', 'inline_binding_recoveries')})


def summarize(rows):
    require(len(rows) == 10 and len({r['id'] for r in rows}) == 10,
            code='technical_report_incomplete', detail='报告必须保留全部10题。')
    turns = [t for r in rows for t in r['turns']]
    violations = sum(len(t['scope']['violations']) for t in turns)
    unknown = sum(bool(t['scope']['unknown']) for t in turns)
    formed = sum(r['formed'] for r in rows)
    return dict(questions=10, turns=14, formed=formed, formation_rate=ratio(formed, 10), minimum_formed=9,
        formation_target_met=formed >= 9, scope_violations=violations, scope_unknown_turns=unknown,
        scope_target_met=violations == 0 and unknown == 0,
        decision_checks_passed=all(t.get('checks', {}).get('rating_direction') and t.get('checks', {}).get('reference_plan')
                                  for t in turns if t['requires_decision']),
        budget_exhausted_questions=sum(any(t['budget_exhausted'] for t in r['turns']) for r in rows),
        quality_failed_questions=sum(not r['formed'] and not any(t['budget_exhausted'] for t in r['turns']) for r in rows),
        outcome_codes=dict(Counter(r['code'] for r in rows)),
        target=aggregate_turns([r['turns'][-1] for r in rows]), setup=aggregate_turns([t for t in turns if t['phase'] == 'setup']),
        all_turns=aggregate_turns(turns), end_to_end_latency=_latency([r['elapsed_ms'] for r in rows]))


def evaluate(**kwargs):
    base = dict(version=VERSION, notice=NOTICE, review_status='draft_requires_manual_review', cases=[], language_quality='requires_manual_review')
    try:
        plan, cases, prices = make_plan(**kwargs)
    except PlanError as error:
        return {**base, **error.result()}
    except Exception:
        return {**base, 'status': 'invalid_request', 'code': 'technical_plan_invalid'}
    output = dict(**base, plan=plan, code_revision=_revision(), working_tree_dirty=_dirty(),
        implementation_sha256=implementation_fingerprint(), evaluated_at=datetime.now(timezone.utc).isoformat(),
        real_model_requested=plan['mode'] != 'offline', real_data_requested=plan['mode'] == 'live',
        evaluation_scope='offline_contracts_not_model_quality' if plan['mode'] == 'offline' else 'real_model_answers_require_manual_review')
    meter, error, denied_network, credentials = None, None, [], []
    diagnostics = AnswerDiagnostics(kwargs['diagnostics_dir']) if kwargs.get('diagnostics_dir') else None
    try:
        from scripts.verify import clean_environment, offline_network
        with ExitStack() as stack:
            if diagnostics:
                stack.enter_context(bind_diagnostics(diagnostics))
            if plan['mode'] == 'offline':
                stack.enter_context(clean_environment())
                stack.enter_context(offline_network())
                def denied(*args, **kw):
                    credentials.append(True)
                    raise RuntimeError('No offline credentials')
                for name in ('load_model_environment', '_read_local_values'):
                    stack.enter_context(patch('trading_companion.local_env.' + name, denied))
            elif plan['mode'] == 'live-model':
                denied_network = stack.enter_context(model_network_boundary(plan['provider']))
            delegate = TechnicalContractModel() if plan['mode'] == 'offline' else build_real_model(plan)
            meter = TimedMeter(delegate, plan['model_call_limit'])
            trace_dir = kwargs.get('trace_dir')
            trace_root = Path(trace_dir) / uuid4().hex if trace_dir else Path(stack.enter_context(TemporaryDirectory(prefix='technical-evaluation-')))
            output['cases'] = _run(cases, plan, meter, delegate, trace_root)
            output['trace_directory'] = str(trace_root.absolute()) if trace_dir else None
    except Exception:
        error = 'evaluation_initialization_or_boundary_error'
    if not output['cases']:
        output['cases'] = [_empty(c, error or 'evaluation_not_run') for c in cases]
    for row in output['cases']:
        row['cost'] = actual_cost(prices, plan['provider'], plan['model'], row['ledger'])
        row['metrics'] = dict(target=aggregate_turns(row['turns'][-1:]), setup=aggregate_turns(row['turns'][:-1]),
                             all_turns=aggregate_turns(row['turns']))
    output.update(metrics=summarize(output['cases']),
        accounting=dict(forwarded=meter.forwarded if meter else 0, attempts=meter.attempts if meter else 0,
                        blocked=meter.blocked if meter else 0, token_usage=reported_usage(meter.usage if meter else [])),
        cost=actual_cost(prices, plan['provider'], plan['model'], meter.ledger if meter else []))
    complete = all(r['status'] == 'executed' and all(t['status'] == 'executed' and t['trace_complete']
                    and not t['budget_exhausted'] and t.get('script_complete', True) for t in r['turns']) for r in output['cases'])
    target = output['metrics']
    passed = complete and target['formation_target_met'] and target['scope_target_met'] and target['decision_checks_passed']
    # Required indicators and explicit history are additional per-turn contracts; a missing narrative alone has the 9/10 threshold.
    contracts = all(all(value for key, value in t.get('checks', {}).items() if key not in ('published', 'model_answer'))
                    for r in output['cases'] for t in r['turns'])
    error = error or ('evaluation_source_network_violation' if denied_network else
                      'evaluation_offline_credential_violation' if credentials else None)
    output.update(status='passed' if passed and contracts and not error else 'failed',
        code=error or ('technical_target_met' if passed and contracts else 'technical_target_not_met'),
        quality_target_claim='not_evaluated_offline' if plan['mode'] == 'offline' else 'mechanical_target_requires_manual_review',
        offline_credential_attempts=len(credentials), blocked_source_network_attempts=len(denied_network))
    if diagnostics:
        output['diagnostics'] = diagnostics.summary()
        if not output['diagnostics']['complete']:
            output.update(status='failed', code='evaluation_diagnostics_incomplete')
    return output


def write_report(report, root):
    check_output_root(root)
    destination = Path(root) / ('technical-' + uuid4().hex)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'report.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# 技术回答评估', '', NOTICE, '', '离线结果只验证人工脚本和检查器，不代表真实模型质量。', '',
             '删句分母沿用产品口径（含标题、两稿）；去重删除的标题和正文句子计入删除数，另列去重数量。', '',
             f"状态：{report['status']}；正文形成：{report['metrics']['formed']}/10；范围违规：{report['metrics']['scope_violations']}。", '',
             '| 题号 | 正文形成 | 范围违规 | 预算耗尽 | 模型调用 |', '| --- | --- | --- | --- | --- |']
    for row in report['cases']:
        lines.append(f"| {row['id']} | {row['formed']} | {sum(len(t['scope']['violations']) for t in row['turns'])} | "
                     f"{any(t['budget_exhausted'] for t in row['turns'])} | {row['model_calls']} |")
    lines += ['', '完整逐轮删句、失败码、截断、成本和未知计数见 report.json。评级和投资含义仍需逐题人工复核。', '']
    (destination / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    reviews = [dict(id=r['id'], notice=NOTICE, status=r['status'], relevance=None, evidence_support=None,
                    scope_semantics=None, wording_and_conditions=None, reviewed_by=None) for r in report['cases']]
    (destination / 'manual-review.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in reviews), encoding='utf-8')
    return str(destination.absolute())
