"""Reviewed development-only label amendments; never modify model input or frozen rows."""

from copy import deepcopy
from hashlib import sha256

from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation_cases import ROOT, valid_row


AMENDMENTS = ROOT / 'data/evaluation/research-intent-v2-dev-amendments.json'


def load_amendments(rows, corpus_sha256, path=AMENDMENTS):
    value = read_json(path, maximum=100_000)
    if (type(value) is not dict or value.keys() != {'version', 'parent_corpus_sha256', 'changes'}
            or value['version'] != 'intent-v2-dev-amendments-v1'
            or value['parent_corpus_sha256'] != corpus_sha256 or type(value['changes']) is not list):
        raise ValueError('Invalid development amendments parent/version')
    parents, changes = {r['id']: r for r in rows}, {}
    for change in value['changes']:
        if (type(change) is not dict or change.keys() != {'id', 'parent_row_sha256', 'field', 'before', 'after',
                'reason', 'reviewer', 'status', 'approval_message'}
                or change['reviewer'] != 'claude' or change['status'] != 'approved'
                or type(change['approval_message']) is not int or change['approval_message'] <= 0
                or type(change['reason']) is not str or not change['reason']
                or change['id'] not in parents or change['field'] not in ('objective_kind', 'indicator_mentions', 'research_kinds')):
            raise ValueError('Invalid or unreviewed development gold change')
        parent = parents[change['id']]
        if (parent['split'] != 'dev' or digest(parent) != change['parent_row_sha256']
                or parent['gold'][change['field']] != change['before'] or change['id'] in changes):
            raise ValueError('Amendments must match unique frozen development parents')
        modified = deepcopy(parent)
        modified['gold'][change['field']] = deepcopy(change['after'])
        valid_row(modified)
        changes[change['id']] = deepcopy(change)
    return changes


def revised_rows(rows, changes):
    revised = deepcopy(rows)
    for row in revised:
        change = changes.get(row['id'])
        if change:
            if row.get('split') != 'dev' or row['gold'][change['field']] != change['before']:
                raise ValueError('Scoring amendment mismatch')
            row['gold'][change['field']] = deepcopy(change['after'])
    return revised


def amendment_fingerprint():
    return sha256(AMENDMENTS.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
