"""不是新成绩：公开原始响应的离线候选回放；不调用模型。"""

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

from evaluation_tools import research_intent_v26_evaluation as evaluation
from scripts.verify import clean_environment, offline_network
from trading_companion import research_intent_v2 as intent
from trading_companion.model import TokenUsage
from trading_companion.research_intent_evaluation_baseline import rule_observation
from trading_companion.research_intent_evaluation_cases import ROOT, input_state
from trading_companion.tool_model import ToolCall, ToolModelResponse

SOURCES = {split: ROOT/'.agentbus/evaluation-runs/research-intent-v2.6/public'/split/'report.json'
           for split in ('dev', 'regression')}
NOTICE = '不是新成绩：旧模型响应只改协议版本后离线回放；提示词改动未受测试，无新付费调用。'


def migrate_response(value, *, source_contract='research-intent-v2.6'):
    """Only change the version on a private copy; never add reference or semantic fields."""
    if source_contract not in ('research-intent-v2.6', 'research-intent-v2.6.1'):
        raise ValueError('Only the fixed public source contracts are accepted')
    expected = {'status', 'text', 'tool_calls', 'provider', 'requested_model', 'model', 'is_mock', 'finish_reason', 'usage'}
    if (type(value) is not dict or value.keys() != expected or type(value['tool_calls']) is not list
            or len(value['tool_calls']) != 1):
        raise ValueError('Missing or invalid archived response envelope')
    copied = deepcopy(value)
    call = copied['tool_calls'][0]
    if (type(call) is not dict or call.keys() != {'call_id', 'name', 'arguments'}
            or call['name'] != intent.TOOL_NAME or type(call['arguments']) is not dict
            or call['arguments'].get('version') != source_contract):
        raise ValueError('The response must match its fixed public source contract')
    call['arguments']['version'] = intent.VERSION
    copied['tool_calls'] = (ToolCall(**call),)
    copied['usage'] = TokenUsage(**copied['usage']) if copied['usage'] is not None else None
    return ToolModelResponse(**copied)


def replay_report(split, archived, *, source_contract='research-intent-v2.6'):
    """Use frozen inputs and neutral state builders; scoring data never enters checking."""
    if (source_contract not in ('research-intent-v2.6', 'research-intent-v2.6.1')
            or archived.get('plan', {}).get('contract_version') != source_contract):
        raise ValueError('Only the fixed public source reports are accepted')
    rows, effective, prefixes, setups = evaluation.populations(split)
    observed = {}
    expected = {r['id']: r for r in rows}
    for old in archived['rows']:
        if old['id'] not in expected or old['id'] in observed:
            raise ValueError('Unexpected or duplicate archived public case')
        row = expected[old['id']]
        if (old['question'] != row['question'] or old['context'] != row['context']
                or old.get('setup_id') != row.get('setup_id')):
            raise ValueError('Archived question/context does not match the frozen public input')
        observed[row['id']] = old
    output = []
    with clean_environment(), offline_network(), \
            patch.object(intent, 'understand', side_effect=AssertionError('No model calls in replay')), \
            patch.object(evaluation, '_live_model', side_effect=AssertionError('No credentials in replay')), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_evaluation_key_environment', side_effect=AssertionError('No credentials')):
        for row in rows:
            original = deepcopy(observed.get(row['id'], {}).get('V2', {}))
            result = evaluation._empty('not_measured', '缺少原始响应；未补造提案或重问')
            if row['id'] in evaluation.NOT_APPLICABLE:
                result = evaluation._empty('not_applicable', '保持事前批准的不适用项')
            else:
                try:
                    context, calls = evaluation.build_context(row, prefixes, setups)
                    local = intent.local_preflight(row['question'], context)
                    if original.get('path') == 'local_preflight' and original.get('attempts') == 0 and local is not None:
                        result = dict(evaluation._empty('ok', None, calls), fields=local.fields(), path='local_preflight',
                                      metadata=dict(provenance=list(local.provenance), diagnostics=deepcopy(local.diagnostics)))
                    elif original.get('response') is not None:
                        checked = evaluation.checked_result(row['question'],
                            migrate_response(original['response'], source_contract=source_contract), context)
                        if (original.get('format_retry', {}).get('attempted')
                                or original.get('response_origin') == 'repaired_tool_arguments'):
                            checked['raw'] = dict(status='failed', reason='tool_arguments_json_invalid', fields={})
                        result = dict(evaluation._empty(checked['status'], checked.get('reason'), calls), **{
                            k: v for k, v in checked.items() if k not in ('status', 'reason')}, path='archived_response')
                except (ValueError, IndexError, KeyError):
                    result = evaluation._empty('not_measured', '原始响应或中性前置不可回放；未补造')
            output.append(dict(row, effective_gold=deepcopy(effective[row['id']]), V2=result,
                A=rule_observation(input_state(row)), archived_observation=original,
                source_pointer=f"rows[id={row['id']}].V2", version_only_copy=True))
    report = evaluation.report_results(output, effective, dict(contract_version=intent.VERSION,
        guard_version=intent.GUARD_VERSION, mode='offline_replay', source_contract=source_contract))
    report.update(kind=intent.VERSION+'-offline-replay', title=NOTICE, notice=NOTICE,
        new_model_results=False, paid_calls=0, passed=None, eligible_for_switch=False,
        authenticity='archived_response_recheck', source_revision=archived['plan'].get('runtime_revision'),
        candidate_revision=evaluation._git('rev-parse', 'HEAD'),
        candidate_workspace_dirty=bool(evaluation._git('status', '--porcelain')),
        candidate_source_sha256_lf=evaluation.fingerprints(),
        archived_paid_calls=archived.get('paid_calls'),
        status_transitions=dict(Counter(original.get('status', 'missing')+' -> '+row['V2']['status']
            for row in output for original in (row['archived_observation'],))),
        limitations=['只检查已保存的最终响应，所有原始尝试和失败随 archived_observation 保留。',
            '没有原始响应不补造；响应副本只改版本，护栏纠正另记日志，不改写原始 operation、task 或 wants_plan。',
            '提示词改动须新一轮真实评估；回放计数不是新准确率，不据此判过门槛。'])
    return report


def run_cli(argv, *, source_contract, sources, prefix):
    parser = argparse.ArgumentParser(description=(
        f'不是新成绩：只读 {source_contract} 公开原始响应，离线检查 {intent.VERSION}；不调用模型。'),
        allow_abbrev=False)
    parser.add_argument('--split', choices=('dev', 'regression', 'all'), default='all')
    args = parser.parse_args(argv)
    folder = ROOT/'.agentbus/tmp'/(prefix+uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    summaries = []
    for split in (sources if args.split == 'all' else (args.split,)):
        source = sources[split]
        raw = source.read_bytes()
        report = replay_report(split, json.loads(raw.decode('utf-8')), source_contract=source_contract)
        report.update(source_report=str(source), source_sha256=sha256(raw).hexdigest())
        evaluation._write_json(folder/(split+'.json'), report)
        (folder/(split+'.md')).write_text(evaluation.markdown(report), encoding='utf-8')
        summaries.append(dict(split=split, rows=len(report['rows']), status_transitions=report['status_transitions'],
            offline_joint_count=report['score_views']['normalized_approved']['applicable']['joint_all_fields'],
            paid_calls=0, passed=None, source_sha256=report['source_sha256']))
    print(json.dumps(dict(notice=NOTICE, artifacts=str(folder), summaries=summaries), ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    return run_cli(argv, source_contract='research-intent-v2.6', sources=SOURCES, prefix='v261-offline-replay-')


if __name__ == '__main__':
    raise SystemExit(main())
