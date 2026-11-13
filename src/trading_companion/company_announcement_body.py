"""Pure Eastmoney announcement-detail parsing; attachments are never fetched here."""

from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
import re
import unicodedata

from trading_companion.research_documents import (
    DocumentCandidate, DocumentError, MAX_ANNOUNCEMENT_CHARACTERS, MAX_ANNOUNCEMENT_PARAGRAPHS,
    MAX_DOCUMENT_BYTES, ResearchDocument, SHANGHAI, announcement_code, normalized_text, timestamp,
    trim_incomplete_tail,
)
from trading_companion.security_directory import _normalized as normalized_security_name


EXTRACTOR_VERSION = "eastmoney-announcement-body-v3"
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"))
_HIDDEN = frozenset(("script", "style", "noscript", "iframe", "template", "ins", "button", "form", "svg"))
_BLOCKS = frozenset(("p", "div", "section", "article", "h1", "h2", "h3", "h4", "li", "tr", "br"))
_PLAIN_END = re.compile(r"[。！？；：][\"'”’）)】\]》」』]*$")
_PLAIN_SECTION = re.compile(
    r"^(?:[一二三四五六七八九十百零〇两]+[、．.]"
    r"|[（(][一二三四五六七八九十百零〇两0-9]+[）)]"
    r"|[0-9]+(?:\.[0-9]+)+(?=\s|[、．:：]|$)|[0-9]+[.、．](?![0-9])"
    r"|第[一二三四五六七八九十百零〇两0-9]+条|附件"
    r"|(?:公告|内容)?摘要[：:]|(?:公告)?正文[：:])")
_TRUTH_STATEMENT = re.compile(
    r"^(?:本公司|公司(?:董事会|及董事会|及全体)).{0,80}?(?:保证|声明)"
    r"(?=.{0,160}(?:真实|虚假记载|误导性|重大遗漏))")


def _visible_characters(value):
    """Replace only BMP private-use glyphs in visible text, before whitespace folds."""
    parts, replaced = [], 0
    for character in value:
        if "\ue000" <= character <= "\uf8ff":
            parts.append(" ")
            replaced += 1
        elif (character.isprintable() or character in "\t\n\r\f\v"
              or unicodedata.category(character) in ("Zs", "Zl", "Zp")):
            parts.append(character)
        else:
            raise DocumentError("document_layout_invalid")
    return "".join(parts), replaced


def _plain_paragraphs(content):
    """Rejoin PDF visual lines without inferring table or numeric semantics."""
    lines = [normalized_text(line) for line in content.splitlines()]
    visual_lines = [line for line in lines if line]
    if (len(visual_lines) > MAX_ANNOUNCEMENT_PARAGRAPHS
            or len("\n\n".join(visual_lines)) > MAX_ANNOUNCEMENT_CHARACTERS):
        raise DocumentError("document_too_large")
    # Only an explicit truthfulness statement identifies front matter. Without
    # one, ordinary text starts at the first line rather than staying fragmented.
    statement = next((index for index, line in enumerate(lines)
                      if line and _TRUTH_STATEMENT.match("".join(lines[index:index + 6])[:300])), 0)
    paragraphs, parts = [], []

    def flush():
        if parts:
            paragraphs.append("".join(parts))
            parts.clear()

    signature = False
    for index, line in enumerate(lines):
        if not line:
            flush()
            continue
        if index < statement or signature or line.startswith("特此公告"):
            flush()
            paragraphs.append(line)
            signature = signature or line.startswith("特此公告")
            continue
        if parts and (_PLAIN_END.search(parts[-1]) or _PLAIN_SECTION.match(line)):
            flush()
        if parts:
            previous, following = parts[-1][-1], line[0]
            left_edge = unicodedata.normalize("NFKC", previous)[-1]
            right_edge = unicodedata.normalize("NFKC", following)[0]
            numeric_boundary = (
                left_edge.isdecimal() and (right_edge.isdecimal() or right_edge.isascii())
                or right_edge.isdecimal() and (left_edge.isdecimal() or left_edge.isascii()))
            # Preserve original glyphs, while preventing even compatible digits
            # such as full-width １ and ２３ from becoming a new number on reflow.
            if previous.isascii() and following.isascii() or numeric_boundary:
                parts.append(" ")
        parts.append(line)
    flush()
    return tuple(paragraphs)


