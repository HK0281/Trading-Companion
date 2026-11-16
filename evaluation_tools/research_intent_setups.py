"""Public research-intent development rows and their neutral program-state setups.

The v2.5/v2.6 sealed evaluations that first built these setups were one-time runs and have
been retired (tag `research-intent-v2.6-freeze-valid`). The integration acceptance tool and
the production-context tests still need the same public rows and the same state construction,
so only that part is kept here. The target question is never executed or used to pick a seed.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path

from evaluation_tools import research_intent_v2_evaluation as core
from evaluation_tools.research_intent_evaluation_cases import ROOT, require, valid_context
from trading_companion.intent_router_task import read_json
from trading_companion.typesafe_jev import decode

DEV = ROOT / 'data/evaluation/research-intent-v2.6-dev.jsonl'
SETUPS = ROOT / 'data/evaluation/research-intent-v2.6-setups.json'
PREFIX_SOURCE = ROOT / 'data/evaluation/research-intent-v2.jsonl'
DEV_COUNT = 64
SETUP_KINDS = ('snapshot', 'company_saved', 'company_horizon', 'company_horizon_source',
               'company_horizon_mode', 'company_horizon_identity', 'conflict', 'index_horizon', 'index_saved')


def load_cases(path=DEV, *, expected_sha256=None):
    """Read the 64 public development rows exactly as they were approved."""
    raw = Path(path).read_bytes().replace(b'\r\n', b'\n')
    require(0 < len(raw) <= 2_000_000, 'Invalid public corpus size')
    if expected_sha256 is not None:
        require(sha256(raw).hexdigest() == expected_sha256, 'Public corpus fingerprint mismatch')
    rows = [decode(line) for line in raw.splitlines()]
    require(len(rows) == DEV_COUNT and len({r['id'] for r in rows}) == DEV_COUNT, 'Public development count mismatch')
    require(all(r['split'] == 'dev' for r in rows), 'Public reader only accepts dev')
    return rows


def load_setups(path=SETUPS):
    value = read_json(path)
    require(type(value) is dict and {'version', 'review', 'entries'} <= value.keys()
            and type(value['entries']) is dict and value['review'].get('status') == 'approved', 'Invalid neutral setups')
    for key, entry in value['entries'].items():
        require(type(key) is str and type(entry) is dict and entry.keys() == {'kind', 'history', 'note'}
                and entry['kind'] in SETUP_KINDS and type(entry['history']) is list
                and all(type(s) is str and s for s in entry['history'])
                and type(entry['note']) is str and entry['note'], 'Invalid neutral setup')
    return value


def public_prefixes(path=PREFIX_SOURCE):
    """Reviewed setup scripts from the original v2 development rows; no gold is used."""
    rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines()]
    return core.prefix_library([row for row in rows if row['split'] == 'dev'])


def state_input(row):
    """Construction input contains no gold, evidence, ID, split or variant labels."""
    return deepcopy({k: row[k] for k in ('question', 'context', 'setup_id')})


def row_context(row, prefixes, setups):
    """Build the program state for a row; the builder never sees the target wording or gold."""
    state = dict(question='仅检查前置状态，不执行目标轮', context=deepcopy(row['context']))
    if 'setup_id' in row:
        state['setup_id'] = row['setup_id']
        return build_context(state, setups, prefixes)
    return core.program_context(state, prefixes)


def _index_context(value, entry):
    from trading_companion.index_agent import run_index_research_turn
    from trading_companion.index_demo import index_demo_response
    from trading_companion.index_source import synthetic_index_bars
    from trading_companion.research_intent_v2 import IntentContext
    from trading_companion.research_session import ResearchSession
    from trading_companion.security_directory import SecurityDirectory
    from trading_companion.workflow_evaluation_fixtures import SECURITIES, NOW, REFERENCE_DATE

    class FixedIndexModel:
        calls = 0

        def complete(self, messages, *, tools):
            self.calls += 1
            response = index_demo_response(messages, tools)
            if response is None:
                raise ValueError('Unexpected fixed index setup call')
            return response

    class FixedIndexSource:
        def __init__(self, request):
            self.request = request

        def read_daily_bars(self):
            return synthetic_index_bars(self.request)

    pending_seed = entry['kind'] == 'index_horizon'
    require(value['source'] == 'tencent' and not value['development'] and value['company_enabled']
            and value['workflow_enabled'] and value['conversation_memory'] and not value['shortlist']
            and value['pending'] == ('index_horizon' if pending_seed else 'none')
            and not value['has_company'] and not value['has_technical']
            and value['has_index'] == (not pending_seed), 'Index setup descriptor mismatch')
    session, model = ResearchSession(), FixedIndexModel()
    directory = SecurityDirectory(SECURITIES, 'v25-neutral-index-setup', NOW, False)
    for question in entry['history']:
        result = run_index_research_turn(question, model=model, directory=directory, session=session,
            now=NOW, reference_date=REFERENCE_DATE, source_factory=FixedIndexSource)
        require(result.status in ('needs_clarification', 'completed', 'limited'), 'Index seed did not build')
    pending = session.index_pending
    if entry['kind'] == 'index_horizon':
        require(pending is not None and pending.stage == 'horizon' and session.latest_research is None,
                'Index seed did not create a native horizon pending')
        require(value['latest'] is None and all(value['pending_binding'].values()), 'Index pending descriptor mismatch')
        session.index_pending = replace(pending, created_at=NOW-timedelta(seconds=value['pending_age_seconds']))
    else:
        require(session.index_saved is not None and session.latest_research is not None, 'Index seed did not publish')
        latest = session.latest_research
        actual = dict(kind=latest.kind, subjects=[latest.scope.instrument_id], age_seconds=0,
                      source=latest.source, development=latest.development, current=session._subject_current,
                      horizon=latest.scope.horizon)
        require(value['latest'] == actual, 'Index publication descriptor mismatch')
    return IntentContext(session, directory, NOW, REFERENCE_DATE, value['source'], value['development'],
        tuple(value['history']), None, value['company_enabled'], value['workflow_enabled'],
        value['conversation_memory']), model.calls


def build_context(state, setups, prefixes):
    require(type(state) is dict and state.keys() == {'question', 'context', 'setup_id'}, 'Construction input schema')
    require(state['setup_id'] in setups['entries'], 'Unknown neutral setup')
    entry, value = setups['entries'][state['setup_id']], state['context']
    valid_context(value)
    require(value['history'] == entry['history'], 'Neutral setup/history mismatch')
    kind = entry['kind']
    if kind.startswith('index_'):
        return _index_context(value, entry)
    seed = deepcopy(value)
    if kind.startswith('company_horizon') or kind == 'conflict':
        require(value['pending'] == ('conflict' if kind == 'conflict' else 'company_horizon'), 'Pending kind mismatch')
        require(value['source'] == ('akshare_eastmoney' if kind == 'company_horizon_source' else 'tencent')
                and value['development'] == (kind == 'company_horizon_mode'), 'Pending mutation descriptor mismatch')
        seed.update(source='tencent', development=False)
    # The target question is never executed, interpreted, or used to select a seed.
    context, calls = core.program_context(dict(question='仅构建程序状态，不执行目标轮', context=seed), prefixes)
    if kind == 'company_saved':
        require(context.session.product_saved is not None, 'Saved setup did not build a native cache')
    context = replace(context, source=value['source'], development=value['development'])
    if kind == 'company_horizon_source':
        require(value['source'] != 'tencent', 'Source fault must switch the current source')
    elif kind == 'company_horizon_mode':
        require(value['development'] is True, 'Mode fault must switch the current mode')
    elif kind == 'company_horizon_identity':
        from trading_companion.security_directory import SecurityDirectory, Security
        securities = tuple(Security(s.instrument_id, s.name + '（目录已更新）', s.aliases)
                           if s.instrument_id == '000001.SZ' else s for s in context.directory.securities)
        context = replace(context, directory=SecurityDirectory(securities, 'v25-directory-switch', context.now, False))
    return context, calls
