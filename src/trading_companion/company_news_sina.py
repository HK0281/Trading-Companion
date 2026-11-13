"""One bounded public Sina news-list page; titles are evidence, never article bodies."""

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from math import isfinite
import re
from time import monotonic
from urllib.parse import urlsplit

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


def _well_formed_sina_url(url):
    """Recognize bounded Sina links for filtering, never for fetching or saving."""
    if (type(url) is not str or not 1 <= len(url) <= 600 or not url.isprintable()
            or any(character.isspace() for character in url) or "\\" in url
            or re.search(r"%(?![0-9A-Fa-f]{2})", url)):
        return False
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ("http", "https") or host is None or parsed.username is not None
                or parsed.password is not None or parsed.netloc.endswith(":")
                or port is not None and not 1 <= port <= 65535
                or len(host) > 253 or not parsed.path or not parsed.path.isprintable()):
            return False
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            return False
        return any(host == domain or host.endswith("." + domain) for domain in ("sina.cn", "sina.com.cn"))
    except ValueError:
        return False


def _watchlist_row_shape(stamp, title, url):
    """Validate before benign filtering, without changing ordinary research order."""
    if (type(title) is not str or not title or len(title) > 600 or not title.isprintable()
            or not _well_formed_sina_url(url)):
        raise _Unavailable("invalid_news_title_or_url")
    if (type(stamp) is not str
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}", stamp)):
        raise _Unavailable("invalid_publication_timestamp")
    try:
        datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except ValueError:
        raise _Unavailable("invalid_publication_timestamp") from None
    if not _safe_article_url(url):
        raise _Unavailable("news_link_form_unsupported")


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

    def fetch_section(self, request, *, deadline=None, scope=None, _watchlist_counts=None):
        if type(request) is not ResearchDataRequest or request.as_of > self._now():
            raise InvalidRequestError("News needs a validated historical research request")
        deadline = monotonic() + 10 if deadline is None else deadline
        try:
            if not request.instrument_id.endswith((".SH", ".SZ")):
                raise _Unavailable("sina_news_exchange_unverified")
            text = self._read(request, deadline, scope=scope)
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
            if _watchlist_counts is None:
                items, skipped = self._parse_rows(request, parser.rows)
            else:
                items, skipped = self._parse_rows(request, parser.rows, _watchlist_counts=_watchlist_counts)
            return ResearchDataSection("news", _SOURCE, "available" if items else "empty", tuple(items),
                "bounded_evidence_available" if items else "no_eligible_evidence_in_bounded_response",
                "已取得与该证券匹配的新闻标题。" if items else "本次列表没有可核验标题；不代表没有相关新闻。",
                self._now(), skipped)
        except _Unavailable as error:
            return ResearchDataSection("news", _SOURCE, "unavailable", (), str(error),
                "新闻来源本次未返回可核验内容。", self._now())

    def _read(self, request, deadline, *, scope=None):
        import requests
        checkpoint()
        remaining = deadline - monotonic() if scope is None else scope.remaining()
        if remaining <= 0:
            raise _Unavailable("research_data_deadline_exceeded")
        timeout = request_timeout(min(self._timeout, remaining))
        if scope is not None:
            timeout = scope.timeout(self._timeout)
            scope.reserve_request()
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
                if scope is None and monotonic() >= deadline:
                    raise _Unavailable("research_data_deadline_exceeded")
                if len(body) + len(chunk) > 512000:
                    raise _Unavailable("source_response_too_large")
                if scope is not None:
                    scope.consume(len(chunk))
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

    def _parse_rows(self, request, rows, *, _watchlist_counts=None):
        items, skipped, seen, retrieved = [], 0, set(), self._now()
        cutoff = request.as_of.astimezone(SHANGHAI)
        for stamp, title, url in rows:
            try:
                if _watchlist_counts is not None:
                    _watchlist_row_shape(stamp, title, url)
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
            except _Unavailable as error:
                if (_watchlist_counts is not None and str(error) in (
                        "news_title_association_unverified", "news_outside_publication_window",
                        "news_link_form_unsupported")):
                    _watchlist_counts["filtered_count"] += 1
                else:
                    skipped += 1
        return items, skipped
