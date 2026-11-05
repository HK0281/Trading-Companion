"""Append-only public v2.6.3 annotation review; pending changes never affect scores."""

from copy import deepcopy

from evaluation_tools import research_intent_v262_amendments as previous
from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation_cases import ROOT, require


VERSION = 'research-intent-v2.6.3-amendments-v1'
AMENDMENTS = ROOT/'data/evaluation/research-intent-v2.6.3-amendments.json'
PARENTS = (*previous.PARENTS, 'data/evaluation/research-intent-v2.6.2-amendments.json')
REVIEW_IDS = frozenset(('V2221', 'V2224', 'V2225', 'V25D066', 'V25D069', 'V25D077'))


def parent_hashes():
    return {name: previous.cases.public_sha256(ROOT/name) for name in PARENTS}


def load_amendments(path=None, *, require_approved=False):
    """Use frozen rows plus earlier review layers, never observed model answers."""
    original, _, _ = previous.cases.load_amendments(require_approved=True)
    _, baseline = previous.load_amendments(require_approved=require_approved)
    value = read_json(path or AMENDMENTS)
    require(type(value) is dict and value.keys() == {
        'version', 'request_message', 'parents_sha256_lf', 'changes', 'unchanged'}
        and value['version'] == VERSION and type(value['request_message']) is int
        and value['request_message'] == 459 and value['parents_sha256_lf'] == parent_hashes()
        and type(value['changes']) is list and value['unchanged'] == [],
        'Invalid v2.6.3 amendment parents/version')
    raw = {r['id']: r for r in original}
    base = {r['id']: r for r in baseline}
    effective, seen = deepcopy(base), set()
    for item in value['changes']:
        require(type(item) is dict and item.keys() == {'id', 'parent_row_sha256',
            'baseline_gold_sha256', 'field', 'before', 'after', 'rule', 'reason',
            'status', 'reviewer', 'approval_message'}, 'Invalid v2.6.3 amendment item')
        identifier = item['id']
        require(type(identifier) is str and identifier in REVIEW_IDS and identifier in raw
                and identifier not in seen and item['field'] == 'subject_source',
                'Duplicate/unknown v2.6.3 amendment or disallowed field')
        require(item['parent_row_sha256'] == digest(raw[identifier])
                and item['baseline_gold_sha256'] == digest(base[identifier]['gold'])
                and item['before'] == base[identifier]['gold']['subject_source'] == 'none'
                and item['after'] == 'previous' and item['rule'] == 'E'
                and type(item['reason']) is str and item['reason'].strip(),
                'V2.6.3 amendment fingerprint/before/after/rule mismatch')
        require(item['status'] in ('pending', 'approved'), 'Invalid v2.6.3 review status')
        approved = item['status'] == 'approved'
        require(item['reviewer'] == ('claude' if approved else None)
                and (type(item['approval_message']) is int and item['approval_message'] > 459
                     if approved else item['approval_message'] is None), 'Invalid v2.6.3 approval')
        if require_approved and not approved:
            raise ValueError('Claude approval required for v2.6.3 annotation review: '+identifier)
        if approved:
            effective[identifier]['gold']['subject_source'] = item['after']
        seen.add(identifier)
    require(seen == REVIEW_IDS, 'Incomplete v2.6.3 annotation review inventory')
    return value, [effective[r['id']] for r in baseline]


def summary():
    result = previous.summary()
    current, _ = load_amendments()
    result['v263'] = dict(file=AMENDMENTS.relative_to(ROOT).as_posix(),
        sha256_lf=previous.cases.public_sha256(AMENDMENTS),
        pending_reviews=sum(i['status'] == 'pending' for i in current['changes']),
        applied_changes=sum(i['status'] == 'approved' for i in current['changes']), **current)
    return result
