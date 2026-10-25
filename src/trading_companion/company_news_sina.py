"""One bounded public Sina news-list page; titles are evidence, never article bodies."""

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from math import isfinite
import re
from time import monotonic

from trading_companion.company_research_contracts import (
    SHANGHAI, ResearchDataRequest, ResearchDataSection, ResearchEvidence, aware,
)
from trading_companion.errors import InvalidRequestError
from trading_companion.turn_control import checkpoint, request_timeout


_ENDPOINT = "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php"
_SOURCE = "sina_company_news"
_WARNING = (
    "News-list title only; article body was not retrieved or verified.",
    "The title contains the issuer name/code; this does not prove issuer authorship or the claim's truth.",
    "Timestamp is the vendor list display time at minute precision, not a verified original publication time.",
    "One bounded current list page, not complete news coverage or a versioned historical archive.",
)


class _Unavailable(ValueError):
    pass


class _NewsList(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page_title, self.rows, self.prefix, self.link_text = [], [], [], []
        self.in_title, self.depth, self.hidden, self.lists, self.href = False, 0, 0, 0, None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag == "title":
            self.in_title = True
        if tag == "div":
            if "datelist" in (attributes.get("class") or "").split():
                self.lists += 1
                if self.lists != 1:
                    raise _Unavailable("news_list_not_unique")
                self.depth = 1
            elif self.depth:
                self.depth += 1
        if self.depth and tag == "br":
            self.prefix = []
        if self.depth and tag == "a":
            if self.href is not None:
                raise _Unavailable("invalid_news_list_markup")
            self.href, self.link_text = attributes.get("href", ""), []

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        if tag == "title":
            self.in_title = False
        if self.depth and tag == "a" and self.href is not None:
            self.rows.append((" ".join("".join(self.prefix).split()),
                              " ".join("".join(self.link_text).split()), self.href))
            self.href, self.link_text, self.prefix = None, [], []
            if len(self.rows) > 50:
                raise _Unavailable("source_record_limit_exceeded")
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, value):
        if self.hidden:
            return
        if self.in_title:
            self.page_title.append(value)
        if self.depth:
            (self.link_text if self.href is not None else self.prefix).append(value)


def _safe_article_url(url):
    return type(url) is str and bool(re.fullmatch(
        r"https://(?:finance\.sina\.com\.cn/(?:[A-Za-z0-9_-]+/)*doc-[a-z0-9]+\.shtml"
        r"|cj\.sina\.cn/articles/view/[0-9]+/[A-Za-z0-9]+)", url))


