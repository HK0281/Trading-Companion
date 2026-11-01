"""v2.4 原始响应经 v2.5 程序回放，不是新模型成绩。禁止网络和模型调用。"""

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
from trading_companion.intent_router_task import digest
from trading_companion.model import TokenUsage
from trading_companion.research_intent_evaluation import same
from trading_companion.research_intent_evaluation_baseline import rule_observation
from trading_companion.research_intent_evaluation_cases import ROOT, input_state
from trading_companion.tool_model import ToolCall, ToolModelResponse

SOURCES = {
    'dev': ROOT/'.agentbus/tmp/intent-v24-live-dev-run1/4a6b8973437046a4a08e74b25eb532b9/report.json',
    'regression': ROOT/'.agentbus/evaluation-runs/research-intent-v2.4/holdout/report.json',
}


def migrate_response(value):
    """An explicit version-only copy; retain the original response in the replay row."""
    expected = {'status', 'text', 'tool_calls', 'provider', 'requested_model', 'model', 'is_mock', 'finish_reason', 'usage'}
    if type(value) is not dict or value.keys() != expected or type(value['tool_calls']) is not list:
        raise ValueError('Missing or invalid archived response envelope')
    copied = deepcopy(value)
    for call in copied['tool_calls']:
        if call.keys() != {'call_id', 'name', 'arguments'} or call['arguments'].get('version') != 'research-intent-v2.4':
            raise ValueError('Replay accepts only archived v2.4 proposals')
        call['arguments']['version'] = intent.VERSION
    copied['tool_calls'] = tuple(ToolCall(**call) for call in copied['tool_calls'])
    copied['usage'] = TokenUsage(**copied['usage']) if copied['usage'] is not None else None
    return ToolModelResponse(**copied)


def replay_reports(reports):
    """Reports are explicit old observations; neither gold nor target scripts enter checking."""
    rows, effective, prefixes, setups = evaluation.populations('all')
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
    output = []
    with clean_environment(), offline_network(), \
            patch.object(intent, 'understand', side_effect=AssertionError('Replay never calls a model')), \
            patch.object(evaluation.core, 'understand', side_effect=AssertionError('Replay never calls a model')), \
            patch.object(evaluation.core, 'predict', side_effect=AssertionError('Replay never predicts a target')), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_evaluation_key_environment', side_effect=AssertionError('No credentials')), \
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
                    elif original.get('response') is not None:
                        response = migrate_response(original['response'])
                        result = dict(evaluation._empty('ok', None, calls),
                            **evaluation.checked_result(row['question'], response, context), path='archived_response',
                            context_sha256=digest(context.model_context()))
                    else:
                        result['setup_calls'] = calls
                except (ValueError, IndexError, KeyError):
                    result = evaluation._empty('not_measured', '原始响应或已审核前置不可回放；未生成替代提案')
            output.append(dict(row, effective_gold=effective[row['id']], V2=result,
                A=rule_observation(input_state(row)), archived_status=original.get('status', 'missing'),
                archived_reason=original.get('reason'), archived_attempts=original.get('attempts'),
                archived_fields=original.get('fields', {}), archived_response=original.get('response'),
                changed_fields=[key for key in evaluation.GOLD_FIELDS if not same(
                    result['fields'].get(key, evaluation.UNKNOWN), original.get('fields', {}).get(key, evaluation.UNKNOWN))],
                replay_migration=dict(field='version', before='research-intent-v2.4', after=intent.VERSION,
                                      applied=result.get('path') == 'archived_response',
                                      original_response_unchanged=True)))
    report = evaluation.report_results(output, effective, dict(kind='offline_replay', target_calls=0,
        model_factory_calls=0, retries=0), notice=evaluation.REPLAY_NOTICE)
    report.update(passed=None, thresholds=None, new_model_results=False,
                  note='仅回放旧提案及新本地前置，不检验新提示词；缺响应、失败和未测仍留在240题分母。')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, default=ROOT/'.agentbus/tmp/intent-v25-replay')
    args = parser.parse_args(argv)
    base = args.report_dir.resolve()
    if not base.is_relative_to((ROOT/'.agentbus/tmp').resolve()):
        parser.error('Replay outputs must remain inside .agentbus/tmp')
    reports, sources = [], []
    for name, path in SOURCES.items():
        raw = path.read_bytes()
        reports.append(json.loads(raw))
        sources.append(dict(dataset=name, path=str(path), sha256=sha256(raw).hexdigest()))
    report = replay_reports(reports)
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
