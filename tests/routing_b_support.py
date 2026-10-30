"""Explicit B overlay on immutable A3 projections and handwritten B contracts."""
from copy import deepcopy
import json

from tests.routing_baseline_support import ROOT
from tests.routing_targets_support import baseline_digest, validate_contracts
from tests.routing_narrative_support import overlay as a3_overlay

PATH = ROOT/'tests/fixtures/request-routing-v5.json'
# Explicit B scope after reviewing the existing knowledge-interlude contract.
# The original pre-implementation expectations remain in their separate fixture.
CHANGED = {'company-pending-owns-replies': (3,4,5,6), 'technical-pending': (3,4,5)}
CONTRACTS = {
    ('company-pending-owns-replies',3): ('company_research','company_research_completed',(1,0,1,0)),
    ('company-pending-owns-replies',4): ('screening_edit','screening_list_unavailable',(0,0,0,0)),
    ('company-pending-owns-replies',5): ('technical_research','indicator_knowledge_answered',(0,0,0,0)),
    ('company-pending-owns-replies',6): ('company_research','company_research_completed',(2,0,1,1)),
    ('technical-pending',3): ('technical_research','research_completed',(2,0,0,1)),
    ('technical-pending',4): ('technical_research','indicator_knowledge_answered',(0,0,0,0)),
    ('technical-pending',5): ('company_research','company_research_completed',(2,0,1,1)),
}
DECISIONS = {
    ('company-pending-owns-replies',3): ('company_clarification','clarification.company'),
    ('company-pending-owns-replies',4): ('screening_edit','screening.edit'),
    ('company-pending-owns-replies',5): ('indicator_knowledge','knowledge.indicator'),
    ('company-pending-owns-replies',6): ('company_research','research.company'),
    ('technical-pending',3): ('technical_clarification','clarification.technical'),
    ('technical-pending',4): ('indicator_knowledge','knowledge.indicator'),
    ('technical-pending',5): ('company_research','research.company'),
}


def corpus():
    value = json.loads(PATH.read_text(encoding='utf-8'))
    if value['metadata']['corpus_version'] != 'request-routing-v5':
        raise AssertionError('Unknown B baseline')
    for name, digest in value['metadata']['parents_sha256_lf'].items():
        if baseline_digest((PATH.parent/name).read_bytes()) != digest:
            raise AssertionError('B parent changed: '+name)
    return value


def overlay(identifier, expected):
    output = a3_overlay(identifier, expected)
    scenario = next(s for s in corpus()['scenarios'] if s['id']==identifier)
    if len(output) != len(scenario['turns']):
        raise AssertionError('B turn count changed')
    changes=[]
    for index,(observation,turn) in enumerate(zip(output,scenario['turns'],strict=True),1):
        if turn['fields']:
            changes.append(index)
        for field in turn['fields']:
            keys=[key.replace('~1','/').replace('~0','~') for key in field['path'].strip('/').split('/')]
            parent=observation
            for key in keys[:-1]: parent=parent[key]
            key=keys[-1]
            before,after=field['before'],field['after']
            if isinstance(before,dict) and before.keys()=={'present','value'} and isinstance(after,dict) and after.keys()=={'present','value'}:
                if (key in parent) != before['present'] or parent.get(key) != before['value']:
                    raise AssertionError('B field presence changed: '+field['path'])
                if after['present']:parent[key]=deepcopy(after['value'])
                else:del parent[key]
            else:
                if parent[key] != before:raise AssertionError('B parent value changed: '+field['path'])
                parent[key]=deepcopy(after)
    if tuple(changes) != CHANGED.get(identifier,()):
        raise AssertionError('B differs from authored change scope: '+identifier)
    return output


def validate_current_contracts(scenario,observations,decisions):
    for index,(contract,observation,decision) in enumerate(zip(scenario['contracts'],observations,decisions,strict=True),1):
        expected=CONTRACTS.get((scenario['id'],index))
        if expected is None:
            validate_contracts({'id':scenario['id'],'contracts':[contract]},[observation],[decision])
            continue
        route,code,counts=expected
        actual=(observation['route'],observation['outcome']['response']['result']['code'],tuple(
            observation['calls'][key] for key in ('model_forwards','quote_reads','company_reads','daily_reads')))
        if actual != (route,code,counts):raise AssertionError(f'{scenario["id"]}/{index}: B contract differs: {actual}')
        intent,rule_id=DECISIONS[scenario['id'],index]
        if decision != dict(kind=route,intent=intent,rule_id=rule_id):raise AssertionError('B route reason changed')
