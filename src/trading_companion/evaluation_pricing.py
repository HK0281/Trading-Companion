"""Auditable prices and partial estimates; neither configuration nor arithmetic makes requests."""

from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


DEFAULT_PRICES = Path(__file__).resolve().parents[2] / "data/evaluation/model-prices.json"
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
CURRENCIES = frozenset(("USD", "CNY", "EUR", "GBP", "JPY", "HKD"))
FIELDS = frozenset(("provider", "model", "currency", "verified_at", "source_url",
                    "input_per_million", "output_per_million", "input_pricing"))


def require(condition):
    if not condition:
        raise ValueError("Invalid evaluation price configuration")


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def rate(value):
    if value is None:
        return None
    require(type(value) in (str, int, float, Decimal) and type(value) is not bool)
    try:
        number = Decimal(str(value))
        require(number.is_finite() and 0 <= number <= Decimal("1000000"))
        return number
    except InvalidOperation:
        raise ValueError("Invalid evaluation price configuration") from None


def load_prices(path=DEFAULT_PRICES):
    path = Path(path)
    require(path.stat().st_size <= 200_000)
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=lambda value: require(False))
    require(type(data) is dict and data.keys() == {"version", "entries"}
            and data["version"] == "evaluation-prices-v1" and type(data["entries"]) is list
            and len(data["entries"]) <= 100)
    seen = set()
    for entry in data["entries"]:
        require(type(entry) is dict and entry.keys() == FIELDS)
        require(entry["provider"] in ("deepseek", "openai") and type(entry["model"]) is str
                and MODEL_ID.fullmatch(entry["model"]) is not None)
        key = (entry["provider"], entry["model"])
        require(key not in seen)
        seen.add(key)
        require(entry["currency"] is None or type(entry["currency"]) is str and entry["currency"] in CURRENCIES)
        require(entry["input_pricing"] in ("uniform", "cache_split"))
        values = [rate(entry[name]) for name in ("input_per_million", "output_per_million")]
        if any(value is not None for value in values) or entry["verified_at"] is not None or entry["source_url"] is not None:
            require(type(entry["verified_at"]) is str and type(entry["source_url"]) is str
                    and entry["currency"] in CURRENCIES)
            require(date.fromisoformat(entry["verified_at"]).isoformat() == entry["verified_at"]
                    and date.fromisoformat(entry["verified_at"]) <= date.today())
            url = urlsplit(entry["source_url"])
            require(url.scheme == "https" and bool(url.hostname) and not url.username and not url.password)
    return data, sha256(raw).hexdigest()


def price_for(prices, provider, model):
    return next((entry for entry in prices["entries"] if (entry["provider"], entry["model"]) == (provider, model)), None)


def money(value):
    return None if value is None else format(value.quantize(Decimal("0.000001")), "f")


def estimate_cost(prices, provider, model, calls, input_tokens=None, output_tokens=None):
    """An explicit scenario, never a hard bill cap or a forecast of actual token counts."""
    require(type(calls) is int and 0 <= calls <= 9000)
    for value in (input_tokens, output_tokens):
        require(value is None or type(value) is int and 0 <= value <= 1_000_000)
    entry = price_for(prices, provider, model)
    amount = None
    reason = "price_unknown"
    if entry and entry["verified_at"] and entry["input_pricing"] == "uniform":
        incoming, outgoing = rate(entry["input_per_million"]), rate(entry["output_per_million"])
        if incoming is not None and outgoing is not None and input_tokens is not None and output_tokens is not None:
            amount = Decimal(calls) * (Decimal(input_tokens) * incoming + Decimal(output_tokens) * outgoing) / 1_000_000
            reason = None
        else:
            reason = "token_assumption_or_price_missing"
    elif entry and entry["input_pricing"] == "cache_split":
        reason = "cache_split_unknown"
    return {"currency": entry["currency"] if entry else None, "amount": money(amount), "unknown_reason": reason,
            "assumptions": {"calls": calls, "input_tokens_per_call": input_tokens, "output_tokens_per_call": output_tokens},
            "scope": "assumed_tokens_not_a_forecast_or_hard_bill_cap"}


def actual_cost(prices, provider, model, ledger):
    entry = price_for(prices, provider, model)
    known, components, complete, reasons = Decimal(0), 0, 0, Counter()
    for record in ledger:
        usage = record["usage"]
        if not entry or not entry["verified_at"]:
            reasons["price_unknown"] += 1
            continue
        if (record["provider"], record["requested_model"], record["model"]) != (provider, model, model):
            reasons["model_identity_unknown_or_mismatch"] += 1
            continue
        prompt, completion, total = (usage.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens"))
        if any(value is not None and (type(value) is not int or not 0 <= value <= 10**12) for value in (prompt, completion, total)):
            reasons["invalid_or_unbounded_usage"] += 1
            continue
        if None not in (prompt, completion, total) and total != prompt + completion:
            reasons["inconsistent_usage"] += 1
            continue
        parts = 0
        for name, value, field in (("input", prompt, "input_per_million"), ("output", completion, "output_per_million")):
            unit = rate(entry[field])
            if value is None or unit is None or name == "input" and entry["input_pricing"] != "uniform":
                reasons[name + "_usage_or_price_unknown"] += 1
            else:
                known += Decimal(value) * unit / 1_000_000
                components += 1
                parts += 1
        complete += int(parts == 2 and total is not None)
    fully_known = bool(ledger) and complete == len(ledger)
    return {"currency": entry["currency"] if entry else None, "known_subtotal": money(known) if components else None,
            "estimated_total": money(known) if fully_known else None, "requests": len(ledger),
            "requests_with_complete_cost": complete, "unknown_requests": len(ledger) - complete,
            "unknown_reasons": dict(reasons), "complete": fully_known,
            "scope": "provider_reported_usage_price_estimate_not_billed_cost"}
