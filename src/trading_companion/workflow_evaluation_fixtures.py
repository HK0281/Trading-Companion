"""Versioned artificial transport/model fixtures. Never use these to claim model quality.

Only source factories and transport clients are substituted; routing, collection,
calculation, validation and session publication are the real implementations.
"""

from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from threading import Event, Lock, Thread
from unittest.mock import patch

from trading_companion.collection_execution import CollectionExecutor, CollectionScope
from trading_companion.company_research import ResearchDataService, _ENDPOINTS
from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError
from trading_companion.open_knowledge import GENERAL_KNOWLEDGE_INSTRUCTION, GENERAL_KNOWLEDGE_VERSION
from trading_companion.product_answer import PRODUCT_ANSWER_VERSION
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_tool import TOOL_NAME
from trading_companion.screening_agent import SCREEN_TOOL
from trading_companion.screening_source import TencentQuoteSnapshotSource, TENCENT_ENDPOINT
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.tool_model import ToolCall, ToolModelResponse, validate_tool_messages
from trading_companion.workflow_response import WORKFLOW_RESPONSE_VERSION


FIXTURE_VERSION = "workflow-fixed-data-v1"
NOW = datetime(2026, 9, 26, 4, tzinfo=timezone.utc)
REFERENCE_DATE = NOW.date()
SECURITIES = (Security("000001.SZ", "平安银行", ("平安",)), Security("600000.SH", "浦发银行"),
              Security("600036.SH", "招商银行"), Security("601318.SH", "中国平安", ("平安",)),
              Security("688981.SH", "中芯国际"), Security("300001.SZ", "特锐德"),
              Security("920000.BJ", "安徽凤凰"))
DATA_PROFILES = frozenset(("normal", "financial_missing", "source_failure", "short_bars", "partial_deadline", "all_deadline"))
PHASES = frozenset(("knowledge", "screen", "company_intent", "company_answer", "company_bad_citation",
                    "company_bad_output", "technical_intent", "technical_tool", "technical_answer"))
SCRIPT_KEYS = frozenset(("phases", "mentions", "kinds", "indicators", "date_expression", "screening"))


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


