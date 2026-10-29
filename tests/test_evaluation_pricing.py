"""Verified repository prices and test-only fixtures preserve strict accounting boundaries."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_companion.evaluation_pricing import load_prices, actual_cost, estimate_cost, price_for


def price_fixture():
    return {"version": "evaluation-prices-v1", "entries": [{"provider": "deepseek", "model": "test-model",
        "currency": "USD", "verified_at": "2026-01-01", "source_url": "https://example.invalid/test-only-prices",
        "input_per_million": "2", "output_per_million": "4", "input_pricing": "uniform"}]}


def record(prompt=1_000_000, completion=500_000, total=1_500_000, **identity):
    return {"provider": "deepseek", "requested_model": "test-model", "model": "test-model", **identity,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prices.json"

    def load(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return load_prices(self.path)[0]

    def test_repository_prices_are_verified_and_no_matching_is_fuzzy(self):
        data, digest = load_prices()
        self.assertEqual(data["entries"], [
            {"provider":"typesafe", "model":"jev-latest", "currency":"USD", "verified_at":"2026-10-28",
             "source_url":"https://typesafe.ai", "input_per_million":"0.042", "output_per_million":"0", "input_pricing":"uniform"},
            {"provider":"deepseek", "model":"deepseek-flash", "currency":"CNY", "verified_at":"2026-10-28",
             "source_url":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing",
             "input_per_million":"2", "output_per_million":"8", "input_pricing":"uniform"},
        ])
        self.assertEqual(len(digest), 64)
        self.assertIsNone(estimate_cost(data, "deepseek", "test-model", 5, 100, 100)["amount"])
        self.assertIsNone(price_for(price_fixture(), "deepseek", "test-model-latest"))
        self.assertIsNone(actual_cost(data, "deepseek", "test-model", [record()])["known_subtotal"])
        jev = estimate_cost(data, "typesafe", "jev-latest", 72, 1000, 100)
        deepseek = estimate_cost(data, "deepseek", "deepseek-flash", 72, 1000, 100)
        self.assertEqual((jev["currency"], jev["amount"]), ("USD", "0.003024"))
        self.assertEqual((deepseek["currency"], deepseek["amount"]), ("CNY", "0.201600"))
        ledger = record(provider="deepseek", requested_model="deepseek-flash", model="deepseek-flash")
        self.assertEqual(actual_cost(data, "deepseek", "deepseek-flash", [ledger])["estimated_total"], "6.000000")

    def test_typesafe_verified_prices_use_the_same_exact_identity_accounting(self):
        data = price_fixture()
        data["entries"][0].update(provider="typesafe", model="jev-latest", input_per_million="0.042", output_per_million="0")
        data = self.load(data)
        ledger = record(prompt=1_000_000, completion=12, total=1_000_012,
                        provider="typesafe", requested_model="jev-latest", model="jev-latest")
        self.assertEqual(actual_cost(data, "typesafe", "jev-latest", [ledger])["estimated_total"], "0.042000")
        ledger["model"] = "jev-served-version"
        self.assertIsNone(actual_cost(data, "typesafe", "jev-latest", [ledger])["estimated_total"])

    def test_decimal_cost_and_explicit_preview_assumptions(self):
        data = self.load(price_fixture())
        self.assertEqual(actual_cost(data, "deepseek", "test-model", [record()])["estimated_total"], "4.000000")
        preview = estimate_cost(data, "deepseek", "test-model", 2, 1_000_000, 500_000)
        self.assertEqual(preview["amount"], "8.000000")
        self.assertEqual(preview["assumptions"]["calls"], 2)
        self.assertIsNone(estimate_cost(data, "deepseek", "test-model", 2)["amount"])

    def test_partial_usage_is_a_subtotal_and_inconsistent_usage_is_unknown(self):
        data = price_fixture()
        output = actual_cost(data, "deepseek", "test-model", [record(completion=None, total=None), record(total=99)])
        self.assertEqual(output["known_subtotal"], "2.000000")
        self.assertIsNone(output["estimated_total"])
        self.assertEqual(output["unknown_requests"], 2)
        self.assertEqual(output["unknown_reasons"]["inconsistent_usage"], 1)
        self.assertIsNone(actual_cost(data, "deepseek", "test-model", [record(None, None, None)])["known_subtotal"])
        self.assertFalse(actual_cost(data, "deepseek", "test-model", [])["complete"])
        excessive = actual_cost(data, "deepseek", "test-model", [record(10**100, 0, 10**100)])
        self.assertIsNone(excessive["known_subtotal"])
        self.assertEqual(excessive["unknown_reasons"], {"invalid_or_unbounded_usage": 1})
        zero = actual_cost(data, "deepseek", "test-model", [record(0, 0, 0)])
        self.assertEqual(zero["estimated_total"], "0.000000")

    def test_cache_split_and_model_mismatch_are_not_assumed_uniform_or_identical(self):
        data = price_fixture()
        data["entries"][0]["input_pricing"] = "cache_split"
        output = actual_cost(data, "deepseek", "test-model", [record()])
        self.assertEqual(output["known_subtotal"], "2.000000")
        self.assertIsNone(output["estimated_total"])
        self.assertIsNone(estimate_cost(data, "deepseek", "test-model", 2, 10, 10)["amount"])
        output = actual_cost(price_fixture(), "deepseek", "test-model", [record(model="test-model-snapshot")])
        self.assertIsNone(output["known_subtotal"])
        self.assertEqual(output["unknown_reasons"], {"model_identity_unknown_or_mismatch": 1})

    def test_currency_selection_does_not_add_another_models_prices(self):
        data = price_fixture()
        second = {**data["entries"][0], "model": "other-model", "currency": "CNY", "input_per_million": "999"}
        data["entries"].append(second)
        self.load(data)
        output = actual_cost(data, "deepseek", "test-model", [record()])
        self.assertEqual((output["currency"], output["estimated_total"]), ("USD", "4.000000"))

    def test_invalid_prices_and_false_verification_are_rejected(self):
        changes = ({"input_per_million": -1}, {"output_per_million": "NaN"}, {"input_per_million": True},
                   {"currency": "ABC"}, {"currency": []}, {"verified_at": "9999-01-01"},
                   {"verified_at": "2026-99-99"}, {"source_url": "http://example.invalid"},
                   {"source_url": "https://secret@example.invalid"}, {"verified_at": None},
                   {"input_pricing": "guessed"}, {"arbitrary": "code"})
        for values in changes:
            candidate = price_fixture()
            candidate["entries"][0].update(values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load(candidate)
        duplicate = price_fixture()
        duplicate["entries"].append(deepcopy(duplicate["entries"][0]))
        with self.assertRaises(ValueError):
            self.load(duplicate)
        self.path.write_text('{"version":"evaluation-prices-v1","entries":[],"entries":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_prices(self.path)

    def test_unverified_empty_entry_preserves_unknown(self):
        data = price_fixture()
        data["entries"][0].update(currency=None, verified_at=None, source_url=None,
                                  input_per_million=None, output_per_million=None)
        self.load(data)
        self.assertIsNone(actual_cost(data, "deepseek", "test-model", [record()])["estimated_total"])
