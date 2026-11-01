"""Public v2.5 development/regression evaluation. No sealed-corpus entry point."""

from copy import deepcopy
from contextlib import nullcontext
from contextvars import ContextVar
from hashlib import sha256
import json
from math import isfinite
import os
import subprocess
from threading import Lock
from time import perf_counter
from unittest.mock import patch

from evaluation_tools import research_intent_v25_cases as cases
from trading_companion import research_intent_v2 as intent
from trading_companion import research_intent_v2_evaluation as core
from trading_companion.agent_input import InputIssue
from trading_companion.deepseek_tool_model import retry_tool_arguments
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.intent_router_task import digest, read_json
from trading_companion.model import ModelCallError, ModelConfigurationError
from trading_companion.research_intent_evaluation import field_scores, same
from trading_companion.research_intent_evaluation_baseline import UNKNOWN, rule_observation
from trading_companion.research_intent_evaluation_cases import GOLD_FIELDS, ROOT, input_state, load_corpus
from trading_companion.turn_control import TurnControl, TurnLimits

NOT_APPLICABLE = {'V2222', 'V2223'}
LIMITS = {'dev': 233, 'regression': 85}
REPLAY_NOTICE = 'v2.4 原始响应经 v2.5.4 程序回放，不是新模型成绩'
REPLAY_251_NOTICE = 'v2.5 原始响应经 v2.5.4 程序回放，不是新模型成绩'
REPLAY_252_NOTICE = 'v2.5.1 原始响应经 v2.5.4 程序回放，不是新模型成绩'
REPLAY_253_NOTICE = 'v2.5.2 原始响应经 v2.5.4 程序回放，不是新模型成绩'
REPLAY_254_NOTICE = 'v2.5.3 原始响应经 v2.5.4 程序回放，不是新模型成绩'
FAILED_ARGUMENT_NOTICE = '评估工件包含失败的模型参数原文'
ADDENDUM = ROOT/'data/evaluation/research-intent-v2.5.1-amendments.json'
_ARGUMENT_SINK = ContextVar('v251_argument_diagnostic_sink', default=None)


def apply_annotation_completion(original, effective, path=ADDENDUM):
    """Exactly the one #404 approval, layered over the unchanged #390 record."""
    value = read_json(path)
    row = next(r for r in original if r['id'] == 'V2162')
    if (value.keys() != {'version', 'parent_corpus_sha256', 'parent_amendments_sha256', 'changes'}
            or value['version'] != 'research-intent-v2.5.1-addendum-v1'
            or value['parent_corpus_sha256'] != cases.public_sha256(ROOT/'data/evaluation/research-intent-v2.jsonl')
            or value['parent_amendments_sha256'] != cases.public_sha256(cases.AMENDMENTS)
            or type(value['changes']) is not list or len(value['changes']) != 1):
        raise ValueError('Invalid annotation completion parent/inventory')
    change = value['changes'][0]
    expected = dict(id='V2162', parent_row_sha256=digest(row), field='date_expression', before=None,
                    after='今天', reviewer='claude', status='approved', approval_message=404)
    if (type(change) is not dict or change.keys() != {*expected, 'reason'}
            or any(change[key] != val or type(change[key]) is not type(val) for key, val in expected.items())
            or type(change['reason']) is not str or not change['reason'].strip()
            or effective['V2162']['date_expression'] is not None):
        raise ValueError('Only the single approved #404 annotation completion is accepted')
    result = deepcopy(effective)
    result['V2162']['date_expression'] = '今天'
    return result, dict(label='标注补齐', model_improvement=False, **value)


def annotation_completion():
    original, _ = load_corpus()
    _, _, approved = cases.load_regression_amendments()
    return apply_annotation_completion(original, {r['id']: r['gold'] for r in approved})[1]


