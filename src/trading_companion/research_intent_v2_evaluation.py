"""Explicit, bounded dev-only v2 experiment; production routing is not changed."""

from collections import Counter
from copy import deepcopy
from dataclasses import fields as dataclass_fields, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from math import ceil, isfinite
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import uuid4

from trading_companion.classification_metrics import ratio
from trading_companion.intent_router_task import digest, read_json
from trading_companion.model import ModelConfigurationError
from trading_companion.product_plan import ProductPlan
from trading_companion.agent_input import InputIssue
from trading_companion.errors import InvalidRequestError, DataValidationError
from trading_companion.research_intent_evaluation import field_scores, same
from trading_companion.research_intent_evaluation_baseline import UNKNOWN
from trading_companion.research_intent_evaluation_cases import ROOT, GOLD_FIELDS, input_state, load_corpus, valid_context
from trading_companion.research_intent_evaluation_fixtures import fixed_execution
from trading_companion.research_intent_v2 import TEMPERATURE, IntentContext, check_response, local_preflight, safe_failure, understand
from trading_companion.research_intent_v2_annotations import load_amendments, revised_rows
from trading_companion.research_objective import ResearchObjective
from trading_companion.research_session import ResearchSession, PendingResearch
from trading_companion.research_intent import ResearchConditions
from trading_companion.security_directory import SecurityDirectory
from trading_companion.tool_model import ToolModelResponse
from trading_companion.turn_control import TurnControl, TurnLimits
from trading_companion.workflow_evaluation_fixtures import SECURITIES, NOW, REFERENCE_DATE, PHASES, SCRIPT_KEYS


EXPOSURE = ROOT / 'data/evaluation/research-intent-v2-exposure.json'
BASELINE = ROOT / 'data/evaluation/research-intent-v2-rule-baseline-dev.json'
MODEL = 'deepseek-flash'
MAX_CALLS = 155
SETUPS = ROOT / 'data/evaluation/research-intent-v2-setups.json'


def exposure_registry():
    value = json.loads(EXPOSURE.read_text(encoding='utf-8'))
    if (value.keys() != {'version', 'known_during_implementation'}
            or value['version'] != 'research-intent-v2-exposure-v1'
            or type(value['known_during_implementation']) is not dict):
        raise ValueError('Invalid exposure registry')
    for key, item in value['known_during_implementation'].items():
        if (type(key) is not str or not key.startswith('V2') or type(item) is not dict
                or item.keys() != {'reason', 'unseen_holdout_eligible'}
                or type(item['reason']) is not str or not item['reason']
                or item['unseen_holdout_eligible'] is not False):
            raise ValueError('Invalid exposure entry')
    return value


def prefix_library(rows):
    """Only previously reviewed dev scripts; no gold or target-response synthesis."""
    variants = {}
    for row in rows:
        if row['split'] != 'dev':
            continue
        steps = row['probe']['steps']
        for length in range(1, len(steps) + 1):
            prefix = deepcopy(steps[:length])
            key = tuple(s['question'] for s in prefix)
            variants.setdefault(key, {})[digest(prefix)] = prefix
    reviewed = read_json(SETUPS, maximum=100_000)
    if (reviewed.keys() != {'version', 'reviewer', 'approval_message', 'prefixes'}
            or reviewed['version'] != 'intent-v2-setups-v1' or reviewed['reviewer'] != 'claude'
            or type(reviewed['approval_message']) is not int or reviewed['approval_message'] <= 0
            or type(reviewed['prefixes']) is not list):
        raise ValueError('Invalid reviewed setup library')
    for prefix in reviewed['prefixes']:
        if type(prefix) is not list or not 1 <= len(prefix) <= 6:
            raise ValueError('Invalid setup prefix')
        for step in prefix:
            if (type(step) is not dict or step.keys() != {'question', 'script'}
                    or type(step['question']) is not str or not 0 < len(step['question']) <= 2000
                    or type(step['script']) is not dict or not step['script'].keys() <= SCRIPT_KEYS
                    or type(step['script'].get('phases')) is not list
                    or not set(step['script']['phases']) <= PHASES):
                raise ValueError('Invalid setup step')
        key = tuple(s['question'] for s in prefix)
        variants.setdefault(key, {})[digest(prefix)] = deepcopy(prefix)
    return {key: next(iter(items.values())) if len(items) == 1 else None for key, items in variants.items()}


