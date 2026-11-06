"""离线统计公开请求的 ASCII 序列化长度；无模型调用，不是评测成绩。"""

import argparse
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
from trading_companion.intent_router_task import digest
from trading_companion.model import ModelRequestError
from trading_companion.tool_model import MAX_TOTAL_CHARACTERS, validate_tool_messages


def measure_request(question, context):
    """Use the same builder, serialization and validator as an actual candidate call."""
    messages = intent.request_messages(question, context)
    total = sum(len(json.dumps(tool.to_dict(), ensure_ascii=True)) for tool in (intent.TOOL,))
    total += sum(len(json.dumps(message.to_dict(), ensure_ascii=True)) for message in messages)
    error = None
    try:
        validate_tool_messages(messages, tools=(intent.TOOL,))
    except ModelRequestError as invalid:
        error = str(invalid)
    return dict(serialized_characters=total, headroom=MAX_TOTAL_CHARACTERS-total,
                valid=error is None, validation_error=error)


def measure_split(split):
    rows, _, prefixes, setups = evaluation.populations(split)
    measured, skipped = [], []
    for row in rows:
        if row['id'] in evaluation.NOT_APPLICABLE:
            skipped.append(dict(id=row['id'], reason='not_applicable'))
            continue
        try:
            context, _ = evaluation.build_context(row, prefixes, setups)
        except (ValueError, IndexError, KeyError):
            if row['id'] != 'V2002':
                raise ValueError('Unexpected public context failure; length coverage is incomplete') from None
            skipped.append(dict(id=row['id'], reason='context_unavailable'))
            continue
        local = intent.local_preflight(row['question'], context) is not None
        measured.append(dict(id=row['id'], local_zero_call=local, **measure_request(row['question'], context)))
    model = [item for item in measured if not item['local_zero_call']]
    maximum = lambda items: max(items, key=lambda item: item['serialized_characters'], default=None)
    return dict(total=len(rows), serialized=len(measured), local_zero_calls=len(measured)-len(model),
                model_request_cases=len(model), skipped=skipped, maximum_model_request=maximum(model),
                maximum_buildable=maximum(measured), requests=measured,
                passed=all(item['valid'] for item in measured))


def report_lengths(splits=('dev', 'regression')):
    if not splits or any(split not in ('dev', 'regression') for split in splits):
        raise ValueError('Only fixed public dev/regression inputs are accepted')
    with clean_environment(), offline_network(), \
            patch.object(intent, 'understand', side_effect=AssertionError('No model calls in length checks')), \
            patch.object(evaluation, '_live_model', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_evaluation_key_environment', side_effect=AssertionError('No credentials')):
        results = {split: measure_split(split) for split in splits}
    return dict(notice='离线请求长度核验，不是评测成绩；本地零调用题的请求仅为长度检查，不发送。',
        contract_version=intent.VERSION, guard_version=intent.GUARD_VERSION, limit=MAX_TOTAL_CHARACTERS,
        metric='sum(len(json.dumps(item.to_dict(), ensure_ascii=True))) over tools and messages; default separators',
        prompt_characters=len(intent.INSTRUCTION), prompt_sha256=sha256(intent.INSTRUCTION.encode('utf-8')).hexdigest(),
        tool_sha256=digest(intent.TOOL.to_dict()), source_sha256_lf=evaluation.fingerprints(), results=results,
        passed=all(result['passed'] for result in results.values()), paid_calls=0, new_model_results=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--split', choices=('dev', 'regression', 'all'), default='all')
    args = parser.parse_args(argv)
    report = report_lengths(('dev', 'regression') if args.split == 'all' else (args.split,))
    output = evaluation.ROOT/'.agentbus/tmp'/('v264-request-lengths-'+uuid4().hex+'.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    summary = dict(artifact=str(output), passed=report['passed'], paid_calls=0, limit=report['limit'],
        prompt_characters=report['prompt_characters'],
        results={split: {key: value for key, value in result.items() if key != 'requests'}
                 for split, result in report['results'].items()})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
