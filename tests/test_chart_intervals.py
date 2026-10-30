"""Offline chart arithmetic, transport, API binding, isolation and lifecycle regressions."""

from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from trading_companion import _indicator_values as kernel, indicators
from trading_companion.api import create_app
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.chart_hourly import MAX_RESPONSE_BYTES, parse_tencent_hours, read_tencent_hours
from trading_companion.chart_intervals import (
    ChartUnavailable, SHANGHAI, bind_chart, build_interval_chart, capabilities, two_hour_bars, weekly_bars,
)
from trading_companion.daily_bar import DailyBar
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel, QUESTION
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.technical_insights import aggregate_completed_weeks
from trading_companion.tool_model import ToolCall

NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)


def hourly_payload(symbol="sz000001", days=("20260924", "20260925")):
    return {"code": 0, "data": {symbol: {"m60": [
        [day + endpoint, "10", "11", "12", "9", "1.25", {}, "ignored"]
        for day in days for endpoint in ("1030", "1130", "1400", "1500")
    ]}}}


def hours():
    return parse_tencent_hours(hourly_payload(), "000001.SZ", now=NOW)


class KernelTests(unittest.TestCase):
    def test_all_ten_algorithms_match_pre_extraction_golden_and_daily_wrappers(self):
        # Golden captured from 0462857 before moving any arithmetic, never recalculated by the test.
        golden = json.loads((Path(__file__).parent / "fixtures/chart-indicator-golden.json").read_text())
        rows = [DailyBar("000001.SZ", date(2026, 1, 1) + timedelta(days=i), 10 + i % 7, 20,
                        5, 10.5 + i % 5, 0 if i % 6 == 0 else (i + 1) * 100, 0) for i in range(64)]
        numeric = [kernel.NumericBar(b.open_price, b.high_price, b.low_price, b.close_price, b.volume_shares) for b in rows]
        for kind, case in golden.items():
            with self.subTest(kind=kind):
                daily = getattr(indicators, case["function"])(rows, **case["parameters"])
                shared = getattr(kernel, case["function"])(numeric, **case["parameters"])
                self.assertEqual(daily, shared)
                self.assertEqual([asdict(v) if is_dataclass(v) else v for v in shared], case["values"])
                with self.assertRaises(ValueError):
                    getattr(indicators, case["function"])(numeric, **case["parameters"])
                with self.assertRaises(ValueError):
                    getattr(indicators, case["function"])([rows[0], rows[0]], **case["parameters"])


