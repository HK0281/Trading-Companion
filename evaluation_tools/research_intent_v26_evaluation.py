"""Public v2.6 candidate evaluation; only frozen dev and exposed regression inputs."""

from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite
import os
from threading import Lock

from evaluation_tools import research_intent_v25_evaluation as previous
from evaluation_tools import research_intent_v26_cases as cases
from trading_companion import research_intent_v2 as intent
from trading_companion.intent_router_task import digest
from trading_companion.model import ModelConfigurationError
from trading_companion.research_intent_evaluation import field_scores, same
from trading_companion.research_intent_evaluation_baseline import UNKNOWN, rule_observation
from trading_companion.research_intent_evaluation_cases import GOLD_FIELDS, ROOT, input_state, load_corpus
from trading_companion.turn_control import TurnControl, TurnLimits

core = previous.core
NOT_APPLICABLE = cases.NOT_APPLICABLE
LIMITS = {'dev': 64, 'regression': 414}
CALL_CAPS = {'dev': 80, 'regression': 520}
MINIMUM_CORRECT = {'dev': 61, 'regression': 392}
APPROVAL_MESSAGE = 439
FAILED_ARGUMENT_NOTICE = previous.FAILED_ARGUMENT_NOTICE
_git = previous._git
RunInvalidated = previous.RunInvalidated


def populations(split):
    if type(split) is not str or split not in LIMITS:
        raise ValueError('Only public dev/regression are accepted; no custom or holdout entry')
    freeze = cases.validate_freeze(require_review=True)
    legacy, _, amended = cases.load_amendments(require_approved=True)
    fresh = cases.load_cases(expected_sha256=freeze['dev_sha256_lf'])
    rows = deepcopy(fresh if split == 'dev' else legacy)
    effective = {r['id']: deepcopy(r['gold']) for r in (fresh if split == 'dev' else amended)}
    for row in rows:
        row.update(split=split, dataset=split)
    if len(rows) != LIMITS[split]:
        raise ValueError('Frozen public population drift')
    original, _ = load_corpus()
    return rows, effective, core.prefix_library(original), cases.load_setups()


def build_context(row, prefixes, setups):
    # The builder receives neither the target wording, its ID, nor any gold/diagnostic fields.
    state = dict(question='仅检查前置状态，不执行目标轮', context=deepcopy(row['context']))
    if 'setup_id' in row:
        state['setup_id'] = row['setup_id']
        return cases.build_context(state, setups, prefixes)
    return core.program_context(state, prefixes)


def fingerprints():
    paths = {p for folder in ('src', 'scripts', 'evaluation_tools') for p in (ROOT/folder).rglob('*.py')}
    paths.update((ROOT/'tests').glob('test_research_intent*.py'))
    paths.update(ROOT/name for name in (*cases.PUBLIC_FILES, *cases.PARENT_FILES,
        'data/evaluation/research-intent-v2.6-freeze.json', 'data/evaluation/research-intent-v2.4-holdout-population.json',
        'tests/test_deepseek_tool_model.py', 'tests/test_turn_control.py', 'README.md',
        'docs/research-intent-v2.6-implementation.md'))
    return {p.relative_to(ROOT).as_posix(): cases.public_sha256(p) for p in sorted(paths)}


