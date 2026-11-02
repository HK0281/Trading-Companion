"""Locked v2.5 holdout admission. Imports and default plans never decode sealed cases."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
import os
from pathlib import Path
import re
import subprocess

from evaluation_tools import research_intent_v25_cases as cases
from evaluation_tools import research_intent_v25_evaluation as evaluation
from scripts.verify import clean_environment, offline_network
from trading_companion import research_intent_v2 as intent
from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation import field_scores
from trading_companion.research_intent_evaluation_baseline import rule_observation
from trading_companion.research_intent_evaluation_cases import ROOT, input_state, load_corpus


LOCK_NAME = 'data/evaluation/research-intent-v2.5-lock.json'
FREEZE_NAME = 'data/evaluation/research-intent-v2.5-freeze.json'
LOCK_VERSION = 'research-intent-v2.5-lock-v1'
RUNNER_VERSION = 'research-intent-v2.5-holdout-runner-v1'
CANDIDATE_REVISION = 'd67c0c7924ea235ca5b1fa8908e6ac5a1df77292'
CANDIDATE_SHA256 = 'c95c24c940e65232419c200842a3b8f93314634b10c425c519bbe250a69831a3'
SEALED_SHA256 = '99a5e163cb7840a1f9e135a68578da8ff096185c3efc3fae33262944debc5a6e'
COUNT, SEMANTIC_COUNT = 96, 79
MAX_CORPUS_BYTES = 2_000_000
REFERENCE_EXCLUDED = ('V25H001', 'V25H009', 'V25H010', 'V25H012', 'V25H015',
                      'V25H036', 'V25H049', 'V25H057', 'V25H069')
RUNNER_FILES = (
    'evaluation_tools/research_intent_v25_holdout.py',
    'evaluation_tools/evaluate_research_intents_v25_holdout.py',
    'tests/test_research_intent_v25_holdout.py',
    'docs/research-intent-v2.5-holdout.md',
    'docs/research-intent-v2.5-implementation.md',
    'README.md',
)
MODEL_POLICY = dict(requested_model='deepseek-flash', temperature=0, thinking='disabled',
    max_output_tokens=1500, timeout_seconds=45, max_attempts_per_target=2, maximum_target_attempts=120,
    response_diagnostics=True, save_failed_arguments=True, failed_arguments_max_characters=10000,
    allowed_repairs=['control_char', 'code_fence', 'trailing_text', 'unquoted_scalar'],
    retry_repairs=['unquoted_scalar'], retry_only='tool_arguments_json_invalid', retry_same_messages=True,
    retry_transient_errors=False, global_budget_includes_retries=True, evaluation_scope='sealed',
    exception_approval_message=420)
ISOLATION = '检验题由独立 Codex 会话起草、Claude 审核；实现会话在调参期间不可见'
STATUS_EXCEPTION = '#396 自动投递封存状态通知，无题干或答案；已于 #398 披露并由 #399 接受。'
RECORD_NOTE = '被 Git 忽略的本地防误用记录，不是防篡改证明；结合运行提交、锁指纹及起止时间核对。'
RunInvalidated = evaluation.RunInvalidated


def _now():
    return datetime.now(timezone.utc).isoformat()


def _git(*args):
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, timeout=15, check=True)
    return result.stdout.decode('utf-8').strip()


def _path(name):
    if (type(name) is not str or '\\' in name or Path(name).is_absolute()
            or '..' in Path(name).parts or not (ROOT/name).resolve().is_relative_to(ROOT.resolve())):
        raise ValueError('Invalid locked relative path')
    return ROOT/name


def _hash(path):
    return sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def validate_lock(*, require_clean=False):
    """Only public metadata and repository files; no sealed path or credential access."""
    lock = read_json(ROOT/LOCK_NAME, maximum=500_000)
    if (lock.keys() != {'version', 'status', 'candidate', 'candidate_sha256', 'runner', 'live_admitted'}
            or lock['version'] != LOCK_VERSION or lock['status'] != 'locked' or lock['live_admitted'] is not True
            or lock['candidate_sha256'] != CANDIDATE_SHA256 or digest(lock['candidate']) != CANDIDATE_SHA256):
        raise ValueError('Missing or changed candidate lock')
    candidate, runner = lock['candidate'], lock['runner']
    if (candidate['revision'] != CANDIDATE_REVISION or candidate['contract_version'] != intent.VERSION
            or candidate['guard_version'] != intent.GUARD_VERSION or candidate['model'] != MODEL_POLICY
            or candidate['prompt'] != dict(text=intent.INSTRUCTION, sha256_utf8=sha256(intent.INSTRUCTION.encode()).hexdigest())
            or candidate['tool'] != dict(definition=intent.TOOL.to_dict(), sha256=digest(intent.TOOL.to_dict()))
            or runner.keys() != {'version', 'plan_approval_message', 'source_sha256_lf'}
            or runner['version'] != RUNNER_VERSION or runner['plan_approval_message'] != 422
            or set(runner['source_sha256_lf']) != set(RUNNER_FILES)
            or set(candidate['source_sha256_lf']) & set(RUNNER_FILES)):
        raise ValueError('Locked file mismatch: candidate or runner binding changed')
    freeze = cases.validate_freeze(require_review=True)
    frozen = candidate['freeze']
    if (frozen['path'] != FREEZE_NAME or frozen['sha256_lf'] != _hash(ROOT/FREEZE_NAME)
            or frozen['public_files_sha256'] != freeze['public_files_sha256']
            or frozen['holdout_sha256'] != SEALED_SHA256 or frozen['holdout_sha256'] != freeze['holdout']['sha256']
            or frozen['summary'] != freeze['holdout']['summary'] or frozen['thresholds'] != freeze['thresholds']
            or frozen['summary']['count'] != COUNT or frozen['summary']['semantic_calls'] != SEMANTIC_COUNT
            or candidate['exposure'] != dict(implementation_target_ids=[], reference_excluded_ids=list(REFERENCE_EXCLUDED),
                approval_message=420, isolation_statement=ISOLATION, automatic_status_exception=STATUS_EXCEPTION)):
        raise ValueError('Freeze or exposure registry changed')
    for mapping in (candidate['source_sha256_lf'], frozen['public_files_sha256'], runner['source_sha256_lf']):
        for name, fingerprint in mapping.items():
            if _hash(_path(name)) != fingerprint:
                raise ValueError('Locked file mismatch')
    # The reviewed evidence snapshot is pinned by CANDIDATE_SHA256. Ignored
    # public run artifacts need not be present on a fresh checkout or in CI.
    inventory = {p.relative_to(ROOT).as_posix() for folder in ('src', 'scripts', 'evaluation_tools')
                 for p in (ROOT/folder).rglob('*.py')}
    expected = {p for p in set(candidate['source_sha256_lf']) | set(RUNNER_FILES)
                if p.startswith(('src/', 'scripts/', 'evaluation_tools/')) and p.endswith('.py')}
    if inventory != expected:
        raise ValueError('Locked source inventory mismatch')
    revision = _git('rev-parse', 'HEAD')
    _git('merge-base', '--is-ancestor', candidate['revision'], revision)
    dirty = bool(_git('status', '--porcelain', '--untracked-files=normal'))
    if require_clean and dirty:
        raise ValueError('Live holdout requires a clean reviewed commit')
    return dict(lock=lock, lock_sha256=digest(lock), runtime_revision=revision, workspace_dirty=dirty)


def _sealed_bytes(path, expected_sha256, candidate):
    if (path is None or type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256)
            or expected_sha256 != SEALED_SHA256 or expected_sha256 != candidate['freeze']['holdout_sha256']):
        raise ValueError('An explicit sealed path and frozen exact-byte SHA256 are required')
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_CORPUS_BYTES + 1)
    if not 0 < len(raw) <= MAX_CORPUS_BYTES or sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Sealed exact-byte SHA256 mismatch or size limit')
    if len(raw.splitlines()) != COUNT:
        raise ValueError('Sealed physical line count mismatch')
    return raw


def _paths(candidate):
    base = ROOT/'.agentbus/evaluation-runs'
    return base/'research-intent-v2.5/holdout', base/'datasets'/('sealed-' + candidate['freeze']['holdout_sha256'] + '.jsonl')


def _plan(snapshot, raw):
    candidate = snapshot['lock']['candidate']
    folder, usage = _paths(candidate)
    available = not folder.exists() and not usage.exists()
    return dict(status='planned', kind='research-intent-v2.5-holdout-plan',
        contract_version=intent.VERSION, guard_version=intent.GUARD_VERSION,
        candidate_revision=candidate['revision'], runtime_revision=snapshot['runtime_revision'],
        lock_sha256=snapshot['lock_sha256'], workspace_dirty=snapshot['workspace_dirty'],
        corpus_sha256=sha256(raw).hexdigest(), corpus_bytes=len(raw), physical_lines=len(raw.splitlines()),
        sealed_json_decoded=False, context_preflight=dict(status='not_run', target_calls=0, prediction_rows=0, score_rows=0),
        frozen_summary=deepcopy(candidate['freeze']['summary']), thresholds=deepcopy(candidate['freeze']['thresholds']),
        model=deepcopy(candidate['model']), exposure=deepcopy(candidate['exposure']),
        tool_arguments_policy=dict(strict_first=True, candidate_only=True, repair_enabled=True,
            allowed_repairs=deepcopy(MODEL_POLICY['allowed_repairs']), retry_repairs=deepcopy(MODEL_POLICY['retry_repairs']),
            retries=1, retry_only=MODEL_POLICY['retry_only'], retry_same_messages=True, failed_body_saved=True,
            retry_transient_errors=False, global_budget_includes_retries=True, failed_arguments_max_characters=10000,
            approval_messages=[420,422]),
        effective_call_policy=dict(max_attempts_per_target=2, maximum_target_attempts=120, approval_message=420,
            original_frozen_maximum=candidate['freeze']['thresholds']['maximum_target_attempts']),
        run_directory=str(folder), dataset_usage_record=str(usage), run_available=available,
        ready_for_live=False, metadata_ready=available and not snapshot['workspace_dirty'],
        live_enabled=False, paid_calls=0, prediction_rows=0, eligible_for_switch=False,
        output_token_upper_bound=120*1500, input_token_upper_bound=None, estimated_cost=None,
        cost_estimation='输入 token 上限未知：默认模式不解码题干；正式前置检查后按全部消息和工具结构估算，'
            '最多120次（包含重问）。费用≈输入token×运行时输入单价+输出token×输出单价；'
            '输出最多180000 token，缺失用量或单价保持未知，不按零计。',
        record_note=RECORD_NOTE, isolation_statement=ISOLATION, automatic_status_exception=STATUS_EXCEPTION)


def build_plan(*, sealed_corpus, expected_sha256):
    snapshot = validate_lock()
    return _plan(snapshot, _sealed_bytes(sealed_corpus, expected_sha256, snapshot['lock']['candidate']))


def _check_population(rows, candidate):
    if (len(rows) != COUNT or {r['id'] for r in rows} != {f'V25H{i:03}' for i in range(1, COUNT+1)}
            or any(r['split'] != 'holdout' for r in rows)
            or cases.population_summary(rows) != candidate['freeze']['summary']):
        raise ValueError('Frozen holdout population mismatch')


def _decode_rows(raw, candidate):
    # Only the very same bytes already hashed by _sealed_bytes enter decoding.
    rows = [cases.decode(line) for line in raw.splitlines()]
    for row in rows:
        cases.valid_new_row(row)
    _check_population(rows, candidate)
    return rows


def _preflight(rows, prefixes, setups):
    # The public helper already forbids target understanding, local preflight,
    # scoring, credentials and network access while constructing neutral states.
    neutral = [dict(row, dataset='holdout', question='仅检查封存前置，不执行目标轮') for row in rows]
    result = evaluation.preflight(neutral, prefixes, setups)
    if (not result['passed'] or result['buildable'] != COUNT or result['unbuildable'] or result['not_applicable']):
        raise ValueError('All frozen holdout contexts must be buildable before claiming the run')
    return result


def _live_model():
    from trading_companion.deepseek_model import DeepSeekConfig
    from trading_companion.deepseek_tool_model import DeepSeekToolModel
    from trading_companion.local_env import load_evaluation_key_environment
    key = load_evaluation_key_environment('DEEPSEEK_API_KEY').get('DEEPSEEK_API_KEY', '')
    return DeepSeekToolModel(DeepSeekConfig('deepseek-flash', key, timeout_seconds=45, max_output_tokens=1500),
        temperature=0, response_diagnostics=True, repair_tool_arguments=True,
        argument_diagnostic_sink=evaluation._argument_diagnostic, evaluation_scope='sealed', save_failed_arguments=True)


def _append(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def _write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        _append(stream, value)


def _groups(rows, exposure):
    return dict(all=rows, implementation_unseen=[r for r in rows if r['id'] not in exposure['implementation_target_ids']],
        semantic=[r for r in rows if not r['gold']['zero_intent_calls']],
        reference_without_reviewer_mentions=[r for r in rows if r['id'] not in exposure['reference_excluded_ids']],
        **{'family:'+f: [r for r in rows if r['family_id']==f] for f in sorted({r['family_id'] for r in rows})})


def make_report(rows, *, plan, candidate, started_at, ended_at):
    _check_population(rows, candidate)
    report = evaluation.report_results(rows, {r['id']:r['gold'] for r in rows}, plan, live=True)
    groups = _groups(rows, plan['exposure'])
    views = {}
    for name in ('raw_original', 'normalized_original', 'normalized_approved'):
        selected = deepcopy(rows)
        if name == 'raw_original':
            for row in selected:
                row['V2'] = row['V2'].get('raw', row['V2'])
        views[name] = {key:field_scores(group, 'V2') for key, group in _groups(selected, plan['exposure']).items()}
    thresholds, gates = candidate['freeze']['thresholds'], {}
    for key in ('all', 'implementation_unseen', 'semantic'):
        target = thresholds[key]
        score = views['normalized_approved'][key]['joint_all_fields']
        minimum = target.get('minimum_correct', ceil(target.get('minimum_fraction', 0)*target['n']))
        gates[key] = dict(n=len(groups[key]), expected_n=target['n'], minimum_correct=minimum,
            correct=score['numerator'], passed=len(groups[key])==target['n'] and score['numerator']>=minimum)
    for family, target in thresholds['families'].items():
        group = 'family:'+family
        score = views['normalized_approved'][group]['joint_all_fields']
        gates[group] = dict(n=len(groups[group]), expected_n=target['n'], minimum_correct=target['minimum_correct'],
            correct=score['numerator'], passed=len(groups[group])==target['n'] and score['numerator']>=target['minimum_correct'])
    observed = [a['response'] for r in rows for a in r['V2'].get('attempt_history', []) if a.get('response') is not None]
    observed += [r['V2']['response'] for r in rows if not r['V2'].get('attempt_history') and r['V2'].get('response') is not None]
    authenticity = ('mock_experiment' if any(r['is_mock'] for r in observed) else
        'live_provider' if observed and all(r['provider']=='deepseek' and r['requested_model']=='deepseek-flash' for r in observed)
        else 'no_provider_observation')
    passed = (authenticity=='live_provider' and all(g['passed'] for g in gates.values())
              and report['common_comparison']['passed'] and report['zero_tolerance']['passed'])
    report.update(status='completed', kind='research-intent-v2.5-holdout-report', title='v2.5.4 一次性封存检验',
        started_at=started_at, ended_at=ended_at, score_views=views, thresholds=deepcopy(thresholds),
        gates=gates, passed=passed, thresholds_passed=passed, authenticity=authenticity,
        group_common_comparison={name:evaluation._common(group) for name,group in groups.items()},
        reference=dict(n=len(groups['reference_without_reviewer_mentions']), excluded_ids=list(REFERENCE_EXCLUDED),
            participates_in_gate=False), gold_amendments=[], threshold_basis='normalized_approved',
        isolation_statement=ISOLATION, automatic_status_exception=STATUS_EXCEPTION,
        record_note=RECORD_NOTE, billing_note='每次预留请求（含重问）分别计量；未知用量不按零计；前置替身调用另列。')
    return report


def format_report(report):
    return '\n'.join(['# v2.5.4 一次性封存检验', '', ISOLATION, '', STATUS_EXCEPTION, '', RECORD_NOTE, '',
        '96题/实现未用于调参子集/S=79/逐族为主门槛；87题为参考，不参与判定。',
        '失败、无效、未测保留分母；正式产品接入及端到端仍未测。', '',
        '```json', json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), '```', ''])


class _LockedModel:
    def __init__(self, delegate, recheck):
        self.delegate, self.recheck, self.forwarded, self.invalidated = delegate, recheck, 0, False

    def complete(self, messages, *, tools):
        try:
            self.recheck()
        except Exception:
            self.invalidated = True
            raise RunInvalidated('Locked run changed') from None
        self.forwarded += 1
        return self.delegate.complete(messages, tools=tools)


def _execute(rows, *, plan, candidate, prefixes, setups, recheck):
    folder, usage = _paths(candidate)
    if folder.exists() or usage.exists():
        raise ValueError('Version or sealed dataset already used; no resume')
    folder.mkdir(parents=True, exist_ok=False)  # Atomic permanent claim, even on any later failure.
    identity = dict(started_at=_now(), runtime_revision=plan['runtime_revision'], lock_sha256=plan['lock_sha256'],
                    candidate_revision=candidate['revision'], corpus_sha256=plan['corpus_sha256'], record_note=RECORD_NOTE)
    output, attempt_records, budget, model, usage_owned, journal_failed = [], [], None, None, False, False
    try:
        _write_json(folder/'reserved.json', dict(identity, status='reserved'))
        usage.parent.mkdir(parents=True, exist_ok=True)
        _write_json(usage, dict(identity, status='reserved'))
        usage_owned = True
        _write_json(folder/'plan.json', plan)
        recheck()
        model = _LockedModel(_live_model(), recheck)
        with (folder/'attempts.jsonl').open('x', encoding='utf-8', newline='\n') as events, \
                (folder/'rows.jsonl').open('x', encoding='utf-8', newline='\n') as journal:
            def event_sink(event):
                nonlocal journal_failed
                attempt_records.append(deepcopy(event))
                try:
                    _append(events, event)
                except BaseException:
                    journal_failed = True
                    raise
            budget = evaluation.AttemptBudget(120, event_sink)
            for row in rows:
                recheck()
                observation = evaluation.predict(row, prefixes=prefixes, setups=setups, model=model,
                                                 budget=budget, timeout_seconds=45)
                if model.invalidated or journal_failed:
                    raise RunInvalidated('Locked run or durable attempt journal changed')
                result = dict(deepcopy(row), dataset='holdout', effective_gold=deepcopy(row['gold']),
                              V2=observation, A=rule_observation(input_state(row)))
                output.append(result)
                _append(journal, result)
        recheck()
        report = make_report(output, plan=plan, candidate=candidate, started_at=identity['started_at'], ended_at=_now())
        report.update(artifacts=str(folder), provider_calls_forwarded=model.forwarded)
        _write_json(folder/'report.json', report)
        with (folder/'report.md').open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(format_report(report))
            stream.flush()
            os.fsync(stream.fileno())
        recheck()
        receipt = dict(identity, status='completed', ended_at=report['ended_at'], passed=report['passed'])
        with usage.open('a', encoding='utf-8', newline='\n') as stream:
            _append(stream, receipt)
        recheck()
        _write_json(folder/'completed.json', receipt)
        return report
    except (Exception, KeyboardInterrupt) as error:
        seen = {r['id'] for r in output}
        missing = []
        for row in rows:
            if row['id'] in seen:
                continue
            events = [e for e in attempt_records if e.get('id')==row['id']]
            reserved = sum(e['phase']=='reserved' for e in events)
            history = [deepcopy(e) for e in events if e['phase']=='finished']
            observation = evaluation._empty('failed' if reserved else 'not_measured', '运行中止，未补跑')
            observation.update(attempts=reserved, attempt_history=history,
                argument_diagnostics=[deepcopy(e['diagnostic']) for e in events if e['phase']=='argument_diagnostic'])
            missing.append(dict(deepcopy(row), dataset='holdout', V2=observation, A=dict(status='not_measured', fields={})))
        report = dict(identity, status='aborted', reason='lock_changed' if isinstance(error, RunInvalidated)
            else 'interrupted' if isinstance(error, KeyboardInterrupt) else 'run_failed', ended_at=_now(),
            rows=[*output, *missing], paid_calls=budget.attempts if budget else 0,
            attempts_reserved=budget.attempts if budget else 0, provider_calls_forwarded=model.forwarded if model else 0,
            unfinished_attempts=sum(e['phase']=='reserved' for e in attempt_records)-sum(e['phase']=='finished' for e in attempt_records),
            passed=False, thresholds_passed=False, eligible_for_switch=False, artifacts=str(folder),
            isolation_statement=ISOLATION, automatic_status_exception=STATUS_EXCEPTION,
            billing_note='未完成尝试的响应/用量可能未知；以 attempts.jsonl 为准，不按零计。')
        _write_json(folder/'aborted.json', report)
        # A late failure must replace any passing output, while retaining every claim.
        temporary = folder/'report.abort.tmp'
        _write_json(temporary, report)
        os.replace(temporary, folder/'report.json')
        temporary = folder/'report-md.abort.tmp'
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(format_report(report))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, folder/'report.md')
        if usage_owned:
            with usage.open('a', encoding='utf-8', newline='\n') as stream:
                _append(stream, {k:v for k,v in report.items() if k!='rows'})
        return report


def evaluate_holdout(*, sealed_corpus, expected_sha256, live=False):
    if type(live) is not bool:
        raise ValueError('Live must be an explicit boolean')
    snapshot = validate_lock(require_clean=live)
    candidate = snapshot['lock']['candidate']
    raw = _sealed_bytes(sealed_corpus, expected_sha256, candidate)
    plan = _plan(snapshot, raw)
    if not live:
        return plan
    if not plan['run_available']:
        raise ValueError('Version or sealed dataset already used; no resume')
    with clean_environment(), offline_network():
        rows = _decode_rows(raw, candidate)
        original, _ = load_corpus()
        prefixes, setups = evaluation.core.prefix_library([r for r in original if r['split']=='dev']), cases.load_setups()
        plan['context_preflight'] = _preflight(rows, prefixes, setups)
    plan.update(sealed_json_decoded=True, ready_for_live=True, live_enabled=True)

    def recheck():
        try:
            current = validate_lock(require_clean=True)
            if current['runtime_revision'] != plan['runtime_revision'] or current['lock_sha256'] != plan['lock_sha256']:
                raise ValueError('Run identity changed')
            # Recheck file identity, but never decode or substitute a second input.
            if _sealed_bytes(sealed_corpus, expected_sha256, candidate) != raw:
                raise ValueError('Sealed input changed')
        except Exception:
            raise RunInvalidated('Locked run changed') from None
    recheck()
    return _execute(rows, plan=plan, candidate=candidate, prefixes=prefixes, setups=setups, recheck=recheck)
