"""公开历史响应离线回放，不是新模型成绩。禁止网络和模型调用。"""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation_tools import research_intent_v25_evaluation as evaluation
from scripts.verify import clean_environment, offline_network
from trading_companion import research_intent_v2 as intent
from trading_companion.deepseek_tool_model import MAX_FAILED_ARGUMENT_CHARACTERS, _tool_arguments
from trading_companion.intent_router_task import digest
from trading_companion.model import ModelCallError, ModelRequestError, TokenUsage
from trading_companion.research_intent_evaluation import same
from trading_companion.research_intent_evaluation_baseline import rule_observation
from trading_companion.research_intent_evaluation_cases import ROOT, input_state
from trading_companion.tool_model import ToolCall, ToolModelResponse

SOURCES = {
    'dev': ROOT/'.agentbus/tmp/intent-v24-live-dev-run1/4a6b8973437046a4a08e74b25eb532b9/report.json',
    'regression': ROOT/'.agentbus/evaluation-runs/research-intent-v2.4/holdout/report.json',
}
PUBLIC_V25 = ROOT/'.agentbus/evaluation-runs/research-intent-v2.5/public/cbdc7d62b342a3edee292d1305795bbb343fec04'
PUBLIC_V251 = ROOT/'.agentbus/evaluation-runs/research-intent-v2.5/public/e49ff07172df3b148cfc3dc0ba8a8b96e31ac1ab'
PUBLIC_V252 = ROOT/'.agentbus/evaluation-runs/research-intent-v2.5/public/30339be85e3ae262dda2ccfddc25be35c61b7df3'
PUBLIC_V253 = ROOT/'.agentbus/evaluation-runs/research-intent-v2.5/public/3c0ba9d534ca99af89d8670f9eba4889166addd3'
SOURCES_BY_VERSION = {'v2.4': SOURCES,
                      'v2.5': {split: PUBLIC_V25/split/'report.json' for split in ('dev', 'regression')},
                      'v2.5.1': {split: PUBLIC_V251/split/'report.json' for split in ('dev', 'regression')},
                      'v2.5.2': {split: PUBLIC_V252/split/'report.json' for split in ('dev', 'regression')},
                      'v2.5.3': {split: PUBLIC_V253/split/'report.json' for split in ('dev', 'regression')}}


def migrate_response(value, source_version='v2.5.3'):
    """An explicit version-only copy; retain the original response in the replay row."""
    expected = {'status', 'text', 'tool_calls', 'provider', 'requested_model', 'model', 'is_mock', 'finish_reason', 'usage'}
    if type(value) is not dict or value.keys() != expected or type(value['tool_calls']) is not list:
        raise ValueError('Missing or invalid archived response envelope')
    if source_version not in SOURCES_BY_VERSION:
        raise ValueError('Only the fixed public archive versions are accepted')
    copied = deepcopy(value)
    for call in copied['tool_calls']:
        if call.keys() != {'call_id', 'name', 'arguments'} or call['arguments'].get('version') != 'research-intent-' + source_version:
            raise ValueError('Replay accepts only the selected archived proposal version')
        call['arguments']['version'] = intent.VERSION
    copied['tool_calls'] = tuple(ToolCall(**call) for call in copied['tool_calls'])
    copied['usage'] = TokenUsage(**copied['usage']) if copied['usage'] is not None else None
    return ToolModelResponse(**copied)


def _saved_arguments(event, attempt, source_version, question, context):
    """Validate saved parameter text in an explicitly synthetic carrier, not a recovered response."""
    raw = event.get('arguments_raw')
    if (type(raw) is not str or not 0 < len(raw) <= MAX_FAILED_ARGUMENT_CHARACTERS
            or event.get('arguments_raw_truncated') is not False
            or event.get('arguments_length') != len(raw) or event.get('target_attempt') != attempt
            or event.get('call_index') != 0 or event.get('finish_reason') != 'tool_calls'
            or event.get('repair') is not None or event.get('correction') is not None):
        raise ValueError('Saved argument provenance is incomplete')
    try:
        json.loads(raw)
    except json.JSONDecodeError as error:
        if (event.get('position'), event.get('line'), event.get('column')) != (error.pos, error.lineno, error.colno):
            raise ValueError('Saved parser position does not match') from None
    else:
        raise ValueError('Saved text is not a failed JSON argument')
    arguments, diagnostic = _tool_arguments(raw, 'tool_calls', 0, diagnostics=True, repair=True,
                                           sink=None, scalar_only=True)
    call = ToolCall('offline-arguments-' + str(attempt), intent.TOOL_NAME, arguments)
    arguments = call.arguments
    if arguments.get('version') != 'research-intent-' + source_version:
        raise ValueError('Saved argument version does not match')
    arguments['version'] = intent.VERSION
    carrier = ToolModelResponse('tool_calls', tool_calls=(ToolCall(call.call_id, call.name, arguments),),
        provider='offline-parameter-validation', requested_model='unknown', model='unknown', usage=None,
        is_mock=True, finish_reason='tool_calls')
    checked = evaluation.checked_result(question, carrier, context)
    checked['raw'] = dict(status='failed', reason='tool_arguments_json_invalid', fields={})
    checked.setdefault('corrections', []).append(dict(kind='tool_arguments_repaired', field='tool_arguments',
        before='invalid_json', after='checked_json', source='saved_arguments', repair=diagnostic['repair']))
    checked.update(validation_carrier=dict(synthetic=True, purpose='仅校验保存的参数，不是原始响应',
        original_call_id=None, original_tool_name=None, original_call_count=None,
        original_provider=None, original_model=None, original_usage=None, value=carrier.to_dict()),
        argument_diagnostics=[dict(diagnostic, target_attempt=attempt, correction='tool_arguments_repaired')],
        response_origin='saved_arguments', selected_archived_attempt=attempt)
    return checked


