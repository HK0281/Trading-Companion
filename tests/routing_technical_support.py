"""Explicit v6 overlay on frozen v5, with independently authored technical targets."""
from copy import deepcopy
import json
from trading_companion.multi_contracts import IndicatorSpec

from tests.routing_baseline_support import ROOT
from tests.routing_targets_support import baseline_digest, validate_contracts
from tests import routing_b_support as parent

PATH = ROOT / 'tests/fixtures/request-routing-v6.json'
EXPECTED = ROOT / 'tests/fixtures/request-routing-technical-expected-v1.json'
# Route, result code, model/quote/company/daily calls, semantic intent, rule.
# These follow the approved plan, independently of field capture.
CONTRACTS = {
    ('technical-pending', 2): ('company_research', 'product_instrument_needs_clarification', (1,0,0,0), 'technical_research', 'research.technical'),
    ('technical-pending', 3): ('company_research', 'company_research_completed', (1,0,0,1), 'company_clarification', 'clarification.company'),
    ('indicator-pending', 1): ('company_research', 'company_research_completed', (2,0,0,1), 'technical_research', 'research.technical'),
    ('indicator-pending', 2): ('company_research', 'indicator_edit_ambiguous', (0,0,0,0), 'technical_research', 'research.technical'),
    ('indicator-pending', 4): ('company_research', 'company_research_completed', (1,0,0,1), 'company_clarification', 'clarification.company'),
    ('company-technical-capabilities', 2): ('company_research', 'company_research_completed', (2,0,0,1), 'technical_research', 'research.technical'),
    ('company-technical-capabilities', 3): ('company_research', 'company_research_completed', (2,0,0,1), 'technical_research', 'research.technical'),
    ('company-technical-capabilities', 4): ('company_research', 'company_research_completed', (2,0,0,1), 'technical_research', 'research.technical'),
    ('technical-edit-and-commands', 1): ('company_research', 'company_research_completed', (2,0,0,1), 'technical_research', 'research.technical'),
    ('technical-edit-and-commands', 2): ('company_research', 'company_research_completed', (1,0,0,1), 'technical_research', 'research.technical'),
    ('technical-edit-and-commands', 4): ('company_research', 'product_saved_result', (0,0,0,0), 'history', 'session.history'),
    ('selected-technical-followup', 2): ('screening_followup', 'company_research_completed', (2,0,0,1), 'screening_followup', 'screening.followup'),
    ('selected-technical-followup', 3): ('company_research', 'company_research_completed', (1,0,0,1), 'technical_research', 'research.technical'),
    ('failed-research-consumes-current', 2): ('company_research', 'company_research_unavailable', (1,0,0,1), 'technical_research', 'research.technical'),
}
CALLS = ('model_forwards','quote_reads','company_reads','daily_reads')


def corpus():
    value = json.loads(PATH.read_text(encoding='utf-8'))
    if value['metadata']['corpus_version'] != 'request-routing-v6':
        raise AssertionError('Unknown technical baseline')
    for name, digest in value['metadata']['parents_sha256_lf'].items():
        if baseline_digest((PATH.parent / name).read_bytes()) != digest:
            raise AssertionError('Technical parent changed: ' + name)
    return value


def overlay(identifier, expected):
    output = parent.overlay(identifier, expected)
    scenario = next(s for s in corpus()['scenarios'] if s['id'] == identifier)
    if len(output) != len(scenario['turns']):
        raise AssertionError('Technical turn count changed')
    for observation, turn in zip(output, scenario['turns'], strict=True):
        for field in turn['fields']:
            keys = [key.replace('~1','/').replace('~0','~') for key in field['path'].split('/')[1:]]
            owner = observation
            for key in keys[:-1]:
                owner = owner[key]
            key, before, after = keys[-1], field['before'], field['after']
            if isinstance(before, dict) and before.keys() == {'present','value'} and isinstance(after, dict) and after.keys() == {'present','value'}:
                if (key in owner) != before['present'] or owner.get(key) != before['value']:
                    raise AssertionError('Technical parent presence changed: ' + field['path'])
                if after['present']:
                    owner[key] = deepcopy(after['value'])
                else:
                    del owner[key]
            else:
                if owner[key] != before:
                    raise AssertionError('Technical parent value changed: ' + field['path'])
                owner[key] = deepcopy(after)
    return output


def validate_current_contracts(scenario, observations, decisions):
    contracts = deepcopy(scenario['contracts'])
    for index, contract in enumerate(contracts, 1):
        key = scenario['id'], index
        if key in parent.CONTRACTS:
            route, code, counts = parent.CONTRACTS[key]
            intent, rule = parent.DECISIONS[key]
            contract.update(route=route, code=code, calls=dict(zip(CALLS, counts, strict=True)),
                            decision=dict(kind=route, intent=intent, rule_id=rule))
        if key in CONTRACTS:
            route, code, counts, intent, rule = CONTRACTS[key]
            contract.update(route=route, code=code, calls=dict(zip(CALLS, counts, strict=True)),
                            decision=dict(kind=route, intent=intent, rule_id=rule))
    # Keep the original generic/knowledge-state checks. The historical B2-specific
    # legacy-pending assertion is superseded only for this migrated scenario.
    validate_contracts({'id': 'technical-current:' + scenario['id'], 'contracts': contracts}, observations, decisions)
    validate_authored_targets(scenario['id'], observations, decisions)


def validate_authored_targets(identifier, observations, decisions):
    targets = json.loads(EXPECTED.read_text(encoding='utf-8'))
    for target in targets['assertions']:
        if target['scenario'] != identifier:
            continue
        index = target['turn'] - 1
        observation, decision = observations[index], decisions[index]
        result = observation['outcome'].get('response', {}).get('result', {})
        for key in ('kind', 'intent'):
            if key in target and (decision or {}).get(key) != target[key]:
                raise AssertionError(f'{identifier}/{index+1}: authored {key} differs')
        if 'code' in target and result.get('code') != target['code']:
            raise AssertionError('Authored completion/clarification changed')
        for key in CALLS:
            if key in target and observation['calls'][key] != target[key]:
                raise AssertionError('Authored read budget changed')
        if 'indicators' in target:
            plan = observation['after']['product_saved']['plan']
            indicators = plan['indicators']
            actual = [IndicatorSpec(**item).indicator_id for item in indicators]
            if actual != target['indicators']:
                raise AssertionError(f'Authored indicator scope changed: {actual}')