class _Body(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.paragraphs, self.parts = [], [], []
        self.nodes = 0
        self.private_use_characters_replaced = 0

    def _flush(self):
        text = normalized_text("".join(self.parts))
        self.parts = []
        if text:
            if not text.isprintable():
                raise DocumentError("document_layout_invalid")
            self.paragraphs.append(text)
            if (len(self.paragraphs) > MAX_ANNOUNCEMENT_PARAGRAPHS
                    or len("\n\n".join(self.paragraphs)) > MAX_ANNOUNCEMENT_CHARACTERS):
                raise DocumentError("document_too_large")

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 12000 or len(self.stack) >= 64:
            raise DocumentError("document_too_large")
        values = dict(attrs)
        if len(values) != len(attrs):
            raise DocumentError("document_layout_invalid")
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in _HIDDEN or "hidden" in values
                  or values.get("aria-hidden") == "true"
                  or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", values.get("style") or "", re.I))
                  or any(re.search(r"(?:^|[-_])(?:ad|ads|promo|recommend)(?:$|[-_])", c, re.I)
                         for c in (values.get("class") or "").split()))
        if tag in _BLOCKS and not hidden:
            self._flush()
        if tag in ("td", "th") and not hidden:
            self.parts.append(" ")
        if tag not in _VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1][0] != tag:
            raise DocumentError("document_layout_invalid")
        _, hidden = self.stack.pop()
        if tag in _BLOCKS and not hidden:
            self._flush()
        elif tag in ("td", "th") and not hidden:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.stack or not self.stack[-1][1]:
            value, replaced = _visible_characters(value)
            self.private_use_characters_replaced += replaced
            self.parts.append(value)

    def handle_entityref(self, name):
        self.handle_data(unescape("&" + name + ";"))

    def handle_charref(self, name):
        if self.stack and self.stack[-1][1]:
            return
        try:
            value = chr(int(name[1:], 16) if name[:1].lower() == "x" else int(name))
        except (ValueError, OverflowError):
            raise DocumentError("document_layout_invalid") from None
        self.handle_data(value)


def _one_text(data, names, *, code, required=True):
    values = [data[name] for name in names if name in data and data[name] is not None]
    if not values:
        if required:
            raise DocumentError(code)
        return ""
    if any(type(value) is not str for value in values):
        raise DocumentError(code)
    normalized = [normalized_text(value) for value in values]
    if len(set(normalized)) != 1:
        raise DocumentError(code)
    return values[0]


