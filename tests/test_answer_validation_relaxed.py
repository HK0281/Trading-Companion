"""Approved #243 boundaries: metadata recovery must not launder false prices or RSI."""
from copy import deepcopy
from dataclasses import replace
import unittest

from tests.test_answer_validation import context
from tests.test_answer_technical_validation import technical_context
from tests.test_product_decision_answer import decision_bundle
from trading_companion.answer_validation import check_claim, HARD
from trading_companion.answer_numeric_bindings import COUNTERS, RELAXATION_COUNTERS, CLAUSE_COUNTERS
from trading_companion.answer_validation_metrics import clause_metrics


class RelaxedValidationTests(unittest.TestCase):
    def check(self, text, numbers=None, ctx=None):
        stats = dict.fromkeys((*COUNTERS, *RELAXATION_COUNTERS, *CLAUSE_COUNTERS), 0)
        result = check_claim(dict(text=text, numbers=numbers or []), instrument='000001.SZ',
                             context=ctx or context(), section='current', index=0, stats=stats)
        return result, stats

    def test_nonnumeric_annotations_and_duplicate_refs(self):
        text = 'MA20仍需观察，收盘29.7元。'
        result, stats = self.check(text, [{'text': 'MA20', 'ref': 'E404'},
            {'text': '29.7元', 'ref': 'E1'}, {'text': '29.7元', 'ref': 'E1'}])
        self.assertEqual(result[0], text)
        self.assertEqual((stats['ignored_nonnumeric_bindings'], stats['numeric_tokens'], stats['bound_matched']), (1, 1, 1))
        for ref in ('E2', 'E5'):
            self.assertFalse(self.check('读数29.7元。', [{'text': '29.7元', 'ref': 'E1'}, {'text': '29.7元', 'ref': ref}])[0][0])
        self.assertEqual(self.check('MA20需观察。', [{'text': 'MA20', 'ref': 'E5'}])[0][2][0].code, HARD)

    def test_unknown_ref_and_field_have_separate_auditable_counts(self):
        for binding, counter in (({'text': '29.7元', 'ref': 'E404'}, 'invalid_binding_fallback_matched'),
                                 ({'text': '29.7元', 'ref': 'E1', 'field': 'raw_value'}, 'ignored_binding_fields')):
            result, stats = self.check('读数29.7元。', [binding])
            self.assertTrue(result[0]); self.assertEqual(stats[counter], 1)
        result, stats = self.check('读数999元。', [{'text': '999元', 'ref': 'E404'}])
        self.assertFalse(result[0]); self.assertEqual(stats['invalid_binding_fallback_rejected'], 1)

    def test_block_rejection_still_counts_all_original_sentences_and_clauses(self):
        result, stats = self.check('收盘29.7元，仍需观察。注意风险。', [{'text': '没有出现的999元', 'ref': 'E1'}])
        self.assertEqual(result[3:], (2, 0))
        self.assertEqual((stats['generated_clauses'], stats['deleted_clauses']), (3, 3))

    def test_plan_prices_cannot_fall_back_to_equal_close_or_wrong_fields(self):
        from trading_companion.product_narrative_answer import _context
        ctx = _context(decision_bundle())
        level = next(l for l in ctx['price_levels'] if l['field'] == 'reference_stop')
        text = '止损' + level['price'] + '元。'
        ordinary = next(r for r in ctx['evidence'] if r.get('unit') == 'CNY/share')
        ordinary['raw_value'] = level['price']
        for ref, field in (('E404', None), ('L:E1', None), (ordinary['id'], None), (level['id'], 'target_1')):
            binding = dict(text=level['price']+'元', ref=ref)
            if field: binding['field'] = field
            self.assertFalse(self.check(text, [binding], ctx)[0][0])
        self.assertEqual(self.check(text, [dict(text=level['price']+'元', ref=level['id'])], ctx)[0][0], text)

    def test_clause_recovery_counts_original_numeric_tokens_only_once(self):
        result, stats = self.check('收盘29.7元，营业收入999亿元；仍需谨慎观察。')
        self.assertEqual(result[0], '收盘29.7元，仍需谨慎观察。')
        self.assertEqual(result[3:], (1, 1))
        self.assertEqual((stats['numeric_tokens'], stats['generated_clauses'], stats['retained_clauses'], stats['deleted_clauses']), (2, 3, 2, 1))
        self.assertEqual(stats['partially_retained_sentences'], 1)
        for text in ('收盘999元，但。', '收盘999元，且。', '若收盘999元，则趋势走强。',
                     '收盘999元，29.7。', '收盘999元，约29.7元。', '收盘999元，但趋势走强。', '收盘999元，营业收入888万元。'):
            self.assertFalse(self.check(text)[0][0], text)
        result = check_claim(dict(text='收盘29.7元，营业收入999亿元。', evidence_ids=['E1', 'E2']),
            instrument='000001.SZ', context=context(), section='current', index=0)
        self.assertEqual(result[1], ['E1'])

    def test_protected_failures_do_not_get_clause_recovery(self):
        for text in ('收盘29.7元，止损999元。', '收盘29.7元，浦发银行仍需观察。',
                     '收盘29.7元，上涨概率70%。', 'RSI低于30，收盘29.7元。'):
            self.assertFalse(self.check(text, ctx=technical_context(45))[0][0], text)

    def test_clause_offsets_skip_parameters_thousands_and_quoted_commas(self):
        from trading_companion.answer_clauses import clause_spans
        text = 'MACD(12,26,9)待观察，成交1,234股；“收盘29.7元，仍需观察”。'
        spans = clause_spans(text)
        self.assertEqual(len(spans), 3)
        self.assertTrue(all(text[start:end] == part for start,end,part in spans))
        self.assertEqual(spans[1][2], '成交1,234股')
        self.assertEqual(spans[2][2], '“收盘29.7元，仍需观察”')
        result, stats = self.check('ﬃ收盘２９．７元，营业收入９９９亿元。', [{'text': '２９．７元', 'ref': 'E1'}])
        import unicodedata
        self.assertEqual(unicodedata.normalize('NFKC', result[0]), 'ffi收盘29.7元。')
        self.assertEqual(stats['numeric_tokens'], 2)

    def test_plan_role_does_not_absorb_a_coordinated_historical_low(self):
        from trading_companion.product_narrative_answer import _context
        ctx = _context(decision_bundle())
        level = next(l for l in ctx['price_levels'] if l['field'] == 'reference_stop')
        close = next(r for r in ctx['evidence'] if r.get('label') == '最新收盘价')
        text = f"跌破{level['price']}元计划止损且失守{close['raw_value']}元历史价，需重新评估。"
        self.assertEqual(self.check(text, ctx=ctx)[0][0], text)
        # An invalid L denotes a protected price reference, even with a neutral noun.
        self.assertFalse(self.check('读数29.7元。', [{'text': '29.7元', 'ref': 'L404'}])[0][0])

    def test_rsi_reading_and_all_relations_must_share_one_timeframe(self):
        ctx = technical_context(45)
        ctx['evidence'].append(dict(id='E7', instrument_id='000001.SZ', label='RSI',
            raw_value=25, unit='index_points', date='2026-09-25', content_kind='fact'))
        ctx['_technical_validation']['000001.SZ']['values'].append(dict(
            indicator_id='rsi(window=14)', timeframe='weekly', field='value', value='25', date='2026-09-25', ref='E7'))
        for text in ('RSI为45点且低于30。', 'RSI高于30且低于30。', '日线RSI低于30。'):
            self.assertFalse(self.check(text, ctx=ctx)[0][0], text)
        for text in ('RSI为25点且低于30。', 'RSI低于30。', '周线RSI为25点且低于30。'):
            self.assertTrue(self.check(text, ctx=ctx)[0][0], text)

    def test_rsi_local_values_and_clause_local_conditions(self):
        ctx = technical_context(45)
        sidecar = ctx['_technical_validation']['000001.SZ']
        sidecar['local_rsi'] = [dict(**sidecar['values'][0], instrument_id='000001.SZ', source='local_computed_rsi')]
        sidecar['values'] = []
        ctx['evidence'].pop()
        result, stats = self.check('趋势可能仍反复，RSI低于50中线。', ctx=ctx)
        self.assertTrue(result[0]); self.assertEqual(stats['rsi_local_value_checks'], 1)
        self.assertNotIn('E6', result[1])
        self.assertFalse(self.check('RSI低于30。', ctx=ctx)[0][0])
        self.assertFalse(self.check('若RSI低于50，则继续观察。', ctx=ctx)[0][0])
        self.assertTrue(self.check('RSI的30/70仅为参考阈值。', ctx=ctx)[0][0])
        sidecar['local_rsi'][0]['instrument_id'] = '600000.SH'
        self.assertFalse(self.check('RSI低于50。', ctx=ctx)[0][0])

    def test_adjacent_indicator_readings_select_timeframe_and_exact_parameters(self):
        ctx = technical_context()
        sidecar = ctx['_technical_validation']['000001.SZ']
        for ref, value, timeframe in (('E7', 27.95, 'daily'), ('E8', 28.5, 'weekly')):
            ctx['evidence'].append(dict(id=ref, instrument_id='000001.SZ', label='SMA3', raw_value=value,
                unit='CNY/share', date='2026-09-25', content_kind='fact'))
            sidecar['values'].append(dict(indicator_id='sma(window=3)', timeframe=timeframe,
                field='value', value=str(value), date='2026-09-25', ref=ref))
        for text in ('SMA3约27.95元/股。', 'SMA(3)27.95元。', '周线方面SMA(3)28.5元。', '周线SMA(3)28.5元。'):
            self.assertTrue(self.check(text, ctx=ctx)[0][0], text)
        for text in ('日线SMA(3)28.5元。', '周线SMA(3)27.95元。', 'SMA6约27.95元。'):
            self.assertFalse(self.check(text, ctx=ctx)[0][0], text)

    def test_atr_reading_can_use_the_supplied_program_operand(self):
        from trading_companion.product_narrative_answer import _context
        ctx = _context(decision_bundle())
        ctx['evidence'] = [r for r in ctx['evidence'] if r.get('label') != 'ATR(14)']
        plan = next(r for r in ctx['evidence'] if r.get('content_kind') == 'trade_plan')
        text = f"ATR约{plan['values']['atr']}元。"
        self.assertTrue(self.check(text, [dict(text=plan['values']['atr']+'元', ref=plan['id'], field='atr')], ctx)[0][0])
        self.assertFalse(self.check('周线'+text, ctx=ctx)[0][0])

    def test_real_bundle_rsi_survives_model_context_trimming_without_new_data(self):
        from trading_companion.product_narrative_answer import _context
        from trading_companion.answer_technical_validation import technical_context as build_context
        bundle = decision_bundle()
        ctx = _context(bundle)
        sidecar = build_context(bundle, {**ctx, 'evidence': []})
        self.assertEqual(sidecar['000001.SZ']['local_rsi'][0]['value'], '50.0')
        ctx['_technical_validation'] = sidecar
        ctx['evidence'] = [r for r in ctx['evidence'] if not str(r.get('label', '')).startswith('RSI')]
        result, stats = self.check('RSI高于30。', ctx=ctx)
        self.assertTrue(result[0]); self.assertEqual(stats['rsi_local_value_checks'], 1)

    def test_clause_metric_versions_do_not_treat_history_as_zero(self):
        _, stats = self.check('收盘29.7元，营业收入999万元。')
        quality = dict(stats, sentence_count_version='sentence-counts-v2')
        self.assertEqual(clause_metrics([quality])['clause_deletion_rate'], .5)
        old = clause_metrics([quality, {}])
        self.assertIsNone(old['clause_deletion_rate']); self.assertEqual(old['clause_counts_unknown'], 1)

    def test_price_requires_rating_and_original_single_repair_restores_it(self):
        from tests.test_product_narrative_answer import NarrativeModel
        from trading_companion.product_narrative_answer import synthesize_narrative_answer, _context, _Checker
        def refuse_first(payload, attempt, context):
            if attempt == 1:
                payload['decisions'][0].update(status='insufficient_evidence', rating=None, direction=None,
                                              conviction=None, plan_id=None)
        bundle = decision_bundle()
        model = NarrativeModel(refuse_first)
        result = synthesize_narrative_answer('能买吗', bundle, model=model, repair_invalid=True)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result.answer.decisions[0]['status'], 'available')
        result = synthesize_narrative_answer('能买吗', bundle, model=NarrativeModel(refuse_first), repair_invalid=False)
        self.assertEqual(result.answer.decisions[0]['code'], 'decision_rating_required')
        self.assertIn('SMA3-only', model.calls[0][0].content)
        context = _context(bundle)
        context['evidence'] = [r for r in context['evidence'] if r.get('unit') == 'index_points']
        raw = dict(status='insufficient_evidence', horizon=context['objective']['horizon'],
                   reasons=[dict(text='没有有效价格，暂无法形成研判。')], invalidation=dict(text='取得有效价格后重评。'))
        self.assertEqual(_Checker(bundle, context).decision(raw, '000001.SZ')['code'], 'decision_insufficient_evidence')

    def test_strict_sma_only_is_low_conviction_without_atr_or_rsi(self):
        from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW
        from tests.test_product_narrative_answer import NarrativeModel
        from trading_companion.multi_contracts import IndicatorSpec
        from trading_companion.product_research import collect_product_research
        from trading_companion.product_narrative_answer import synthesize_narrative_answer, _context
        plan = replace(decision_bundle().plan, indicators=(IndicatorSpec('sma', window=3),),
                       indicators_explicit=True, user_question='只看SMA3，现在能买吗')
        bundle = collect_product_research(plan, data_service=CompanyDataFixture(), now=NOW, source_factory=MemorySourceFactory())
        def conviction(payload, attempt, context):
            payload['decisions'][0]['conviction'] = '高'
        run = synthesize_narrative_answer(plan.user_question, bundle, model=NarrativeModel(conviction))
        self.assertEqual(run.answer.decisions[0]['conviction'], '低')
        self.assertIsNone(run.answer.decisions[0]['plan_id'])
        self.assertTrue(all(p.code == 'risk_atr_not_requested' for p in bundle.companies[0].trade_plans))
        self.assertFalse(_context(bundle)['_technical_validation']['000001.SZ']['local_rsi'])
