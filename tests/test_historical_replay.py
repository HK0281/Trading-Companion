from dataclasses import replace
from datetime import date, timedelta
from io import StringIO
from contextlib import redirect_stdout
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.historical_replay import (
    ReplayConfig, build_historical_replay, replay_archived_snapshot, replay_bars, replay_config_from_question,
)
from trading_companion.replay_cli import main
from trading_companion.snapshot_store import ResearchSnapshotStore
from tests.test_snapshot_store import make_bundle, OBSERVED


def bars(prices, opens=None):
    opens = prices if opens is None else opens
    return [DailyBar("000001.SZ", date(2026, 1, 1) + timedelta(days=index), opening,
                    max(opening, close), min(opening, close), close, 1000, close * 1000)
            for index, (opening, close) in enumerate(zip(opens, prices))]


def replay(records, **kwargs):
    return replay_bars(records, request_id="replay-fixture", instrument_id="000001.SZ",
                       config=ReplayConfig(2, kwargs.get("cost", 0)))


class HistoricalReplayTests(unittest.TestCase):
    def test_explicit_cost_and_sma_are_honoured_without_parameter_search(self):
        from trading_companion.multi_contracts import IndicatorSpec
        specs = (IndicatorSpec("sma", window=7),)
        config = replay_config_from_question("回测平安银行SMA7，交易成本0.1%", specs, indicators_explicit=True)
        self.assertEqual(config, ReplayConfig(7, 10))
        with self.assertRaises(ValueError):
            replay_config_from_question("回测平安银行，成本越低越好", specs)
        with self.assertRaises(ValueError):
            replay_config_from_question("回测SMA7和RSI14", (*specs, IndicatorSpec("rsi")), indicators_explicit=True)
        for question in ("回测SMA7做空", "回测SMA7加止损", "回测周线SMA7"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                replay_config_from_question(question, specs, indicators_explicit=True)

    def test_config_rejects_invalid_values(self):
        for window in (True, 1, 121, 2.5, "20"):
            with self.subTest(window=window), self.assertRaises(ValueError):
                ReplayConfig(window=window)
        for cost in (True, -1, 101, float("nan"), float("inf"), "5"):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                ReplayConfig(cost_bps=cost)

    def test_warmup_needs_signal_and_a_next_open_interval(self):
        for count in range(4):
            result = replay(bars(list(range(10, 10 + count))))
            self.assertEqual(result.code, "replay_insufficient_history")
            self.assertIsNone(result.total_return_percent)

    def test_rising_market_matches_same_interval_buy_hold_without_cost(self):
        result = replay(bars([10, 11, 12, 13, 14]))
        self.assertEqual(result.status, "available")
        self.assertEqual(result.start_date, "2026-01-03")
        self.assertEqual(result.end_date, "2026-01-05")
        self.assertAlmostEqual(result.total_return_percent, (14 / 12 - 1) * 100)
        self.assertAlmostEqual(result.benchmark_return_percent, result.total_return_percent)
        self.assertEqual(result.max_drawdown_percent, 0)
        self.assertEqual(result.turnover_count, 2)
        self.assertEqual(result.active_intervals, 2)

    def test_downtrend_stays_cash_while_benchmark_declines(self):
        result = replay(bars([14, 13, 12, 11, 10]))
        self.assertEqual(result.total_return_percent, 0)
        self.assertAlmostEqual(result.benchmark_return_percent, (10 / 12 - 1) * 100)
        self.assertAlmostEqual(result.benchmark_max_drawdown_percent, (1 - 10 / 12) * 100)
        self.assertEqual(result.turnover_count, 0)

    def test_current_close_cannot_change_exposure_at_current_open(self):
        records = bars([10, 11, 12, 13, 14])
        changed = list(records)
        changed[2] = replace(changed[2], close_price=1, low_price=1)
        first, second = replay(records), replay(changed)
        self.assertEqual(first.steps[0].exposure, second.steps[0].exposure)
        self.assertEqual(first.steps[0].equity, second.steps[0].equity)
        self.assertNotEqual(first.steps[1].exposure, second.steps[1].exposure)
        self.assertEqual(first.steps[0].signal_date, "2026-01-02")
        self.assertEqual(first.steps[0].start_date, "2026-01-03")

    def test_prefix_steps_do_not_change_when_future_bars_are_added(self):
        records = bars([10, 11, 9, 12, 10, 14, 13])
        short, long = replay(records[:5]), replay(records)
        self.assertEqual(short.steps, long.steps[:len(short.steps)])

    def test_gaps_use_the_next_observed_open_without_same_close_execution(self):
        result = replay(bars([10, 11, 12, 13], [10, 11, 20, 10]))
        self.assertAlmostEqual(result.total_return_percent, -50)
        self.assertEqual(result.steps[0].signal_close, 11)
        self.assertEqual(result.steps[0].interval_return, -0.5)

    def test_fixed_turnover_cost_charges_entry_and_final_exit_to_both_curves(self):
        result = replay(bars([10, 11, 12, 13]), cost=100)
        expected = (0.99 * 13 / 12 * 0.99 - 1) * 100
        self.assertAlmostEqual(result.total_return_percent, expected)
        self.assertAlmostEqual(result.benchmark_return_percent, expected)
        self.assertEqual(result.final_liquidation_cost, 0.01)
        self.assertEqual(result.steps[0].turnover_cost, 0.01)
        self.assertAlmostEqual(result.max_drawdown_percent, 1)
        self.assertAlmostEqual(result.benchmark_max_drawdown_percent, 1)

    def test_equal_price_to_sma_is_cash(self):
        result = replay(bars([10, 10, 10, 10]))
        self.assertEqual(result.steps[0].exposure, 0)

    def test_drawdown_uses_path_not_only_final_return(self):
        result = replay(bars([10, 11, 12, 6, 18]))
        self.assertAlmostEqual(result.max_drawdown_percent, 50)

    def test_duplicate_dates_cross_security_and_malformed_records_are_rejected(self):
        original = bars([10, 11, 12, 13])
        cases = ([original[0], original[0]], [replace(original[0], instrument_id="600000.SH")],
                 [{"close_price": 10}], list(reversed(original)))
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replay(values)

    def test_snapshot_binding_is_checked_and_network_is_not_used(self):
        bundle = make_bundle(technical=True)
        technical = bundle.companies[0].technical
        with patch("socket.socket.connect", side_effect=AssertionError("No replay network")):
            output = build_historical_replay(technical, expected_request=technical.request,
                                            config=ReplayConfig(2, 0))
        self.assertEqual(output.data_authenticity, "synthetic")
        self.assertEqual(output.price_adjustment, "unknown")
        with self.assertRaises(ValueError):
            build_historical_replay(technical, expected_request=replace(technical.request, request_id="another"))

    def test_archive_replay_is_read_only_and_observation_cutoff_remains_enforced(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "archive.sqlite3"
            with ResearchSnapshotStore(path, clock=lambda: OBSERVED) as store:
                identifier = store.save(make_bundle(technical=True))
                output = replay_archived_snapshot(store.load(identifier, known_at=OBSERVED),
                    instrument_id="000001.SZ", config=ReplayConfig(2, 0))
                self.assertEqual(output.status, "available")
                self.assertEqual(len(store.list()), 1)
            buffer = StringIO()
            with redirect_stdout(buffer):
                status = main(["--db", str(path), "replay", identifier, "--stock", "000001.SZ", "--window", "2"])
            self.assertEqual(status, 0)
            public = json.loads(buffer.getvalue())
            self.assertFalse(public["trading_enabled"])
            self.assertEqual(public["data_authenticity"], "synthetic")
            self.assertIn("no_lot_size_limit_price_liquidity_or_order_fill_model", public["limitation_codes"])

    def test_archive_replay_rejects_another_stock_and_date_tampering(self):
        with TemporaryDirectory() as folder, ResearchSnapshotStore(Path(folder) / "archive", clock=lambda: OBSERVED) as store:
            payload = store.load(store.save(make_bundle(technical=True)))
            with self.assertRaises(ValueError):
                replay_archived_snapshot(payload, instrument_id="600000.SH")
            payload["companies"][0]["technical"]["bars"][0]["trade_date"] = "2020-01-01"
            with self.assertRaises(ValueError):
                replay_archived_snapshot(payload, instrument_id="000001.SZ")

    def test_product_question_reuses_one_daily_read_and_archives_the_same_replay(self):
        from trading_companion.product_agent import run_product_research_turn
        from trading_companion.research_session import ResearchSession
        from tests.test_product_agent import ProductModelFixture
        from tests.test_product_research import CompanyDataFixture, NOW, directory
        from tests.test_technical_research_module import LongHistoryFactory
        factory, model = LongHistoryFactory(), ProductModelFixture()
        with patch("socket.socket.connect", side_effect=AssertionError("No network")):
            result = run_product_research_turn("回测平安银行SMA7，比较同期持有收益和最大回撤，交易成本0.1%", session=ResearchSession(),
                model=model, directory=directory(), now=NOW, source="tencent",
                data_service=CompanyDataFixture(), source_factory=factory)
        company = result.product_bundle.companies[0]
        self.assertEqual(company.historical_replay.status, "available")
        self.assertEqual(company.historical_replay.config, ReplayConfig(7, 10))
        self.assertEqual(len(factory.reads), 1)
        self.assertEqual(result.analyses_used, 1)
        self.assertEqual(result.model_calls_attempted, 2)
        self.assertEqual(result.product_plan.kinds, ("technical",))
        records = model.contexts[0]["evidence"]
        summaries = [item for item in records if item["temporal_scope"] == "historical_exposure_replay"]
        self.assertEqual(len(summaries), 9)
        self.assertFalse(any("steps" in item for item in summaries))
        with TemporaryDirectory() as folder, ResearchSnapshotStore(Path(folder) / "archive") as store:
            payload = store.load(store.save(result.product_bundle))
            self.assertEqual(payload["companies"][0]["historical_replay"], company.historical_replay.to_dict())

    def test_unsupported_replay_preserves_technical_results_without_claiming_strategy_metrics(self):
        from trading_companion.product_agent import run_product_research_turn
        from trading_companion.research_session import ResearchSession
        from tests.test_product_agent import ProductModelFixture
        from tests.test_product_research import CompanyDataFixture, NOW, directory
        from tests.test_technical_research_module import LongHistoryFactory
        result = run_product_research_turn("回测平安银行，只看RSI7", session=ResearchSession(),
            model=ProductModelFixture(), directory=directory(), now=NOW, source="tencent",
            data_service=CompanyDataFixture(), source_factory=LongHistoryFactory())
        company = result.product_bundle.companies[0]
        self.assertIsNone(company.historical_replay)
        self.assertIsNotNone(company.technical)
        self.assertTrue(any(item.id.endswith("|replay:unsupported") for item in company.evidence))


if __name__ == "__main__":
    unittest.main()