def program_context(state, prefixes):
    """Snapshot IDs are program state; history alone cannot manufacture a pending."""
    if state.keys() != {'question', 'context'}:
        raise ValueError('Prediction accepts question/context only')
    value = state['context']
    valid_context(value)
    if value['pending'] != 'none':
        if not all(value['pending_binding'].values()):
            raise ValueError('架构不存在会话/轮次待办归属，不能真实表示该描述符')
        if value['pending'] == 'conflict' or value['pending_age_seconds'] != 0:
            return _pending_fault_context(value, prefixes)
    setup_calls = 0
    prefix = prefixes.get(tuple(value['history']))
    capture_saved = (value['has_company'] and prefix and value['latest'] is not None
        and value['latest']['age_seconds'] == 0 and value['latest']['current']
        and value['source'] == 'tencent' and not value['development']
        and value['company_enabled'] and value['workflow_enabled'] and not value['shortlist'])
    if value['pending'] != 'none' or capture_saved:
        if not prefix:
            raise ValueError('待办缺少唯一的已审核前置脚本，未从聊天文字补身份')
        probe = dict(steps=[*prefix, dict(question=state['question'], script=dict(phases=[]))], unmeasured_reason='')
        captured = fixed_execution(state, probe, capture_context=True)
        if captured['status'] != 'ok':
            raise ValueError(captured['reason'])
        session, directory, quotes = (captured[k] for k in ('session', 'directory', 'quote_source'))
        setup_calls = captured['setup_calls']
    else:
        from trading_companion.index_contracts import IndexResearchRequest
        from trading_companion.screening import ScreeningCondition, ScreeningRequest
        from trading_companion.screening_context import SavedScreening
        session = ResearchSession()
        directory = SecurityDirectory(SECURITIES, 'intent-v2-program-snapshot', NOW, value['development'])
        quotes = object()
        latest = value['latest']
        if latest is not None:
            if latest['kind'] == 'index':
                if len(latest['subjects']) != 1:
                    raise ValueError('Invalid index snapshot')
                securities = ()
                scope = IndexResearchRequest(latest['subjects'][0], REFERENCE_DATE - timedelta(days=365),
                    REFERENCE_DATE - timedelta(days=1), source=latest['source'], horizon=latest['horizon'])
            else:
                securities = tuple(directory.lookup(code).candidates[0] for code in latest['subjects'])
                scope = ProductPlan(securities=securities, source=latest['source'],
                    objective=ResearchObjective('decision' if latest['horizon'] else 'analysis', latest['horizon']))
            session.stage_research_subject(securities, kind=latest['kind'], source=latest['source'],
                development=latest['development'], now=NOW-timedelta(seconds=latest['age_seconds']), scope=scope)
            session._subject_current = latest['current']
        if value['shortlist']:
            session.screening_saved = SavedScreening(NOW,
                ScreeningRequest((ScreeningCondition('price', 'gt', 0),), limit=20),
                tuple(directory.lookup(code).candidates[0] for code in value['shortlist']),
                quotes, value['source'], value['development'])
    return IntentContext(session, directory, NOW, REFERENCE_DATE, value['source'], value['development'],
        tuple(value['history']), quotes, value['company_enabled'], value['workflow_enabled'],
        value['conversation_memory']), setup_calls