def populations(split='all'):
    if split not in ('all', *LIMITS):
        raise ValueError('Only public dev/regression are accepted; no holdout entry')
    freeze = cases.validate_freeze(require_review=True)
    original, _ = load_corpus()
    _, _, approved = cases.load_regression_amendments()
    fresh = cases.load_cases(expected_sha256=freeze['dev_sha256'])
    population = read_json(ROOT / 'data/evaluation/research-intent-v2.4-holdout-population.json')
    if (set(population['not_applicable']) != NOT_APPLICABLE or population['approval_message'] != 383
            or population['reviewer'] != 'claude'):
        raise ValueError('Unapproved regression exclusions')
    effective = {r['id']: r['gold'] for r in [*approved, *fresh]}
    effective, _ = apply_annotation_completion(original, effective)
    rows = [*original, *fresh]
    for row in rows:
        row['dataset'] = 'regression' if row['split'] == 'holdout' else 'dev'
        if row['dataset'] == 'regression':
            row['split'] = 'regression'  # All original 85 cases are exposed, never unseen.
    selected = [r for r in rows if split == 'all' or r['dataset'] == split]
    if len(selected) != (318 if split == 'all' else LIMITS[split]):
        raise ValueError('Public evaluation population drift')
    prefixes = core.prefix_library([r for r in original if r['split'] == 'dev'])
    return selected, effective, prefixes, cases.load_setups()


def build_context(row, prefixes, setups):
    if 'setup_id' in row:
        return cases.build_context(cases.state_input(row), setups, prefixes)
    return core.program_context(dict(question='仅检查程序上下文，不执行目标轮', context=deepcopy(row['context'])), prefixes)


def preflight(rows, prefixes, setups):
    from scripts.verify import clean_environment, offline_network
    records = []
    with clean_environment(), offline_network(), \
            patch.object(intent, 'understand', side_effect=AssertionError('No target understanding')), \
            patch.object(intent, 'local_preflight', side_effect=AssertionError('No target interpretation')), \
            patch.object(core, 'predict', side_effect=AssertionError('No target prediction')), \
            patch.object(core, 'understand', side_effect=AssertionError('No target understanding')), \
            patch.object(core, 'local_preflight', side_effect=AssertionError('No target interpretation')), \
            patch.object(core, 'metrics', side_effect=AssertionError('No target scoring')), \
            patch(__name__ + '._live_model', side_effect=AssertionError('No credentials')), \
            patch.object(core, '_live_model', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_evaluation_key_environment', side_effect=AssertionError('No credentials')):
        for row in rows:
            if row['id'] in NOT_APPLICABLE:
                records.append(dict(id=row['id'], dataset=row['dataset'], status='not_applicable'))
                continue
            try:
                context, calls = build_context(row, prefixes, setups)
                records.append(dict(id=row['id'], dataset=row['dataset'], status='buildable', setup_calls=calls,
                    descriptor_sha256=digest(row['context']), context_sha256=digest(context.model_context()),
                    setup=core.setup_metadata(context)))
            except (ValueError, IndexError, KeyError):
                records.append(dict(id=row['id'], dataset=row['dataset'], status='unbuildable',
                                    reason='已审核前置不能构建声明状态；未补造'))
    failed = [r['id'] for r in records if r['status'] == 'unbuildable']
    expected = ['V2002'] if any(r['id'] == 'V2002' for r in rows) else []
    return dict(total=len(rows), buildable=sum(r['status'] == 'buildable' for r in records),
        not_applicable=[r['id'] for r in records if r['status'] == 'not_applicable'], unbuildable=failed,
        expected_legacy_gap=expected, passed=failed == expected, target_calls=0, prediction_rows=0, score_rows=0, rows=records)


def _git(*args):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, timeout=15, check=True)
    return result.stdout.decode('utf-8').strip()


def fingerprints():
    paths = [p for folder in ('src', 'scripts', 'evaluation_tools') for p in (ROOT/folder).rglob('*.py')]
    paths += list((ROOT/'tests').glob('test_research_intent*.py'))
    paths += [ROOT/'tests/test_deepseek_tool_model.py', ROOT/'tests/test_turn_control.py', ADDENDUM]
    paths += [ROOT/name for name in cases.PUBLIC_FILES]
    paths += [ROOT/'data/evaluation/research-intent-v2.5-freeze.json',
              ROOT/'data/evaluation/research-intent-v2.4-holdout-population.json']
    return {p.relative_to(ROOT).as_posix(): cases.public_sha256(p) for p in sorted(set(paths))}


