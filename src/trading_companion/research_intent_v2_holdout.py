"""Locked, one-use holdout admission; imports never run or inspect predictions."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation import same
from trading_companion.research_intent_evaluation_baseline import UNKNOWN, rule_observation
from trading_companion.research_intent_evaluation_cases import ROOT, GOLD_FIELDS, input_state, load_corpus
from trading_companion import research_intent_v2_evaluation as core


LOCK_PATH = ROOT / 'data/evaluation/research-intent-v2.4-lock.json'
CANDIDATE_SHA256 = '2ecaddd5fad72faa51b2b97a0629cab01642278b9e59019c4f50f0dc11e58dc9'
VERSION = 'research-intent-v2.4'
RUNNER_FILES = (
    'src/trading_companion/research_intent_v2_holdout.py',
    'scripts/evaluate_research_intents_holdout.py',
    'tests/test_research_intent_v2_holdout.py',
)
THRESHOLDS = dict(all=dict(n=83, minimum_correct=79), unseen_holdout=dict(n=82, minimum_correct=78))
REFERENCE_THRESHOLDS = dict(all=dict(n=85, minimum_correct=81), unseen_holdout=dict(n=84, minimum_correct=80))
POPULATION_PATH = 'data/evaluation/research-intent-v2.4-holdout-population.json'
RECORD_NOTE = '被Git忽略的本地防误用记录，不是防篡改证明；请结合运行提交、锁指纹及起止时间核对。'


class RunInvalidated(RuntimeError):
    """A sticky lock/revision drift; never a retryable model failure."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _git(*arguments):
    result = subprocess.run(['git', *arguments], cwd=ROOT, capture_output=True, timeout=10, check=False)
    if result.returncode:
        raise ValueError('Repository identity check failed')
    return result.stdout.decode('utf-8').strip()


def _file_hash(path):
    return sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def validate_lock(*, require_clean=False):
    """Read-only: no corpus decoding, prediction loading, credentials or run claims."""
    lock = read_json(LOCK_PATH, maximum=100_000)
    if (type(lock) is not dict or lock.keys() != {
            'version', 'status', 'candidate', 'candidate_sha256', 'development_evidence',
            'holdout_runner', 'holdout_live_admitted', 'admission_note', 'superseded_candidates'}
            or lock['version'] != 'research-intent-v2-lock-v1' or lock['status'] != 'locked'
            or lock['holdout_live_admitted'] is not True
            or lock['candidate_sha256'] != CANDIDATE_SHA256
            or digest(lock['candidate']) != CANDIDATE_SHA256):
        raise ValueError('Missing, incomplete or changed candidate lock')
    candidate, runner = lock['candidate'], lock['holdout_runner']
    if (type(runner) is not dict or runner.keys() != {
            'version', 'plan_approval_message', 'population_approval_message', 'source_sha256_lf', 'thresholds'}
            or runner['version'] != 'intent-v2-holdout-runner-v1' or runner['plan_approval_message'] != 379
            or runner['population_approval_message'] != 383
            or runner['thresholds'] != THRESHOLDS or type(runner['source_sha256_lf']) is not dict
            or set(runner['source_sha256_lf']) != set(RUNNER_FILES)):
        raise ValueError('Missing or changed holdout runner binding')
    superseded = candidate['superseded_candidate']
    if (lock['superseded_candidates'] != [superseded] or superseded['status'] != 'superseded'
            or superseded['holdout_started'] is not False or _file_hash(ROOT/superseded['archive']) != superseded['sha256_lf']):
        raise ValueError('Missing or changed superseded candidate archive')
    for mapping in (candidate['source_sha256_lf'], candidate['evaluation_input_sha256_lf'], runner['source_sha256_lf']):
        for name, expected in mapping.items():
            if _file_hash(ROOT / name) != expected:
                raise ValueError('Locked file mismatch')
    expected_sources = set(candidate['source_sha256_lf']) | {p for p in RUNNER_FILES if not p.startswith('tests/')}
    actual_sources = {p.relative_to(ROOT).as_posix() for folder in ('src', 'scripts')
                      for p in (ROOT / folder).rglob('*.py')}
    if actual_sources != expected_sources:
        raise ValueError('Locked source inventory mismatch')
    revision = _git('rev-parse', 'HEAD')
    _git('merge-base', '--is-ancestor', candidate['revision'], revision)
    dirty = bool(_git('status', '--porcelain', '--untracked-files=normal'))
    if require_clean and dirty:
        raise ValueError('Holdout requires a clean reviewed commit')
    return dict(lock=lock, lock_sha256=digest(lock), runtime_revision=revision, workspace_dirty=dirty)


