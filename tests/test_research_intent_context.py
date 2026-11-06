"""Program-state parity, isolation and full serialized request bounds."""

from dataclasses import replace
from copy import deepcopy
from datetime import timedelta, timezone
import json
import unittest
from unittest.mock import patch

from evaluation_tools import research_intent_v26_evaluation as evaluation
from tests.test_research_intent_v2 import context
from trading_companion.conversation_memory import TurnDigest
from trading_companion.research_intent_context import (
    IntentContextError, build_intent_context, prepare_intent_request,
)
from trading_companion.research_intent_v2 import TOOL
from trading_companion.model import ModelRequestError
from trading_companion.tool_model import validate_tool_messages
from pathlib import Path


def configuration(ctx):
    return dict(session=ctx.session, directory=ctx.directory, now=ctx.now,
        reference_date=ctx.reference_date, source=ctx.source, development=ctx.development,
        digests=tuple(TurnDigest(q, 'other', 'completed') for q in ctx.history),
        quote_source=ctx.quote_source, company_enabled=ctx.company_enabled,
        workflow_enabled=ctx.workflow_enabled, conversation_memory=ctx.conversation_memory)


def public_replay_inputs():
    # Read the preserved replay, never claim that the old lock admits new source.
    root = Path(__file__).resolve().parents[1]
    rows = [r['input'] for r in json.loads((root /
        'tests/fixtures/research-intent-v268-public-replay.json').read_text('utf-8'))['rows']]
    source = [json.loads(line) for line in (root/'data/evaluation/research-intent-v2.jsonl').read_text('utf-8').splitlines()]
    prefixes = evaluation.core.prefix_library([r for r in source if r['split'] == 'dev'])
    return rows, prefixes, evaluation.cases.load_setups()


class ProductionContextTests(unittest.TestCase):
    def test_complete_model_context_matches_every_public_setup_without_mutation(self):
        rows, prefixes, setups = public_replay_inputs()
        dev = Path(__file__).resolve().parents[1]/'data/evaluation/research-intent-v2.5-dev.jsonl'
        rows += [{key: row[key] for key in ('question', 'context', 'setup_id')}
                 for row in (json.loads(line) for line in dev.read_text('utf-8').splitlines())]
        examples = {row['setup_id']: row for row in rows}
        # Remaining public neutral setups use explicit synthetic state descriptors,
        # derived solely from their public setup notes, without sealed target rows.
        parents = {'saved-index': 'saved-index-star', 'pending-index': 'pending-index-star',
            'snapshot-old-rating': 'snapshot-old-period', 'snapshot-from-demo': 'snapshot-mode-switch',
            'snapshot-future-index': 'snapshot-future', 'snapshot-consumed-other': 'snapshot-consumed',
            'snapshot-source-other': 'snapshot-source-switch', 'snapshot-memory-off': 'snapshot-memory-off-dev'}
        for name, parent in parents.items():
            row = deepcopy(examples[parent])
            row.update(setup_id=name, question='仅检查公开中立状态，不执行目标轮')
            state = row['context']
            state['history'] = setups['entries'][name]['history']
            if name == 'saved-index':
                state['latest']['subjects'] = ['000300.SH']
            elif name == 'snapshot-from-demo':
                state['development'], state['latest']['development'] = False, True
            elif name == 'snapshot-future-index':
                state.update(has_company=False, has_index=True)
                state['latest'].update(kind='index', subjects=['000300.SH'], age_seconds=-1)
            elif name in ('snapshot-consumed-other', 'snapshot-source-other'):
                state['latest']['subjects'] = ['601318.SH']
            rows.append(row)
        covered = set()
        for row in rows:
            with self.subTest(setup=row['setup_id'], question=row['question']):
                ctx, _ = evaluation.build_context(row, prefixes, setups)
                before = dict(ctx.session.__dict__)
                generated = build_intent_context(**configuration(ctx))
                actual = generated.model_context()
                self.assertEqual(ctx.session.__dict__, before)
                self.assertIsNot(generated.session, ctx.session)
                self.assertIsNone(generated.session._turn_parent)
                self.assertEqual(actual, ctx.model_context())
                covered.add(row['setup_id'])
        self.assertEqual(covered, setups['entries'].keys())

    def test_history_uses_six_recent_questions_in_order_and_respects_memory_off(self):
        args = configuration(context())
        args['digests'] = tuple(TurnDigest(f'问题{i}', 'other', 'completed') for i in range(8))
        self.assertEqual(build_intent_context(**args).history, tuple(f'问题{i}' for i in range(2, 8)))
        self.assertEqual(build_intent_context(**{**args, 'conversation_memory': False}).model_context()['history'], [])
        self.assertEqual(build_intent_context(**{**args, 'reference_date': None}).reference_date,
                         args['now'].astimezone(timezone(timedelta(hours=8))).date())

    def test_expiry_checks_cannot_clear_the_original_or_its_turn_parent(self):
        ctx = context(previous=True)
        parent = ctx.session
        working = parent._fork_for_turn()
        before = dict(parent.__dict__), dict(working.__dict__)
        cfg = configuration(replace(ctx, session=working, now=ctx.now + timedelta(days=10)))
        generated = build_intent_context(**cfg)
        generated.model_context()
        self.assertEqual((parent.__dict__, working.__dict__), before)

    def test_ascii_serialized_count_matches_protocol_and_checks_the_exact_limit(self):
        cfg = configuration(context())
        request = prepare_intent_request('研究平安银行', **cfg)
        expected = sum(len(json.dumps(x.to_dict(), ensure_ascii=True)) for x in (*request.messages, TOOL))
        self.assertEqual(request.serialized_characters, expected)
        validate_tool_messages(request.messages, tools=(TOOL,))
        with patch('trading_companion.research_intent_context.MAX_TOTAL_CHARACTERS', expected):
            self.assertEqual(prepare_intent_request('研究平安银行', **cfg).serialized_characters, expected)
        with patch('trading_companion.research_intent_context.MAX_TOTAL_CHARACTERS', expected - 1):
            with self.assertRaises(IntentContextError) as caught:
                prepare_intent_request('研究平安银行', **cfg)
            self.assertEqual(caught.exception.code, 'context_request_too_large')

    def test_oversized_background_is_rejected_whole_without_truncating_questions(self):
        cfg = configuration(context())
        cfg['digests'] = tuple(TurnDigest('背景' * 1000, 'other', 'completed') for _ in range(6))
        with self.assertRaises(IntentContextError) as caught:
            prepare_intent_request('研究平安银行', **cfg)
        self.assertEqual(caught.exception.code, 'context_request_too_large')
        self.assertEqual(len(cfg['digests'][0].question), 2000)
        for question in ('', 'x' * 2001, '问题\x00'):
            with self.subTest(question=question[:10]), self.assertRaises(IntentContextError):
                prepare_intent_request(question, **configuration(context()))
        with patch('trading_companion.research_intent_context.validate_tool_messages',
                   side_effect=ModelRequestError('private')):
            with self.assertRaises(IntentContextError) as caught:
                prepare_intent_request('问题', **configuration(context()))
            self.assertEqual(str(caught.exception), 'intent_context_invalid')

