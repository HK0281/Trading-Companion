"""Independent same-turn conflict and recent-research ordinal boundaries."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from evaluation_tools import research_intent_v25_evaluation as experiment
from evaluation_tools import replay_research_intents_v25 as replay
from test_research_intent_v2 import context, draft, response
from test_research_intent_v251 import OfflineTests
from test_research_intent_v25_evaluation import sample
from trading_companion import research_intent_v2 as intent
from trading_companion.agent_input import InputIssue
from trading_companion.security_directory import SecurityDirectory
from trading_companion.workflow_evaluation_fixtures import SECURITIES


class ConflictTests(OfflineTests):
    def check(self, question, value, ctx=None, **kwargs):
        return intent.check_response(question, response(value), context=ctx or context(), **kwargs)

    def test_domain_overlap_clarifies_and_keeps_strict_scope_veto_subjects_and_original(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        value = draft(subject=dict(source='previous', mentions=[], selection='前面这两只'), operation='update',
            research_kinds=[dict(kind='financial', phrase='只看财务')], strict_scope=True,
            scope_phrase='只看财务', exclusions=[dict(kind='financial', phrase='不要财务')])
        original, log = deepcopy(value), []
        result = self.check('前面这两只的比较只看财务，但不要财务', value, ctx, corrections=log)
        self.assertEqual((result.outcome, result.strict_scope, result.research_kinds, result.exclusions),
                         ('clarify_conflict', True, (), ('financial',)))
        self.assertEqual(result.bound_subjects, tuple(s.instrument_id for s in SECURITIES[:2]))
        self.assertEqual(result.operation, 'update')
        self.assertIn('domain_conflict', [c['kind'] for c in log])
        self.assertEqual(value, original)
        with self.assertRaises(InputIssue):
            self.check('前面这两只的比较只看财务，但不要财务', value, ctx, normalize=False)

    def test_domain_overlap_keeps_independent_domains_and_conflict_beats_other_clarification(self):
        for reason in (None, 'horizon', 'conditions', 'conflict'):
            value = draft(research_kinds=[dict(kind='news', phrase='新闻'), dict(kind='financial', phrase='财务')],
                          clarification_reason=reason, exclusions=[dict(kind='financial', phrase='不要财务')])
            result = self.check('平安银行研究新闻和财务，但不要财务', value)
            self.assertEqual((result.outcome, result.research_kinds, result.exclusions),
                             ('clarify_conflict', ('news',), ('financial',)))

    def test_indicator_overlap_removes_all_parameters_of_excluded_kind_without_default(self):
        value = draft(indicator_mentions=['RSI8', 'RSI14'], strict_scope=True, scope_phrase='只用RSI8和RSI14',
                      exclusions=[dict(kind='indicator:rsi', phrase='不要RSI')])
        original, log = deepcopy(value), []
        result = self.check('平安银行只用RSI8和RSI14，但不要RSI', value, corrections=log)
        self.assertEqual((result.outcome, result.indicator_mentions, result.research_kinds, result.strict_scope),
                         ('clarify_conflict', (), (), True))
        self.assertIn('indicator:rsi', result.exclusions)
        self.assertIn('indicator_conflict', [c['kind'] for c in log])
        self.assertEqual(value, original)
        with self.assertRaises(InputIssue):
            self.check('平安银行只用RSI8和RSI14，但不要RSI', value, normalize=False)

    def test_mixed_indicator_phrase_preserves_only_grounded_nonexcluded_spans(self):
        value = draft(indicator_mentions=['SMA3,RSI8'], strict_scope=True, scope_phrase='只用SMA3,RSI8',
                      exclusions=[dict(kind='indicator:rsi', phrase='不用RSI')])
        result = self.check('平安银行只用SMA3,RSI8，但不用RSI', value)
        self.assertEqual((result.outcome, result.indicator_mentions, result.strict_scope),
                         ('clarify_conflict', ('SMA3',), True))
        self.assertIn('technical', result.research_kinds)

    def test_overlap_does_not_hide_fabricated_evidence_bad_shape_or_dropped_scope(self):
        question = '平安银行只看财务，但不要财务'
        base = draft(research_kinds=[dict(kind='financial', phrase='财务')],
                     exclusions=[dict(kind='financial', phrase='不要财务')], strict_scope=True, scope_phrase='只看财务')
        for bad in (dict(base, extra=True), dict(base, scope_phrase='不在原文中'),
                    dict(base, exclusions=[dict(kind='financial', phrase='财务')]),
                    dict(base, research_kinds=[dict(kind='financial', phrase='不存在的财务')]),
                    dict(base, strict_scope=False)):
            with self.subTest(bad=bad), self.assertRaises(InputIssue):
                self.check(question, bad)
        with self.assertRaises(InputIssue):
            self.check('平安银行只用SMA3，但不用RSI', draft(indicator_mentions=['RSI8']))

    def test_splitting_a_mixed_phrase_cannot_exceed_the_original_array_limit(self):
        phrase = 'RSI8,' + ','.join('SMA' + str(n) for n in range(3, 16))
        value = draft(indicator_mentions=[phrase], exclusions=[dict(kind='indicator:rsi', phrase='不用RSI')])
        with self.assertRaises(InputIssue):
            self.check('平安银行研究' + phrase + '，但不用RSI', value)


class OrdinalTests(OfflineTests):
    def check(self, selection, ctx=None, **kwargs):
        value = draft(operation='update', subject=dict(source='previous', mentions=[], selection=selection),
                      indicator_mentions=['RSI8'])
        return intent.check_response(selection + '，补上RSI8', response(value),
                                     context=ctx or context(previous=True, securities=SECURITIES[:2]), **kwargs)

    def test_neutral_latest_research_prefix_selects_one_checked_member(self):
        for selection in ('上一份比较里的第二只', '上次研究的第2个', '刚才比较中的第二只',
                          '前面的比较结果里的第二只', '上一份报告的第二只', '上次的第二只'):
            log = []
            with self.subTest(selection=selection):
                result = self.check(selection, corrections=log)
                self.assertEqual(result.bound_subjects, (SECURITIES[1].instrument_id,))
                self.assertEqual(result.outcome, 'research')
                self.assertEqual(result.indicator_mentions, ('RSI8',))
                self.assertIn('previous_research_ordinal', [c['kind'] for c in log])
        self.assertEqual(self.check('上一份比较里的第二只', normalize=False).bound_subjects, ())

    def test_recent_comparison_never_selects_a_coexisting_screening_list(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        ctx.session.screening_saved = object()  # Must not be consulted by this reference.
        with patch.object(intent, 'select_screening', side_effect=AssertionError('Wrong scope')):
            self.assertEqual(self.check('上一份比较里的第二只', ctx).bound_subjects, (SECURITIES[1].instrument_id,))

    def test_out_of_range_single_subject_invalid_identity_and_context_still_clarify(self):
        for selection in ('上一份比较里的第0只', '上一份比较里的第四只', '上次研究的第十个'):
            self.assertEqual(self.check(selection).outcome, 'clarify_subject')
        base = context(previous=True, securities=SECURITIES[:2])
        changed = replace(SECURITIES[0], name='已更名的人工样例')
        directory = SecurityDirectory((changed, *SECURITIES[1:]), 'changed', base.now, False)
        invalid = (context(), context(previous=True), context(previous=True, kind='index'),
                   replace(base, conversation_memory=False), replace(base, source='akshare_eastmoney'),
                   replace(base, development=True), replace(base, now=base.now-timedelta(seconds=1)),
                   replace(base, directory=directory))
        for ctx in invalid:
            with self.subTest(ctx=ctx):
                self.assertEqual(self.check('上一份比较里的第一只', ctx).outcome, 'clarify_subject')
        base.session._subject_current = False
        self.assertEqual(self.check('上一份比较里的第一只', base).outcome, 'clarify_subject')

    def test_neutral_prefix_cannot_select_pending_or_fall_back_to_a_list_without_research(self):
        ctx = context(previous=True, securities=SECURITIES[:2])
        ctx.session.product_pending, ctx.session.index_pending = object(), object()
        self.assertEqual(self.check('上一份比较里的第二只', ctx).bound_subjects, ())
        ctx = context()
        ctx.session.screening_saved = object()
        with patch.object(intent, 'select_screening', side_effect=AssertionError('No list fallback')):
            self.assertEqual(self.check('上一份比较里的第二只', ctx).bound_subjects, ())

    def test_prefix_with_security_or_list_name_is_never_stripped_into_latest_research(self):
        for selection in ('上次筛选名单里的第二只', '上一份归档报告2024里的第二只',
                          '上一份浦发银行比较里的第二只', '刚才沪深300报告里的第二只'):
            self.assertIsNone(intent._prefixed_previous_ordinal(selection))
        self.assertEqual(self.check('上次筛选名单里的第二只').bound_subjects, ())
        for selection in ('上一份浦发银行比较里的第二只', '刚才沪深300报告里的第二只'):
            with self.assertRaises(InputIssue):
                self.check(selection)
        ctx = context(previous=True, securities=SECURITIES[:2])
        ctx.session.screening_saved = object()
        with patch.object(intent, 'select_screening', return_value=SimpleNamespace(ready=False)) as selector:
            self.assertEqual(self.check('上次筛选名单里的第二只', ctx).bound_subjects, ())
        selector.assert_called_once()


class ReplayTests(OfflineTests):
    def test_v251_archive_is_default_and_every_table_names_current_replay(self):
        row = sample()
        raw = response(draft(version='research-intent-v2.5.1')).to_dict()
        archived = dict(row, V2=dict(status='ok', attempts=1, response=raw, fields=row['gold']))
        original = deepcopy(archived)
        with patch.object(experiment, 'populations', return_value=([row], {row['id']: row['gold']}, {}, {})):
            report = replay.replay_reports([dict(rows=[archived])])
        self.assertEqual(archived, original)
        self.assertEqual(report['rows'][0]['V2']['status'], 'ok')
        self.assertEqual(replay.migrate_response(raw).tool_calls[0].arguments['version'], intent.VERSION)
        self.assertIsNone(report['passed'])
        self.assertIsNone(report['thresholds'])
        self.assertFalse(report['new_model_results'])
        self.assertEqual(report['paid_calls'], 0)
        self.assertEqual(report['authenticity'], 'archived_response_replay')
        lines = experiment.markdown(report).splitlines()
        self.assertIn(experiment.REPLAY_252_NOTICE, lines[0])
        for index, line in enumerate(lines):
            if line.startswith('| ---'):
                self.assertIn(experiment.REPLAY_252_NOTICE, lines[index-1])


if __name__ == '__main__':
    unittest.main()