def _identity(data, candidate):
    def name(value, *, required=True):
        if (type(value) is not str or not 1 <= len(value) <= 80
                or not normalized_text(value).isprintable() or not normalized_text(value)):
            if required:
                raise DocumentError("document_identity_mismatch")
            return None
        return normalized_security_name(normalized_text(value))

    expected_name = name(candidate.security_name)
    expected_market = {"SH": "1", "SZ": "0"}[candidate.instrument_id[-2:]]
    expected_stock = candidate.instrument_id[:6]
    rows = data.get("security")
    if type(rows) is not list or not 1 <= len(rows) <= 50 or any(type(row) is not dict for row in rows):
        raise DocumentError("document_identity_mismatch")
    names, matching = set(), []
    for row in rows:
        stock, market = row.get("stock"), row.get("market_uni")
        if (type(stock) is not str or re.fullmatch(r"[0-9]{5,6}", stock) is None
                or type(market) is not str or re.fullmatch(r"[0-9]{0,5}", market) is None):
            raise DocumentError("document_identity_mismatch")
        target = stock == expected_stock
        # Other listings can carry historical names; they cannot authorize the
        # target identity, but usable names may explain the aggregate label.
        row_names = [value for key in ("short_name", "short_name_ch", "stock_name") if key in row
                     if (value := name(row[key], required=target)) is not None]
        if ("stock_code" in row and row["stock_code"] != stock
                or "market_code" in row and row["market_code"] != market):
            raise DocumentError("document_identity_mismatch")
        names.update(row_names)
        if target:
            if not row_names or len(set(row_names)) != 1 or market != expected_market or row_names[0] != expected_name:
                raise DocumentError("document_identity_mismatch")
            matching.append(row)
    if not matching:
        raise DocumentError("document_identity_mismatch")

    # The detail's aggregate name may belong to another listing in security (for
    # example an H-share line). It never supplies the target A-share's name.
    aggregate_names = [name(data[key]) for key in ("short_name", "short_name_ch", "stock_name") if key in data]
    if len(set(aggregate_names)) > 1 or any(value not in names for value in aggregate_names):
        raise DocumentError("document_identity_mismatch")
    for key in ("stock", "stock_code"):
        if key in data and data[key] != expected_stock:
            raise DocumentError("document_identity_mismatch")
    for key in ("market_uni", "market_code"):
        if key in data and data[key] != expected_market:
            raise DocumentError("document_identity_mismatch")

    # Older synthetic aliases cannot authorize a detail without real security
    # identity. If supplied alongside it, they must agree rather than override it.
    for key in ("stock_list", "codes"):
        if key not in data:
            continue
        rows = data[key]
        if type(rows) is not list or not 1 <= len(rows) <= 50 or any(type(row) is not dict for row in rows):
            raise DocumentError("document_identity_mismatch")
        matching = [row for row in rows if row.get("stock_code") == expected_stock]
        if not matching:
            raise DocumentError("document_identity_mismatch")
        for row in matching:
            if ("market_code" in row and row["market_code"] != expected_market
                    or "market_uni" in row and row["market_uni"] != expected_market
                    or "stock" in row and row["stock"] != expected_stock):
                raise DocumentError("document_identity_mismatch")
            for key in ("stock_name", "short_name", "short_name_ch"):
                if key in row and name(row[key]) != expected_name:
                    raise DocumentError("document_identity_mismatch")


def _detail_date(value):
    if type(value) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?", value):
        raise DocumentError("document_time_unavailable")
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value), None
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)
        return stamp.date(), stamp
    except ValueError:
        raise DocumentError("document_time_unavailable") from None


def _published(data, candidate):
    notice_day, _ = _detail_date(data.get("notice_date"))
    parent = timestamp(candidate.published_at) if candidate.published_at is not None else None
    difference = (notice_day - date.fromisoformat(candidate.published_date)).days
    # The provider's notice day can roll to the next natural day for late
    # publication. Its date label and ingestion time never supply publication.
    if difference != 0 and not (difference == 1 and parent is not None and parent.hour >= 15):
        raise DocumentError("document_time_mismatch")
    if "display_time" in data:
        display_day, display = _detail_date(data["display_time"])
        if display_day.isoformat() != candidate.published_date:
            raise DocumentError("document_time_mismatch")
        if display is None or parent is None:
            raise DocumentError("document_time_unavailable")
        if display.replace(second=0, microsecond=0) != parent.replace(second=0, microsecond=0):
            raise DocumentError("document_time_mismatch")
    return candidate.published_at


