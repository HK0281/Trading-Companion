"""Public v2.6.2 annotation addendum; only individually approved values take effect."""

from copy import deepcopy

from evaluation_tools import research_intent_v26_cases as cases
from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation_cases import ROOT, require


VERSION = 'research-intent-v2.6.2-amendments-v1'
AMENDMENTS = ROOT/'data/evaluation/research-intent-v2.6.2-amendments.json'
PARENTS = (*cases.PARENT_FILES, 'data/evaluation/research-intent-v2.6-amendments.json',
           'data/evaluation/research-intent-v2.6-freeze.json')
REVIEW_IDS = frozenset((*('V'+str(n) for n in range(2216, 2221)), 'V25H082', 'V25H088',
                      'V25D059', 'V25D060', 'V25D061', 'V25H086', 'V25H087', 'V25H089'))


def parent_hashes():
    return {name: cases.public_sha256(ROOT/name) for name in PARENTS}


def load_amendments(path=None, *, require_approved=False):
    """Read only frozen public rows, never model observations or private material."""
    original, _, baseline = cases.load_amendments(require_approved=True)
    value = read_json(path or AMENDMENTS)
    require(type(value) is dict and value.keys() == {
        'version', 'request_message', 'parents_sha256_lf', 'changes', 'unchanged'}
        and value['version'] == VERSION and type(value['request_message']) is int
        and value['request_message'] == 456 and value['parents_sha256_lf'] == parent_hashes()
        and type(value['changes']) is list and type(value['unchanged']) is list,
        'Invalid v2.6.2 amendment parents/version')
    raw = {r['id']: r for r in original}
    base = {r['id']: r for r in baseline}
    effective, seen = deepcopy(base), set()
    for group in ('changes', 'unchanged'):
        for item in value[group]:
            require(type(item) is dict and item.keys() == {'id', 'parent_row_sha256',
                'baseline_gold_sha256', 'field', 'before', 'after', 'rule', 'reason',
                'status', 'reviewer', 'approval_message'}, 'Invalid v2.6.2 amendment item')
            identifier = item['id']
            require(type(identifier) is str and identifier in REVIEW_IDS and identifier in raw
                    and identifier not in seen and item['field'] == 'subject_source',
                    'Duplicate/unknown v2.6.2 amendment or disallowed field')
            require(item['parent_row_sha256'] == digest(raw[identifier])
                    and item['baseline_gold_sha256'] == digest(base[identifier]['gold'])
                    and type(item['before']) is str and type(item['after']) is str
                    and item['before'] == base[identifier]['gold']['subject_source']
                    and item['rule'] == 'E' and type(item['reason']) is str and item['reason'].strip(),
                    'V2.6.2 amendment fingerprint/before/rule mismatch')
            require((item['before'], item['after']) == ('none', 'previous') if group == 'changes'
                    else item['before'] == item['after'] == 'none',
                    'Only subject_source none-to-previous or an explicit unchanged none is allowed')
            require(item['status'] in ('pending', 'approved'), 'Invalid v2.6.2 review status')
            approved = item['status'] == 'approved'
            require(item['reviewer'] == ('claude' if approved else None)
                    and (type(item['approval_message']) is int and item['approval_message'] > 456
                         if approved else item['approval_message'] is None), 'Invalid v2.6.2 approval')
            if require_approved and not approved:
                raise ValueError('Claude approval required for v2.6.2 annotation review: '+identifier)
            if approved and group == 'changes':
                effective[identifier]['gold']['subject_source'] = item['after']
            seen.add(identifier)
    require(seen == REVIEW_IDS, 'Incomplete v2.6.2 annotation review inventory')
    return value, [effective[r['id']] for r in baseline]


def summary():
    _, previous, _ = cases.load_amendments(require_approved=True)
    current, _ = load_amendments()
    items = [*current['changes'], *current['unchanged']]
    return dict(label='公开标注修订（不计模型改善）', model_improvement=False,
        previous=dict(file=cases.AMENDMENTS.relative_to(ROOT).as_posix(),
            sha256_lf=cases.public_sha256(cases.AMENDMENTS), **previous),
        v262=dict(file=AMENDMENTS.relative_to(ROOT).as_posix(), sha256_lf=cases.public_sha256(AMENDMENTS),
            pending_reviews=sum(i['status'] == 'pending' for i in items),
            applied_changes=sum(i['status'] == 'approved' for i in current['changes']), **current))
