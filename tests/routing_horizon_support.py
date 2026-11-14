"""v7 preserves 117 original messages and declares horizon transitions independently."""
from copy import deepcopy
import json

from tests.routing_baseline_support import ROOT, read_baseline, project_observation
from tests.routing_targets_support import TARGET, baseline_digest, field_deltas, observe_target_scenario
from tests import routing_relaxed_support as parent

PATH = ROOT / 'tests/fixtures/request-routing-v7.json'
# Authored from the approved missing-horizon policy, before the v7 capture.
# Values: result code, model forwards, daily reads. No supplement is inserted.
CONTRACTS = {
    ('decision-direct-and-history', 1): ('decision_horizon_required', 1, 0),
    ('decision-direct-and-history', 2): ('product_result_unavailable', 0, 0),
    ('decision-paraphrases', 2): ('decision_horizon_required', 1, 0),
    ('decision-paraphrases', 3): ('decision_horizon_required', 0, 0),
    ('decision-paraphrases', 4): ('decision_horizon_required', 0, 0),
    ('decision-identity-boundary', 2): ('decision_horizon_required', 1, 0),
}


def validate_current_decisions(scenario, observed):
    from tests.routing_narrative_support import current_decision_contracts
    from tests.routing_decision_support import validate
    current = current_decision_contracts(scenario)
    for index, contract in enumerate(current['contracts'], 1):
        target = CONTRACTS.get((scenario['id'], index))
        if target:
            code, models, daily = target
            contract.update(code=code, version=None, model_forwards=models, daily_reads=daily)
    validate(current, observed)


def corpus():
    value = read_baseline(PATH)
    for name, digest in value['metadata']['parents_sha256_lf'].items():
        if baseline_digest((PATH.parent / name).read_bytes()) != digest:
            raise AssertionError('Horizon parent changed: ' + name)
    return value


def overlay(identifier, expected):
    output = parent.overlay(identifier, expected)
    scenario = next(s for s in corpus()['scenarios'] if s['id'] == identifier)
    if len(output) != len(scenario['turns']):
        raise AssertionError('Horizon original turn count changed')
    for observation, turn in zip(output, scenario['turns'], strict=True):
        for field in turn['fields']:
            keys = [key.replace('~1', '/').replace('~0', '~') for key in field['path'].split('/')[1:]]
            owner = observation
            for key in keys[:-1]:
                owner = owner[key]
            key, before, after = keys[-1], field['before'], field['after']
            if isinstance(before, dict) and before.keys() == {'present', 'value'}:
                if (key in owner) != before['present'] or owner.get(key) != before['value']:
                    raise AssertionError('Horizon parent presence changed: ' + field['path'])
                if after['present']:
                    owner[key] = deepcopy(after['value'])
                else:
                    del owner[key]
            else:
                if owner[key] != before:
                    raise AssertionError('Horizon parent value changed: ' + field['path'])
                owner[key] = deepcopy(after)
    return output


def capture():
    if PATH.exists():
        raise ValueError('Refusing to overwrite horizon baseline')
    from tests.routing_decision_support import observe
    from tests.routing_technical_support import validate_current_contracts
    originals = read_baseline(TARGET)['scenarios'] + read_baseline(ROOT/'tests/fixtures/request-routing-v3.json')['scenarios']
    scenarios = []
    for scenario in originals:
        before = parent.overlay(scenario['id'], scenario['observed'])
        if scenario['id'].startswith('decision-'):
            observed = observe(scenario)
            validate_current_decisions(scenario, observed)
        else:
            observed, decisions = observe_target_scenario(scenario)
            validate_current_contracts(scenario, observed, decisions)
        turns = []
        for index, (old, full) in enumerate(zip(before, observed, strict=True), 1):
            fields = field_deltas(old, project_observation(full))
            turns.append(dict(index=index, fields=fields))
        scenarios.append(dict(id=scenario['id'], turns=turns))
    payload = dict(metadata=dict(corpus_version='request-routing-v7', base_revision='639d33e',
        source_kind='uncommitted_worktree_candidate', parents_sha256_lf={name:baseline_digest((PATH.parent/name).read_bytes())
            for name in ('request-routing-v2.json','request-routing-v3.json','request-routing-v6.json','request-routing-validation-v1.json')}),
        scenarios=scenarios)
    PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    capture()