def build_plan(split='all', maximum=None):
    rows, _, prefixes, setups = populations(split)
    limit = 318 if split == 'all' else LIMITS[split]
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= limit):
        raise ValueError('Attempt limit must only tighten the public population cap')
    return dict(kind='research-intent-v2.5-public-plan', contract_version=intent.VERSION,
        guard_version=intent.GUARD_VERSION, split=split, total=len(rows), max_calls=maximum or limit,
        max_attempts_per_target=2, model='deepseek-flash', temperature=0, thinking='disabled', max_output_tokens=1500,
        timeout_seconds=45, runtime_revision=_git('rev-parse', 'HEAD'), workspace_dirty=bool(_git('status', '--porcelain')),
        source_sha256_lf=fingerprints(), prompt_sha256=sha256(intent.INSTRUCTION.encode('utf-8')).hexdigest(),
        tool_sha256=digest(intent.TOOL.to_dict()), context_preflight=preflight(rows, prefixes, setups),
        annotation_completion=annotation_completion(),
        tool_arguments_policy=dict(strict_first=True, candidate_only=True, repair_enabled=True,
            allowed_repairs=['control_char', 'code_fence', 'trailing_text', 'unquoted_scalar'], retries=1, failed_body_saved=True,
            retry_only='tool_arguments_json_invalid', retry_same_messages=True, retry_repairs=['unquoted_scalar'],
            retry_transient_errors=False, failed_arguments_max_characters=10000,
            global_budget_includes_retries=True, approval_messages=[415, 416, 417]),
        live_enabled=False, paid_calls=0, eligible_for_switch=False,
        thresholds=dict(dev=dict(n=233, minimum_correct=222), regression=dict(n=83, minimum_correct=79),
                        reference_regression_n=85, common_fields_no_regression=True, redline_violations=0),
        note='仅公开开发和已暴露回归；完整分母保留失败，前置替身调用单列，无新检验入口。')


class RunInvalidated(RuntimeError):
    pass


def assert_current(plan):
    if (_git('rev-parse', 'HEAD') != plan['runtime_revision'] or _git('status', '--porcelain')
            or fingerprints() != plan['source_sha256_lf']):
        raise RunInvalidated('Reviewed revision or source drift; run invalidated, no retry')


class AttemptBudget(core.AttemptBudget):
    def __init__(self, maximum, event_sink=None):
        if type(maximum) is not int or not 1 <= maximum <= 233:
            raise ValueError('Maximum public attempts must be 1..233')
        self.maximum, self.attempts, self.lock = maximum, 0, Lock()
        self.event_sink, self.identifiers = event_sink, set()

    def target(self, model, identifier=None):
        with self.lock:
            if identifier in self.identifiers:
                raise ValueError('No second target wrapper for a target')
            self.identifiers.add(identifier)
        return TargetAttempt(self, model, identifier)


class TargetAttempt(core.TargetAttempt):
    def __init__(self, *args):
        super().__init__(*args)
        self.argument_diagnostics = []
        self.attempt_history = []
        self._retry_pending = False
        self._request = None

    def argument_diagnostic(self, event):
        event = dict(deepcopy(event), target_attempt=self.attempts)
        self.argument_diagnostics.append(event)
        self.record('argument_diagnostic', diagnostic=event, target_attempt=self.attempts)

    def can_retry_arguments(self):
        with self.budget.lock:
            return self._retry_pending and self.attempts == 1 and self.budget.attempts < self.budget.maximum

    def complete(self, messages, *, tools):
        request = deepcopy((messages, tools))
        with self.budget.lock:
            if (self.attempts >= 2 or self.budget.attempts >= self.budget.maximum
                    or self.attempts and (not self._retry_pending or request != self._request)):
                raise ModelConfigurationError('Public attempt budget exhausted or unauthorized retry')
            self.attempts += 1
            self.budget.attempts += 1
            self._retry_pending, self._request = False, request
            self.response = None
            self.record('reserved', target_attempt=self.attempts)
        start, failure, diagnostic_start = perf_counter(), None, len(self.argument_diagnostics)
        token = _ARGUMENT_SINK.set(self.argument_diagnostic)
        try:
            with retry_tool_arguments() if self.attempts == 2 else nullcontext():
                self.response = self.delegate.complete(messages, tools=tools)
            return self.response
        except BaseException as error:
            failure = intent.safe_failure(error) if isinstance(error, Exception) else 'interrupted'
            self._retry_pending = (self.attempts == 1 and isinstance(error, ModelCallError)
                                   and error.code == 'tool_arguments_json_invalid')
            raise
        finally:
            _ARGUMENT_SINK.reset(token)
            elapsed = round((perf_counter() - start) * 1000, 3)
            self.elapsed_ms = round((self.elapsed_ms or 0) + elapsed, 3)
            record = dict(target_attempt=self.attempts, failure=failure, call_ms=elapsed,
                          response=self.response.to_dict() if self.response is not None else None,
                          argument_diagnostics=deepcopy(self.argument_diagnostics[diagnostic_start:]))
            self.attempt_history.append(record)
            self.record('finished', returned=self.response is not None, **record)


