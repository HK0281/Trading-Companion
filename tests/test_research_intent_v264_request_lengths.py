"""Actual request construction, ASCII accounting and fail-closed length coverage."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from unittest.mock import Mock, patch

from evaluation_tools import research_intent_v264_request_lengths as lengths
from test_research_intent_v2 import context, response
from test_research_intent_v25_evaluation import OfflineTests, sample
from trading_companion import research_intent_v2 as intent


class V264RequestLengthTests(OfflineTests):
    def test_unicode_accounting_and_real_call_share_the_candidate_builder(self):
        ctx = context()
        state = dict(ctx.session.__dict__)
        self.assertEqual(lengths.measure_request('中', ctx)['serialized_characters']
                         - lengths.measure_request('a', ctx)['serialized_characters'], 5)
        expected = intent.request_messages('研究平安银行', ctx)
        model = Mock()
        model.complete.return_value = response()
        with patch.object(intent, 'request_messages', wraps=intent.request_messages) as builder:
            run = intent.understand('研究平安银行', model=model, context=ctx)
            measured = lengths.measure_request('研究平安银行', ctx)
        self.assertEqual(run.status, 'ok')
        self.assertEqual(builder.call_count, 2)
        self.assertEqual(model.complete.call_args.args[0], expected)
        self.assertEqual(model.complete.call_args.kwargs['tools'], (intent.TOOL,))
        self.assertTrue(measured['valid'])
        self.assertEqual(measured['headroom'], 20000-measured['serialized_characters'])
        self.assertEqual(ctx.session.__dict__, state)

    def test_excessive_semantic_prompt_is_reported_and_never_reaches_a_model(self):
        model = Mock()
        with patch.object(intent, 'INSTRUCTION', '中'*4000):
            result = lengths.measure_request('研究平安银行', context())
            run = intent.understand('研究平安银行', model=model, context=context())
        self.assertFalse(result['valid'])
        self.assertLess(result['headroom'], 0)
        self.assertIn('character limit', result['validation_error'])
        self.assertEqual(run.status, 'failed')
        model.complete.assert_not_called()
        self.assertEqual(lengths.MAX_TOTAL_CHARACTERS, 20000)

    def test_every_row_is_accounted_for_without_fabricating_unavailable_context(self):
        rows = [sample() for _ in range(4)]
        for row, identifier in zip(rows, ('model', 'local', 'V2002', 'V2222')):
            row['id'] = identifier
        rows[1]['question'] = '取消研究'
        before = deepcopy(rows)
        def build(row, prefixes, setups):
            if row['id'] == 'V2002':
                raise ValueError('Known frozen context gap')
            return context(), 0
        with patch.object(lengths.evaluation, 'populations', return_value=(rows, {}, {}, {})), \
                patch.object(lengths.evaluation, 'build_context', side_effect=build), \
                patch.object(lengths.evaluation, 'fingerprints', return_value={}):
            report = lengths.report_lengths(('dev',))
        result = report['results']['dev']
        self.assertEqual((result['total'], result['serialized'], result['local_zero_calls'],
                          result['model_request_cases']), (4, 2, 1, 1))
        self.assertEqual(result['skipped'], [dict(id='V2002', reason='context_unavailable'),
                                            dict(id='V2222', reason='not_applicable')])
        self.assertTrue(report['passed'])
        self.assertEqual(report['paid_calls'], 0)
        self.assertFalse(report['new_model_results'])
        self.assertEqual(rows, before)

    def test_unexpected_context_failure_cannot_silently_reduce_coverage(self):
        with patch.object(lengths.evaluation, 'populations', return_value=([sample()], {}, {}, {})), \
                patch.object(lengths.evaluation, 'build_context', side_effect=ValueError('Unexpected')):
            with self.assertRaisesRegex(ValueError, 'coverage is incomplete'):
                lengths.report_lengths(('dev',))

    def test_only_fixed_public_splits_are_accepted_before_reading_inputs(self):
        with patch.object(lengths.evaluation, 'populations', side_effect=AssertionError('No inputs')):
            for args in (['--split', 'holdout'], ['--source', 'private'], ['--live'], ['--s', 'dev']):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    lengths.main(args)
            with self.assertRaises(ValueError):
                lengths.report_lengths(('holdout',))