def _pending_fault_context(value, prefixes):
    """Descriptor-only fault injection over reviewed, program-created pending objects."""
    seed = deepcopy(value)
    seed['pending_age_seconds'] = 0
    if value['pending'] == 'conflict':
        seed['pending'] = 'company_horizon'
    # Empty history describes no conversational seed. Search only the reviewed
    # library, and require exactly one matching prefix rather than inventing one
    # from the target question, a case ID or an expected answer.
    histories = [tuple(seed['history'])] if seed['history'] else sorted(prefixes)
    matches = []
    for history in histories:
        if not prefixes.get(history):
            continue
        declared = dict(seed, history=list(history))
        try:
            context, calls = program_context(dict(question='仅捕获已审核前置状态', context=declared), prefixes)
        except (ValueError, IndexError, KeyError):
            continue
        matches.append((context, calls))
    if len(matches) != 1:
        raise ValueError('故障待办缺少唯一、状态匹配的已审核前置')
    context, calls = matches[0]
    session = context.session
    slots = ('pending', 'pending_edit', 'product_pending', 'index_pending')
    occupied = [name for name in slots if getattr(session, name) is not None]
    if len(occupied) != 1:
        raise ValueError('故障前置必须只产生一个真实待办')
    name = occupied[0]
    pending = getattr(session, name)
    created = context.now - timedelta(seconds=value['pending_age_seconds'])
    updates = dict(created_at=created)
    if 'expires_at' in {field.name for field in dataclass_fields(pending)}:
        updates['expires_at'] = created + (pending.expires_at - pending.created_at)
    pending = replace(pending, **updates)
    setattr(session, '_' + name if name in ('pending', 'pending_edit') else name, pending)
    if value['pending'] == 'conflict':
        # This is explicitly a state fault, not a normal API dialogue. Both
        # native objects use only conditions and identities checked by the seed.
        from trading_companion.product_plan import ProductPlanContext
        if type(pending) is not ProductPlanContext or name != 'product_pending' or not pending.securities:
            raise ValueError('双槽位故障需要真实公司待办及已核对的证券')
        conditions = ResearchConditions(pending.start_date, pending.end_date, pending.indicators,
            pending.source, pending.reference_date, pending.applied_defaults, pending.objective)
        session._pending = PendingResearch(conditions, pending.securities, created,
                                          user_question=pending.original_question)
    # The seed's question is evidence for the injected objects, not target history.
    return replace(context, history=tuple(value['history'])), calls


def setup_metadata(context):
    """Describe fixture construction separately from the model's trusted context."""
    pending = [getattr(context.session, name) for name in
               ('pending', 'pending_edit', 'product_pending', 'index_pending')
               if getattr(context.session, name) is not None]
    return dict(fault_injection='multi_pending' if len(pending) > 1 else None,
        pending_types=[type(item).__name__ for item in pending],
        pending_ages_seconds=[int((context.now-item.created_at).total_seconds()) for item in pending],
        original_questions=[getattr(item, 'original_question', getattr(item, 'user_question',
                                    getattr(item, 'question', ''))) for item in pending])


class AttemptBudget:
    """Reserve immediately before the transport, once per target and at most 155 total."""

    def __init__(self, maximum, event_sink=None):
        if type(maximum) is not int or not 1 <= maximum <= MAX_CALLS:
            raise ValueError('Maximum calls must be 1..155')
        self.maximum, self.attempts, self.lock = maximum, 0, Lock()
        self.event_sink = event_sink

    def target(self, model, identifier=None):
        return TargetAttempt(self, model, identifier)


class TargetAttempt:
    def __init__(self, budget, delegate, identifier=None):
        self.budget, self.delegate = budget, delegate
        self.attempts, self.response, self.elapsed_ms = 0, None, None
        self.identifier = identifier

    def record(self, phase, **values):
        if self.budget.event_sink is not None:
            self.budget.event_sink(dict(phase=phase, id=self.identifier,
                at=datetime.now(timezone.utc).isoformat(), attempt_number=self.budget.attempts, **values))

    def complete(self, messages, *, tools):
        with self.budget.lock:
            if self.attempts or self.budget.attempts >= self.budget.maximum:
                raise ModelConfigurationError('Evaluation attempt budget exhausted; no retry')
            self.attempts += 1
            self.budget.attempts += 1
            # Persist reservation before transport. A killed run leaves a known unknown-cost attempt.
            self.record('reserved')
        start = perf_counter()
        failure = None
        try:
            self.response = self.delegate.complete(messages, tools=tools)
            return self.response
        except BaseException as error:
            failure = safe_failure(error) if isinstance(error, Exception) else 'interrupted'
            raise
        finally:
            self.elapsed_ms = round((perf_counter() - start) * 1000, 3)
            self.record('finished', returned=self.response is not None, failure=failure,
                        call_ms=self.elapsed_ms, response=self.response.to_dict() if type(self.response) is ToolModelResponse else None)