def _require_substantive_body(paragraphs, title, attachment):
    """Validate the retained text, including after an incomplete suffix is removed."""
    if not paragraphs:
        raise DocumentError("document_attachment_only" if attachment else "document_empty_body")
    if (all(paragraph == title for paragraph in paragraphs)
            or normalized_text("".join(paragraphs)) == title):
        raise DocumentError("document_attachment_only" if attachment else "document_empty_body")
    body = paragraphs[1:] if paragraphs[0] == title else paragraphs
    if (body and re.match(r"^(?:公告|内容)?摘要(?:[：:]|$)", body[0])
            and not any(re.match(r"^(?:公告)?正文(?:[：:]|$)", paragraph) for paragraph in body[1:])):
        raise DocumentError("document_attachment_only" if attachment else "document_empty_body")
    # A download prompt is not the body, even when the vendor puts it in content.
    if attachment and len("".join(paragraphs)) <= 200 and re.search(
            r"(?:详见|参见|查看|下载)(?:公告)?附件", "".join(paragraphs)):
        raise DocumentError("document_attachment_only")


def parse_eastmoney_announcement(payload, candidate, *, retrieved_at):
    """Use detail identity and its body field; never turn a title/attachment into full text."""
    if (type(candidate) is not DocumentCandidate or candidate.kind != "announcements"
            or candidate.source != "eastmoney_announcements"):
        raise DocumentError("document_invalid_candidate")
    if (type(payload) is not dict or type(payload.get("data")) is not dict
            or "success" in payload and payload["success"] not in (True, 1)
            or "code" in payload and payload["code"] not in (1, "1")):
        raise DocumentError("document_invalid_response")
    data = payload["data"]
    if data.get("art_code") != announcement_code(candidate.url, candidate.instrument_id):
        raise DocumentError("document_identity_mismatch")
    _identity(data, candidate)
    title = _one_text(data, ("notice_title", "title"), code="document_title_mismatch")
    if normalized_text(title) != candidate.title:
        raise DocumentError("document_title_mismatch")
    published = _published(data, candidate)
    # Read only this bounded content page; subsequent pages and attachments are
    # not fetched. Coverage metadata prevents presenting its prefix as complete.
    pages = data.get("page_size", 1)
    if type(pages) is not int or pages < 1:
        raise DocumentError("document_layout_invalid")
    content = _one_text(data, ("notice_content", "content"), code="document_layout_invalid", required=False)
    attachment = bool(data.get("attach_url") or data.get("attach_list"))
    if not content.strip():
        raise DocumentError("document_attachment_only" if attachment else "document_empty_body")
    try:
        if len(content) > MAX_DOCUMENT_BYTES or len(content.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise DocumentError("document_too_large")
    except UnicodeError:
        raise DocumentError("document_layout_invalid") from None
    if "<" not in content:
        content, replaced = _visible_characters(content)
        paragraphs = _plain_paragraphs(content)
        # Reflow must not turn repeated titles or an explicit summary into body.
        visual_lines = tuple(normalized_text(line) for line in content.splitlines() if normalized_text(line))
        _require_substantive_body(visual_lines, candidate.title, attachment)
    else:
        parser = _Body()
        try:
            parser.feed(content)
            parser.close()
            if parser.stack:
                raise DocumentError("document_layout_invalid")
            parser._flush()
        except DocumentError:
            raise
        except (ValueError, AssertionError, RecursionError):
            raise DocumentError("document_layout_invalid") from None
        paragraphs = tuple(parser.paragraphs)
        replaced = parser.private_use_characters_replaced
    # Check the retained extraction as well as the original visual lines above,
    # before trimming: neither reflow nor a partial prefix may evade the limits.
    if len(paragraphs) > MAX_ANNOUNCEMENT_PARAGRAPHS or len("\n\n".join(paragraphs)) > MAX_ANNOUNCEMENT_CHARACTERS:
        raise DocumentError("document_too_large")
    _require_substantive_body(paragraphs, candidate.title, attachment)
    omitted = 0
    if pages > 1:
        paragraphs, omitted = trim_incomplete_tail(paragraphs)
        _require_substantive_body(paragraphs, candidate.title, attachment)
    return ResearchDocument(candidate, paragraphs, retrieved_at, EXTRACTOR_VERSION, published,
        complete=pages == 1, incomplete_reasons=("document_first_page_only",) if pages > 1 else (),
        page_count=pages, private_use_characters_replaced=replaced, omitted_trailing_characters=omitted)