class HourlyTests(unittest.TestCase):
    def test_real_contract_end_labels_board_units_and_no_lunch_pair(self):
        bars = hours()
        self.assertEqual([b.start.strftime("%H:%M") for b in bars[:4]], ["09:30", "10:30", "13:00", "14:00"])
        self.assertEqual(bars[0].values.volume_shares, 125)
        merged = two_hour_bars(bars)
        self.assertEqual([(b.start.strftime("%H:%M"), b.end.strftime("%H:%M")) for b in merged[:2]],
                         [("09:30", "11:30"), ("13:00", "15:00")])
        self.assertEqual(merged[0].values.volume_shares, 250)
        for symbol, instrument, volume in (("sh600519", "600519.SH", 125), ("sz300059", "300059.SZ", 125),
                                            ("sh688981", "688981.SH", 2)):
            payload = hourly_payload(symbol)
            if symbol.startswith("sh688"):
                for row in payload["data"][symbol]["m60"]:
                    row[5] = "2.00"
            self.assertEqual(parse_tencent_hours(payload, instrument, now=NOW)[0].values.volume_shares, volume)

    def test_whole_batch_rejects_pollution_including_rows_outside_research_window(self):
        mutations = [lambda r: r.reverse(), lambda r: r.append(r[-1]), lambda r: r.__setitem__(0, r[1]),
            lambda r: r[0].__setitem__(0, "202609261030"), lambda r: r[0].__setitem__(0, "202609241200"),
            lambda r: r[0].__setitem__(0, "202609311030"), lambda r: r[0].__setitem__(0, "202609281030"),
            lambda r: r[0].__setitem__(1, True), lambda r: r[0].__setitem__(1, "NaN"),
            lambda r: r[0].__setitem__(3, "8"), lambda r: r[0].__setitem__(5, "-1"),
            lambda r: r[0].__setitem__(5, "0.001"), lambda r: r[0].__setitem__(5, "1e10000"),
            lambda r: r.extend(r * 41)]
        for mutate in mutations:
            payload = hourly_payload()
            mutate(payload["data"]["sz000001"]["m60"])
            with self.subTest(mutate=mutate), self.assertRaises(ChartUnavailable):
                parse_tencent_hours(payload, "000001.SZ", now=NOW)
        with self.assertRaises(ChartUnavailable):
            parse_tencent_hours(hourly_payload("sh600519"), "000001.SZ", now=NOW)
        for bad in (hours()[1:], (hours()[0], hours()[2]), (hours()[3], hours()[4])):
            with self.assertRaises(ChartUnavailable):
                two_hour_bars(bad)

    def connection(self, chunks, *, length=None, status=200):
        response = Mock(status=status)
        response.getheader.side_effect = lambda name, default=None: length if name == "Content-Length" else default
        response.read1.side_effect = chunks
        connection = Mock()
        connection.getresponse.return_value = response
        return connection, response, Mock(return_value=connection)

    def test_bounded_fixed_transport_reads_and_closes_without_retries_or_redirects(self):
        body = json.dumps(hourly_payload()).encode()
        connection, response, factory = self.connection([body, b""], length=str(len(body)))
        actual = read_tencent_hours("000001.SZ", deadline=108, clock=lambda: 100, now=NOW, connection_factory=factory)
        self.assertEqual(actual, hours())
        factory.assert_called_once_with("ifzq.gtimg.cn", timeout=8)
        request = connection.request.call_args
        self.assertEqual(request.args[0], "GET")
        self.assertIn("param=sz000001%2Cm60%2C%2C320", request.args[1])
        response.close.assert_called_once()
        connection.close.assert_called_once()
        for length, chunks, status in ((str(MAX_RESPONSE_BYTES + 1), [], 200),
                                       (None, [b"x" * (MAX_RESPONSE_BYTES + 1)], 200), (None, [], 302)):
            connection, response, factory = self.connection(chunks, length=length, status=status)
            with self.subTest(length=length, status=status), self.assertRaises(ChartUnavailable):
                read_tencent_hours("000001.SZ", deadline=108, clock=lambda: 100, now=NOW, connection_factory=factory)
            factory.assert_called_once()
            connection.close.assert_called_once()

    def test_deadline_checked_between_reads_and_transport_timeout_is_fixed(self):
        connection, response, factory = self.connection([b"{}"])
        seconds = [100]
        def slow(*args):
            seconds[0] = 109
            return b"{}"
        response.read1.side_effect = slow
        with self.assertRaises(ChartUnavailable) as caught:
            read_tencent_hours("000001.SZ", deadline=108, clock=lambda: seconds[0], now=NOW, connection_factory=factory)
        self.assertEqual(caught.exception.code, "chart_timeout")
        self.assertEqual(response.read1.call_count, 1)
        connection.connect.side_effect = TimeoutError("private source error")
        with self.assertRaises(ChartUnavailable) as caught:
            read_tencent_hours("000001.SZ", deadline=108, clock=lambda: 100, now=NOW, connection_factory=factory)
        self.assertEqual(str(caught.exception), "chart_timeout")


