"""Offline service journeys for screening, exchange background and cited education."""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.development_fixtures import load_development_fixtures
from trading_companion.exchange_directory import DIRECTORY_SOURCE
from trading_companion.open_knowledge import GENERAL_KNOWLEDGE_INSTRUCTION
from trading_companion.screening import demo_screening_snapshot
from trading_companion.screening_agent import SCREEN_TOOL
from trading_companion.security_directory import SecurityDirectory
from tests.test_market_background_integration import CountingExchangeSource, MetadataTransport
from tests.test_open_knowledge_api import EducationAndResearchModel, TEXT
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW
from tests.test_screening_agent import Model as ScreeningModel


SCREEN_QUESTION = "筛选PE低于20且换手率大于2%，按成交额降序选5只"
BACKGROUND_QUESTION = "介绍平安银行的公司背景和交易规则"
KNOWLEDGE_QUESTION = "怎么用PE/PB看估值"
CONDITIONS = (
    {"field": "pe_dynamic", "operator": "lt", "value": 20, "phrase": "PE低于20"},
    {"field": "turnover_percent", "operator": "gt", "value": 2, "phrase": "换手率大于2%"},
)


class ModulesModel:
    """Dispatch only to deterministic, contract-faithful model fixtures."""

    def __init__(self):
        self.calls = []
        self.company = ProductModelFixture()
        self.screening = ScreeningModel(CONDITIONS)
        self.knowledge = EducationAndResearchModel()

    def complete(self, messages, *, tools):
        self.calls.append((tuple(messages), tuple(tools)))
        if messages[0].content == GENERAL_KNOWLEDGE_INSTRUCTION:
            return self.knowledge.complete(messages, tools=tools)
        if tools and tools[0].name == SCREEN_TOOL.name:
            return self.screening.complete(messages, tools=tools)
        return self.company.complete(messages, tools=tools)


class QuoteSource:
    """Return an in-memory quote sample; no real provider or environment reads."""

    def __init__(self, snapshot):
        self.snapshot, self.calls = snapshot, []

    def read_snapshot(self, directory):
        self.calls.append(directory)
        return self.snapshot


