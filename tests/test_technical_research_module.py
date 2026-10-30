"""Offline user journeys for task-driven indicators and evidence-bound technical research."""

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.errors import InvalidRequestError
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_answer import synthesize_product_answer
from trading_companion.product_plan import wants_product_research
from trading_companion.product_research import model_research_context
from trading_companion.research_session import ResearchSession
from trading_companion.research_intent import _indicator as parse_daily_indicator
from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError
from tests.test_product_agent import ProductModelFixture
from tests.test_product_answer import RecordingModel, response
from tests.test_product_research import (
    CompanyDataFixture, MemoryDailySource, MemorySourceFactory, NOW, directory,
)


class LongHistorySource(MemoryDailySource):
    """A bounded artificial year with repeated swings and complete trading-week history."""

    def read_daily_bars(self):
        self.owner.reads.append(self.request)
        output, current = [], self.request.start_date
        while current <= self.request.as_of_date:
            if current.weekday() < 5:
                index = len(output)
                close = 30 + (index % 12) * 0.2
                output.append(DailyBar(self.request.instrument_id, current, close - 0.05,
                    close + 0.3, close - 0.3, close, 100000 + index * 100, 3000000 + index * 1000))
            current += timedelta(days=1)
        return output


class LongHistoryFactory(MemorySourceFactory):
    def __call__(self, request):
        self.requests.append(request)
        return LongHistorySource(request, self)


