"""Bounded public A-share context. No model dependency, URL input, login, retry or silent failover."""

from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
from functools import partial
import json
from math import isfinite
import re
from time import monotonic
from threading import Lock
from urllib.parse import urlencode

from trading_companion.company_research_contracts import (
    RESEARCH_KINDS, SHANGHAI, ResearchDataBatch, ResearchDataRequest, ResearchDataSection, ResearchEvidence, aware,
)
from trading_companion.company_news_sina import SinaCompanyNewsAdapter
from trading_companion.collection_execution import (
    CollectionExecutor, CollectionScope, CollectionUnavailable, POLL_SECONDS, shared_collection_executor,
)
from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import checkpoint, request_timeout
from trading_companion.turn_trace import trace_event


_ENDPOINTS = {
    "profile": ("GET", "https://datacenter-web.eastmoney.com/api/data/v1/get"),
    "financial": ("GET", "https://datacenter.eastmoney.com/securities/api/data/get"),
    "news": ("GET", "https://search-api-web.eastmoney.com/search/jsonp"),
    "announcements": ("GET", "https://np-anotice-stock.eastmoney.com/api/security/ann"),
    "cninfo_directory": ("GET", "https://www.cninfo.com.cn/new/data/szse_stock.json"),
    "cninfo_announcements": ("POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query"),
}
_SOURCES = {"profile": "eastmoney_company", "financial": "eastmoney_financial",
            "news": "eastmoney_news", "announcements": "eastmoney_announcements"}
_CALLBACK = "tcCompanyResearch"
_SNAPSHOT_WARNING = "Current vendor snapshot filtered by publication dates; historical revision versions are not verified."


class _Rejected(ValueError):
    pass


class _CollectionEvents:
    """Finish only entered, unaccepted sources on a source deadline, without joining workers."""

    def __init__(self):
        self._lock, self._handles, self._missing = Lock(), {}, frozenset()

    def register(self, index, event):
        with self._lock:
            self._handles[index] = event
            stopped = index in self._missing
        if stopped:
            event.stop("research_data_deadline_exceeded")

    def stop_missing(self, results):
        with self._lock:
            self._missing = frozenset(index for index, result in enumerate(results) if result is None)
            handles = tuple(event for index, event in self._handles.items() if index in self._missing)
        for event in handles:
            event.stop("research_data_deadline_exceeded")


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def _text(value, maximum=1200):
    if type(value) is not str:
        raise _Rejected("invalid_source_text")
    parser = _PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())[:maximum]


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Rejected("duplicate_source_json_key")
        result[key] = value
    return result


