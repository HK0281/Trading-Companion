"""The product and legacy research paths share date/indicator boundary decisions."""
from datetime import timedelta
import unittest

from trading_companion.product_plan import prepare_product_plan
from trading_companion.research_intent import prepare_research_question
from trading_companion.tool_model import FakeToolModel
from tests.test_product_plan import _directory, _arguments, _response, REFERENCE, NOW
from tests.test_research_intent import interpretation, intent_arguments


class IndicatorDateBoundaryTests(unittest.TestCase):
    def product(self, text, expression=None):
        return prepare_product_plan('分析平安银行' + text,
            model=FakeToolModel([_response(_arguments(kinds=['technical'], expression=expression))]),
            directory=_directory(), reference_date=REFERENCE, now=NOW, source='csv', development=True)

    def legacy(self, text, phrases, expression=None):
        return prepare_research_question('分析平安银行' + text,
            model=FakeToolModel([interpretation(intent_arguments(indicators=phrases, expression=expression))]),
            directory=_directory(), reference_date=REFERENCE, source='csv', development=True)

    def test_explicit_suffix_wins_over_relative_day_count_in_both_paths(self):
        for text, phrase, expected, days in (
            ('最近20天RSI6怎么样', 'RSI6', ['rsi(window=6)'], 20),
            ('近60日SMA3', 'SMA3', ['sma(window=3)'], 60),
            ('过去20天 RSI(6)', 'RSI(6)', ['rsi(window=6)'], 20),
            ('最近 20 日 sma(3)', 'sma(3)', ['sma(window=3)'], 20)):
            with self.subTest(text=text):
                for result in (self.product(text), self.legacy(text, [phrase])):
                    self.assertEqual(result.status, 'ready', result)
                    conditions = result.conditions if hasattr(result, 'conditions') else result
                    self.assertEqual([s.indicator_id for s in conditions.indicators], expected)
                    cutoff = conditions.end_date if hasattr(conditions, 'end_date') else conditions.as_of_date
                    self.assertEqual(cutoff, REFERENCE - timedelta(days=1))
                    self.assertEqual(conditions.start_date, cutoff - timedelta(days=days - 1))

    def test_relative_prefix_uses_defaults_while_bare_period_remains_parameter(self):
        for text, phrase, expected, days in (
            ('近20日RSI', 'RSI', ['rsi(window=14)'], 20),
            ('近20日均线', '均线', ['sma(window=20)', 'sma(window=60)'], 20),
            ('20日均线', '20日均线', ['sma(window=20)'], 365),
            ('20日RSI', '20日RSI', ['rsi(window=20)'], 365)):
            with self.subTest(text=text):
                for result in (self.product(text), self.legacy(text, [phrase])):
                    self.assertEqual(result.status, 'ready', result)
                    conditions = result.conditions if hasattr(result, 'conditions') else result
                    self.assertEqual([s.indicator_id for s in conditions.indicators], expected)
                    self.assertEqual((REFERENCE - timedelta(days=1) - conditions.start_date).days + 1, days)

    def test_old_model_swallowed_day_prefix_is_recovered_only_from_original(self):
        for text, phrase, expected in (('最近20天RSI6', '20天RSI', 'rsi(window=6)'),
                                       ('近60日SMA3', '60日SMA', 'sma(window=3)'),
                                       ('近20日RSI', '20日RSI', 'rsi(window=14)'),
                                       ('近20日均线', '20日均线', 'sma(window=20)')):
            with self.subTest(text=text):
                result = self.legacy(text, [phrase])
                self.assertEqual(result.status, 'ready', result)
                self.assertEqual(result.conditions.indicators[0].indicator_id, expected)
        self.assertNotEqual(self.legacy('最近20天RSI6和MACD', ['20天RSI']).status, 'ready')
        self.assertNotEqual(self.legacy('最近20天RSI6', ['RSI14']).status, 'ready')
        self.assertNotEqual(self.product('最近20天RSI0').status, 'ready')