def predict(state, *, prefixes, model, budget, timeout_seconds=45, identifier=None):
    """Isolated adapter: neither scripts, IDs, tags nor gold reach the target model."""
    try:
        context, setup_calls = program_context(state, prefixes)
    except (ValueError, IndexError, KeyError) as error:
        return dict(status='not_measured', reason=str(error), fields={}, attempts=0, setup_calls=0,
                    response=None, call_ms=None)
    local = local_preflight(state['question'], context)
    if local is not None:
        return dict(status='ok', reason=None, fields=local.fields(), attempts=0, setup_calls=setup_calls,
            response=None, call_ms=None, low_confidence=False, context=context.model_context(),
            path='local_preflight', setup=setup_metadata(context), metadata=dict(provenance=list(local.provenance)))
    if budget.attempts >= budget.maximum:
        return dict(status='not_measured', reason='全局尝试上限已到；未重试或丢弃分母', fields={},
                    attempts=0, setup_calls=setup_calls, response=None, call_ms=None)
    target = budget.target(model, identifier)
    control = TurnControl(TurnLimits(model_calls=1, timeout_seconds=timeout_seconds))
    try:
        run = understand(state['question'], model=target, context=context, control=control)
        result = dict(status=run.status, reason=run.code, fields=run.intent.fields() if run.intent else {},
            runtime=run.runtime, low_confidence=bool(run.intent and run.intent.low_confidence),
            corrections=list(run.corrections),
            metadata=dict(historical_window=list(run.intent.historical_window),
                provenance=list(run.intent.provenance), plan_blocked=run.intent.plan_blocked) if run.intent else {})
    except Exception as error:
        # Never serialize provider exception text or traceback, including unknown failures.
        result = dict(status='failed', reason=safe_failure(error), fields={}, runtime=control.to_dict(), corrections=[])
    raw = dict(status=result['status'], reason=result['reason'], fields={})
    if result['status'] in ('ok', 'invalid') and target.response is not None:
        try:
            original = check_response(state['question'], target.response, context=context, normalize=False)
            raw = dict(status='ok', reason='intent_v2_raw_checked', fields=original.fields())
        except InputIssue as error:
            raw = dict(status='invalid', reason=error.code, fields={})
        except (ValueError, InvalidRequestError, DataValidationError):
            raw = dict(status='invalid', reason='intent_v2_context_or_conditions_invalid', fields={})
    response = target.response.to_dict() if type(target.response) is ToolModelResponse else None
    return dict(result, attempts=target.attempts, setup_calls=setup_calls, response=response,
                call_ms=target.elapsed_ms, context=context.model_context(), setup=setup_metadata(context), path='semantic', raw=raw)


def _groups(rows):
    return dict(all=rows, semantic=[r for r in rows if 'local' not in r['tags']],
        local=[r for r in rows if 'local' in r['tags']],
        reported_failure=[r for r in rows if r['source']['kind'] == 'reported_failure'],
        multi_turn=[r for r in rows if r['context']['history']], negative=[r for r in rows if 'negative' in r['tags']])


def metrics(rows, registry):
    known = registry['known_during_implementation']
    holdout = [r for r in rows if r.get('split') == 'holdout']
    groups = _groups(rows)
    groups.update(implementation_known=[r for r in holdout if r['id'] in known],
                  unseen_holdout=[r for r in holdout if r['id'] not in known])
    return {name: field_scores(group, 'V2') for name, group in groups.items()}


def common_comparison(rows, baseline):
    """A's unknown fields cannot be used as evidence of V2 improvement."""
    results = {}
    for layer in ('A', 'B'):
        fields = {}
        for field in GOLD_FIELDS:
            observed = [(r, baseline[r['id']][layer]) for r in rows if r['id'] in baseline
                and baseline[r['id']][layer]['status'] == 'ok'
                and baseline[r['id']][layer]['fields'].get(field, UNKNOWN) != UNKNOWN]
            fields[field] = dict(n=len(observed), baseline=ratio(sum(same(old['fields'][field], r['gold'][field])
                for r, old in observed), len(observed)), v2=ratio(sum(r['V2']['status'] == 'ok'
                and same(r['V2']['fields'].get(field, UNKNOWN), r['gold'][field]) for r, old in observed), len(observed)))
        results[layer] = dict(fields=fields, scope='共同可观测字段；A为规则，B为历史固定模拟执行，V2只为预计动作')
    return results


