"""Compact consumed helper observations without changing market evidence or calls."""

from datetime import date, datetime
import re

from trading_companion.indicator_knowledge import DEFINITIONS, KNOWLEDGE_VERSION
from trading_companion.model import ModelRequestError
from trading_companion.tool_model import MAX_MESSAGES, ToolMessage
from trading_companion.workflow_tools import KNOWLEDGE_TOOL_NAME, SNAPSHOT_TOOL_NAME


_KNOWLEDGE_KEYS = {"status", "version", "origin", "topics", "market_data_used", "text"}
_SNAPSHOT_KEYS = {
    "status", "snapshot_key", "section", "market_data_requested", "request_id", "instrument_id",
    "source", "data_authenticity", "price_adjustment", "retrieved_at", "data_status",
    "calculation_status", "synthesis_allowed", "bars_used",
}
_FIELD_STATES = {"available", "insufficient_history", "undefined", "calculation_error"}
# Canonical English summaries of the application-owned DEFINITIONS at KNOWLEDGE_VERSION.
# Keep calculations and limitations in context: model requests do not share hidden memory.
_KNOWLEDGE_SUMMARIES = {
    "sma": "Mean of last N daily closes. Shorter windows react faster. Close above the mean describes position, not a forecast.",
    "ema": "Recent closes weigh more. Seed with first N closes' SMA, then alpha=2/(N+1). Early values depend on start; not prediction.",
    "macd": "DIF=fast EMA-slow EMA; DEA=EMA(DIF); histogram=2*(DIF-DEA). Position is not a crossing; need two adjacent valid records.",
    "dif": "Fast EMA minus slow EMA. Defaults fast=12, slow=26; use actual research parameters. The difference is not an upside probability.",
    "dea": "EMA-smoothed DIF, default signal=9; seed with the first signal-count valid DIF mean. Missing DEA is not zero even if DIF exists.",
    "histogram": "Histogram=2*(DIF-DEA). An increase alone does not prove stronger bullish momentum; consider sign, prior change and other evidence.",
    "rsi": "Wilder-smoothed average gains/losses over N close changes, range 0-100. Above 70/below 30 are reference zones, not guaranteed reversals.",
    "boll": "Mean of N closes +/- multiplier*population stddev (ddof=0). Upper-band contact does not imply reversal; zero volatility can collapse bands.",
    "atr": "Wilder-smoothed true range uses daily high/low and prior close. CNY/share volatility scale, not direction or a stop-loss price.",
    "volume": "Today, inclusive N-record mean and strictly prior N-record mean. Relative volume=today/prior mean; zero baseline is undefined, not zero.",
    "adx": "Wilder-smoothed DI and DX; ADX is strength, +DI/-DI provide direction references. 25 is a reference, never probability.",
    "obv": "Zero-seeded signed cumulative share volume; up-close adds, down-close subtracts, flat leaves it unchanged. Not currency flow.",
    "vwma": "N-record close weighted by share volume; all-zero volume window is undefined, never filled. Position is not prediction.",
}


def _iso_date(value, *, optional=False):
    if value is None:
        return optional
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _paired_calls(messages):
    """A helper may be compacted only when its own unique preceding proposal matches."""
    proposals, duplicate_ids = {}, set()
    for index, message in enumerate(messages):
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            if call.call_id in proposals:
                duplicate_ids.add(call.call_id)
            proposals[call.call_id] = (index, call)
    return {call_id: value for call_id, value in proposals.items() if call_id not in duplicate_ids}


def _knowledge(payload, arguments):
    if (payload.keys() != _KNOWLEDGE_KEYS or payload.get("status") != "knowledge_result"
            or payload.get("version") != KNOWLEDGE_VERSION
            or payload.get("origin") != "application_curated"
            or payload.get("market_data_used") is not False
            or type(payload.get("text")) is not str or not 1 <= len(payload["text"]) <= 3000
            or arguments.keys() != {"topics"}):
        return None
    topics = payload.get("topics")
    if (type(topics) is not list or not 1 <= len(topics) <= 3
            or any(type(topic) is not str or topic not in DEFINITIONS
                   or topic not in _KNOWLEDGE_SUMMARIES for topic in topics)
            or len(set(topics)) != len(topics) or arguments["topics"] != topics):
        return None
    return {
        "status": "consumed_knowledge_result", "version": KNOWLEDGE_VERSION, "topics": topics,
        "definitions": {topic: _KNOWLEDGE_SUMMARIES[topic] for topic in topics},
        "scope": "indicator_definitions_only", "note": "Canonical summary, not model memory.",
    }


