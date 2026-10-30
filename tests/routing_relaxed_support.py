"""Versioned validation-only deltas; all historical observers remain frozen."""
from copy import deepcopy
import json

from tests.routing_baseline_support import ROOT, project_observation, read_baseline
from tests.routing_targets_support import TARGET, baseline_digest, field_deltas, observe_target_scenario
from tests import routing_technical_support as parent

PATH = ROOT / 'tests/fixtures/request-routing-validation-v1.json'


def corpus():
    value = read_baseline(PATH)
    for name, digest in value['metadata']['parents_sha256_lf'].items():
        if baseline_digest((PATH.parent / name).read_bytes()) != digest:
            raise AssertionError('Validation parent changed: ' + name)
    return value


def overlay(identifier, expected):
    output = parent.overlay(identifier, expected)
    scenario = next(s for s in corpus()['scenarios'] if s['id'] == identifier)
    if len(output) != len(scenario['turns']):
        raise AssertionError('Validation turn count changed')
    for observation, turn in zip(output, scenario['turns'], strict=True):
        for field in turn['fields']:
            keys = [key.replace('~1', '/').replace('~0', '~') for key in field['path'].split('/')[1:]]
            owner = observation
            for key in keys[:-1]:
                owner = owner[key]
            key, before, after = keys[-1], field['before'], field['after']
            if isinstance(before, dict) and before.keys() == {'present', 'value'}:
                if (key in owner) != before['present'] or owner.get(key) != before['value']:
                    raise AssertionError('Validation parent presence changed: ' + field['path'])
                if after['present']:
                    owner[key] = deepcopy(after['value'])
                else:
                    del owner[key]
            else:
                if owner[key] != before:
                    raise AssertionError('Validation parent value changed: ' + field['path'])
                owner[key] = deepcopy(after)
    return output


def capture():
    """Explicit candidate capture, refusing to replace any frozen data."""
    if PATH.exists():
        raise ValueError('Refusing to overwrite validation baseline')
    from tests.routing_decision_support import observe, validate
    from tests.routing_narrative_support import current_decision_contracts
    originals = read_baseline(TARGET)['scenarios'] + read_baseline(ROOT/'tests/fixtures/request-routing-v3.json')['scenarios']
    scenarios = []
    lines = ['# 回答核对放宽后的117轮差异', '',
             '父基线为v6；v1～v6和历史观察器保持原字节。此层不占用期限功能预留的v7。',
             '质量统计升级为v2；提示词精简后预算内可多保留同轮已有证据，部分公开引用增加。输出和保存摘要随之变化；以下逐轮列全字段差异。路由、调用预算和独立指标范围仍按既有契约验证。', '']
    for scenario in originals:
        before = parent.overlay(scenario['id'], scenario['observed'])
        if scenario['id'].startswith('decision-'):
            observed = observe(scenario)
            validate(current_decision_contracts(scenario), observed)
        else:
            observed, decisions = observe_target_scenario(scenario)
            parent.validate_current_contracts(scenario, observed, decisions)
        turns = []
        for index, (old, full) in enumerate(zip(before, observed, strict=True), 1):
            new = project_observation(full)
            fields = field_deltas(old, new)
            # These must be unchanged even if prose/quality fingerprints change.
            for field in ('route', 'executor_chain', 'calls'):
                if old[field] != new[field]:
                    raise AssertionError(f'{scenario["id"]}/{index}: changed {field}')
            turns.append(dict(index=index, fields=fields))
            paths = ', '.join('`'+f['path']+'`' for f in fields)
            lines.append(f'- {scenario["id"]}/{index}：' + (f'v2质量/同轮已有证据引用及响应保存摘要：{paths}。' if fields else '无差异。'))
        scenarios.append(dict(id=scenario['id'], turns=turns))
    payload = dict(metadata=dict(corpus_version='request-routing-validation-v1', base_revision='b5d77eb',
        source_kind='uncommitted_worktree_candidate', parents_sha256_lf={name: baseline_digest((PATH.parent/name).read_bytes())
            for name in ('request-routing-v2.json', 'request-routing-v3.json', 'request-routing-v6.json')}), scenarios=scenarios)
    PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    (ROOT/'docs/answer-validation-routing-deltas.md').write_text('\n'.join(lines)+'\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    capture()