def build_plan(split='dev', maximum=None):
    rows, _, _, _ = populations(split)
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= CALL_CAPS[split]):
        raise ValueError('Call limit may only tighten the separately approved split cap')
    if intent.VERSION != 'research-intent-v2.6':
        raise ValueError('Only the approved v2.6 contract may use this public runner')
    return dict(kind='research-intent-v2.6-public-plan', contract_version=intent.VERSION,
        guard_version=intent.GUARD_VERSION, split=split, total=len(rows), max_calls=maximum or CALL_CAPS[split],
        paid_approval_message=APPROVAL_MESSAGE, approved_call_caps=deepcopy(CALL_CAPS), combined_call_cap=600,
        max_attempts_per_target=2, model='deepseek-flash', temperature=0, thinking='disabled', max_output_tokens=1500,
        timeout_seconds=45, observed_token_limit=24000, input_usage='unknown', cost='unknown',
        runtime_revision=_git('rev-parse', 'HEAD'), workspace_dirty=bool(_git('status', '--porcelain')),
        source_sha256_lf=fingerprints(), freeze_sha256_lf=cases.public_sha256(cases.FREEZE),
        prompt_sha256=sha256(intent.INSTRUCTION.encode('utf-8')).hexdigest(), tool_sha256=digest(intent.TOOL.to_dict()),
        context_preflight=cases.preflight(rows),
        tool_arguments_policy=dict(strict_first=True, candidate_only=True, repair_enabled=True,
            allowed_repairs=['control_char', 'code_fence', 'trailing_text', 'unquoted_scalar'], retries=1,
            retry_only='tool_arguments_json_invalid', retry_same_messages=True, retry_repairs=['unquoted_scalar'],
            retry_transient_errors=False, failed_body_saved=True, failed_arguments_max_characters=10000,
            global_budget_includes_retries=True, approval_messages=[APPROVAL_MESSAGE]),
        live_enabled=False, paid_calls=0, eligible_for_switch=False,
        thresholds=dict(dev=dict(n=64, minimum_correct=61), regression=dict(n=412, minimum_correct=392),
            reference_regression_n=414, common_fields_no_regression=True, redline_violations=0),
        isolation=dict(implementation_unseen=True, automatic_status_notice=436, status_notice_accepted_by=439,
            note='仅自动投递封存完成汇总，无逐题内容；未访问封存材料或作者日志。'),
        note='只运行公开开发/已暴露回归；失败和V2002缺口留分母，未接正式路由。')


def assert_current(plan):
    if (_git('rev-parse', 'HEAD') != plan['runtime_revision'] or _git('status', '--porcelain')
            or fingerprints() != plan['source_sha256_lf']):
        raise RunInvalidated('Reviewed revision or source drift; run invalidated, no retry')


class TargetAttempt(previous.TargetAttempt):
    """Reuse the reviewed format-only retry and per-attempt diagnostic accounting."""

    def record(self, phase, **values):
        try:
            super().record(phase, **values)
        except Exception as error:
            raise RunInvalidated('Attempt journal could not be persisted; stop the run') from error


class AttemptBudget(previous.AttemptBudget):
    def __init__(self, maximum, event_sink=None, *, split='dev'):
        if split not in CALL_CAPS or type(maximum) is not int or not 1 <= maximum <= CALL_CAPS[split]:
            raise ValueError('Attempts must remain within the approved split budget')
        self.maximum, self.attempts, self.lock = maximum, 0, Lock()
        self.event_sink, self.identifiers = event_sink, set()

    def target(self, model, identifier=None):
        with self.lock:
            if identifier in self.identifiers:
                raise ValueError('No second target wrapper for a target')
            self.identifiers.add(identifier)
        return TargetAttempt(self, model, identifier)


_live_model = previous._live_model
_empty = previous._empty


def checked_result(question, response, context):
    result = previous.checked_result(question, response, context)
    if result['status'] == 'ok':
        checked = intent.check_response(question, response, context=context)
        result['metadata']['diagnostics'] = deepcopy(checked.diagnostics)
    return result


def predict(row, *, prefixes, setups, model, budget, timeout_seconds=45):
    if row['id'] in NOT_APPLICABLE:
        return _empty('not_applicable', '事前批准的架构不适用，未缩其他分母')
    try:
        context, calls = build_context(row, prefixes, setups)
    except (ValueError, IndexError, KeyError):
        return _empty('not_measured', '已批准前置无法构建；V2002仍计入适用分母')
    local = intent.local_preflight(row['question'], context)
    if local is not None:
        return dict(_empty('ok', None, calls), fields=local.fields(), path='local_preflight',
                    metadata=dict(provenance=list(local.provenance), diagnostics=deepcopy(local.diagnostics)))
    if budget.attempts >= budget.maximum:
        return _empty('not_measured', '全局调用额度耗尽（含格式重问）；保留分母', calls)
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
        argument_diagnostics=target.argument_diagnostics, attempt_history=target.attempt_history,
        format_retry=dict(attempted=retried, successful=retried and result['status'] == 'ok'),
        response_origin='format_retry' if retried else 'repaired_tool_arguments' if repairs else 'provider',
        path='semantic', context_sha256=digest(context.model_context()), setup=core.setup_metadata(context))