def _argument_diagnostic(event):
    sink = _ARGUMENT_SINK.get()
    if sink is None:
        raise ModelConfigurationError('Candidate diagnostics require a metered target')
    sink(event)


def _live_model(timeout_seconds):
    from trading_companion.deepseek_model import DeepSeekConfig
    from trading_companion.deepseek_tool_model import DeepSeekToolModel
    from trading_companion.local_env import load_evaluation_key_environment
    key = load_evaluation_key_environment('DEEPSEEK_API_KEY').get('DEEPSEEK_API_KEY', '')
    return DeepSeekToolModel(DeepSeekConfig(model='deepseek-flash', api_key=key,
        timeout_seconds=timeout_seconds, max_output_tokens=1500), temperature=intent.TEMPERATURE,
        response_diagnostics=True, repair_tool_arguments=True, argument_diagnostic_sink=_argument_diagnostic,
        evaluation_scope='public', save_failed_arguments=True)


def checked_result(question, response, context):
    log = []
    try:
        result = intent.check_response(question, response, context=context, corrections=log)
        normalized = dict(status='ok', reason=None, fields=result.fields(), low_confidence=result.low_confidence,
            corrections=log, metadata=dict(historical_window=list(result.historical_window),
                provenance=list(result.provenance), plan_blocked=result.plan_blocked))
    except (InputIssue, InvalidRequestError, DataValidationError, ValueError) as error:
        normalized = dict(status='invalid', reason=error.code if isinstance(error, InputIssue) else 'invalid_conditions',
                          fields={}, corrections=log)
    try:
        raw = intent.check_response(question, response, context=context, normalize=False)
        normalized['raw'] = dict(status='ok', fields=raw.fields())
    except (InputIssue, InvalidRequestError, DataValidationError, ValueError) as error:
        normalized['raw'] = dict(status='invalid', fields={},
                                reason=error.code if isinstance(error, InputIssue) else 'invalid_conditions')
    return normalized


def predict(row, *, prefixes, setups, model, budget, timeout_seconds=45):
    if row['id'] in NOT_APPLICABLE:
        return _empty('not_applicable', '架构不存在会话/轮次归属，#383已批准不适用')
    try:
        context, calls = build_context(row, prefixes, setups)
    except (ValueError, IndexError, KeyError):
        return _empty('not_measured', '已审核前置不能构建声明状态；未补造')
    local = intent.local_preflight(row['question'], context)
    if local is not None:
        return dict(_empty('ok', None, calls), fields=local.fields(), path='local_preflight',
                    metadata=dict(provenance=list(local.provenance)))
    if budget.attempts >= budget.maximum:
        return _empty('not_measured', '全局尝试上限已到（含格式重问）；未缩分母', calls)
    target = budget.target(model, row['id'])
    control = TurnControl(TurnLimits(model_calls=2, timeout_seconds=timeout_seconds))
    try:
        run = intent.understand(row['question'], model=target, context=context, control=control, retry_json_arguments=True)
        result = (checked_result(row['question'], target.response, context) if run.status in ('ok', 'invalid')
                  and target.response is not None else dict(status=run.status, reason=run.code, fields={}))
        result['runtime'] = run.runtime
    except RunInvalidated:
        raise
    except Exception as error:
        result = dict(status='failed', reason=intent.safe_failure(error), fields={})
    repairs = [event for event in target.argument_diagnostics if event['correction'] == 'tool_arguments_repaired']
    if repairs:
        result.setdefault('corrections', []).extend(dict(kind='tool_arguments_repaired', field='tool_arguments',
            before='invalid_json', after='checked_json', source='transport', repair=event['repair']) for event in repairs)
        result['raw'] = dict(status='failed', reason='tool_arguments_json_invalid', fields={})
    retried = target.attempts == 2
    if retried:
        result['raw'] = dict(status='failed', reason=target.attempt_history[0]['failure'], fields={})
    return dict(_empty(result['status'], result.get('reason'), calls), **{k: v for k, v in result.items()
        if k not in ('status', 'reason')}, attempts=target.attempts,
        response=target.response.to_dict() if target.response else None, call_ms=target.elapsed_ms,
        argument_diagnostics=target.argument_diagnostics,
        attempt_history=target.attempt_history,
        format_retry=dict(attempted=retried, successful=retried and result['status'] == 'ok'),
        response_origin='format_retry' if retried else 'repaired_tool_arguments' if repairs else 'provider',
        path='semantic', context_sha256=digest(context.model_context()), setup=core.setup_metadata(context))