def _replay_attempts(original, row, context, source_version):
    """Preserve every saved failure; select the first checked observation in recorded order."""
    history = original.get('attempt_history')
    if not history:
        if original.get('response') is not None:
            return evaluation.checked_result(row['question'], migrate_response(original['response'], source_version), context), []
        return None, []
    if (type(history) is not list or not 1 <= len(history) <= 2
            or original.get('attempts') != len(history)
            or any(type(h) is not dict or h.get('target_attempt') != i for i, h in enumerate(history, 1))):
        raise ValueError('Invalid archived attempt order')
    selected, ledger = None, []
    for attempt in history:
        number = attempt['target_attempt']
        candidate = None
        if attempt.get('response') is not None:
            candidate = evaluation.checked_result(row['question'], migrate_response(attempt['response'], source_version), context)
            candidate['selected_archived_attempt'] = number
            candidate['response_origin'] = 'archived_response'
        events = attempt.get('argument_diagnostics', [])
        for index, event in enumerate(events):
            if 'arguments_raw' not in event:
                continue
            entry = dict(id=row['id'], dataset=row['dataset'], source_version=source_version,
                source_report=str(SOURCES_BY_VERSION[source_version][row['dataset']]),
                source_pointer=f"rows[id={row['id']}].V2.attempt_history[{number-1}].argument_diagnostics[{index}]",
                target_attempt=number, arguments_sha256=sha256(event['arguments_raw'].encode('utf-8')).hexdigest(),
                arguments_length=event.get('arguments_length'), repaired=False, repair=None,
                selected=False, status='not_measured', reason=None, corrections=[], synthetic_carrier=False,
                provider_metadata_recovered=False)
            try:
                if (attempt.get('failure') != 'intent_v2_model_tool_arguments_json_invalid'
                        or attempt.get('response') is not None or len(events) != 1
                        or event not in original.get('argument_diagnostics', [])):
                    raise ValueError('Saved event does not match its failed attempt')
                recovered = _saved_arguments(event, number, source_version, row['question'], context)
                entry.update(repaired=True, repair='unquoted_scalar', status=recovered['status'], synthetic_carrier=True,
                    reason=recovered.get('reason'), corrections=deepcopy(recovered['corrections']),
                    fields=deepcopy(recovered['fields']), validation_carrier=recovered['validation_carrier'])
                candidate = recovered
            except (ValueError, ModelCallError, ModelRequestError, RecursionError):
                entry['reason'] = '保存的原文或调用阶段不能恢复；未补造'
            ledger.append(entry)
        if selected is None and candidate is not None:
            selected = candidate
            for entry in ledger:
                if entry['target_attempt'] == number and entry['repaired']:
                    entry['selected'] = True
    if selected is not None and history[0].get('failure') == 'intent_v2_model_tool_arguments_json_invalid':
        selected['raw'] = dict(status='failed', reason='tool_arguments_json_invalid', fields={})
    return selected, ledger