def telemetry(rows):
    observations = [r['V2'] for r in rows]
    calls = [r for r in observations if r['attempts']]
    elapsed = sorted(r['call_ms'] for r in calls if r['call_ms'] is not None)
    responses = [r['response'] for r in calls if r['response'] is not None]
    inconsistent = sum(r['usage'] is not None and all(v is not None for v in r['usage'].values())
        and r['usage']['prompt_tokens'] + r['usage']['completion_tokens'] != r['usage']['total_tokens'] for r in responses)
    tokens = {}
    for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        values = [r['usage'][field] for r in responses if r['usage'] is not None and r['usage'][field] is not None]
        tokens[field] = dict(reported_sum=sum(values), known_attempts=len(values), unknown_attempts=len(calls)-len(values),
                             complete_total=sum(values) if len(values) == len(calls) and not inconsistent else None)
    return dict(attempts=sum(r['attempts'] for r in observations), fixed_setup_calls=sum(r['setup_calls'] for r in observations),
        returned_models=dict(Counter(r['model'] for r in responses)), missing_responses=len(calls)-len(responses),
        mock_responses=sum(r['is_mock'] for r in responses), tokens=tokens, inconsistent_usage_responses=inconsistent,
        latency=dict(n=len(elapsed), p50_ms=elapsed[ceil(len(elapsed)*.5)-1] if elapsed else None,
            p95_ms=elapsed[ceil(len(elapsed)*.95)-1] if elapsed else None, method='nearest_rank_transport_including_failures'),
        low_confidence=sum(bool(r.get('low_confidence')) for r in observations),
        raw_low_confidence=sum(any(c.get('arguments', {}).get('confidence') == 'low'
            for c in (r['response'] or {}).get('tool_calls', [])) for r in observations),
        unexpected_local_intent_calls=[r['id'] for r in rows if r['gold']['zero_intent_calls'] and r['V2']['attempts']])


def correction_summary(rows):
    groups = {}
    for row in rows:
        for correction in row['V2'].get('corrections', []):
            group = groups.setdefault(correction['kind'], dict(events=0, ids=[]))
            group['events'] += 1
            if row['id'] not in group['ids']:
                group['ids'].append(row['id'])
    for group in groups.values():
        group.update(questions=len(group['ids']), requires_individual_review=len(group['ids']) > 10)
    return groups


def score_views(rows, amendments, registry, baseline):
    raw = deepcopy(rows)
    for row in raw:
        row['V2'] = deepcopy(row['V2'].get('raw', row['V2']))
    revised = revised_rows(rows, amendments)
    baseline_scores = {}
    for name, gold_rows in (('original_gold', rows), ('revised_gold', revised)):
        compared = []
        for row in gold_rows:
            observation = baseline.get(row['id'])
            if observation is None:
                continue
            compared.append(dict(row, A=observation['A'], B=observation['B']))
        baseline_scores[name] = {layer: field_scores(compared, layer) for layer in ('A', 'B')}
    return dict(model_raw_original_gold=metrics(raw, registry), normalized_original_gold=metrics(rows, registry),
                normalized_revised_gold=metrics(revised, registry), frozen_baseline_rescored=baseline_scores)


def zero_tolerance(rows):
    # Intent fields and proposed outcomes do not prove an execution red line passed.
    checks = {key: dict(violations=0, measured=0, unmeasured=len(rows), scope='新版尚未接执行器，未测')
              for key in ('vetoed_plan', 'wrong_subject', 'cross_object', 'strict_scope')}
    local = [r for r in rows if r['gold']['zero_intent_calls']]
    measured = [r for r in local if r['V2']['status'] == 'ok' or r['V2']['attempts']]
    checks['local_intent_call'] = dict(violations=sum(bool(r['V2']['attempts']) for r in measured),
        measured=len(measured), unmeasured=len(local)-len(measured), scope='仅离线候选目标轮理解调用；不代表正式入口')
    return checks


def _run_rows(rows, *, plan, model, maximum, timeout_seconds, prefixes, baseline, registry, live=False, journal=None,
              amendments=None, event_sink=None):
    budget, results = AttemptBudget(maximum, event_sink), []
    amendments = amendments or {}
    for row in rows:
        result = {key: deepcopy(row[key]) for key in ('id', 'split', 'question', 'context', 'gold', 'tags', 'source', 'fault')}
        result['implementation_known'] = row['id'] in registry['known_during_implementation']
        result['V2'] = predict(input_state(row), prefixes=prefixes, model=model, budget=budget,
                               timeout_seconds=timeout_seconds, identifier=row['id'])
        result['effective_gold'] = revised_rows([result], amendments)[0]['gold']
        results.append(result)
        if journal is not None:
            journal.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
            journal.flush()
    stats = telemetry(results)
    views = score_views(results, amendments, registry, baseline)
    revised = revised_rows(results, amendments)
    return dict(status='completed', kind='v2_development', evaluated_at=datetime.now(timezone.utc).isoformat(),
        plan=plan, scope='V2为独立离线候选：本地前置加单次模型理解，outcome仅预计动作；B为历史固定模拟执行。未接产品。',
        authenticity=('no_provider_observation' if live and not stats['attempts'] else
                      'live_provider' if live and not stats['mock_responses'] else 'mock_experiment'),
        paid_calls=stats['attempts'] if live else 0, temperature=plan.get('temperature'), rows=results, telemetry=stats,
        metrics=views['normalized_revised_gold'], score_views=views, corrections=correction_summary(results),
        gold_amendments=list(amendments.values()),
        scoring_policy='三列均保留全部分母；原样关闭受限规范化，仍执行当前版本通用校验、程序绑定和本地前置（含v2.2起的指数领域检查），并非v2.1同口径成绩。门槛使用规范化+修订gold；改标签不算模型改善。',
        threshold_basis='normalized_revised_gold',
        billing_note='paid_calls为发往传输层的尝试上限记录，不证明服务商实际计费；未知token不按0计费用',
        zero_tolerance=zero_tolerance(results),
        common_comparison=common_comparison(revised, baseline),
        common_comparison_original_gold=common_comparison(results, baseline), exposure=registry,
        unmeasured=['真实计划执行及价位', '跨对象证据借用', '完整严格范围执行', '正式会话零调用及端到端', '新协议故障注入', '留出成绩'],
        eligible_for_switch=False)