def _empty(status, reason, setup_calls=0):
    return dict(status=status, reason=reason, fields={}, attempts=0, setup_calls=setup_calls,
                response=None, call_ms=None, corrections=[], argument_diagnostics=[])


def _groups(rows):
    return {'all': rows, 'applicable': [r for r in rows if r['id'] not in NOT_APPLICABLE],
            'semantic': [r for r in rows if not r['gold']['zero_intent_calls']],
            'local': [r for r in rows if r['gold']['zero_intent_calls']],
            **{dataset: [r for r in rows if r['dataset'] == dataset] for dataset in LIMITS},
            **{'family:' + f: [r for r in rows if r['family_id'] == f] for f in sorted({r['family_id'] for r in rows})}}


def _common(rows):
    fields = {}
    for field in GOLD_FIELDS:
        observed = [r for r in rows if r['A']['status'] == 'ok' and r['A']['fields'].get(field, UNKNOWN) != UNKNOWN]
        old = sum(same(r['A']['fields'][field], r['gold'][field]) for r in observed)
        new = sum(r['V2']['status'] == 'ok' and same(r['V2']['fields'].get(field, UNKNOWN), r['gold'][field]) for r in observed)
        fields[field] = dict(n=len(observed), rule_correct=old, candidate_correct=new, passed=new >= old)
    return dict(fields=fields, passed=all(f['passed'] for f in fields.values()),
                scope='同一输入和批准标注的规则共同可观测字段，不推断规则无法观测的字段')


def redlines(rows):
    checks = {key: dict(measured=0, violations=0, unmeasured=0) for key in
              ('local_intent_call', 'wrong_subject', 'vetoed_plan', 'strict_scope')}
    for row in rows:
        if row['id'] in NOT_APPLICABLE:
            continue
        gold, observation = row['gold'], row['V2']
        values = observation['fields']
        if gold['zero_intent_calls']:
            checks['local_intent_call']['measured'] += 1
            checks['local_intent_call']['violations'] += bool(observation['attempts'])
        for key in ('wrong_subject', 'vetoed_plan', 'strict_scope'):
            if observation['status'] != 'ok':
                checks[key]['unmeasured'] += 1
                continue
            checks[key]['measured'] += 1
        if observation['status'] != 'ok':
            continue
        checks['wrong_subject']['violations'] += bool(values['bound_subjects'] and not same(values['bound_subjects'], gold['bound_subjects']))
        checks['vetoed_plan']['violations'] += bool('plan' in gold['exclusions'] and values['wants_plan'])
        checks['strict_scope']['violations'] += bool(gold['strict_scope'] and
            (not values['strict_scope'] or set(values['research_kinds']) - set(gold['research_kinds'])
             or set(values['indicator_mentions']) - set(gold['indicator_mentions'])))
    return dict(checks=checks, passed=not any(c['violations'] for c in checks.values()),
                scope='仅候选理解字段与调用；实际执行、价位、跨对象证据及正式会话端到端未测')