def _date(value):
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: 00:00:00)?", value):
        raise _Rejected("invalid_source_date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise _Rejected("invalid_source_date") from None


def _publication(value, *, date_only=False):
    if date_only:
        return None, _date(value), "date"
    if type(value) is not str:
        raise _Rejected("missing_publication_timestamp")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None, _date(value), "date"
    normalized = re.sub(r"( [0-9]{2}:[0-9]{2}:[0-9]{2}):([0-9]{3})$", r"\1.\2", value)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?", normalized):
        raise _Rejected("invalid_publication_timestamp")
    try:
        published = datetime.fromisoformat(normalized).replace(tzinfo=SHANGHAI)
    except ValueError:
        raise _Rejected("invalid_publication_timestamp") from None
    return published, published.date(), "datetime"


def _eligible(published_at, published_date, request, *, window=False):
    cutoff = request.as_of.astimezone(SHANGHAI)
    if published_at is not None:
        eligible = published_at <= cutoff
    else:
        eligible = published_date < cutoff.date() or (published_date == cutoff.date()
            and cutoff.time().replace(tzinfo=None) >= time(23, 59, 59))
    return eligible and (not window or published_date >= request.publication_start)


def _number(value):
    if type(value) not in (str, int, float) or type(value) is bool:
        raise _Rejected("invalid_source_number")
    try:
        number = Decimal(str(value))
        result = float(number)
        if not number.is_finite() or not isfinite(result):
            raise ValueError
        return result
    except (ValueError, InvalidOperation, OverflowError):
        raise _Rejected("invalid_source_number") from None


def _identifier(row, request):
    if type(row) is not dict or row.get("SECUCODE") != request.instrument_id:
        raise _Rejected("source_instrument_mismatch")
    if row.get("SECURITY_CODE", request.instrument_id[:6]) != request.instrument_id[:6]:
        raise _Rejected("source_instrument_mismatch")


def _evidence(request, kind, source, metric, value, unit, url, retrieved_at, *, publication=(None, None, "unknown"),
              report_period=None, updated_date=None, content_kind="fact", temporal_scope="published_before_cutoff",
              warnings=()):
    published_at, published_date, precision = publication
    identity = json.dumps([request.instrument_id, source, metric, value, url,
        published_at.isoformat() if published_at else None, published_date.isoformat() if published_date else None,
        report_period.isoformat() if report_period else None], ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    key = "company:" + sha256(identity.encode()).hexdigest()[:32]
    return ResearchEvidence(key, request.instrument_id, kind, source, metric, value, unit, url,
        published_at, published_date, precision, retrieved_at, report_period=report_period,
        updated_date=updated_date, content_kind=content_kind, temporal_scope=temporal_scope, warnings=tuple(warnings))


class ResearchDataService:
    """Trusted fixed-endpoint collectors; one failed section never discards the others."""

    def __init__(self, *, session=None, clock=None, timeout_seconds=6.0, deadline_seconds=20.0,
                 announcement_provider="eastmoney", news_provider="sina", session_factory=None,
                 max_workers=None, executor=None, monotonic_clock=None):
        workers = (1 if session is not None else 3) if max_workers is None else max_workers
        if (type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds) or not 0 < timeout_seconds <= 10
                or type(deadline_seconds) not in (int, float) or not isfinite(deadline_seconds)
                or not 0 < deadline_seconds <= 30 or announcement_provider not in ("eastmoney", "cninfo")
                or news_provider not in ("sina", "eastmoney")
                or clock is not None and not callable(clock)
                or monotonic_clock is not None and not callable(monotonic_clock)
                or type(workers) is not int or not 1 <= workers <= 3
                or session_factory is not None and not callable(session_factory)
                or session is not None and (session_factory is not None or workers != 1)
                or executor is not None and type(executor) is not CollectionExecutor):
            raise InvalidRequestError("Use bounded research-source configuration")
        self._session, self._clock = session, clock or (lambda: datetime.now(timezone.utc))
        self._timeout, self._deadline = timeout_seconds, deadline_seconds
        self._announcement_provider = announcement_provider
        self._news_provider = news_provider
        self.max_workers, self._executor = workers, executor
        self._session_factory, self._client_lock = session_factory, Lock()
        self._timer = monotonic_clock or (lambda: monotonic())
        self._scope = None
        self._events, self._event_index = None, None

    def _now(self):
        value = self._clock()
        if not aware(value):
            raise InvalidRequestError("Research retrieval clock must be timezone-aware")
        return value

    def _validate(self, request):
        if type(request) is not ResearchDataRequest or request.as_of > self._now():
            raise InvalidRequestError("Company research needs a trusted historical cutoff")

    def collect(self, request, kinds=RESEARCH_KINDS):
        return self.collect_many((request,), kinds)[0]

    def collect_many(self, requests, kinds=RESEARCH_KINDS):
        """One source window and dispatch width for up to three independent companies."""
        if type(requests) is not tuple or not 1 <= len(requests) <= 3:
            raise InvalidRequestError("Choose one to three distinct company requests")
        for request in requests:
            self._validate(request)
        if len({request.instrument_id for request in requests}) != len(requests):
            raise InvalidRequestError("Choose one to three distinct company requests")
        if (type(kinds) is not tuple or not kinds or any(type(kind) is not str or kind not in RESEARCH_KINDS for kind in kinds)
                or len(set(kinds)) != len(kinds)):
            raise InvalidRequestError("Choose distinct supported research sections")
        scope = CollectionScope(deadline=self._timer() + self._deadline,
            company_allowances=tuple(self._allowances(request, kinds) for request in requests), clock=self._timer)
        indices = (tuple((company, kind) for company in range(len(requests)) for kind in kinds)
                   if self.max_workers == 1 else
                   tuple((company, kind) for kind in kinds for company in range(len(requests))))
        events = _CollectionEvents()
        tasks = tuple(partial(self._run_section, requests[company], kind, scope.for_company(company), events, index)
                      for index, (company, kind) in enumerate(indices))
        results = (self._executor or shared_collection_executor()).run(
            tasks, scope=scope, max_workers=self.max_workers)
        events.stop_missing(results)
        checkpoint()
        sections = {(company, kind): result if result is not None else self._section_unavailable(
            kind, "research_data_deadline_exceeded")
            for (company, kind), result in zip(indices, results, strict=True)}
        checkpoint()
        return tuple(ResearchDataBatch(request, tuple(sections[company, kind] for kind in kinds))
                     for company, request in enumerate(requests))

    def fetch_section(self, request, kind):
        if type(kind) is not str or kind not in RESEARCH_KINDS:
            raise InvalidRequestError("Choose one supported research section")
        return self.collect(request, (kind,)).source_results[0]

    def _allowances(self, request, kinds):
        requests = response_bytes = 0
        for kind in kinds:
            if (kind == "profile" and not request.allow_current_background
                    or kind == "news" and self._news_provider == "sina" and request.instrument_id.endswith(".BJ")
                    or kind == "announcements" and self._announcement_provider == "cninfo"
                    and not request.instrument_id.endswith(".SZ")
                    or kind == "announcements" and self._announcement_provider == "eastmoney"
                    and request.instrument_id.endswith(".BJ")):
                continue
            requests += 1
            response_bytes += 512_000
            if kind == "announcements" and self._announcement_provider == "cninfo":
                requests += 1
                response_bytes += 2_000_000
        return requests, response_bytes

    def _run_section(self, request, kind, scope, events, index):
        scope.check()
        borrowed = self._session is not None
        if borrowed:
            while not self._client_lock.acquire(timeout=min(POLL_SECONDS, scope.remaining())):
                scope.check()
            client = self._session
        else:
            import requests
            client = (self._session_factory or requests.Session)()
        try:
            scope.check()
            # Each task owns this lightweight adapter; the long-lived service is never mutated.
            task_service = ResearchDataService(session=client, clock=self._clock, timeout_seconds=self._timeout,
                deadline_seconds=self._deadline, announcement_provider=self._announcement_provider,
                news_provider=self._news_provider, monotonic_clock=self._timer)
            task_service._scope = scope
            task_service._events, task_service._event_index = events, index
            result = task_service._fetch(request, kind, scope.deadline)
        finally:
            if borrowed:
                self._client_lock.release()
            else:
                client.close()
        scope.check()
        return result

    def _section_unavailable(self, kind, code):
        source = ("sina_company_news" if kind == "news" and self._news_provider == "sina" else
                  "cninfo_announcements" if kind == "announcements" and self._announcement_provider == "cninfo"
                  else _SOURCES[kind])
        message = ("资料获取已到本批次截止时间，本领域未取得可核验资料；已取得的其他资料仍可使用。"
                   if code == "research_data_deadline_exceeded" else
                   "该来源本次未返回可核验的资料；没有伪造空结果、重试或静默换源。")
        return ResearchDataSection(kind, source, "unavailable", (), code, message, self._now())

    def _json(self, endpoint, params, deadline, *, payload=None):
        checkpoint()
        remaining = deadline - self._timer()
        if remaining <= 0:
            raise _Rejected("research_data_deadline_exceeded")
        import requests
        client = requests if self._session is None else self._session
        method, url = _ENDPOINTS[endpoint]
        timeout = request_timeout(min(self._timeout, remaining))
        if self._scope is not None:
            timeout = self._scope.timeout(self._timeout)
            self._scope.reserve_request()
        response = None
        try:
            response = client.request(method, url, params=params, data=payload, timeout=(min(3.0, timeout), timeout),
                allow_redirects=False, stream=True,
                headers={"User-Agent": "TradingCompanion/0.1 public-research", "Accept": "application/json"})
            checkpoint()
            if 300 <= response.status_code <= 399:
                raise _Rejected("source_redirect_blocked")
            if response.status_code != 200:
                raise _Rejected("source_http_error")
            maximum = 2_000_000 if endpoint == "cninfo_directory" else 512_000
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isascii() or not declared.isdigit() or int(declared) > maximum):
                raise _Rejected("source_response_too_large")
            body = bytearray()
            for chunk in response.iter_content(8192):
                checkpoint()
                if self._timer() >= deadline:
                    raise _Rejected("research_data_deadline_exceeded")
                if len(body) + len(chunk) > maximum:
                    raise _Rejected("source_response_too_large")
                if self._scope is not None:
                    self._scope.consume(len(chunk))
                body.extend(chunk)
            text = bytes(body).decode("utf-8-sig").strip()
            if endpoint == "news" and not text.startswith("{"):
                match = re.fullmatch(re.escape(_CALLBACK) + r"\((\{.*\})\);?", text, re.DOTALL)
                if match is None:
                    raise _Rejected("news_callback_mismatch")
                text = match[1]
            value = json.loads(text, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(_Rejected("invalid_source_json")))
            if type(value) is not dict:
                raise _Rejected("invalid_source_json")
            return value
        except requests.RequestException:
            raise _Rejected("source_connection_failed") from None
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise _Rejected("invalid_source_json") from None
        finally:
            if response is not None:
                response.close()

    def _fetch(self, request, kind, deadline):
        with trace_event("source", "company_section", source=(type(self)._fetch, (self, request, kind, deadline))) as event:
            if self._events is not None:
                self._events.register(self._event_index, event)
            try:
                if self._scope is not None:
                    self._scope.check()
                result = self._fetch_checked(request, kind, deadline)
                if self._scope is not None:
                    self._scope.check()
            except CollectionUnavailable as error:
                result = self._section_unavailable(kind, error.code)
            event.result(result)
            return result

    def _fetch_checked(self, request, kind, deadline):
        if kind == "news" and self._news_provider == "sina":
            return SinaCompanyNewsAdapter(session=self._session, clock=self._clock,
                timeout_seconds=self._timeout).fetch_section(request, deadline=deadline, scope=self._scope)
        source = "cninfo_announcements" if kind == "announcements" and self._announcement_provider == "cninfo" else _SOURCES[kind]
        if kind == "profile" and not request.allow_current_background:
            return ResearchDataSection(kind, source, "unavailable", (), "current_background_not_allowed",
                "公司资料缺少可靠发布时间；历史研究不使用当前背景替代历史事实。", self._now())
        try:
            method = self._cninfo if source == "cninfo_announcements" else getattr(self, "_" + kind)
            items, skipped = method(request, deadline)
            # Financials are five fields over at most five already returned periods.
            bound = 25 if kind == "financial" else request.limit
            deduplicated = tuple({item.id: item for item in items}.values())[:bound]
            return ResearchDataSection(kind, source, "available" if deduplicated else "empty", deduplicated,
                "bounded_evidence_available" if deduplicated else "no_eligible_evidence_in_bounded_response",
                "已核对来源、证券及时间；仅为有界查询，不保证完整覆盖。" if deduplicated else
                "本次有界响应没有可用于该截止时间的记录；不代表没有相关信息。", self._now(), skipped)
        except _Rejected as error:
            return ResearchDataSection(kind, source, "unavailable", (), str(error),
                "该来源本次未返回可核验的资料；没有伪造空结果、重试或静默换源。", self._now())
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            return ResearchDataSection(kind, source, "unavailable", (), "invalid_source_shape",
                "该来源的响应结构不符合约定；未展示无法核验的内容。", self._now())

    @staticmethod
    def _rows(payload):
        if payload.get("success") is not True:
            raise _Rejected("source_business_error")
        result = payload.get("result")
        if result is None:
            return []
        if type(result) is not dict or type(result.get("data")) is not list:
            raise _Rejected("invalid_source_rows")
        if len(result["data"]) > 50:
            raise _Rejected("source_record_limit_exceeded")
        return result["data"]

    @staticmethod
    def _page_url(request, fragment):
        code, exchange = request.instrument_id.split(".")
        return ("https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html?"
                + urlencode({"type": "web", "code": exchange + code}) + "#/" + fragment)

    def _profile(self, request, deadline):
        payload = self._json("profile", {"reportName": "RPT_F10_ORG_BASICINFO", "columns": "ALL",
            "filter": f'(SECUCODE="{request.instrument_id}")', "pageNumber": "1", "pageSize": "1",
            "source": "WEB", "client": "WEB"}, deadline)
        rows = self._rows(payload)
        if len(rows) > 1:
            raise _Rejected("source_profile_not_unique")
        if not rows:
            return (), 0
        row, items = rows[0], []
        _identifier(row, request)
        retrieved = self._now()
        for field, metric in (("ORG_NAME", "company_name"), ("MAIN_BUSINESS", "main_business"),
                              ("CSRC_INDUSTRY_NAME", "industry"), ("ORG_PROFILE", "company_profile"),
                              ("LISTING_DATE", "listing_date")):
            value = row.get(field)
            if value is None or value == "":
                continue
            value = _date(value).isoformat() if field == "LISTING_DATE" else _text(value)
            items.append(_evidence(request, "profile", _SOURCES["profile"], metric, value, "text",
                self._page_url(request, "gsgk"), retrieved, content_kind="background", temporal_scope="current_context",
                warnings=("Current company background; publication time and historical validity are unknown.",)))
        return items, 0

    def _financial(self, request, deadline):
        payload = self._json("financial", {"type": "RPT_F10_FINANCE_MAINFINADATA", "sty": "APP_F10_MAINFINADATA",
            "filter": f'(SECUCODE="{request.instrument_id}")', "p": "1", "ps": "5", "sr": "-1",
            "st": "REPORT_DATE", "source": "HSF10", "client": "PC"}, deadline)
        candidates, skipped = [], 0
        for row in self._rows(payload)[:5]:
            try:
                _identifier(row, request)
                period = _date(row.get("REPORT_DATE"))
                publication = _publication(row.get("NOTICE_DATE"), date_only=True)
                updated = _date(row["UPDATE_DATE"]) if row.get("UPDATE_DATE") is not None else None
                if (period > publication[1] or not _eligible(*publication[:2], request)
                        or updated is not None and not _eligible(None, updated, request)):
                    raise _Rejected("financial_record_not_available_at_cutoff")
                candidates.append((period, row, publication, updated))
            except _Rejected:
                skipped += 1
        if not candidates:
            return (), skipped
        items, retrieved = [], self._now()
        fields = (("TOTALOPERATEREVE", "revenue", "cny"), ("PARENTNETPROFIT", "parent_net_profit", "cny"),
                  ("TOTALOPERATEREVETZ", "revenue_yoy", "percent"),
                  ("PARENTNETPROFITTZ", "parent_net_profit_yoy", "percent"), ("ROEJQ", "weighted_roe", "percent"))
        for period, row, publication, updated in sorted(candidates, key=lambda item: item[0], reverse=True):
            for field, metric, unit in fields:
                try:
                    if unit == "cny" and row.get("CURRENCY") not in ("CNY", "RMB", "人民币", "元"):
                        raise _Rejected("financial_currency_unknown")
                    value = _number(row.get(field))
                except _Rejected:
                    skipped += 1
                    continue
                items.append(_evidence(request, "financial", _SOURCES["financial"], metric, value, unit,
                    self._page_url(request, "cwfx"), retrieved, publication=publication, report_period=period,
                    updated_date=updated, warnings=(_SNAPSHOT_WARNING,
                        "Revenue and profit are cumulative report-period values; percentage values are not fractional ratios.")))
        return items, skipped

    def _news(self, request, deadline):
        query = {"uid": "", "keyword": request.instrument_id[:6], "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr", "param": {"cmsArticleWebOld": {
                "searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": request.limit,
                "preTag": "", "postTag": ""}}}
        payload = self._json("news", {"cb": _CALLBACK, "param": json.dumps(query, ensure_ascii=True)}, deadline)
        result = payload.get("result")
        if type(payload.get("code")) is not int or payload["code"] != 0 or type(result) is not dict:
            raise _Rejected("invalid_news_result")
        rows = result.get("cmsArticleWebOld")
        if type(rows) is not list or len(rows) > 50:
            raise _Rejected("news_article_result_missing")
        items, skipped, retrieved = [], 0, self._now()
        for row in rows[:request.limit]:
            try:
                if (type(row) is not dict or type(row.get("code")) is not str
                        or not re.fullmatch(r"[0-9]{12,40}", row["code"])):
                    raise _Rejected("invalid_news_document_id")
                title, summary = _text(row.get("title"), 300), _text(row.get("content", ""), 1000)
                combined = title + (" — " + summary if summary else "")
                if request.instrument_id[:6] not in combined and request.security_name not in combined:
                    raise _Rejected("news_company_association_unverified")
                publication = _publication(row.get("date"))
                if not _eligible(*publication[:2], request, window=True):
                    raise _Rejected("news_outside_publication_window")
                media = _text(row.get("mediaName", ""), 80)
                items.append(_evidence(request, "news", _SOURCES["news"], "news_excerpt", combined, "text",
                    "https://finance.eastmoney.com/a/" + row["code"] + ".html", retrieved,
                    publication=publication, content_kind="snippet", warnings=(
                        "Search excerpt only; the article body was not retrieved or verified.",
                        "Keyword/name association is not proof of an issuer-authored document.", "Reported media: " + media)))
            except _Rejected:
                skipped += 1
        return items, skipped

    def _announcements(self, request, deadline):
        exchange = request.instrument_id[-2:]
        if exchange not in ("SH", "SZ"):
            raise _Rejected("announcement_exchange_mapping_unverified")
        payload = self._json("announcements", {"sr": "-1", "page_size": str(request.limit), "page_index": "1",
            "ann_type": "A", "client_source": "web", "stock_list": request.instrument_id[:6],
            "begin_time": request.publication_start.isoformat(),
            "end_time": request.as_of.astimezone(SHANGHAI).date().isoformat()}, deadline)
        data = payload.get("data")
        if payload.get("success") not in (True, 1) or type(data) is not dict or type(data.get("list")) is not list:
            raise _Rejected("invalid_announcement_result")
        if len(data["list"]) > 50:
            raise _Rejected("source_record_limit_exceeded")
        items, skipped, retrieved = [], 0, self._now()
        for row in data["list"][:request.limit]:
            try:
                if type(row) is not dict or not re.fullmatch(r"AN[0-9]{12,40}", str(row.get("art_code", ""))):
                    raise _Rejected("invalid_announcement_document_id")
                codes = row.get("codes")
                if type(codes) is not list or not any(type(code) is dict and code.get("stock_code") == request.instrument_id[:6]
                    and code.get("market_code") == {"SH": "1", "SZ": "0"}[exchange] for code in codes):
                    raise _Rejected("announcement_instrument_mismatch")
                publication = (_publication(row["display_time"]) if row.get("display_time")
                    else _publication(row.get("notice_date"), date_only=True))
                if not _eligible(*publication[:2], request, window=True):
                    raise _Rejected("announcement_outside_publication_window")
                title = _text(row.get("title"), 600)
                if not title:
                    raise _Rejected("invalid_source_text")
                items.append(_evidence(request, "announcements", _SOURCES["announcements"], "announcement_title", title,
                    "text", "https://data.eastmoney.com/notices/detail/" + request.instrument_id[:6]
                    + "/" + row["art_code"] + ".html", retrieved, publication=publication, content_kind="title",
                    warnings=("Announcement title only; the filing body was not retrieved.",
                              "Timestamp is the vendor display time, not a verified original exchange publication time.")))
            except _Rejected:
                skipped += 1
        return items, skipped

    def _cninfo(self, request, deadline):
        if not request.instrument_id.endswith(".SZ"):
            raise _Rejected("cninfo_exchange_mapping_unverified")
        payload = self._json("cninfo_directory", {}, deadline)
        rows = payload.get("stockList")
        if type(rows) is not list or len(rows) > 15000:
            raise _Rejected("invalid_cninfo_directory")
        matches = [row for row in rows if type(row) is dict and row.get("code") == request.instrument_id[:6]]
        if (len(matches) != 1 or type(matches[0].get("orgId")) is not str
                or not re.fullmatch(r"[0-9]{5,30}", matches[0]["orgId"])):
            raise _Rejected("cninfo_company_not_unique")
        org_id = matches[0]["orgId"]
        payload = self._json("cninfo_announcements", None, deadline, payload={"pageNum": "1", "pageSize": str(request.limit),
            "column": "szse", "tabName": "fulltext", "plate": "", "stock": request.instrument_id[:6] + "," + org_id,
            "searchkey": "", "secid": "", "category": "", "trade": "",
            "seDate": request.publication_start.isoformat() + "~" + request.as_of.astimezone(SHANGHAI).date().isoformat(),
            "sortName": "", "sortType": "", "isHLtitle": "false"})
        rows = payload.get("announcements")
        if type(rows) is not list or len(rows) > 50:
            raise _Rejected("invalid_cninfo_announcements")
        items, skipped, retrieved = [], 0, self._now()
        for row in rows[:request.limit]:
            try:
                if (type(row) is not dict or row.get("secCode") != request.instrument_id[:6] or row.get("orgId") != org_id
                        or not re.fullmatch(r"[0-9]{5,30}", str(row.get("announcementId", "")))
                        or type(row.get("announcementTime")) is not int):
                    raise _Rejected("cninfo_instrument_or_document_mismatch")
                try:
                    published = datetime.fromtimestamp(row["announcementTime"] / 1000, SHANGHAI)
                except (ValueError, OverflowError, OSError):
                    raise _Rejected("invalid_publication_timestamp") from None
                if not _eligible(published, published.date(), request, window=True):
                    raise _Rejected("announcement_outside_publication_window")
                title = _text(row.get("announcementTitle"), 600)
                if not title:
                    raise _Rejected("invalid_source_text")
                url = "https://www.cninfo.com.cn/new/disclosure/detail?" + urlencode({
                    "stockCode": request.instrument_id[:6], "announcementId": row["announcementId"], "orgId": org_id})
                items.append(_evidence(request, "announcements", "cninfo_announcements", "announcement_title", title,
                    "text", url, retrieved, publication=(published, published.date(), "datetime"), content_kind="title",
                    warnings=("Disclosure-platform timestamp and title only; the PDF body was not retrieved.",)))
            except _Rejected:
                skipped += 1
        return items, skipped
