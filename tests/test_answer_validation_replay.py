from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import replay_answer_validation as replay


class AnswerReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent/'fixtures/answer-validation-replay-v1.json').read_text(encoding='utf-8'))

    def inputs(self):
        f = self.fixture
        identity = dict(case_id='fake', phase='target', turn='message')
        events = [dict(identity, kind='model_context', context=f['context']),
                  dict(identity, kind='model_draft', draft=1, text=json.dumps(f['draft'], ensure_ascii=False))]
        report = dict(cases=[dict(id='fake', company_research=dict(evidence=f['public']))])
        return events, report

    def test_same_jobs_preserve_bindings_and_only_local_rsi_borrows_public_facts(self):
        jobs, gaps = replay.make_jobs('fixture', *self.inputs())
        self.assertEqual(gaps, [])
        self.assertEqual(len(jobs), 3)  # summary, driver title and driver text once each
        self.assertEqual(jobs[1]['item']['numbers'], self.fixture['draft']['narratives'][0]['drivers'][0]['numbers'])
        self.assertEqual(len(jobs[0]['context']['evidence']), 1)
        with tempfile.TemporaryDirectory(dir=replay.ROOT/'.agentbus/tmp') as folder:
            source, output = Path(folder)/'jobs.json', Path(folder)/'out.json'
            replay.write_json(source, jobs)
            replay.worker(replay.ROOT, source, output)
            rows = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(rows[0]['text'], 'RSI低于50。')
        self.assertNotIn('E2', rows[0]['evidence_ids'])
        self.assertEqual(rows[1]['text'], '收盘29.7元。')
        self.assertEqual(rows[1]['stats']['deleted_clauses'], 1)

    def test_missing_or_mismatched_public_context_is_unknown(self):
        events, report = self.inputs()
        report['cases'][0]['company_research']['evidence'][0] = dict(self.fixture['public'][0], raw_value=777)
        jobs, gaps = replay.make_jobs('fixture', events, report)
        self.assertEqual(jobs, [])
        self.assertEqual(gaps[0]['reason'], 'replay_context_unavailable')

    def test_context_occurrence_phase_draft_and_path_remain_distinct(self):
        events, report = self.inputs()
        events += deepcopy(events)
        jobs, _ = replay.make_jobs('fixture', events, report)
        self.assertEqual([j['context_occurrence'] for j in jobs], [1,1,1,2,2,2])
        self.assertEqual(len({(j['context_occurrence'],j['path']) for j in jobs}), 6)

    def test_classification_distinguishes_partial_and_regression(self):
        rejected = dict(text='', stats={}, retained=0, generated=1)
        accepted = dict(text='样例。', stats={}, retained=1, generated=1)
        partial = dict(accepted, stats=dict(partially_retained_sentences=1))
        self.assertEqual(replay.category(rejected, partial), 'partially_recovered')
        self.assertEqual(replay.category(rejected, accepted), 'fully_recovered')
        self.assertEqual(replay.category(accepted, rejected), 'new_regression')