def report_results(rows, effective, plan, *, live=False, notice=None):
    views = {}
    approved = []
    for name in ('raw_original', 'normalized_original', 'normalized_approved'):
        selected = deepcopy(rows)
        for row in selected:
            if name == 'raw_original':
                row['V2'] = row['V2'].get('raw', row['V2'])
            if name == 'normalized_approved':
                row['gold'] = deepcopy(effective[row['id']])
        views[name] = {key: field_scores(group, 'V2') for key, group in _groups(selected).items()}
        if name == 'normalized_approved':
            approved = selected
    common, lines = _common([r for r in approved if r['id'] not in NOT_APPLICABLE]), redlines(approved)
    thresholds = {}
    for dataset, minimum in (('dev', 222), ('regression', 79)):
        selected = [r for r in approved if r['dataset'] == dataset and r['id'] not in NOT_APPLICABLE]
        if not selected:
            continue
        scores = field_scores(selected, 'V2')
        correct = scores['joint_all_fields']['numerator']
        thresholds[dataset] = dict(n=len(selected), correct=correct, minimum_correct=minimum,
            joint_passed=correct >= minimum, common=_common(selected), redlines=redlines(selected))
    # Old telemetry assumes one call per row. Expand only metered histories so a
    # missing first response remains unknown usage, rather than disappearing.
    metered_rows = []
    for row in rows:
        observation = row['V2']
        history = observation.get('attempt_history', [])
        if history:
            for index, attempt in enumerate(history):
                metered_rows.append(dict(row, V2=dict(observation, attempts=1,
                    response=attempt['response'], call_ms=attempt['call_ms'],
                    setup_calls=observation['setup_calls'] if index == 0 else 0)))
        else:
            metered_rows.append(row)
    telemetry = core.telemetry(metered_rows)
    telemetry['low_confidence'] = sum(bool(r['V2'].get('low_confidence')) for r in rows)
    telemetry['raw_low_confidence'] = sum(any(c.get('arguments', {}).get('confidence') == 'low'
        for c in (r['V2']['response'] or {}).get('tool_calls', [])) for r in rows)
    retried = [r for r in rows if r['V2'].get('format_retry', {}).get('attempted')]
    recovered = [r for r in retried if r['V2']['format_retry']['successful']]
    saved = sum('arguments_raw' in e for r in rows for e in r['V2'].get('argument_diagnostics', []))
    repairs = {}
    for row in rows:
        for event in row['V2'].get('argument_diagnostics', []):
            if event['correction'] == 'tool_arguments_repaired':
                group = repairs.setdefault(event['repair'], dict(count=0, ids=[]))
                group['count'] += 1
                if row['id'] not in group['ids']:
                    group['ids'].append(row['id'])
    authenticity = ('archived_response_replay' if notice in (REPLAY_NOTICE, REPLAY_251_NOTICE, REPLAY_252_NOTICE, REPLAY_253_NOTICE, REPLAY_254_NOTICE) else
        'mock_experiment' if telemetry['mock_responses'] else
        'live_provider' if live and telemetry['attempts'] else 'no_provider_observation')
    return dict(kind='research-intent-v2.5-public-report', title=notice or 'v2.5 公开开发与已暴露回归',
        notice=notice, plan=plan, rows=rows, score_views=views, common_comparison=common, zero_tolerance=lines,
        thresholds=thresholds, paid_calls=telemetry['attempts'] if live else 0, telemetry=telemetry, authenticity=authenticity,
        corrections=core.correction_summary(rows), eligible_for_switch=False,
        tool_arguments_repairs=dict(total=sum(g['count'] for g in repairs.values()), by_category=repairs),
        format_retries=dict(total=len(retried), successful=len(recovered), ids=[r['id'] for r in retried],
                            successful_ids=[r['id'] for r in recovered]),
        failed_arguments=dict(notice=FAILED_ARGUMENT_NOTICE, saved_events=saved, max_characters=10000,
                              enabled=plan.get('tool_arguments_policy', {}).get('failed_body_saved', False)),
        annotation_completion=plan.get('annotation_completion'),
        passed=authenticity == 'live_provider' and bool(thresholds) and all(t['joint_passed'] and t['common']['passed'] and t['redlines']['passed']
                                       for t in thresholds.values()),
        unmeasured=['正式会话端到端', '真实研究执行和价位', '跨对象证据', '完整严格范围执行', '封存检验'],
        scoring_policy='三列保留失败与未测分母；raw仍通过当前通用校验，不是旧版本重演；旧85题全部已暴露。')