def _groups(rows):
    return dict(all=rows, applicable=[r for r in rows if r['id'] not in NOT_APPLICABLE],
        semantic=[r for r in rows if not r['gold']['zero_intent_calls']],
        local=[r for r in rows if r['gold']['zero_intent_calls']],
        **{split: [r for r in rows if r['dataset'] == split] for split in LIMITS},
        **{'family:' + family: [r for r in rows if r['family_id'] == family]
           for family in sorted({r['family_id'] for r in rows})})


def redlines(rows):
    result = previous.redlines(rows)
    for name in ('reference_scope', 'plan_authorization'):
        result['checks'][name] = dict(measured=0, violations=0, unmeasured=0)
    for row in rows:
        if row['id'] in NOT_APPLICABLE:
            continue
        observation, gold = row['V2'], row['gold']
        diagnostics = observation.get('metadata', {}).get('diagnostics', {})
        for name, relevant, proof in (
            ('reference_scope', gold['subject_source'] == 'previous', diagnostics.get('binding')),
            ('plan_authorization', gold['wants_plan'] or observation['fields'].get('wants_plan'), diagnostics.get('authorization'))):
            if not relevant:
                continue
            check = result['checks'][name]
            if observation['status'] != 'ok' or not proof:
                check['unmeasured'] += 1
                continue
            check['measured'] += 1
            if name == 'reference_scope':
                bound = observation['fields'].get('bound_subjects', [])
                check['violations'] += bool(bound and (proof.get('issue') or
                    set(bound) - set(proof.get('scope_subjects', []))))
            else:
                check['violations'] += bool(observation['fields'].get('wants_plan') and
                    (proof.get('plan_source') not in ('question', 'program_pending') or proof.get('plan_veto')
                     or proof.get('plan_source') == 'program_pending' and not proof.get('same_task_pending')))
    for name in ('reference_scope', 'plan_authorization'):
        check = result['checks'][name]
        check['status'] = ('failed' if check['violations'] else 'unmeasured' if check['unmeasured'] else
                           'passed' if check['measured'] else 'not_applicable')
    result['passed'] = (not any(check['violations'] for check in result['checks'].values())
                        and all(result['checks'][name]['status'] != 'unmeasured'
                                for name in ('reference_scope', 'plan_authorization')))
    return result