def fixture_manifest():
    # Both data parameters AND fixture implementation are fingerprinted.
    descriptor = {"version": FIXTURE_VERSION, "reference_time": NOW.isoformat(),
                  "securities": [item.to_dict() for item in SECURITIES], "daily_records": 100,
                  "financial_revenue": 123000000, "financial_profit": -4560000,
                  "artificial_wait_ms": 2, "authenticity": "fixed_synthetic"}
    descriptor["implementation_sha256"] = sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    descriptor["sha256"] = sha256(json.dumps(descriptor, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return descriptor


class FixedResponse:
    def __init__(self, payload=None, *, body=None, status=200):
        self.body = json.dumps(payload, ensure_ascii=False).encode() if body is None else body
        self.status_code, self.headers, self.closed = status, {}, False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        self.closed = True


class SourceClock:
    def __init__(self):
        self._value, self._lock = 0.0, Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def expire(self):
        with self._lock:
            self._value = 21.0


class DeadlineFault:
    """Expire only after entered late work and five actual accepted results."""

    def __init__(self, profile, clock):
        self.profile, self.clock = profile, clock
        self.entered, self.accepted, self.release = Event(), Event(), Event()
        self.finished, self.failed = Event(), False
        self.count, self.thread = 0, None

    def start(self, stack):
        if self.profile not in ("partial_deadline", "all_deadline"):
            return
        original = CollectionScope.accept
        fault = self

        def accept(scope, index, value):
            original(scope, index, value)
            fault.count += 1  # Only the coordinator calls accept.
            if fault.count >= 5:
                fault.accepted.set()
        stack.enter_context(patch.object(CollectionScope, "accept", accept))
        if self.profile == "all_deadline":
            self.accepted.set()

        def advance():
            try:
                for event in (self.entered, self.accepted):
                    if not event.wait(5):
                        if not self.release.is_set():
                            self.failed = True
                        return
                if not self.release.is_set():
                    self.clock.expire()
            finally:
                self.finished.set()
        self.thread = Thread(target=advance, name="evaluation-deadline", daemon=True)
        self.thread.start()

    def before_response(self, instrument, kind):
        targeted = (self.profile == "all_deadline" or self.profile == "partial_deadline"
                    and instrument == "600036.SH" and kind == "news")
        if targeted:
            self.entered.set()
            if not self.release.wait(5):
                self.failed = True
                raise DataSourceError("Artificial deadline gate was not released")

    def close(self):
        self.release.set()
        # Wake a controller when execution never reached collection, without waiting five seconds.
        self.entered.set()
        self.accepted.set()
        if self.thread is not None:
            self.thread.join(5)
            if self.thread.is_alive():
                self.failed = True


class FixedTransport:
    """Each real collection task owns one client; bodies use actual vendor parsers."""

    def __init__(self, directory, profile, fault):
        self.directory, self.profile, self.fault = directory, profile, fault
        self.lock = Lock()
        self.active = self.maximum = self.calls = self.bytes = self.closed_clients = 0
        self.responses = []
        self.symbols = {item.instrument_id[-2:].lower() + item.instrument_id[:6]: item for item in directory.securities}

    def factory(self):
        owner = self

        class Client:
            def request(self, method, url, **kwargs):
                return owner.request(method, url, **kwargs)

            def close(self):
                with owner.lock:
                    owner.closed_clients += 1
        return Client()

    def request(self, method, url, **kwargs):
        if method != "GET" or not kwargs.get("stream") or kwargs.get("allow_redirects") is not False:
            raise AssertionError("Unexpected fixed transport protocol")
        with self.lock:
            self.calls += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            Event().wait(.002)  # Artificial I/O overlap, never a real-source latency measurement.
            if url.startswith(TENCENT_ENDPOINT):
                value = self.quotes(url)
            else:
                kind = next((key for key, (_, endpoint) in _ENDPOINTS.items() if url == endpoint), None)
                if kind not in ("profile", "financial", "news", "announcements"):
                    raise AssertionError("Unknown fixed endpoint")
                params = kwargs["params"]
                if kind in ("profile", "financial"):
                    instrument = re.search(r'SECUCODE="([0-9]{6}\.(?:SH|SZ|BJ))"', params["filter"])[1]
                else:
                    code = json.loads(params["param"])["keyword"] if kind == "news" else params["stock_list"]
                    instrument = next(item.instrument_id for item in self.directory.securities if item.instrument_id[:6] == code)
                self.fault.before_response(instrument, kind)
                value = (FixedResponse({}, status=503) if self.profile == "source_failure" and kind == "news"
                         else FixedResponse(self.company(instrument, kind)))
            with self.lock:
                self.bytes += len(value.body)
                self.responses.append(value)
            return value
        finally:
            with self.lock:
                self.active -= 1

    def company(self, instrument, kind):
        code = instrument[:6]
        if kind == "profile":
            return {"success": True, "result": {"data": [{"SECUCODE": instrument, "SECURITY_CODE": code,
                "ORG_NAME": "人工公司名称", "MAIN_BUSINESS": "人工业务资料，不代表真实经营情况。",
                "CSRC_INDUSTRY_NAME": "人工行业", "ORG_PROFILE": "人工简介", "LISTING_DATE": "2012-04-10 00:00:00"}]}}
        if kind == "financial":
            row = {"SECUCODE": instrument, "SECURITY_CODE": code, "REPORT_DATE": "2026-06-30 00:00:00",
                   "NOTICE_DATE": "2026-08-22 00:00:00", "UPDATE_DATE": "2026-08-22 00:00:00", "CURRENCY": "CNY",
                   "TOTALOPERATEREVE": 123000000, "PARENTNETPROFIT": -4560000, "TOTALOPERATEREVETZ": 25.5,
                   "PARENTNETPROFITTZ": -15.5, "ROEJQ": 8.25}
            return {"success": True, "result": {"data": [] if self.profile == "financial_missing" else [row]}}
        if kind == "news":
            return {"code": 0, "result": {"cmsArticleWebOld": [{"code": "202609250001234567",
                "title": code + "人工新闻", "content": "人工摘要，不是真实新闻。", "date": "2026-09-25 10:00:00",
                "mediaName": "人工媒体"}]}}
        return {"success": True, "data": {"list": [{"art_code": "AN202609251234567890", "title": "人工公告标题",
            "codes": [{"stock_code": code, "market_code": "1" if instrument.endswith(".SH") else "0"}],
            "display_time": "2026-09-25 10:30:00", "notice_date": "2026-09-25 00:00:00"}]}}

    def quotes(self, url):
        lines = []
        for symbol in url.removeprefix(TENCENT_ENDPOINT).split(","):
            security = self.symbols[symbol]
            fields = [""] * 88
            fields[0:3] = ["1", security.name, security.instrument_id[:6]]
            for index, value in {3: "11.30", 30: "20260925150000", 32: "0.89", 35: "11.30/1323113/1485999504",
                38: "2.68", 39: "5.04", 44: "2192.84", 45: "2192.87", 46: "0.46", 52: "4.20",
                53: "5.14", 57: "148599.9504", 82: "CNY"}.items():
                fields[index] = value
            lines.append('v_' + symbol + '="' + "~".join(fields) + '";')
        return FixedResponse(body="\n".join(lines).encode("gb18030"))

    def report(self):
        return {"requests": self.calls, "response_bytes": self.bytes, "maximum_in_flight": self.maximum,
                "closed_clients": self.closed_clients, "responses_closed": all(item.closed for item in self.responses)}


class FixedDailySource:
    adjustment, is_synthetic = "unadjusted", True

    def __init__(self, request, owner):
        self.request, self.owner = request, owner
        self.source_name, self.instrument_id = request.source, request.instrument_id

    def read_daily_bars(self):
        self.owner.daily_reads += 1
        count = 5 if self.owner.profile == "short_bars" else 100
        days, cursor = [], self.request.as_of_date
        while len(days) < count:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        return [DailyBar(self.instrument_id, day, 10, 11, 9, 10, 100000 + index * 100, 1000000)
                for index, day in enumerate(reversed(days))]


class FixedSources:
    def __init__(self, profile, workers):
        self.profile, self.daily_reads = profile, 0
        self.directory = SecurityDirectory(SECURITIES, "evaluation_fixed_synthetic", NOW, False)
        self.pool, self.clock = CollectionExecutor(), SourceClock()
        self.fault = DeadlineFault(profile, self.clock)
        self.http = FixedTransport(self.directory, profile, self.fault)
        self.company = ResearchDataService(session_factory=self.http.factory, executor=self.pool, max_workers=workers,
            clock=lambda: NOW, news_provider="eastmoney", monotonic_clock=self.clock)
        self.quotes = TencentQuoteSnapshotSource(session_factory=self.http.factory, executor=self.pool,
            max_workers=workers, clock=lambda: NOW)
        self.stack = ExitStack()

    def __enter__(self):
        self.stack.enter_context(patch("trading_companion.product_research._build_source", self.daily))
        self.stack.enter_context(patch("trading_companion.research_tool._build_source", self.daily))
        # Freeze data eligibility and retrieval metadata, not the trace/latency clock.
        for module in ("multi_analysis", "analysis", "research_tool"):
            self.stack.enter_context(patch("trading_companion." + module + ".datetime", FixedDateTime))
        self.fault.start(self.stack)
        return self

    def daily(self, request):
        return FixedDailySource(request, self)

    def __exit__(self, *args):
        self.fault.close()
        self.pool.close(wait=True)
        self.stack.close()


class ScriptedWorkflowModel:
    """A finite script independent of expectations; unexpected calls cannot adapt to routes."""

    def __init__(self):
        self.script, self.remaining = {}, []
        self.failed = False

    def start_step(self, script):
        self.script, self.remaining, self.failed = script, list(script["phases"]), False

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        if not self.remaining:
            self.failed = True
            raise AssertionError("Unexpected model call outside fixed script")
        phase = self.remaining.pop(0)
        try:
            return self._respond(phase, messages, tools)
        except Exception:
            self.failed = True
            raise

    def _respond(self, phase, messages, tools):
        if phase == "knowledge":
            if tools or messages[0].content != GENERAL_KNOWLEDGE_INSTRUCTION:
                raise AssertionError("Unexpected knowledge protocol")
            return self.payload({"version": GENERAL_KNOWLEDGE_VERSION, "paragraphs": [
                "PE比较股价与每股收益，PB比较股价与每股净资产。", "估值需结合行业、盈利持续性和资产质量理解。"],
                "citations": [{"paragraph": 0, "sources": ["K1", "K2"]}, {"paragraph": 1, "sources": ["K1", "K2"]}]})
        if phase in ("company_intent", "technical_intent", "screen"):
            name = {"company_intent": PRODUCT_INTENT_TOOL_NAME, "technical_intent": INTENT_TOOL_NAME,
                    "screen": SCREEN_TOOL.name}[phase]
            if len(tools) != 1 or tools[0].name != name:
                raise AssertionError("Unexpected intent tool")
            if phase == "screen":
                arguments = self.script["screening"]
            else:
                arguments = {"intent": "research", "instrument_mentions": self.script.get("mentions", []),
                    "date_expression": self.script.get("date_expression"), "clarification_reason": None}
                if phase == "company_intent":
                    arguments.update(research_kinds=self.script.get("kinds", []), outlook_expression=None, wants_risk=False)
                else:
                    arguments["indicator_mentions"] = self.script.get("indicators", ["SMA3"])
            return ToolModelResponse("tool_calls", tool_calls=(ToolCall("fixed_intent", name, arguments),))
        if phase.startswith("company_"):
            if tools or not any(message.role == "assistant" and message.content.startswith("UNTRUSTED_RESEARCH_CONTEXT\n")
                                for message in messages):
                raise AssertionError("Unexpected company answer protocol")
            context = json.loads(next(message.content.split("\n", 1)[1] for message in messages
                                     if message.role == "assistant" and message.content.startswith("UNTRUSTED_RESEARCH_CONTEXT\n")))
            if phase == "company_bad_output":
                return ToolModelResponse("complete", "private-evaluation-draft-never-export")
            paragraphs = []
            for company in context["securities"]:
                record = next((item for item in context["evidence"] if item["instrument_id"] == company["instrument_id"]), None)
                if record:
                    paragraphs.append({"text": company["name"] + "的人工资料只能支持有条件的观察，仍需核对其他证据。",
                                       "evidence_ids": [record["id"]]})
            if phase == "company_bad_citation":
                records = [item for item in context["evidence"] if item["kind"] == "financial"]
                revenue, profit = records[:2]
                paragraphs = [{"text": "样例营业收入为" + revenue["display"] + "，样例归母净利润为" + profit["display"] + "。",
                               "evidence_ids": [revenue["id"]],
                               "numbers": [{"text":profit['display'], "ref":revenue['id']}]}]
            if len(paragraphs) == 1:
                paragraphs.append({"text": "现有资料不保证未来方向，需要持续观察相反证据。", "evidence_ids": paragraphs[0]["evidence_ids"]})
            return self.payload({"version": PRODUCT_ANSWER_VERSION, "paragraphs": paragraphs})
        if phase in ("technical_tool", "technical_answer"):
            if not tools or TOOL_NAME not in {tool.name for tool in tools}:
                raise AssertionError("Unexpected technical workflow protocol")
            if phase == "technical_tool":
                marker = "\nconfirmed_request="
                scope = (json.JSONDecoder().raw_decode(messages[0].content.split(marker, 1)[1])[0]
                         if marker in messages[0].content else json.loads(messages[1].content))
                scope = scope.get("confirmed_request", scope)
                return ToolModelResponse("tool_calls", tool_calls=(ToolCall("fixed_primary", TOOL_NAME, scope),))
            primary = next(message.result for message in messages if message.role == "tool"
                           and message.result.get("version") == "research-view-v1")
            states = {row[0]: row[1] for row in primary["tables"]["states"]["rows"]}
            relation = next(row for row in primary["tables"]["relations"]["rows"]
                            if row[0] is not None and states[row[3]] == "available")
            claim = {"snapshot": "primary", "evidence_id": relation[0], "relation": relation[2], "value": relation[4]}
            return self.payload({"version": WORKFLOW_RESPONSE_VERSION, "request_id": primary["request_id"],
                "instrument_id": primary["instrument_id"], "trade_date": primary["dates"][0], "claims": [claim],
                "paragraphs": [{"role": role, "text": text, "evidence_ids": ["primary:" + relation[0]]}
                    for role, text in (("answer", "已有人工指标关系需要结合样本范围理解。"),
                                       ("outlook", "如果价格保持稳定，可继续观察已有关系。"),
                                       ("watch_for", "应持续观察相反信号，指标不保证未来方向。"))]})
        raise AssertionError("Unknown script phase")

    @staticmethod
    def payload(value):
        return ToolModelResponse("complete", json.dumps(value, ensure_ascii=False))