class ProductModulesApiTests(unittest.TestCase):
    def setUp(self):
        self.model, self.data = ModulesModel(), CompanyDataFixture()
        self.daily = MemorySourceFactory()
        self.transport = MetadataTransport()
        self.background = CountingExchangeSource(self.transport)
        sample = demo_screening_snapshot(clock=lambda: NOW)
        self.directory = SecurityDirectory(sample.directory.securities, DIRECTORY_SOURCE, NOW, False)
        self.quotes = QuoteSource(replace(sample, directory=self.directory,
            source="offline_service_quote_fixture", is_fixture=False))
        self.loader = Mock(return_value=self.directory)
        self.context_loader = Mock(side_effect=AssertionError("No legacy market-context reads"))
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No live network")),
            patch("requests.get", side_effect=AssertionError("No live provider HTTP")),
            patch("requests.post", side_effect=AssertionError("No live model HTTP")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("trading_companion.product_research._build_source", self.daily),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.install()

    def install(self, **changes):
        options = dict(model_factory=lambda: self.model, directory_loader=self.loader,
            provider="fake", data_mode="tencent", reference_date=NOW.date(),
            product_data_service=self.data, screening_source=self.quotes,
            market_background_source=self.background, market_context_loader=self.context_loader,
            clock=lambda: 100.0, wall_clock=lambda: NOW)
        options.update(changes)
        self.service = ResearchApiService(**options)
        self.addCleanup(self.service.close)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]

    def send(self, question, *, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def test_natural_potential_stock_request_reaches_activity_shortlist_with_visible_defaults(self):
        self.model.screening = ScreeningModel()
        reply = self.send("帮我选几个潜力股")["result"]
        self.assertEqual(reply["stage"], "screening")
        self.assertEqual(reply["status"], "completed")
        self.assertIn("screening:activity_shortlist", reply["applied_defaults"])
        self.assertEqual(len(self.quotes.calls), 1)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.background.snapshot_calls, [])

    def assert_no_source_reads(self):
        self.assertEqual(self.quotes.calls, [])
        self.assertEqual(self.background.snapshot_calls, [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.daily.reads, [])
        self.assertEqual(self.data.calls, [])

    def test_screening_message_uses_injected_source_and_preserves_public_sample_contract(self):
        response = self.send(SCREEN_QUESTION)
        turn, screening = response["result"], response["result"]["screening"]
        self.assertFalse(response["replayed"])
        self.assertEqual((turn["status"], turn["stage"]), ("completed", "screening"))
        self.assertEqual(self.quotes.calls, [self.directory])
        self.assertEqual(self.background.snapshot_calls, [])
        self.assertEqual(self.data.calls, [])
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (1, 0))
        self.assertEqual(screening["version"], "quote-screening-result-v1")
        self.assertEqual(screening["source"]["source"], "offline_service_quote_fixture")
        self.assertEqual(screening["source"]["data_authenticity"], "vendor_snapshot")
        self.assertEqual(screening["coverage"]["scanned_records"], 5)
        self.assertEqual(screening["coverage"]["identity_verified_records"], 5)
        self.assertEqual(screening["coverage"]["condition_matched_records"], 2)
        self.assertEqual([row["instrument_id"] for row in screening["rows"]], ["920000.BJ", "300001.SZ"])
        self.assertFalse(screening["coverage"]["independent_full_market_completeness_proven"])
        for row in screening["rows"]:
            self.assertEqual(row["quote_date"], NOW.date().isoformat())
            self.assertEqual([item["status"] for item in row["condition_checks"]], ["passed", "passed"])
            self.assertLess(row["values"]["pe_dynamic"], 20)
            self.assertGreater(row["values"]["turnover_percent"], 2)
            self.assertEqual(row["sort_value"], row["values"]["amount_cny"])
        for category in ("star_market", "st", "new_listings", "suspensions"):
            self.assertEqual(screening["inclusion_policy"][category], "not_excluded")
        self.assertIsNone(screening["overall_score"])
        self.assertIsNone(screening["forecast"])
        self.assertFalse(screening["trading_enabled"])
        for internal in ("model_responses", "tool_calls", "prompt", "product_bundle"):
            self.assertNotIn(internal, turn)
        for view in ("report", "parameters", "chart", "market_context"):
            self.assertIsNone(turn[view])

    def test_company_message_uses_real_background_adapter_and_exposes_source_dated_rules(self):
        turn = self.send(BACKGROUND_QUESTION)["result"]
        self.assertEqual(turn["stage"], "company_research")
        self.assertIn(turn["status"], ("completed", "limited"))
        self.assertEqual(self.background.snapshot_calls, [(self.directory, ("000001.SZ",))])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.quotes.calls, [])
        company = turn["company_research"]
        self.assertEqual(company["version"], "product-research-answer-v3")
        self.assertFalse(company["trading_enabled"])
        self.assertEqual(len(company["market_backgrounds"]), 1)
        background = company["market_backgrounds"][0]
        self.assertEqual(background["instrument_id"], "000001.SZ")
        self.assertEqual(background["board"], "szse_main")
        self.assertEqual(background["listing_date"], "1991-04-03")
        self.assertEqual(background["observation_date"], NOW.date().isoformat())
        self.assertEqual(background["analysis_date_st_status"], "unknown")
        self.assertEqual(background["rules"]["rule_version"], "exchange-trading-2026")
        self.assertIsNone(background["rules"]["applied_daily_limit_percent"])
        evidence = self.model.company.contexts[0]["evidence"]
        self.assertTrue(any(item["label"] == "交易所当前板块" for item in evidence))
        self.assertTrue(any(item["temporal_scope"] == "market_rule_explanation" for item in evidence))

    def test_unauthorized_messages_do_not_read_any_directory_model_or_source(self):
        for question in (SCREEN_QUESTION, BACKGROUND_QUESTION, KNOWLEDGE_QUESTION):
            with self.subTest(question=question), self.assertRaises(ApiProblem) as raised:
                self.service.message(self.sid, "invalid-session-capability", uuid4().hex, question)
            self.assertEqual((raised.exception.status, raised.exception.code), (404, "session_not_found"))
        self.loader.assert_not_called()
        self.assertEqual(self.model.calls, [])
        self.assert_no_source_reads()

    def test_screening_replay_returns_detached_result_without_another_source_read(self):
        request_id = uuid4().hex
        first = self.send(SCREEN_QUESTION, request_id=request_id)
        expected = deepcopy(first["result"])
        first["result"]["screening"]["rows"][0]["values"]["price"] = -1
        replay = self.send(SCREEN_QUESTION, request_id=request_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], expected)
        replay["result"]["screening"]["coverage"]["returned_records"] = -1
        self.assertEqual(self.send(SCREEN_QUESTION, request_id=request_id)["result"], expected)
        self.assertEqual(self.quotes.calls, [self.directory])
        self.loader.assert_called_once_with(NOW)
        self.assertEqual(len(self.model.calls), 1)

    def test_background_replay_reuses_frozen_result_without_another_exchange_read(self):
        request_id = uuid4().hex
        first = self.send(BACKGROUND_QUESTION, request_id=request_id)
        expected = deepcopy(first["result"])
        first["result"]["company_research"]["market_backgrounds"][0]["board"] = "tampered"
        replay = self.send(BACKGROUND_QUESTION, request_id=request_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"], expected)
        self.assertEqual(len(self.background.snapshot_calls), 1)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(len(self.data.calls), 1)
        self.loader.assert_called_once_with(NOW)
        self.assertEqual(len(self.model.calls), 2)

    def test_reused_request_id_cannot_redirect_screening_to_company_background(self):
        request_id = uuid4().hex
        self.send(SCREEN_QUESTION, request_id=request_id)
        with self.assertRaises(ApiProblem) as raised:
            self.send(BACKGROUND_QUESTION, request_id=request_id)
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "request_id_conflict"))
        self.assertEqual(self.quotes.calls, [self.directory])
        self.assertEqual(self.background.snapshot_calls, [])
        self.assertEqual(self.data.calls, [])
        self.loader.assert_called_once_with(NOW)
        self.assertEqual(len(self.model.calls), 1)

    def test_development_mode_cannot_reach_injected_live_screening_source(self):
        development_source, development_directory = load_development_fixtures()
        self.install(data_mode="synthetic", development_source=development_source,
            directory_loader=lambda now: development_directory)
        turn = self.send(SCREEN_QUESTION)["result"]
        self.assertEqual(turn["code"], "screening_source_unavailable")
        self.assertIsNone(turn["screening"])
        self.assertEqual(self.model.calls, [])
        self.assert_no_source_reads()

    def test_development_mode_cannot_reach_injected_live_background_source(self):
        development_source, development_directory = load_development_fixtures()
        self.install(data_mode="synthetic", development_source=development_source,
            directory_loader=lambda now: development_directory)
        turn = self.send(BACKGROUND_QUESTION)["result"]
        self.assertEqual(turn["stage"], "company_research")
        self.assertEqual(turn["company_research"]["market_backgrounds"], [])
        self.assertEqual(self.background.snapshot_calls, [])
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.quotes.calls, [])

    def test_knowledge_message_exposes_local_citations_without_market_or_exchange_access(self):
        turn = self.send(KNOWLEDGE_QUESTION)["result"]
        self.assertEqual((turn["status"], turn["stage"]), ("completed", "knowledge"))
        self.assertEqual(turn["answer"], TEXT)
        knowledge = turn["knowledge"]
        self.assertEqual((knowledge["origin"], knowledge["status"]), ("model", "accepted"))
        self.assertFalse(knowledge["market_data_requested"])
        self.assertFalse(knowledge["trading_enabled"])
        self.assertFalse(knowledge["retrieval"]["network_requested"])
        self.assertEqual(knowledge["retrieval"]["status"], "matched")
        self.assertEqual(len(knowledge["citations"]), 1)
        citation = knowledge["citations"][0]
        self.assertEqual(citation["id"], "K1")
        self.assertIn(citation["snippet_id"], ("valuation_pe", "valuation_pb"))
        self.assertIn(citation["snippet_id"], knowledge["retrieval"]["snippet_ids"])
        self.assertEqual(citation["paragraph_indices"], [0])
        self.assertEqual(citation["content_origin"], "maintainer_explanation")
        self.assertTrue(citation["url"].startswith("https://"))
        self.assertTrue(citation["publisher"])
        self.assertTrue(citation["reference_version"])
        self.assertTrue(citation["excerpt"])
        self.assertFalse(citation["live_verified"])
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (1, 0))
        self.assertEqual(len(self.model.knowledge.calls), 1)
        self.context_loader.assert_not_called()
        self.assert_no_source_reads()


if __name__ == "__main__":
    unittest.main()