class TechnicalResearchModuleTests(unittest.TestCase):
    def setUp(self):
        self.session, self.data = ResearchSession(), CompanyDataFixture()
        self.factory = LongHistoryFactory()
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No network in integration tests")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credential reads")),
            patch("trading_companion.product_research._build_source", side_effect=AssertionError("Use memory source")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def run_turn(self, question, *, model=None, now=NOW):
        return run_product_research_turn(question, model=model or ProductModelFixture(),
            session=self.session, directory=directory(), now=now, source="tencent",
            data_service=self.data, source_factory=self.factory)

    def collected(self):
        result = self.run_turn("分析平安银行的周线和支撑压力，并结合基本面")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertIsNotNone(result.product_bundle)
        return result.product_bundle

    def test_real_wording_selects_relevant_profiles_and_reads_market_once(self):
        cases = (
            ("分析平安银行的量价配合", {"vwma", "obv", "volume"}),
            ("分析平安银行的趋势强度", {"adx", "sma", "macd"}),
            ("分析平安银行有没有背离", {"rsi", "macd", "adx"}),
            ("分析平安银行的周线与支撑", {"sma", "boll", "atr"}),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                self.session = ResearchSession()
                self.factory = MemorySourceFactory()
                self.assertTrue(wants_product_research(question))
                result = self.run_turn(question)
                self.assertIn(result.status, ("completed", "limited"))
                self.assertEqual(result.product_plan.kinds, ("technical",))
                self.assertTrue(expected <= {spec.kind for spec in result.product_plan.indicators})
                self.assertEqual(result.analyses_used, 1)
                self.assertEqual(len(self.factory.reads), 1)
                company = result.product_bundle.companies[0]
                self.assertIsNotNone(company.technical_insights)
                self.assertEqual(company.technical_insights.code, "technical_insights_computed")
                self.assertEqual(company.technical.request.indicators, result.product_plan.indicators)
                self.assertEqual(self.data.calls, [])

    def test_explicit_periods_override_profile_dependencies_without_silent_additions(self):
        result = self.run_turn("分析平安银行的趋势强度，只使用ADX7和VWMA10")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators],
                         ["adx(window=7)", "vwma(window=10)"])
        self.assertEqual(len(self.factory.reads), 1)
        insights = result.product_bundle.companies[0].technical_insights
        self.assertFalse(any(item.category == "divergence" for item in insights.observations))
        self.assertFalse(any("sma(" in item.key for item in insights.observations))

    def test_explicit_adx_exclusion_is_honoured_by_trend_profile(self):
        result = self.run_turn("分析平安银行的趋势强度，不要ADX")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertNotIn("adx", {spec.kind for spec in result.product_plan.indicators})
        self.assertTrue(result.product_plan.indicators_explicit)
        self.assertFalse(any("ADX" in item.label for item in result.product_bundle.evidence))
        self.assertEqual(len(self.factory.reads), 1)

    def test_historical_followup_keeps_explicit_indicator_and_dates(self):
        historical = "2026-09-01至2026-09-20"
        first = self.run_turn("分析平安银行" + historical + "的背离，使用RSI7",
                              model=ProductModelFixture(date_expression=historical))
        self.assertIn(first.status, ("completed", "limited"))
        second = self.run_turn("那周线呢", now=NOW + timedelta(minutes=1))
        self.assertIn(second.status, ("completed", "limited"))
        self.assertEqual(second.product_plan.indicators, first.product_plan.indicators)
        self.assertEqual([spec.indicator_id for spec in second.product_plan.indicators], ["rsi(window=7)"])
        self.assertEqual(second.product_plan.start_date, date(2026, 9, 1))
        self.assertEqual(second.product_plan.end_date, date(2026, 9, 20))
        self.assertTrue(second.product_plan.explicit_historical)
        self.assertEqual(len(self.factory.reads), 2)
        self.assertEqual(self.factory.reads[-1].as_of_date, date(2026, 9, 20))
        self.assertEqual({request.instrument_id for request in self.factory.reads}, {"000001.SZ"})
        self.assertEqual({request.source for request in self.factory.reads}, {"tencent"})
        self.assertEqual(self.data.calls, [])

    def test_explicit_twenty_week_mean_is_a_parameter_not_a_historical_range(self):
        question = "分析平安银行的20周均线和支撑"
        self.assertTrue(wants_product_research(question))
        result = self.run_turn(question)
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators], ["sma(window=20)"])
        self.assertEqual((result.product_plan.end_date - result.product_plan.start_date).days + 1, 365)
        self.assertFalse(result.product_plan.explicit_historical)
        self.assertEqual(len(self.factory.reads), 1)
        weekly = next(item for item in result.product_bundle.companies[0].technical_insights.observations
                      if item.key == "weekly:sma(window=20):value")
        self.assertEqual(weekly.status, "available")
        self.assertEqual(weekly.timeframe, "weekly")
        self.assertIsNotNone(weekly.value)

    def test_original_daily_only_parser_does_not_treat_a_week_as_a_day(self):
        with self.assertRaises(InvalidRequestError):
            parse_daily_indicator("20周均线")
        self.assertEqual([spec.indicator_id for spec in parse_daily_indicator("20日均线")],
                         ["sma(window=20)"])

    def test_long_chinese_weekly_indicator_names_route_to_weekly_capable_research(self):
        for phrase, kind in (("20周指数移动平均线", "ema"), ("20周简单移动平均线", "sma")):
            with self.subTest(phrase=phrase):
                self.session, self.factory = ResearchSession(), LongHistoryFactory()
                question = "分析平安银行的" + phrase
                self.assertTrue(wants_product_research(question))
                result = self.run_turn(question)
                self.assertIn(result.status, ("completed", "limited"))
                self.assertEqual(result.product_plan.kinds, ("technical",))
                self.assertEqual([spec.indicator_id for spec in result.product_plan.indicators],
                                 [f"{kind}(window=20)"])
                weekly = next(item for item in result.product_bundle.companies[0].technical_insights.observations
                              if item.key == f"weekly:{kind}(window=20):value")
                self.assertEqual(weekly.status, "available")
                self.assertEqual(len(self.factory.reads), 1)

    def test_model_context_contains_derived_timeframes_without_the_full_fact_graph(self):
        bundle = self.collected()
        context = model_research_context(bundle)
        records = context["evidence"]
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 14500)
        scopes = {item["temporal_scope"] for item in records if item["kind"] == "technical"}
        self.assertIn("daily_signal_derived", scopes)
        self.assertIn("completed_week_derived", scopes)
        self.assertIn("cross_timeframe_derived", scopes)
        self.assertFalse(any("facts" in item or "dependencies" in item for item in records))
        graph_ids = {item.fact_id for item in bundle.companies[0].technical_insights.facts}
        self.assertFalse(any(item["id"] in graph_ids for item in records))
        tight = model_research_context(bundle, limit=4000)
        self.assertLessEqual(len(json.dumps(tight, ensure_ascii=False)), 4000)
        self.assertGreater(tight["omitted_from_model"], 0)
        self.assertIn("cross_timeframe_derived", {item["temporal_scope"] for item in tight["evidence"]})

    def test_weekly_macd_components_have_distinct_labels_in_collected_and_model_evidence(self):
        model = ProductModelFixture()
        result = self.run_turn("分析平安银行的周线，只使用MACD(2,3,2)", model=model)
        self.assertIn(result.status, ("completed", "limited"))
        company = result.product_bundle.companies[0]
        by_id = {item.id: item for item in company.evidence}
        observations = [item for item in company.technical_insights.observations
                        if item.timeframe == "weekly" and item.indicator_id.startswith("macd(")]
        self.assertEqual({item.field for item in observations}, {"dif", "dea", "histogram"})
        expected = {"dif": "DIF", "dea": "DEA", "histogram": "柱值"}
        labels = set()
        for observation in observations:
            self.assertEqual(observation.status, "available")
            evidence = by_id[observation.dependencies[0]]
            self.assertTrue(evidence.label.endswith(" · " + expected[observation.field]))
            self.assertIn(expected[observation.field] + "：", evidence.display)
            self.assertEqual(evidence.raw_value, observation.value)
            self.assertEqual(evidence.unit, observation.unit)
            labels.add(evidence.label)
        self.assertEqual(len(labels), 3)
        supplied = {item["label"] for item in model.contexts[0]["evidence"]}
        self.assertTrue(labels <= supplied)
        self.assertEqual(len(self.factory.reads), 1)

    def test_weekly_close_and_single_value_indicators_do_not_share_labels(self):
        result = self.run_turn("分析平安银行的周线，只使用SMA3、EMA3、RSI2和MACD(2,3,2)")
        self.assertIn(result.status, ("completed", "limited"))
        evidence = [item for item in result.product_bundle.evidence
                    if item.temporal_scope == "completed_week_derived"]
        self.assertEqual(len(evidence), 7)
        self.assertEqual(len({item.label for item in evidence}), 7)
        self.assertTrue(any(item.label == "周线观察 · 收盘价" for item in evidence))
        for kind in ("sma", "ema", "rsi"):
            self.assertEqual(sum(" · " + kind + "(" in item.label for item in evidence), 1)

    def test_unavailable_weekly_macd_components_remain_distinguishable(self):
        result = self.run_turn("分析平安银行的周线，只使用MACD(100,120,60)")
        self.assertIn(result.status, ("completed", "limited"))
        company = result.product_bundle.companies[0]
        observations = [item for item in company.technical_insights.observations
                        if item.timeframe == "weekly" and item.indicator_id.startswith("macd(")]
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(item.status == "insufficient_history" for item in observations))
        evidence = [item for item in company.evidence if item.temporal_scope == "completed_week_derived"
                    and "macd(" in item.label]
        self.assertEqual(len({item.label for item in evidence}), 3)
        self.assertTrue(all(item.raw_value is None for item in evidence))

    def restricted_bundle(self, bundle, predicate):
        company = bundle.companies[0]
        selected = tuple(item for item in company.evidence if predicate(item))
        self.assertTrue(selected)
        return replace(bundle, companies=(replace(company, evidence=selected),))

    def answer(self, bundle, text):
        def output(messages):
            context = json.loads(messages[2].content.split("\n", 1)[1])
            identifiers = [item["id"] for item in context["evidence"]]
            return response((
                {"text": text, "evidence_ids": identifiers},
                {"text": "这些历史观察仍需结合后续价格表现核对。", "evidence_ids": identifiers},
            ))
        model = RecordingModel(output)
        run = synthesize_product_answer("解释平安银行的周线和价格区域", bundle, model=model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][1], ())
        return run

    def test_weekly_value_is_checked_but_prose_timeframe_is_not_inferred(self):
        bundle = self.restricted_bundle(self.collected(), lambda item:
            item.temporal_scope == "completed_week_derived" and item.content_kind == "fact"
            and "SMA(20)" in item.display)
        item = bundle.evidence[0]
        price = format(item.raw_value, ".7g")
        accepted = self.answer(bundle, f"平安银行20周均线为{price}元，可以作为较长周期的价格参考。")
        self.assertEqual(accepted.answer.status, "accepted")
        rejected = self.answer(bundle, f"平安银行20日均线为{price}元，可以作为较长周期的价格参考。")
        self.assertEqual(rejected.answer.status, "accepted")
        self.assertIn("20日均线", rejected.answer.text)

    def test_non_numeric_timeframe_interpretation_is_left_to_the_model(self):
        bundle = self.restricted_bundle(self.collected(), lambda item:
            item.temporal_scope in ("completed_week_derived", "cross_timeframe_derived"))
        run = self.answer(bundle, "周线结论较前一交易日更强，日周方向因此得到确认。")
        self.assertEqual(run.answer.status, "accepted")
        self.assertIn("前一交易日", run.answer.text)

    def test_calculated_support_price_is_citable_but_cannot_be_changed(self):
        bundle = self.restricted_bundle(self.collected(), lambda item:
            item.temporal_scope == "daily_signal_derived" and item.content_kind == "fact"
            and item.unit == "CNY/share" and "支撑候选" in item.display)
        item = bundle.evidence[0]
        price = format(item.raw_value, ".7g")
        valid = self.answer(bundle, f"平安银行历史支撑候选为{price}元，仍需观察实际价格反应。")
        self.assertEqual(valid.answer.status, "accepted")
        altered = format(item.raw_value + 1, ".7g")
        invalid = self.answer(bundle, f"平安银行历史支撑候选为{altered}元，仍需观察实际价格反应。")
        self.assertEqual(invalid.answer.status, "limited")
        self.assertNotIn(altered + "元", invalid.answer.text)

    def test_snapshot_keeps_complete_derived_graph_and_omits_private_question(self):
        bundle = self.collected()
        marker = "PRIVATE_TECHNICAL_QUESTION_NOT_FOR_ARCHIVE"
        bundle = replace(bundle, plan=replace(bundle.plan, user_question=marker))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "technical-research.sqlite3"
            with ResearchSnapshotStore(path) as store:
                payload = store.load(store.save(bundle))
            archived = payload["companies"][0]["technical_insights"]
            self.assertEqual(archived, bundle.companies[0].technical_insights.to_dict())
            self.assertTrue(archived["weekly_bars"])
            facts = {item["fact_id"]: item for item in archived["facts"]}
            self.assertTrue(facts)
            for fact in facts.values():
                self.assertEqual(fact["instrument_id"], "000001.SZ")
                self.assertTrue(set(fact["dependencies"]) <= facts.keys())
            self.assertNotIn(marker, json.dumps(payload))
            self.assertNotIn(marker.encode(), path.read_bytes())
            self.assertNotIn("user_question", payload["plan"])

    def test_another_companys_derived_graph_cannot_be_archived(self):
        bundle = self.collected()
        company = bundle.companies[0]
        forged = replace(company.technical_insights, instrument_id="600000.SH")
        invalid = replace(bundle, companies=(replace(company, technical_insights=forged),))
        with TemporaryDirectory() as folder, ResearchSnapshotStore(Path(folder) / "snapshots.sqlite3") as store:
            with self.assertRaises(SnapshotStoreError):
                store.save(invalid)

    def test_nested_fact_identity_and_dependency_tampering_cannot_be_archived(self):
        bundle = self.collected()
        company = bundle.companies[0]
        insights = company.technical_insights
        fact = insights.facts[0]
        for changed in (replace(fact, instrument_id="600000.SH"),
                        replace(fact, dependencies=("absent-dependency",)),
                        replace(fact, dependencies=(fact.fact_id,))):
            with self.subTest(fact=changed):
                altered = replace(insights, facts=(changed, *insights.facts[1:]))
                invalid = replace(bundle, companies=(replace(company, technical_insights=altered),))
                with TemporaryDirectory() as folder, ResearchSnapshotStore(Path(folder) / "snapshots.sqlite3") as store:
                    with self.assertRaises(SnapshotStoreError):
                        store.save(invalid)


if __name__ == "__main__":
    unittest.main()