class SinaCompanyNewsAdapter:
    def __init__(self, *, session=None, clock=None, timeout_seconds=6.0):
        if (type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10 or clock is not None and not callable(clock)):
            raise InvalidRequestError("Use a bounded Sina news request")
        self._session, self._clock, self._timeout = session, clock or (lambda: datetime.now(timezone.utc)), timeout_seconds

    def _now(self):
        value = self._clock()
        if not aware(value):
            raise InvalidRequestError("News retrieval clock must be timezone-aware")
        return value

    def fetch_section(self, request, *, deadline=None):
        if type(request) is not ResearchDataRequest or request.as_of > self._now():
            raise InvalidRequestError("News needs a validated historical research request")
        deadline = monotonic() + 10 if deadline is None else deadline
        try:
            if not request.instrument_id.endswith((".SH", ".SZ")):
                raise _Unavailable("sina_news_exchange_unverified")
            text = self._read(request, deadline)
            parser = _NewsList()
            try:
                parser.feed(text)
                parser.close()
            except _Unavailable:
                raise
            except (ValueError, AssertionError):
                raise _Unavailable("news_layout_invalid") from None
            expected = request.security_name + "(" + request.instrument_id[:6] + ")个股资讯_新浪财经_新浪网"
            if " ".join("".join(parser.page_title).split()) != expected:
                raise _Unavailable("news_page_instrument_mismatch")
            if parser.lists != 1 or parser.depth or parser.href is not None:
                raise _Unavailable("news_list_missing_or_incomplete")
            items, skipped = self._parse_rows(request, parser.rows)
            return ResearchDataSection("news", _SOURCE, "available" if items else "empty", tuple(items),
                "bounded_evidence_available" if items else "no_eligible_evidence_in_bounded_response",
                "已取得与该证券匹配的新闻标题。" if items else "本次列表没有可核验标题；不代表没有相关新闻。",
                self._now(), skipped)
        except _Unavailable as error:
            return ResearchDataSection("news", _SOURCE, "unavailable", (), str(error),
                "新闻来源本次未返回可核验内容。", self._now())

    def _read(self, request, deadline):
        import requests
        checkpoint()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _Unavailable("research_data_deadline_exceeded")
        timeout = request_timeout(min(self._timeout, remaining))
        client = requests if self._session is None else self._session
        response = None
        try:
            response = client.request("GET", _ENDPOINT,
                params={"symbol": request.instrument_id[-2:].lower() + request.instrument_id[:6], "Page": 1},
                timeout=(min(3.0, timeout), timeout), allow_redirects=False, stream=True,
                headers={"User-Agent": "TradingCompanion/0.1 public-research", "Accept": "text/html"})
            checkpoint()
            if 300 <= response.status_code <= 399:
                raise _Unavailable("source_redirect_blocked")
            if response.status_code != 200:
                raise _Unavailable("source_http_error")
            declared = response.headers.get("Content-Length")
            if declared is not None and (not declared.isascii() or not declared.isdigit() or int(declared) > 512000):
                raise _Unavailable("source_response_too_large")
            body = bytearray()
            for chunk in response.iter_content(8192):
                checkpoint()
                if monotonic() >= deadline:
                    raise _Unavailable("research_data_deadline_exceeded")
                if len(body) + len(chunk) > 512000:
                    raise _Unavailable("source_response_too_large")
                body.extend(chunk)
            if "charset=utf-8" in response.headers.get("Content-Type", "").lower().replace(" ", ""):
                return bytes(body).decode("utf-8-sig")
            return bytes(body).decode("gb18030")
        except requests.RequestException:
            raise _Unavailable("source_connection_failed") from None
        except UnicodeError:
            raise _Unavailable("invalid_source_encoding") from None
        finally:
            if response is not None:
                response.close()

    def _parse_rows(self, request, rows):
        items, skipped, seen, retrieved = [], 0, set(), self._now()
        cutoff = request.as_of.astimezone(SHANGHAI)
        for stamp, title, url in rows:
            try:
                if (not title or len(title) > 600 or not _safe_article_url(url)
                        or request.security_name not in title and request.instrument_id[:6] not in title):
                    raise _Unavailable("news_title_association_unverified")
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}", stamp):
                    raise _Unavailable("invalid_publication_timestamp")
                try:
                    display_time = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
                except ValueError:
                    raise _Unavailable("invalid_publication_timestamp") from None
                published_date = display_time.date()
                date_only = display_time.time().replace(tzinfo=None) == time(0)
                availability = (datetime.combine(published_date, time(23, 59, 59), SHANGHAI)
                    if date_only else display_time + timedelta(seconds=59))
                if availability > cutoff or published_date < request.publication_start:
                    raise _Unavailable("news_outside_publication_window")
                identity = json.dumps([request.instrument_id, _SOURCE, title, url, stamp], ensure_ascii=True)
                key = "company:" + sha256(identity.encode()).hexdigest()[:32]
                if key not in seen:
                    seen.add(key)
                    items.append(ResearchEvidence(key, request.instrument_id, "news", _SOURCE, "news_title", title,
                        "text", url, None if date_only else display_time, published_date,
                        "date" if date_only else "datetime", retrieved, content_kind="title", warnings=_WARNING))
                if len(items) >= request.limit:
                    break
            except _Unavailable:
                skipped += 1
        return items, skipped