def report_results(rows, effective, plan, *, live=False):
    views, approved = {}, []
    for view in ('raw_original', 'normalized_original', 'normalized_approved'):
        selected = deepcopy(rows)
        for row in selected:
            if view == 'raw_original':
                row['V2'] = deepcopy(row['V2'].get('raw', row['V2']))
            elif view == 'normalized_approved':
                row['gold'] = deepcopy(effective[row['id']])
        views[view] = {group: field_scores(items, 'V2') for group, items in _groups(selected).items()}
        if view == 'normalized_approved':
            approved = selected
    thresholds = {}
    for split in LIMITS:
        selected = [r for r in approved if r['dataset'] == split and r['id'] not in NOT_APPLICABLE]
        if not selected:
            continue
        correct = field_scores(selected, 'V2')['joint_all_fields']['numerator']
        thresholds[split] = dict(n=len(selected), correct=correct, minimum_correct=MINIMUM_CORRECT[split],
            joint_passed=len(selected) == (64 if split == 'dev' else 412) and correct >= MINIMUM_CORRECT[split],
            common=previous._common(selected), redlines=redlines(selected))
    metered = []
    for row in rows:
        observation = row['V2']
        history = observation.get('attempt_history', [])
        for index, attempt in enumerate(history):
            metered.append(dict(row, V2=dict(observation, attempts=1, response=attempt['response'],
                call_ms=attempt['call_ms'], setup_calls=observation['setup_calls'] if index == 0 else 0)))
        if not history:
            metered.append(row)
    telemetry = core.telemetry(metered)
    attempts = [r['V2'] for r in metered if r['V2']['attempts']]
    valid_provider = any(a['response'] is not None for a in attempts) and all(a['response'] is None or
        (a['response']['provider'] == 'deepseek' and a['response']['requested_model'] == 'deepseek-flash'
         and a['response']['is_mock'] is False and a['response']['model']) for a in attempts)
    authenticity = ('mock_experiment' if telemetry['mock_responses'] else
                    'live_provider' if live and valid_provider else 'no_provider_observation')
    repairs = {}
    for row in rows:
        for event in row['V2'].get('argument_diagnostics', []):
            if event['correction'] == 'tool_arguments_repaired':
                group = repairs.setdefault(event['repair'], dict(count=0, ids=[]))
                group['count'] += 1
                if row['id'] not in group['ids']:
                    group['ids'].append(row['id'])
    retried = [r for r in rows if r['V2'].get('format_retry', {}).get('attempted')]
    recovered = [r for r in retried if r['V2']['format_retry']['successful']]
    applicable = [r for r in approved if r['id'] not in NOT_APPLICABLE]
    return dict(kind='research-intent-v2.6-public-report', title='v2.6 公开开发与已暴露回归', notice=None,
        plan=plan, rows=rows, score_views=views, common_comparison=previous._common(applicable), zero_tolerance=redlines(approved),
        thresholds=thresholds, paid_calls=telemetry['attempts'] if live else 0, telemetry=telemetry,
        reference_comparison=[dict(id=r['id'], expected=r['reference_expectation']['reference'],
            actual=r['V2'].get('metadata', {}).get('diagnostics', {}).get('reference'),
            matches=r['V2'].get('metadata', {}).get('diagnostics', {}).get('reference') == r['reference_expectation']['reference'])
            for r in rows if 'reference_expectation' in r and r['V2'].get('path') != 'local_preflight'],
        input_usage='unknown', cost='unknown', authenticity=authenticity, corrections=core.correction_summary(rows),
        eligible_for_switch=False, tool_arguments_repairs=dict(total=sum(g['count'] for g in repairs.values()), by_category=repairs),
        format_retries=dict(total=len(retried), successful=len(recovered), ids=[r['id'] for r in retried],
                            successful_ids=[r['id'] for r in recovered]),
        failed_arguments=dict(notice=FAILED_ARGUMENT_NOTICE, max_characters=10000,
            saved_events=sum('arguments_raw' in e for r in rows for e in r['V2'].get('argument_diagnostics', [])),
            enabled=True),
        passed=authenticity == 'live_provider' and bool(thresholds) and all(
            t['joint_passed'] and t['common']['passed'] and t['redlines']['passed'] for t in thresholds.values()),
        unmeasured=['正式会话端到端', '真实研究执行和价位', '跨对象证据', '完整严格范围执行', 'G正式回退', '封存检验'],
        scoring_policy='固定64/412主分母及414参考；三列保留无效、失败及未测；reference仅诊断，不进入14字段。')


def markdown(report):
    text = previous.markdown(report)
    return text + '\nreference、收窄接受和扩权阻断诊断见 report.json 的 metadata.diagnostics；输入用量和费用 unknown。\n'


def _write_json(path, value):
    previous._write_json(path, value)


def _invalidate(folder):
    # A late failure must never leave a previously passing report visible.
    for name in ('report.json', 'report.md'):
        path = folder/name
        if path.exists():
            path.unlink()
    _write_json(folder/'invalidated.json', dict(status='invalidated', passed=False,
        reason='运行中断、持久化失败或版本漂移；该运行占用不重开。'))


def evaluate(*, split='dev', live=False, maximum=None, timeout_seconds=45):
    if type(live) is not bool:
        raise ValueError('live must be an explicit boolean')
    if type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds) or not 0 < timeout_seconds <= 45:
        raise ValueError('Timeout may only tighten the approved shared 45 seconds')
    plan = build_plan(split, maximum)
    plan['timeout_seconds'] = timeout_seconds
    if not live:
        return plan
    if plan['workspace_dirty'] or not plan['context_preflight']['passed']:
        raise ValueError('Live evaluation requires a clean reviewed commit and complete public preflight')
    assert_current(plan)
    # Approval is per version/split, never renewed by making another commit.
    folder = ROOT/'.agentbus/evaluation-runs/research-intent-v2.6/public'/split
    folder.mkdir(parents=True, exist_ok=False)
    try:
        plan['live_enabled'] = True
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
            budget, output = AttemptBudget(plan['max_calls'], event_sink, split=split), []
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
        assert_current(plan)
        _write_json(folder/'report.json', report)
        (folder/'report.md').write_text(markdown(report), encoding='utf-8')
        assert_current(plan)
        return report
    except BaseException:
        _invalidate(folder)
        raise