def _population(lock):
    registry = read_json(ROOT/POPULATION_PATH, maximum=10_000)
    if (registry != lock['candidate']['holdout_population_revision']
            or registry.keys() != {'version', 'reviewer', 'approval_message', 'decided_at',
                                   'decided_before_holdout', 'not_applicable'}
            or registry['version'] != 'research-intent-v2-holdout-population-v1'
            or registry['reviewer'] != 'claude' or registry['approval_message'] != 383
            or registry['decided_before_holdout'] is not True
            or set(registry['not_applicable']) != {'V2222', 'V2223'}):
        raise ValueError('Unreviewed holdout population revision')
    return registry


def _holdout_inputs(plan):
    rows, manifest = load_corpus()
    selected = [r for r in rows if r['split'] == 'holdout']
    known = plan['exposure']['known_during_implementation']
    excluded = plan['population_revision']['not_applicable']
    if (manifest['annotation_status'] != 'reviewed' or manifest['corpus_sha256'] != plan['corpus_sha256']
            or manifest['split_sha256'] != plan['split_sha256'] or len(selected) != 85
            or len({r['id'] for r in selected}) != 85
            or {r['id'] for r in selected if r['id'] in known} != {'V2203'}
            or {r['id'] for r in selected if r['id'] in excluded} != set(excluded)):
        raise ValueError('Locked holdout population mismatch')
    return selected, core.prefix_library([r for r in rows if r['split'] == 'dev'])


def _context_preflight(rows, prefixes, registry):
    """Only construct contexts: no target understanding, provider or scoring."""
    items, failures = [], []
    excluded = registry['not_applicable']
    for row in rows:
        state = dict(question='仅检查程序上下文', context=deepcopy(row['context']))
        if row['id'] in excluded:
            # An exclusion never licenses a different, buildable descriptor.
            if state['context']['pending'] == 'none' or all(state['context']['pending_binding'].values()):
                raise ValueError('Not-applicable descriptor mismatch')
            items.append(dict(id=row['id'], status='not_applicable', **excluded[row['id']]))
            continue
        try:
            context, calls = core.program_context(state, prefixes)
            items.append(dict(id=row['id'], status='buildable', context_sha256=digest(context.model_context()),
                              setup_calls=calls, setup=core.setup_metadata(context)))
        except (ValueError, IndexError, KeyError):
            failures.append(row['id'])
            items.append(dict(id=row['id'], status='unbuildable', reason='无法从已审核前置真实构建声明状态'))
    applicable = len(rows)-len(excluded)
    return dict(total=len(rows), applicable=applicable, buildable=sum(i['status']=='buildable' for i in items),
        not_applicable=list(excluded), unbuildable=failures, rows=items,
        passed=len(rows)==85 and applicable==83 and not failures)


def _paths(snapshot):
    candidate = snapshot['lock']['candidate']
    base = ROOT / '.agentbus/evaluation-runs'
    dataset_key = digest(dict(corpus=candidate['corpus_sha256'], split=candidate['split_sha256']))
    return base / VERSION / 'holdout', base / 'datasets' / (dataset_key + '.jsonl')


def build_plan(*, require_clean=False):
    snapshot = validate_lock(require_clean=require_clean)
    folder, usage = _paths(snapshot)
    available = not folder.exists() and not usage.exists()
    plan = dict(status='planned', kind='v2_holdout_plan', contract_version=VERSION,
        candidate_revision=snapshot['lock']['candidate']['revision'], runtime_revision=snapshot['runtime_revision'],
        lock_sha256=snapshot['lock_sha256'], workspace_dirty=snapshot['workspace_dirty'],
        thresholds=deepcopy(THRESHOLDS), model=deepcopy(snapshot['lock']['candidate']['model']),
        corpus_sha256=snapshot['lock']['candidate']['corpus_sha256'],
        split_sha256=snapshot['lock']['candidate']['split_sha256'], exposure=snapshot['lock']['candidate']['exposure'],
        population_revision=_population(snapshot['lock']),
        run_directory=str(folder), dataset_usage_record=str(usage), record_note=RECORD_NOTE,
        run_available=available, ready_for_live=available and not snapshot['workspace_dirty'],
        live_enabled=False, paid_calls=0, prediction_rows=0, eligible_for_switch=False)
    rows, prefixes = _holdout_inputs(plan)
    plan['context_preflight'] = _context_preflight(rows, prefixes, plan['population_revision'])
    plan['ready_for_live'] = plan['ready_for_live'] and plan['context_preflight']['passed']
    return plan


