"""Offline user journeys for exchange background and dated market-rule evidence."""

from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import patch

from trading_companion.errors import DataSourceError
from trading_companion.exchange_directory import DIRECTORY_SOURCE, SSE_URL
from trading_companion.market_context_live import ExchangeMarketContextSource, RISK_QUERIES, RISK_URL, SZSE_JSON_URL
from trading_companion.product_agent import run_product_research_turn
from trading_companion.product_research import collect_product_research, model_research_context
from trading_companion.research_session import ResearchSession
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_control import bind_turn, company_turn_control
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, plan
from tests.test_security_directory_refresh import sse_body, sse_payload


SECURITIES = (Security("000001.SZ", "平安银行"), Security("600000.SH", "浦发银行"))


class MetadataTransport:
    """Serve schema-faithful public-exchange responses from memory."""

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, url, params, *, referer, timeout):
        self.calls.append((url, dict(params), referer, timeout))
        if self.fail:
            raise DataSourceError("Artificial metadata outage")
        if url == SZSE_JSON_URL:
            self_code = params["txtDMorJC"]
            return json.dumps([{"metadata": {"catalogid": "1110", "tabkey": "tab1", "pageno": 1,
                "pagecount": 1, "recordcount": 1, "subname": NOW.date().isoformat() + " "},
                "data": [{"bk": "主板", "agdm": self_code,
                    "agjc": "<a href='https://www.szse.cn/'><u>平安银行</u></a>", "agssrq": "1991-04-03"}]}]).encode()
        if url == SSE_URL:
            payload = sse_payload(params["STOCK_TYPE"])
            payload["result"][0]["LIST_DATE"] = "19991110" if params["STOCK_TYPE"] == "1" else "20200716"
            return sse_body(payload)
        if url == RISK_URL:
            query = params["sqlId"]
            rows = [{"INSTRUMENT_ID": "600000", "INSTRUMENT_SHORT": "*ST人工风险样例"}] if query == RISK_QUERIES[0][0] else []
            return json.dumps({"sqlId": query, "actionErrors": [], "fieldErrors": {}, "result": rows,
                "pageHelp": {"total": len(rows), "pageNo": 1, "pageCount": 1 if rows else 0}}).encode()
        raise AssertionError("Unexpected endpoint in a fixed-source integration test")


class CountingExchangeSource(ExchangeMarketContextSource):
    """Count application fetches while retaining the actual source implementation."""

    def __init__(self, transport):
        super().__init__(transport=transport, clock=lambda: NOW)
        self.snapshot_calls = []

    def read_snapshot(self, *, directory=None, instrument_ids=None):
        self.snapshot_calls.append((directory, instrument_ids))
        return super().read_snapshot(directory=directory, instrument_ids=instrument_ids)


class MarketBackgroundIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.session, self.data = ResearchSession(), CompanyDataFixture()
        self.factory = MemorySourceFactory()
        self.directory = SecurityDirectory(SECURITIES, DIRECTORY_SOURCE, NOW, False)
        self.transport = MetadataTransport()
        self.source = CountingExchangeSource(self.transport)
        self.model = ProductModelFixture()
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No live network in background integration tests")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credential reads")),
            patch("trading_companion.product_research._build_source", side_effect=AssertionError("Use the memory source")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def run_turn(self, question, *, model=None, source=None):
        return run_product_research_turn(question, session=self.session, model=model or self.model,
            directory=self.directory, now=NOW, source="tencent", data_service=self.data,
            source_factory=self.factory, market_background_source=source or self.source)

    def collect(self, *, end_date, source=None):
        selected = replace(plan(securities=(SECURITIES[1],)), end_date=end_date, as_of=end_date)
        control = company_turn_control()
        control.start()
        with bind_turn(control):
            return collect_product_research(selected, data_service=self.data, now=NOW,
                source_factory=self.factory, market_background_source=source or self.source,
                directory=self.directory)

    def test_company_profile_fetches_background_once_and_supplies_dated_rules_to_the_model(self):
        result = self.run_turn("介绍平安银行的公司背景和交易规则")
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual(len(self.source.snapshot_calls), 1)
        self.assertEqual(self.source.snapshot_calls[0], (self.directory, ("000001.SZ",)))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][0], SZSE_JSON_URL)
        self.assertEqual(self.factory.reads, [])
        self.assertEqual(result.model_calls_attempted, 2)
        self.assertEqual(result.product_answer.status, "accepted")
        company = result.product_bundle.companies[0]
        self.assertEqual(company.market_background["board"], "szse_main")
        self.assertEqual(company.market_background["listing_date"], "1991-04-03")
        records = {item["label"]: item for item in self.model.contexts[0]["evidence"]}
        self.assertEqual(records["交易所当前板块"]["display"], "深市主板")
        self.assertEqual(records["交易所上市日期"]["display"], "1991-04-03")
        self.assertIn("未核实", records["当前风险警示观测"]["display"])
        self.assertEqual(records["交易所当前板块"]["temporal_scope"], "current_context")
        self.assertEqual(records["交易所当前板块"]["publication_precision"], "unknown")
        rule = records["有版本的普通A股交易规则"]
        self.assertEqual(rule["temporal_scope"], "market_rule_explanation")
        self.assertIn("2026", rule["display"])
        self.assertIn("2026-07-06", rule["display"])
        self.assertIn("T+1", rule["display"])
        self.assertIn("5个交易日", rule["display"])
        self.assertEqual(company.market_background["rules"]["rule_version"], "exchange-trading-2026")
        self.assertTrue(any(item.url and "szse.cn" in item.url for item in company.evidence
                            if item.temporal_scope == "market_rule_explanation"))

    def test_explicit_historical_research_never_fetches_or_exposes_current_exchange_background(self):
        expression = "2026-09-01至2026-09-20"
        model = ProductModelFixture(date_expression=expression)
        result = self.run_turn("分析平安银行" + expression + "的公司背景和基本面", model=model)
        self.assertIn(result.status, ("completed", "limited"))
        self.assertTrue(result.product_plan.explicit_historical)
        self.assertEqual(self.source.snapshot_calls, [])
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(result.product_bundle.companies[0].market_background)
        self.assertTrue(self.data.calls)
        self.assertFalse(self.data.calls[0][0].allow_current_background)
        self.assertFalse(any(item.id.startswith("exchange:") for item in result.product_bundle.evidence))
        self.assertFalse(any(item.get("temporal_scope") == "current_context" for item in model.contexts[0]["evidence"]))

    def test_metadata_outage_retains_company_and_financial_research_without_retry(self):
        transport = MetadataTransport(fail=True)
        source = CountingExchangeSource(transport)
        result = self.run_turn("介绍平安银行的公司背景和基本面", source=source)
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual(len(source.snapshot_calls), 1)
        self.assertEqual(len(transport.calls), 1)
        company = result.product_bundle.companies[0]
        missing = next(item for item in company.coverage if item["kind"] == "market_context")
        self.assertEqual(missing["status"], "unavailable")
        self.assertIn("不能替代", missing["message"])
        self.assertTrue(any(item["kind"] == "market_context" and item["status"] == "unavailable"
                            for item in self.model.contexts[0]["securities"][0]["coverage"]))
        self.assertEqual(result.product_answer.status, "accepted")
        self.assertIsNone(result.product_bundle.companies[0].market_background)
        self.assertTrue({"profile", "financial"} <= {item.kind for item in result.product_bundle.evidence})
        self.assertEqual(result.model_calls_attempted, 2)
        self.assertIsNotNone(self.session.product_saved)

    def test_current_positive_risk_membership_cannot_fill_yesterdays_st_state(self):
        bundle = self.collect(end_date=date(2026, 9, 25))
        company = bundle.companies[0]
        background = company.market_background
        self.assertEqual(background["observed_st_status"], "star_st")
        self.assertEqual(background["analysis_date_st_status"], "unknown")
        self.assertEqual(background["rules"]["st_status"], "unknown")
        self.assertEqual(background["rules"]["applied_daily_limit_percent"], None)
        evidence = next(item for item in company.evidence if item.label == "当前风险警示观测")
        self.assertIn("*ST", evidence.display)
        self.assertIn("分析截止日状态：未核实", evidence.display)
        self.assertIsNone(evidence.published_at)
        self.assertEqual(evidence.publication_precision, "unknown")
        self.assertEqual(evidence.date, "2026-09-26")
        self.assertEqual(len(self.source.snapshot_calls), 1)

    def test_same_day_positive_risk_membership_stays_dated_and_enters_rules_as_known_state(self):
        bundle = self.collect(end_date=date(2026, 9, 26))
        company = bundle.companies[0]
        background = company.market_background
        self.assertEqual(background["analysis_date_st_status"], "star_st")
        self.assertEqual(background["observation_date"], "2026-09-26")
        self.assertEqual(background["rules"]["st_status"], "star_st")
        self.assertEqual(background["rules"]["ordinary_daily_limit_percent"], 10)
        self.assertIsNone(background["rules"]["applied_daily_limit_percent"])
        record = next(item for item in model_research_context(bundle)["evidence"]
                      if item["label"] == "当前风险警示观测")
        self.assertIn("分析截止日状态：*ST", record["display"])
        self.assertEqual(record["temporal_scope"], "current_context")
        self.assertFalse(bundle.to_dict()["trading_enabled"])

    def test_two_company_profile_research_fetches_one_shared_snapshot_and_keeps_identity_binding(self):
        result = self.run_turn("比较平安银行和浦发银行的公司背景和基本面",
                               model=ProductModelFixture(("平安银行", "浦发银行")))
        self.assertIn(result.status, ("completed", "limited"))
        self.assertEqual(len(self.source.snapshot_calls), 1)
        self.assertEqual(set(self.source.snapshot_calls[0][1]), {"000001.SZ", "600000.SH"})
        self.assertEqual(len(self.transport.calls), 5)
        self.assertEqual({company.instrument_id for company in result.product_bundle.companies},
                         {"000001.SZ", "600000.SH"})
        for company in result.product_bundle.companies:
            self.assertEqual(company.market_background["instrument_id"], company.instrument_id)
            self.assertTrue(all(item.instrument_id == company.instrument_id for item in company.evidence))

    def test_saved_history_reuses_frozen_background_without_another_exchange_read(self):
        first = self.run_turn("介绍平安银行的公司背景")
        self.assertIn(first.status, ("completed", "limited"))
        count = len(self.transport.calls)
        second = self.run_turn("查看上次结果")
        self.assertEqual(second.code, "product_saved_result")
        self.assertEqual(second.product_bundle.companies[0].market_background,
                         first.product_bundle.companies[0].market_background)
        self.assertEqual(len(self.source.snapshot_calls), 1)
        self.assertEqual(len(self.transport.calls), count)


if __name__ == "__main__":
    unittest.main()