def _snapshot(payload, arguments, primary):
    section = payload.get("section")
    if (section not in ("overview", "availability") or arguments != {"section": section}
            or payload.get("status") != "snapshot_summary" or payload.get("snapshot_key") != "primary"
            or payload.get("market_data_requested") is not False or primary is None):
        return None
    extra = {"range", "latest_trade_date", "indicator_count"} if section == "overview" else {"fields"}
    if payload.keys() != _SNAPSHOT_KEYS | extra:
        return None
    if (type(payload.get("request_id")) is not str
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", payload["request_id"]) is None
            or type(payload.get("instrument_id")) is not str
            or re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", payload["instrument_id"]) is None
            or payload.get("source") not in ("csv", "tencent", "akshare_eastmoney")
            or payload.get("data_authenticity") not in ("synthetic", "unverified_csv", "provider_reported")
            or payload.get("price_adjustment") not in ("unknown", "unadjusted")
            or payload.get("data_status") != "ok"
            or payload.get("calculation_status") not in ("complete", "limited", "unavailable")
            or type(payload.get("synthesis_allowed")) is not bool
            or type(payload.get("bars_used")) is not int or not 1 <= payload["bars_used"] <= 366):
        return None
    collected = payload.get("retrieved_at")
    if type(collected) is not str or not 1 <= len(collected) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(collected)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
    except ValueError:
        return None
    # The marker points only to matching facts that remain present in this exact history.
    for key in ("request_id", "instrument_id", "source", "data_authenticity", "price_adjustment",
                "retrieved_at", "data_status", "calculation_status", "synthesis_allowed", "bars_used"):
        if payload[key] != primary.get(key):
            return None
    if section == "overview":
        requested = payload.get("range")
        if (type(requested) is not list or len(requested) != 2 or not all(_iso_date(day) for day in requested)
                or requested != primary.get("range")
                or not _iso_date(payload.get("latest_trade_date"), optional=True)
                or type(payload.get("indicator_count")) is not int
                or not 1 <= payload["indicator_count"] <= 12):
            return None
    else:
        fields = payload.get("fields")
        if (type(fields) is not list or not 1 <= len(fields) <= 48
                or any(type(row) is not list or len(row) != 4
                       or type(row[0]) is not str or not 1 <= len(row[0]) <= 160
                       or type(row[1]) is not str or not 1 <= len(row[1]) <= 40
                       or type(row[2]) is not str or row[2] not in _FIELD_STATES
                       or type(row[3]) is not int or not 0 <= row[3] <= 366 for row in fields)):
            return None
    return {
        "status": "consumed_snapshot_summary", "snapshot_key": "primary", "section": section,
        "note": "See the primary research-view-v1 for unchanged facts and availability.",
    }


def compact_consumed_helpers(messages, *, consumed_message_count: int) -> list[ToolMessage]:
    """Replace only helper results within a prefix already observed by a completed model call.

    The trusted runner supplies the number of messages in its last completed model request.
    Newly appended helper results are outside that prefix and must be sent in full first.
    Calls, pairings and primary/supplement research views are never edited or removed.
    """
    if (type(messages) not in (list, tuple) or not 1 <= len(messages) <= MAX_MESSAGES
            or any(type(message) is not ToolMessage for message in messages)
            or type(consumed_message_count) is not int
            or not 0 <= consumed_message_count <= len(messages)):
        raise ModelRequestError("Helper compaction requires a bounded trusted consumed prefix")
    output = list(messages)
    paired = _paired_calls(messages)
    primary = None
    answered_ids = set()
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        payload = message.result
        if (payload.get("version") == "research-view-v1"
                and payload.get("snapshot_key") == "primary"):
            primary = payload
        proposal = paired.get(message.call_id)
        if (index >= consumed_message_count or proposal is None or proposal[0] >= index
                or proposal[1].name != message.name or message.call_id in answered_ids):
            continue
        answered_ids.add(message.call_id)
        # Versioned market observations, including all evidence aliases, stay byte-for-byte intact.
        if payload.get("version") == "research-view-v1":
            continue
        replacement = None
        if message.name == KNOWLEDGE_TOOL_NAME:
            replacement = _knowledge(payload, proposal[1].arguments)
        elif message.name == SNAPSHOT_TOOL_NAME:
            replacement = _snapshot(payload, proposal[1].arguments, primary)
        if replacement is not None:
            output[index] = ToolMessage("tool", call_id=message.call_id, name=message.name,
                                        result=replacement)
    return output