def _append(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def _write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        _append(stream, value)


def _category(row):
    result = row['V2']
    if result['status'] == 'not_measured':
        return 'not_measured'
    if result['status'] != 'ok':
        return 'failed'
    return ('correct' if all(same(result['fields'].get(field, UNKNOWN), row['gold'][field])
                             for field in GOLD_FIELDS) else 'understanding_error')


def _common_fields(rows):
    fields = {}
    for field in GOLD_FIELDS:
        observed = [r for r in rows if r['A']['status'] == 'ok' and r['A']['fields'].get(field, UNKNOWN) != UNKNOWN]
        correct = lambda r: r['V2']['status'] == 'ok' and same(r['V2']['fields'].get(field, UNKNOWN), r['gold'][field])
        baseline_correct = sum(same(r['A']['fields'][field], r['gold'][field]) for r in observed)
        candidate_correct = sum(correct(r) for r in observed)
        fields[field] = dict(n=len(observed), baseline_correct=baseline_correct, candidate_correct=candidate_correct,
            failure_missing=sum(r['V2']['status'] not in ('ok', 'not_measured') for r in observed),
            unmeasured_missing=sum(r['V2']['status'] == 'not_measured' for r in observed),
            understanding_errors=sum(r['V2']['status'] == 'ok' and not correct(r) for r in observed),
            rule_unobserved=len(rows)-len(observed),
            not_worse=candidate_correct >= baseline_correct if observed else None)
    measured = [v for v in fields.values() if v['n']]
    return dict(fields=fields, passed=bool(measured) and all(v['not_worse'] for v in measured))


def make_report(rows, *, plan, started_at, ended_at):
    registry = plan['exposure']
    known = registry['known_during_implementation']
    excluded = plan['population_revision']['not_applicable']
    applicable = [r for r in rows if r['id'] not in excluded]
    groups = dict(all=applicable, unseen_holdout=[r for r in applicable if r['id'] not in known],
                  implementation_known=[r for r in applicable if r['id'] in known])
    raw = deepcopy(applicable)
    for row in raw:
        row['V2'] = deepcopy(row['V2'].get('raw', row['V2']))
    categories, common, gates = {}, {}, {}
    for name, group in groups.items():
        categories[name] = {kind: [r['id'] for r in group if _category(r) == kind]
                            for kind in ('correct', 'understanding_error', 'failed', 'not_measured')}
        common[name] = _common_fields(group)
        if name in THRESHOLDS:
            target = THRESHOLDS[name]
            covered = len(group) == target['n']
            gates[name] = dict(n=len(group), minimum_correct=target['minimum_correct'], coverage_valid=covered,
                correct=len(categories[name]['correct']),
                joint_passed=covered and len(categories[name]['correct']) >= target['minimum_correct'],
                common_fields_passed=common[name]['passed'])
    reference_rows = deepcopy(rows)
    for row in reference_rows:
        if row['id'] in excluded:
            row['V2'] = dict(status='not_measured', fields={})
    reference = {}
    for name, group in dict(all=reference_rows, unseen_holdout=[r for r in reference_rows if r['id'] not in known]).items():
        correct = sum(_category(row) == 'correct' for row in group)
        reference[name] = dict(n=len(group), correct=correct, minimum_correct=REFERENCE_THRESHOLDS[name]['minimum_correct'],
            joint_passed=correct >= REFERENCE_THRESHOLDS[name]['minimum_correct'], participates_in_gate=False)
    stats = core.telemetry(applicable)
    authenticity = ('no_provider_observation' if not stats['attempts'] else
                    'mock_experiment' if stats['mock_responses'] else 'live_provider')
    zero = core.zero_tolerance(applicable)
    passed = (authenticity == 'live_provider' and all(v['joint_passed'] and v['common_fields_passed'] for v in gates.values())
              and zero['local_intent_call']['violations'] == 0 and zero['local_intent_call']['unmeasured'] == 0)
    return dict(status='completed', kind='v2_holdout', plan=plan, started_at=started_at, ended_at=ended_at,
        candidate_revision=plan['candidate_revision'], runtime_revision=plan['runtime_revision'],
        lock_sha256=plan['lock_sha256'], record_note=RECORD_NOTE,
        completion_marker_required='completed.json',
        authenticity=authenticity, rows=rows, telemetry=stats, paid_calls=stats['attempts'],
        temperature=0, max_output_tokens=1500, exposure=registry,
        score_views=dict(model_raw_original_gold=core.metrics(raw, registry), normalized_original_gold=core.metrics(applicable, registry)),
        population_revision=plan['population_revision'], not_applicable=deepcopy(excluded),
        reference_including_not_applicable=reference,
        setup_fault_injections={r['id']: r['V2']['setup'] for r in applicable
                                if r['V2'].get('setup', {}).get('fault_injection')},
        threshold_basis='normalized_original_gold', gold_amendments=[], failure_categories=categories,
        common_comparison_A=common, gates=gates, thresholds_passed=passed,
        corrections=core.correction_summary(applicable), zero_tolerance=zero, eligible_for_switch=False,
        scope='仅候选意图字段和预计动作；规则A为相同留出输入的冻结规则观测，未运行新版研究执行器。',
        billing_note='attempts为传输前预留的尝试上界；未知token不按0计，固定前置模拟调用另记。',
        unmeasured=['真实价位执行', '跨对象证据', '完整严格范围执行', '正式入口端到端'])


def format_report(report):
    lines = ['# v2.4 留出运行', '', RECORD_NOTE, '',
        f"状态：{report['status']}；开始：{report['started_at']}；结束：{report['ended_at']}",
        f"运行提交：{report['runtime_revision']}；锁指纹：{report['lock_sha256']}", '',
        '按运行前决定#383：完整83题、未见82题；不适用V2222/V2223单列原因。V2203仅从未见口径排除。',
        '另附不适用题算未测的85/84题参考成绩，不参与判定。适用题的失败/未测仍保留分母。',
        '规范化按原留出标注计分，没有开发标注修订；正式产品切换资格仍为false。', '']
    if report['status'] == 'completed':
        lines += ['| 口径 | 全对 | 至少 | 联合通过 | 共同字段通过 |', '| --- | --- | --- | --- | --- |']
        for name, gate in report['gates'].items():
            lines.append(f"| {name} | {gate['correct']}/{gate['n']} | {gate['minimum_correct']} | {gate['joint_passed']} | {gate['common_fields_passed']} |")
        lines += ['', '不适用题算作未测的参考成绩（不参与判定）：']
        for name, view in report['reference_including_not_applicable'].items():
            lines.append(f"- {name}：{view['correct']}/{view['n']}，原门槛{view['minimum_correct']}，联合通过{view['joint_passed']}")
        lines += ['', f"真实性：{report['authenticity']}；全部门槛通过：{report['thresholds_passed']}", '']
    lines += ['完整分组、字段缺输出/理解错误、实际模型、用量、耗时和逐题数据见下方JSON。', '',
              '```json', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), '```', '']
    return '\n'.join(lines)