def markdown(report):
    notice = report.get('notice')
    lines = ['# ' + report['title'], '', report['scoring_policy'], '']
    def table(headers, rows):
        headers = list(headers)
        if notice:
            headers[0] = notice + ' — ' + headers[0]
        lines.extend(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |'])
        lines.extend('| ' + ' | '.join(str(v).replace('|', '/') for v in row) + ' |' for row in rows)
        lines.append('')
    table(('视图', '分组', '全字段正确', '分母'),
          ((name, group, score['joint_all_fields']['numerator'], score['n'])
           for name, groups in report['score_views'].items() for group, score in groups.items()))
    table(('逐题', '状态', '差异字段/原因'), ((r['id'], r['V2']['status'],
        ', '.join(f for f in GOLD_FIELDS if not same(r['V2']['fields'].get(f, UNKNOWN), r['effective_gold'][f]))
        if r['V2']['status'] == 'ok' else r['V2'].get('reason', '')) for r in report['rows']))
    table(('JSON 修复类别', '次数', '题号'), ((kind, group['count'], ', '.join(group['ids']))
          for kind, group in report['tool_arguments_repairs']['by_category'].items()))
    retries = report['format_retries']
    table(('格式重问', '次数', '题号'), [('重问请求', retries['total'], ', '.join(retries['ids'])),
          ('第二次解析（仅允许单处裸值修复）且语义核验成功', retries['successful'], ', '.join(retries['successful_ids']))])
    if 'saved_argument_replays' in report:
        table(('保存参数回放', '原尝试', '修复', '语义状态', '采用', '纠正', '来源'),
              ((e['id'], e['target_attempt'], e['repair'], e['status'], e['selected'],
                ', '.join(c['kind'] for c in e['corrections']), e['source_report'] + '#' + e['source_pointer'])
               for e in report['saved_argument_replays']))
        historical = report['historical_format_retries']
        table(('历史格式重问（本轮新调用0）', '次数', '题号'),
              [('历史请求', historical['total'], ', '.join(historical['ids'])),
               ('历史成功', historical['successful'], ', '.join(historical['successful_ids']))])
        summary = report['saved_argument_replay_summary']
        lines += [f"保存参数 {summary['saved']} 条，修复 {summary['repaired']} 条，采用 {summary['selected']} 条；"
                  'JSON修复分类只计采用结果，逐条回放另见保存参数表。', '']
        lines += [report['note'], '', '保存参数用合成载体校验；原调用名、ID、调用数量及服务商用量未知，未恢复原响应。', '']
    if report.get('annotation_completion'):
        table(('标注补齐（不计模型改善）', '字段', '原值', '新值', '批准消息'),
              ((c['id'], c['field'], c['before'], c['after'], c['approval_message'])
               for c in report['annotation_completion']['changes']))
    lines += [report['failed_arguments']['notice'] + '；单事件最多 10000 字符；本报告保存事件数：'
              + str(report['failed_arguments']['saved_events']), '',
              '实际新付费尝试（含重问）：' + str(report['paid_calls']), '',
              'eligible_for_switch=false；正式会话端到端、执行及新封存检验仍未测。', '']
    return '\n'.join(lines)


def _write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def evaluate(*, split='dev', live=False, maximum=None, timeout_seconds=45):
    if type(live) is not bool or (live and split not in LIMITS):
        raise ValueError('Live runs require one explicit public split')
    if type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds) or not 0 < timeout_seconds <= 90:
        raise ValueError('Timeout must be finite, positive and <=90 seconds')
    plan = build_plan(split, maximum)
    if not live:
        return plan
    if plan['workspace_dirty'] or not plan['context_preflight']['passed']:
        raise ValueError('Live evaluation requires a clean reviewed commit and preflight')
    plan.update(timeout_seconds=timeout_seconds, live_enabled=True)
    assert_current(plan)
    folder = ROOT/'.agentbus/evaluation-runs/research-intent-v2.5/public'/plan['runtime_revision']/split
    folder.mkdir(parents=True, exist_ok=False)  # Persistent claim: no retry/resume/new-directory bypass.
    _write_json(folder/'plan.json', plan)
    rows, effective, prefixes, setups = populations(split)
    with (folder/'attempts.jsonl').open('x', encoding='utf-8', newline='\n') as events, \
            (folder/'rows.jsonl').open('x', encoding='utf-8', newline='\n') as journal:
        def event_sink(event):
            events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
            events.flush()
            os.fsync(events.fileno())
        assert_current(plan)
        delegate = _live_model(timeout_seconds)
        class PinnedModel:
            def complete(self, messages, *, tools):
                assert_current(plan)
                return delegate.complete(messages, tools=tools)
        budget, output = AttemptBudget(plan['max_calls'], event_sink), []
        for row in rows:
            assert_current(plan)
            result = dict(row, effective_gold=effective[row['id']],
                          V2=predict(row, prefixes=prefixes, setups=setups, model=PinnedModel(), budget=budget,
                                     timeout_seconds=timeout_seconds), A=rule_observation(input_state(row)))
            output.append(result)
            journal.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
            journal.flush()
            os.fsync(journal.fileno())
        assert_current(plan)
        report = report_results(output, effective, plan, live=True)
    report['artifacts'] = str(folder)
    _write_json(folder/'report.json', report)
    (folder/'report.md').write_text(markdown(report), encoding='utf-8')
    return report