def _live_model(timeout_seconds):
    from trading_companion.deepseek_model import DeepSeekConfig
    from trading_companion.deepseek_tool_model import DeepSeekToolModel
    from trading_companion.local_env import load_evaluation_key_environment
    key = load_evaluation_key_environment('DEEPSEEK_API_KEY').get('DEEPSEEK_API_KEY', '')
    return DeepSeekToolModel(DeepSeekConfig(model=MODEL, api_key=key, timeout_seconds=timeout_seconds, max_output_tokens=1500),
                             temperature=TEMPERATURE, response_diagnostics=True)


def evaluate_development(*, live=False, split='dev', maximum=MAX_CALLS, timeout_seconds=45, report_dir=None):
    """Only this explicit live entry can read the selected credential or use a provider."""
    from trading_companion.research_intent_evaluation import build_v2_plan, format_report
    if split != 'dev' or type(live) is not bool:
        raise ValueError('Holdout requires a separately reviewed version lock')
    AttemptBudget(maximum)
    if type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds) or not 0 < timeout_seconds <= 90:
        raise ValueError('Timeout must be positive and <=90 seconds')
    plan = build_v2_plan()
    if not live:
        return plan
    if report_dir is None:
        raise ValueError('Live evaluation requires a journal/report directory')
    rows, manifest = load_corpus()
    if manifest['annotation_status'] != 'reviewed':
        raise ValueError('Annotations require review')
    selected = [r for r in rows if r['split'] == 'dev']
    amendments = load_amendments(rows, manifest['corpus_sha256'])
    registry = exposure_registry()
    baseline_report = json.loads(BASELINE.read_text(encoding='utf-8'))
    if (baseline_report['plan']['corpus_sha256'] != plan['corpus_sha256']
            or baseline_report['plan']['baseline'] != plan['baseline']):
        raise ValueError('Frozen baseline does not match this corpus/rules')
    baseline = {r['id']: r for r in baseline_report['rows']}
    if set(baseline) != {r['id'] for r in selected}:
        raise ValueError('Incomplete frozen development baseline')
    prefixes = prefix_library(selected)
    folder = Path(report_dir).absolute() / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    plan.update(kind='v2_development_run', max_dev_attempts=maximum, live_enabled=True,
        transport_timeout_seconds=timeout_seconds, max_output_tokens=1500,
        baseline_report_sha256=sha256(BASELINE.read_bytes()).hexdigest())
    (folder/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    # Load the key only after admission, fingerprints and durable output are ready.
    model = _live_model(timeout_seconds)
    with (folder/'rows.jsonl').open('x', encoding='utf-8', newline='\n') as journal, \
            (folder/'attempts.jsonl').open('x', encoding='utf-8', newline='\n') as attempts:
        def event_sink(event):
            attempts.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
            attempts.flush()
            os.fsync(attempts.fileno())
        report = _run_rows(selected, plan=plan, model=model, maximum=maximum, timeout_seconds=timeout_seconds,
            prefixes=prefixes, baseline=baseline, registry=registry, live=True, journal=journal,
            amendments=amendments, event_sink=event_sink)
    report['artifacts'] = str(folder)
    (folder/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    (folder/'report.md').write_text(format_report(report), encoding='utf-8')
    return report