class _LockedModel:
    def __init__(self, model, recheck):
        self.model, self.recheck, self.invalidated, self.forwarded = model, recheck, False, 0

    def complete(self, messages, *, tools):
        try:
            self.recheck()
        except Exception:
            self.invalidated = True
            raise RunInvalidated('Locked run changed') from None
        self.forwarded += 1
        return self.model.complete(messages, tools=tools)


def _execute(rows, *, plan, prefixes, recheck):
    """Only called after live admission; tests supply independent synthetic inputs."""
    folder, usage = Path(plan['run_directory']), Path(plan['dataset_usage_record'])
    if folder.exists() or usage.exists():
        raise ValueError('Version or dataset already used; no retry')
    folder.mkdir(parents=True, exist_ok=False)
    started_at, results, budget, model = _now(), [], None, None
    identity = dict(contract_version=VERSION, candidate_revision=plan['candidate_revision'],
        runtime_revision=plan['runtime_revision'], lock_sha256=plan['lock_sha256'],
        started_at=started_at, record_note=RECORD_NOTE)
    usage_owned = False
    try:
        _write_json(folder/'reserved.json', dict(identity, status='reserved'))
        usage.parent.mkdir(parents=True, exist_ok=True)
        _write_json(usage, dict(identity, status='reserved', corpus_sha256=plan['corpus_sha256'], split_sha256=plan['split_sha256']))
        usage_owned = True
        _write_json(folder/'plan.json', plan)
        model = _LockedModel(core._live_model(45), recheck)
        with (folder/'rows.jsonl').open('x', encoding='utf-8', newline='\n') as journal, \
                (folder/'attempts.jsonl').open('x', encoding='utf-8', newline='\n') as attempts:
            budget = core.AttemptBudget(85, event_sink=lambda event: _append(attempts, event))
            for row in rows:
                recheck()
                result = {key: deepcopy(row[key]) for key in ('id', 'split', 'question', 'context', 'gold', 'tags', 'source', 'fault')}
                result['input_sha256'] = digest(input_state(row))
                excluded = plan['population_revision']['not_applicable']
                if row['id'] in excluded:
                    reason = excluded[row['id']]['reason']
                    result['A'] = dict(status='not_measured', fields={}, reason=reason)
                    result['V2'] = dict(status='not_applicable', fields={}, reason=reason,
                        attempts=0, setup_calls=0, response=None, call_ms=None)
                else:
                    result['A'] = rule_observation(input_state(row))
                    result['V2'] = core.predict(input_state(row), prefixes=prefixes, model=model, budget=budget,
                                                 timeout_seconds=45, identifier=row['id'])
                results.append(result)
                _append(journal, result)
                if model.invalidated:
                    raise RunInvalidated('Locked run changed')
        recheck()
        report = make_report(results, plan=plan, started_at=started_at, ended_at=_now())
        report['artifacts'] = str(folder)
        report['provider_calls_forwarded'] = model.forwarded
        _write_json(folder/'report.json', report)
        with (folder/'report.md').open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(format_report(report))
            stream.flush()
            os.fsync(stream.fileno())
        recheck()
        receipt = dict(identity, status='completed', ended_at=report['ended_at'],
                       thresholds_passed=report['thresholds_passed'])
        with usage.open('a', encoding='utf-8', newline='\n') as stream:
            _append(stream, receipt)
        _write_json(folder/'completed.json', receipt)
        return report
    except (Exception, KeyboardInterrupt) as error:
        abort_reason = ('lock_changed' if isinstance(error, RunInvalidated) else
                        'interrupted' if isinstance(error, KeyboardInterrupt) else 'run_failed')
        report = dict(identity, status='aborted', reason=abort_reason, ended_at=_now(), rows=results,
            artifacts=str(folder), thresholds_passed=False, eligible_for_switch=False,
            attempts_reserved=budget.attempts if budget else 0,
            provider_calls_forwarded=model.forwarded if model else 0,
            partial_telemetry=core.telemetry(results),
            billing_note='最后未完成尝试的响应/用量可能未知；详见attempts.jsonl，不能按0计。')
        _write_json(folder/'aborted.json', report)
        # A late drift or artifact failure must not leave a usable passing
        # report next to an aborted receipt. These are this run's own files.
        if (folder/'report.json').exists():
            temporary = folder/'report.abort.tmp'
            _write_json(temporary, report)
            os.replace(temporary, folder/'report.json')
        if (folder/'report.md').exists():
            temporary = folder/'report-md.abort.tmp'
            with temporary.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(format_report(report))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder/'report.md')
        if usage_owned:
            with usage.open('a', encoding='utf-8', newline='\n') as stream:
                _append(stream, {k: v for k, v in report.items() if k not in ('rows', 'partial_telemetry')})
        return report


def evaluate_holdout(*, live=False):
    """No model/config override, arbitrary directory, resume or split parameter."""
    if type(live) is not bool:
        raise ValueError('Live must be an explicit boolean')
    plan = build_plan(require_clean=live)
    if not live:
        return plan
    if not plan['run_available']:
        raise ValueError('Version or dataset already used; no retry')
    if not plan['ready_for_live']:
        raise ValueError('Holdout contexts are not all buildable')
    selected, prefixes = _holdout_inputs(plan)

    def recheck():
        try:
            current = validate_lock(require_clean=True)
            if current['lock_sha256'] != plan['lock_sha256'] or current['runtime_revision'] != plan['runtime_revision']:
                raise ValueError('Run identity changed')
        except Exception:
            raise RunInvalidated('Locked run changed') from None

    recheck()
    plan.update(live_enabled=True)
    return _execute(selected, plan=plan, prefixes=prefixes, recheck=recheck)