def replay_reports(reports, source_version='v2.5.3'):
    """Reports are explicit old observations; neither gold nor target scripts enter checking."""
    rows, effective, prefixes, setups = evaluation.populations('all')
    if source_version not in SOURCES_BY_VERSION:
        raise ValueError('Only fixed public archives can be replayed')
    if source_version == 'v2.4':
        rows = [r for r in rows if 'setup_id' not in r]
    by_id = {r['id']: r for r in rows}
    observed = {}
    for report in reports:
        for old in report['rows']:
            if old['id'] not in by_id or old['id'] in observed:
                raise ValueError('Unexpected/duplicate archived public case')
            row = by_id[old['id']]
            if old['question'] != row['question'] or old['context'] != row['context']:
                raise ValueError('Archived question/context does not match the frozen public case')
            observed[row['id']] = old
    output, saved_replays = [], []
    with clean_environment(), offline_network(), \
            patch.object(intent, 'understand', side_effect=AssertionError('Replay never calls a model')), \
            patch.object(evaluation.core, 'understand', side_effect=AssertionError('Replay never calls a model')), \
            patch.object(evaluation.core, 'predict', side_effect=AssertionError('Replay never predicts a target')), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_evaluation_key_environment', side_effect=AssertionError('No credentials')), \
            patch.object(evaluation, '_live_model', side_effect=AssertionError('Replay never loads credentials')), \
            patch.object(evaluation.core, '_live_model', side_effect=AssertionError('Replay never loads credentials')):
        for row in rows:
            old = observed.get(row['id'])
            original = deepcopy((old or {}).get('V2', {}))
            result = evaluation._empty('not_measured', '缺少可回放的原始响应')
            if row['id'] in evaluation.NOT_APPLICABLE:
                result = evaluation._empty('not_applicable', '#383已批准架构不适用')
            else:
                try:
                    context, calls = evaluation.build_context(row, prefixes, setups)
                    local = intent.local_preflight(row['question'], context)
                    if local is not None:
                        result = dict(evaluation._empty('ok', None, calls), fields=local.fields(),
                            path='local_preflight', metadata=dict(provenance=list(local.provenance)))
                    else:
                        result['setup_calls'] = calls
                        checked, ledger = _replay_attempts(original, row, context, source_version)
                        saved_replays.extend(ledger)
                        if checked is not None:
                            result = dict(evaluation._empty('ok', None, calls), **checked,
                                path='saved_arguments' if checked.get('response_origin') == 'saved_arguments' else 'archived_response',
                                context_sha256=digest(context.model_context()))
                except (ValueError, IndexError, KeyError):
                    result = evaluation._empty('not_measured', '原始响应或已审核前置不可回放；未生成替代提案')
            output.append(dict(row, effective_gold=effective[row['id']], V2=result,
                A=rule_observation(input_state(row)), archived_status=original.get('status', 'missing'),
                archived_reason=original.get('reason'), archived_attempts=original.get('attempts'),
                archived_fields=original.get('fields', {}), archived_response=original.get('response'),
                archived_argument_diagnostics=deepcopy(original.get('argument_diagnostics', [])),
                archived_attempt_history=deepcopy(original.get('attempt_history', [])),
                archived_format_retry=deepcopy(original.get('format_retry', {})),
                changed_fields=[key for key in evaluation.GOLD_FIELDS if not same(
                    result['fields'].get(key, evaluation.UNKNOWN), original.get('fields', {}).get(key, evaluation.UNKNOWN))],
                replay_migration=dict(field='version', before='research-intent-' + source_version, after=intent.VERSION,
                                      applied=result.get('path') in ('archived_response', 'saved_arguments'),
                                      original_response_unchanged=True)))
    report = evaluation.report_results(output, effective, dict(kind='offline_replay', target_calls=0,
        model_factory_calls=0, retries=0, annotation_completion=evaluation.annotation_completion()),
        notice={'v2.4': evaluation.REPLAY_NOTICE, 'v2.5': evaluation.REPLAY_251_NOTICE,
                'v2.5.1': evaluation.REPLAY_252_NOTICE, 'v2.5.2': evaluation.REPLAY_253_NOTICE,
                'v2.5.3': evaluation.REPLAY_254_NOTICE}[source_version])
    historical = [r for r in output if r['archived_format_retry'].get('attempted')]
    report.update(passed=None, thresholds=None, new_model_results=False,
        saved_argument_replays=saved_replays,
        saved_argument_replay_summary=dict(saved=len(saved_replays), repaired=sum(e['repaired'] for e in saved_replays),
            selected=sum(e['selected'] for e in saved_replays)),
        historical_format_retries=dict(total=len(historical), ids=[r['id'] for r in historical],
            successful_ids=[r['id'] for r in historical if r['archived_format_retry'].get('successful')],
            successful=sum(bool(r['archived_format_retry'].get('successful')) for r in historical)),
        note='仅回放旧提案、新本地前置及明确保存的参数；参数载体是合成的，服务商元数据未知。'
             '不检验新提示词或推测缺失JSON；缺响应、失败和未测仍留在完整分母。')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, default=ROOT/'.agentbus/tmp/intent-v25-replay')
    parser.add_argument('--source-version', choices=tuple(SOURCES_BY_VERSION), default='v2.5.3')
    args = parser.parse_args(argv)
    base = args.report_dir.resolve()
    if not base.is_relative_to((ROOT/'.agentbus/tmp').resolve()):
        parser.error('Replay outputs must remain inside .agentbus/tmp')
    reports, sources = [], []
    for name, path in SOURCES_BY_VERSION[args.source_version].items():
        raw = path.read_bytes()
        reports.append(json.loads(raw))
        sources.append(dict(dataset=name, path=str(path), sha256=sha256(raw).hexdigest()))
    report = replay_reports(reports, args.source_version)
    report['sources'] = sources
    folder = base / uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    report['artifacts'] = str(folder)
    evaluation._write_json(folder/'report.json', report)
    (folder/'report.md').write_text(evaluation.markdown(report), encoding='utf-8')
    print(json.dumps(dict(title=report['title'], total=len(report['rows']), paid_calls=report['paid_calls'],
        statuses=dict(Counter(r['V2']['status'] for r in report['rows'])), artifacts=str(folder)), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