class ChartServiceTests(unittest.TestCase):
    def setUp(self):
        self.source, directory = load_development_fixtures()
        directory = replace(directory, is_fixture=False, source="offline-chart-contract-test", retrieved_at=NOW)
        self.seconds = 100.0
        self.messages = []
        class CapturingModel(ConversationDemoModel):
            def complete(model, messages, *, tools):
                self.messages.append(deepcopy(messages))
                response = super().complete(messages, tools=tools)
                return replace(response, tool_calls=tuple(ToolCall(call.call_id, call.name, {**call.arguments, "source": "tencent"})
                    if call.name == "analyze_daily_indicators" else call for call in response.tool_calls))
        self.hourly = Mock(return_value=hours())
        self.service = ResearchApiService(model_factory=CapturingModel, directory_loader=lambda _: directory,
            data_mode="tencent", reference_date=REFERENCE_DATE, wall_clock=lambda: NOW,
            clock=lambda: self.seconds, chart_hourly_loader=self.hourly)
        self.addCleanup(self.service.close)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        self.entry = self.service._entries[self.sid]
        with patch("trading_companion.tencent_adapter.TencentDailyBarSource.read_daily_bars", return_value=self.source.read_daily_bars()):
            self.original = self.service.message(self.sid, self.token, uuid4().hex, QUESTION)
        self.research_id = self.original["result"]["report"]["request_id"]
        self.assertEqual(self.original["result"]["chart"]["status"], "available")

    def get(self, interval="60m"):
        return self.service.chart(self.sid, self.token, self.research_id, interval)

    def problem(self, code, callback):
        with self.assertRaises(ApiProblem) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_demand_only_cache_cross_period_and_actual_research_model_isolation(self):
        before = (deepcopy(self.entry.session.to_dict()), deepcopy(self.entry.records), deepcopy(self.messages),
                  self.entry.model.forwarded, self.entry.messages_used)
        self.hourly.assert_not_called()
        caps = self.service.chart_capabilities(self.original["result"])
        self.assertEqual(caps["research_id"], self.research_id)
        self.assertTrue(caps["intervals"]["120m"]["enabled"])
        first = self.get()
        self.assertEqual(first["status"], "available")
        self.assertEqual(first["bars"][0]["end_at"], "2026-09-24T10:30:00+08:00")
        self.assertEqual(self.get("120m")["bars"][0]["volume"], 250)
        self.assertEqual(self.get("1w")["bars"][0]["complete"], False)
        self.assertEqual(self.get(), first)
        self.hourly.assert_called_once()
        self.assertEqual(self.hourly.call_args.kwargs["deadline"], 108)
        self.assertEqual(before, (self.entry.session.to_dict(), self.entry.records, self.messages,
                                  self.entry.model.forwarded, self.entry.messages_used))
        self.seconds += 61
        self.hourly.return_value = tuple(replace(b, values=replace(b.values, close_price=10.5)) for b in hours())
        self.assertNotEqual(self.get()["bars"], first["bars"])
        self.assertEqual(before, (self.entry.session.to_dict(), self.entry.records, self.messages,
                                  self.entry.model.forwarded, self.entry.messages_used))

    def test_failure_ttl_timeout_and_pair_failure_do_not_damage_daily_or_sixty(self):
        self.hourly.side_effect = ChartUnavailable("chart_hourly_invalid")
        self.assertEqual(self.get()["code"], "chart_hourly_invalid")
        self.get()
        self.hourly.assert_called_once()
        self.seconds += 11
        self.hourly.side_effect = None
        self.hourly.return_value = hours()[1:]
        self.assertEqual(self.get("120m")["code"], "chart_hourly_pair_missing")
        self.assertEqual(self.get()["status"], "available")
        self.assertEqual(self.entry.records[self.original["request_id"]].response, self.original)

    def test_binding_auth_other_session_retired_and_http_request_id_rejected(self):
        other = self.service.create_session()
        for sid, token, research in ((self.sid, "bad", self.research_id), (other["session_id"], other["access_token"], self.research_id),
                                     (self.sid, self.token, self.original["request_id"]), (self.sid, self.token, "missing")):
            with self.assertRaises(ApiProblem):
                self.service.chart(sid, token, research, "60m")
        self.service.reset(self.sid, self.token)
        self.problem("chart_research_not_found", self.get)
        self.hourly.assert_not_called()

    def test_chat_reset_invalidates_charts_without_changing_saved_chat_replay(self):
        self.get()
        reset = self.service.message(self.sid, self.token, uuid4().hex, "新对话")
        self.assertEqual(reset["result"]["code"], "session_reset")
        self.problem("chart_research_not_found", self.get)
        self.assertFalse(self.entry.chart_cache)
        replay = self.service.message(self.sid, self.token, self.original["request_id"], QUESTION)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], self.original["result"])
        self.assertIsNone(self.service.chart_capabilities(replay["result"], session_id=self.sid, token=self.token))

    def test_source_capability_server_rejection_and_no_minute_io(self):
        record = self.entry.records[self.original["request_id"]]
        for mode, source, authenticity, adjustment in (("synthetic", "csv", "synthetic", "unknown"),
                ("synthetic", "csv", "unverified_csv", "unknown"), ("akshare_eastmoney", "akshare_eastmoney", "provider_reported", "unadjusted")):
            turn = deepcopy(self.original["result"])
            turn["parameters"]["source"] = source
            turn["chart"].update(source=source, data_authenticity=authenticity, price_adjustment=adjustment)
            record.response["result"] = turn
            self.service.data_mode = mode
            self.entry.chart_cache.clear()
            self.assertEqual(self.get()["code"], "chart_hourly_source_unsupported")
            weekly = self.get("1w")
            self.assertEqual(weekly["data_authenticity"], authenticity)
            self.assertFalse(capabilities(bind_chart(turn, mode), mode)["intervals"]["60m"]["enabled"])
        self.hourly.assert_not_called()

    def test_empty_historical_window_never_replaced_by_latest_and_deadline_failure_cached(self):
        self.hourly.return_value = tuple(replace(b, start=b.start - timedelta(days=20), end=b.end - timedelta(days=20)) for b in hours())
        self.assertEqual(self.get()["code"], "chart_insufficient_history")
        self.seconds += 11
        def late(*args, **kwargs):
            self.seconds += 9
            return hours()
        self.hourly.side_effect = late
        self.assertEqual(self.get()["code"], "chart_timeout")
        self.assertFalse(self.entry.chart_base_cache)

    def start_background(self):
        entered, release, result = Event(), Event(), []
        def load(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("chart test release missing")
            return hours()
        self.hourly.side_effect = load
        def run():
            try:
                result.append(self.get())
            except ApiProblem as error:
                result.append(error.code)
        thread = Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(2))
        return release, result, thread

    def test_single_session_gate_and_reset_delete_expiry_close_reject_late_cache(self):
        for action in ("reset", "delete", "expire", "close"):
            with self.subTest(action=action):
                if action != "reset":
                    self.setUp()
                release, result, thread = self.start_background()
                self.problem("chart_busy", self.get)
                if action == "expire":
                    self.seconds += 1801
                elif action == "close":
                    self.service.close()
                else:
                    getattr(self.service, action)(self.sid, self.token)
                release.set(); thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertIn(result[0], ("chart_research_not_found", "session_not_found", "service_closed"))
                self.assertFalse(self.entry.chart_cache)
                self.assertFalse(self.entry.chart_base_cache)
                self.assertEqual(self.service._chart_active, 0)

    def test_http_contract_token_same_origin_query_allowlist_and_fixed_response(self):
        with TestClient(create_app(service=self.service), base_url="http://localhost") as client:
            path = f"/v1/sessions/{self.sid}/chart"
            params = {"research_id": self.research_id, "interval": "60m"}
            headers = {"Authorization": "Bearer " + self.token}
            self.assertEqual(client.get(path, params=params).status_code, 401)
            self.assertEqual(client.get(path, params=params, headers={**headers, "Origin": "https://other.test"}).status_code, 403)
            for extra in ({"instrument_id": "600519.SH"}, {"source": "https://other.test"}, {"limit": 1000}, {"interval": "5m"}):
                self.assertEqual(client.get(path, params={**params, **extra}, headers=headers).status_code, 422)
            response = client.get(path, params=params, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["interval"], "60m")

    def test_process_gate_is_two_and_network_does_not_hold_global_lock(self):
        others = [self.service.create_session() for _ in range(2)]
        for credentials in others:
            self.service._entries[credentials["session_id"]].records = deepcopy(self.entry.records)
        entered, release, second_result = Event(), Event(), []
        first_release, first_result, first_thread = self.start_background()
        def second_loader(*args, **kwargs):
            entered.set()
            release.wait(3)
            return hours()
        self.hourly.side_effect = second_loader
        second = others[0]
        thread = Thread(target=lambda: second_result.append(self.service.chart(second["session_id"], second["access_token"], self.research_id, "60m")))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.service.inspect(self.sid, self.token)["state"], self.entry.session.to_dict()["state"])
            last = others[1]
            self.problem("chart_busy", lambda: self.service.chart(last["session_id"], last["access_token"], self.research_id, "60m"))
        finally:
            first_release.set(); release.set()
            first_thread.join(3); thread.join(3)
        self.assertEqual(first_result[0]["status"], "available")
        self.assertEqual(second_result[0]["status"], "available")
        self.assertEqual(self.service._chart_active, 0)

    def test_multistock_binding_and_payload_limit_are_rejected(self):
        turn = deepcopy(self.original["result"])
        turn["company_research"] = {"securities": [{"instrument_id": "000001.SZ"}, {"instrument_id": "600519.SH"}]}
        with self.assertRaises(ChartUnavailable):
            bind_chart(turn, "tencent")
        binding = bind_chart(self.original["result"], "tencent")
        with patch("trading_companion.chart_intervals.MAX_JSON_BYTES", 100), self.assertRaises(ChartUnavailable) as caught:
            build_interval_chart(binding, "60m", hours(), NOW)
        self.assertEqual(caught.exception.code, "chart_payload_limit")

    def test_week_aggregation_truncated_start_holiday_gaps_and_period_recalculation(self):
        binding = bind_chart(self.original["result"], "tencent")
        dates = (date(2026, 9, 14), date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 25))
        daily = tuple(replace(binding.bars[i], trade_date=day) for i, day in enumerate(dates))
        request = replace(binding.request, start_date=date(2026, 9, 14))
        binding = replace(binding, request=request, bars=daily)
        bars = weekly_bars(binding)
        completed = aggregate_completed_weeks(daily, start_date=request.start_date, as_of_date=request.as_of_date)
        self.assertEqual(len(completed), 1)
        self.assertEqual(bars[0].values.close_price, completed[0].close_price)
        self.assertTrue(bars[0].complete)
        self.assertFalse(bars[-1].complete)
        weekly = build_interval_chart(binding, "1w", bars, NOW)
        self.assertEqual(weekly["series"][0]["values"], [None, None])  # SMA3 needs three weeks.
        truncated = replace(binding, request=replace(request, start_date=date(2026, 9, 15)))
        self.assertEqual(len(weekly_bars(truncated)), 1)
        with self.assertRaises(ChartUnavailable):
            weekly_bars(replace(binding, request=replace(request, start_date=date(2026, 9, 22))))
