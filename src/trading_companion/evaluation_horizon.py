"""Reviewed, parent-hashed supplements without editing frozen evaluation questions."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def descriptor(kind):
    if kind not in ('decision', 'technical'):
        raise ValueError('Unknown horizon evaluation kind')
    path = ROOT / f'data/evaluation/{kind}-answer-horizon-delta-v1.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    parent = ROOT / f'data/evaluation/{kind}-answer-v1.jsonl'
    allowed = {'version', 'rule_baseline_version', 'parent_sha256_lf', 'reply', 'clarification_cases', 'expectation_changes', 'policy'}
    expected_cases = (['D01','D03','D04','D06','D08','D09','D10','D16','D18','D19'] if kind == 'decision' else ['T06','T07'])
    if (value.keys() != allowed or value['version'] != f'{kind}-answer-horizon-delta-v1'
            or value['parent_sha256_lf'] != sha256(parent.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            or value['reply'] != '中线' or value['rule_baseline_version'] != 'decision-horizon-v1'
            or value['clarification_cases'] != expected_cases
            or value['policy'] != 'one_reply_only_on_decision_horizon_required; preserve_original_questions_denominators_limits_and_all_actual_turns'):
        raise ValueError('Horizon evaluation parent mismatch')
    changes = value['expectation_changes']
    expected = [(i, '未来40个交易日') for i in expected_cases] + [(i, None) for i in ('D05','D15','D20')] if kind == 'decision' else []
    if (type(changes) is not list or len(changes) != len(expected)
            or any(type(c) is not dict or c.keys() != {'id','before','after','reason'}
                or (c['id'],c['after']) != e or c['before'] != '未来20个交易日'
                or type(c['reason']) is not str or not c['reason'] for c,e in zip(changes,expected))):
        raise ValueError('Unexpected horizon evaluation changes')
    return {**value, 'delta_sha256_lf': sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()}


def project_cases(cases, kind):
    value = descriptor(kind)
    output = deepcopy(cases)
    for change in value.get('expectation_changes', []):
        case = next(c for c in output if c['id'] == change['id'])
        if case['expected_horizon'] != change['before']:
            raise ValueError('Horizon expectation parent mismatch')
        case['expected_horizon'] = change['after']
    return output


def turn_record(result, question, elapsed, trace):
    return dict(question=question, status=result['status'], code=result['code'], elapsed_ms=elapsed,
        model_calls=result.get('model_calls_attempted', 0), quality=deepcopy((result.get('company_research') or {}).get('quality')),
        trace_run_id=(trace or {}).get('run_id'), result=deepcopy(result))


def complete_horizon(send, result, elapsed, trace, *, case_id, kind, can_send):
    """Exactly one fixed reply, only for the official code and declared case."""
    value = descriptor(kind)
    extras = []
    if result.get('code') == 'decision_horizon_required' and case_id in value['clarification_cases'] and can_send():
        result, duration, trace = send(value['reply'])
        extras.append(turn_record(result, value['reply'], duration, trace))
        elapsed += duration
    return result, elapsed, trace, extras
