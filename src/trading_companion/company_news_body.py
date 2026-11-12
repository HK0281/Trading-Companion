"""Pure Sina extraction using the 2026-10-27 and 2026-11-12 inspected markup.

The parser and paragraph selection originate in the paused 4a stash. No old
downloader, service wiring, answer checker or archival behavior is reused.
"""

from html.parser import HTMLParser
import re

from trading_companion.research_documents import (
    DocumentCandidate, DocumentError, MAX_DOCUMENT_BYTES, MAX_DOCUMENT_PARAGRAPHS,
    MAX_NEWS_CHARACTERS, ResearchDocument, mentions_instrument, normalized_text, timestamp,
    trim_incomplete_tail,
)


EXTRACTOR_VERSION = "sina-artibody-v3"
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"))
_HIDDEN = frozenset(("script", "style", "noscript", "iframe", "template", "ins", "button", "form"))
_INLINE = frozenset(("a", "abbr", "b", "big", "cite", "code", "em", "font", "i", "label", "mark", "nobr",
                     "s", "small", "span", "strong", "sub", "sup", "u"))
_P_END_START = frozenset(("p", "div", "section", "article", "blockquote", "table", "ul", "ol", "h1", "h2", "h3", "h4", "hr"))
_PROMOTION_LABELS = frozenset(("相关专题top", "期货推广", "秒拍", "视频播放器", "行情图", "正文广告top", "开户二维码", "promo"))
_PROMOTION_CLASSES = frozenset(("appendqr_wrap", "appendqr_normal", "appendqr_normal_txt", "article-editor"))
_PUBLICATION_FIELDS = ("bytedance:published_time", "article:published_time")
_UPDATE_FIELDS = ("bytedance:updated_time", "article:modified_time", "article:updated_time", "og:updated_time")
_TIME_FIELDS = frozenset((*_PUBLICATION_FIELDS, *_UPDATE_FIELDS, "bytedance:lrdate_time"))


class _Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.promotions, self.paragraphs = [], [], []
        self.title, self.date, self.times = [], [], {}
        self.body_depth = self.title_depth = self.date_depth = self.paragraph_depth = None
        self.table_depth, self.omitted_tables, self.quotes = None, 0, []
        self.body_count = self.title_count = self.date_count = self.markers = self.nodes = 0
        self.body_closed = self.ended = False
        self.unparsed_body_text = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 12000 or len(self.stack) >= 64:
            raise DocumentError("document_too_large")
        values = dict(attrs)
        if len(values) != len(attrs):
            raise DocumentError("document_layout_invalid")
        if self.body_depth and self.paragraph_depth is not None and tag in _P_END_START:
            index = self.paragraph_depth - 1
            if any(frame[0] not in _INLINE for frame in self.stack[index + 1:]):
                raise DocumentError("document_layout_invalid")
            self._close_to(index)
        classes = (values.get("class") or "").lower().split()
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in _HIDDEN or "hidden" in values
                  or values.get("aria-hidden") == "true"
                  or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", values.get("style") or "", re.I))
                  or any(c in _PROMOTION_CLASSES or re.search(
                      r"(?:^|[-_])(?:ad|ads|sinaads|promo|recommend|video|carousel)(?:$|[-_])", c) for c in classes))
        if tag == "meta":
            names = {(values.get(attr) or "").lower() for attr in ("property", "name")}
            for name in names.intersection(_TIME_FIELDS):
                self.times.setdefault(name, []).append(values.get("content"))
        if values.get("id") == "artibody":
            self.body_count += 1
            if self.body_count != 1 or tag != "div" or hidden or "article" not in classes:
                raise DocumentError("document_layout_invalid")
            self.body_depth = len(self.stack) + 1
        if tag == "h1" and "main-title" in classes and not hidden:
            self.title_count += 1
            self.title_depth = len(self.stack) + 1
        if tag == "span" and "date" in classes and not hidden:
            self.date_count += 1
            self.date_depth = len(self.stack) + 1
        active = self.body_depth and not self.ended and not hidden and not self.promotions
        if active and self.table_depth is None:
            if self.quotes and tag in _P_END_START:
                self._flush_quote()
            if tag == "table":
                self.table_depth = len(self.stack) + 1
                self.omitted_tables += 1
            elif tag == "blockquote":
                self.quotes.append((len(self.stack) + 1, []))
        if active and self.table_depth is None and tag == "p":
            if self.paragraph_depth is not None:
                raise DocumentError("document_layout_invalid")
            self.paragraph_depth, self.parts = len(self.stack) + 1, []
        if tag not in _VOID:
            self.stack.append((tag, hidden))
        elif tag == "br" and active and self.table_depth is None:
            if self.paragraph_depth:
                self.parts.append(" ")
            elif (parts := self._quote_parts()) is not None:
                parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        positions = [i for i, frame in enumerate(self.stack) if frame[0] == tag]
        if not positions:
            if self.body_depth and tag not in _INLINE and tag != "p":
                raise DocumentError("document_layout_invalid")
            return
        index = positions[-1]
        # Browsers implicitly close inline formatting and paragraphs. Do not use
        # that tolerance to guess a missing structural container boundary.
        if self.body_depth and any(frame[0] not in _INLINE and frame[0] != "p"
                                   for frame in self.stack[index + 1:]):
            raise DocumentError("document_layout_invalid")
        self._close_to(index)

    def _close_to(self, index):
        # HTML5 may reconstruct formatting or keep a hidden paragraph open after
        # misnested closures. Never guess when a closure crosses hidden content.
        if self.body_depth and any(frame[1] for frame in self.stack[index + 1:]):
            raise DocumentError("document_layout_invalid")
        while len(self.stack) > index:
            self._finish_depth(len(self.stack))
            self.stack.pop()

    def _finish_depth(self, depth):
        if self.paragraph_depth == depth:
            self._append_paragraph(self.parts)
            self.paragraph_depth = None
        if self.quotes and self.quotes[-1][0] == depth:
            self._flush_quote()
            self.quotes.pop()
        if self.table_depth == depth:
            self.table_depth = None
        if self.title_depth == depth:
            self.title_depth = None
        if self.date_depth == depth:
            self.date_depth = None
        if self.body_depth == depth:
            self.body_depth, self.body_closed = None, True

    def _append_paragraph(self, parts):
        paragraph = normalized_text("".join(parts))
        if paragraph:
            self.paragraphs.append(paragraph)
            if len(self.paragraphs) > MAX_DOCUMENT_PARAGRAPHS or len("\n\n".join(self.paragraphs)) > MAX_NEWS_CHARACTERS:
                raise DocumentError("document_too_large")

    def _flush_quote(self):
        self._append_paragraph(self.quotes[-1][1])
        self.quotes[-1][1].clear()

    def _quote_parts(self):
        if self.quotes and all(frame[0] in _INLINE for frame in self.stack[self.quotes[-1][0]:]):
            return self.quotes[-1][1]
        return None

    def _navigation_text(self):
        if self.quotes:
            return False
        anchors = [i for i in range(self.body_depth, len(self.stack)) if self.stack[i][0] == "a"]
        return bool(anchors) and all(frame[0] in _INLINE for frame in self.stack[anchors[-1] + 1:])

    def handle_comment(self, value):
        value = normalized_text(value).lower()
        if self.stack and self.stack[-1][1]:
            return
        if self.table_depth is not None:
            if value in ("正文 end", "原始正文end"):
                raise DocumentError("document_layout_invalid")
            return
        if value in ("正文 start", "正文 begin"):
            return
        if value in ("正文 end", "原始正文end") and (self.body_depth or self.body_closed):
            if self.promotions or self.paragraph_depth is not None or self.quotes:
                raise DocumentError("document_layout_invalid")
            self.markers += 1
            self.ended = True
        elif self.body_depth and not self.ended:
            match = re.fullmatch(r"(.+?)\s*(begin|start|end)", value)
            label = match[1].strip() if match else None
            # The inspected template repeats "文末二维码 start" four times with
            # no closing comment. Its explicit appendQr containers are excluded
            # instead; arbitrary unmatched comments must never hide later prose.
            if label not in _PROMOTION_LABELS:
                return
            if match[2] in ("begin", "start"):
                self.promotions.append(label)
            elif match[2] == "end":
                if not self.promotions or self.promotions.pop() != label:
                    raise DocumentError("document_layout_invalid")

    def handle_data(self, value):
        if self.stack and self.stack[-1][1]:
            return
        if self.title_depth:
            self.title.append(value)
        if self.date_depth:
            self.date.append(value)
        if not self.body_depth or self.promotions:
            return
        # End comments stop extraction, but visible text left in the same body
        # is still an error, including the otherwise omitted content paths.
        if self.ended:
            if normalized_text(value):
                self.unparsed_body_text = True
        elif self.table_depth is not None:
            return
        elif self.paragraph_depth:
            self.parts.append(value)
        elif (parts := self._quote_parts()) is not None:
            parts.append(value)
        elif self._navigation_text():
            return
        elif normalized_text(value):
            self.unparsed_body_text = True


def _publication(value):
    if type(value) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})", value):
        raise DocumentError("document_time_unavailable")
    return timestamp(value)


def parse_sina_article(text, candidate, *, retrieved_at):
    """Parse one already authorized article without making requests or following links."""
    if type(candidate) is not DocumentCandidate or candidate.kind != "news" or candidate.source != "sina_company_news":
        raise DocumentError("document_invalid_candidate")
    if type(text) is not str or not text:
        raise DocumentError("document_layout_invalid")
    try:
        if len(text) > MAX_DOCUMENT_BYTES or len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise DocumentError("document_too_large")
    except UnicodeError:
        raise DocumentError("document_layout_invalid") from None
    parser = _Article()
    try:
        parser.feed(text)
        parser.close()
    except DocumentError:
        raise
    except (ValueError, AssertionError, RecursionError):
        raise DocumentError("document_layout_invalid") from None
    if (parser.body_count != 1 or not parser.body_closed or parser.markers < 1 or parser.body_depth
            or parser.paragraph_depth or parser.table_depth or parser.quotes or parser.promotions or not parser.paragraphs
            or parser.unparsed_body_text
            or parser.title_count != 1 or parser.title_depth or parser.date_count != 1 or parser.date_depth):
        raise DocumentError("document_layout_invalid")
    if normalized_text("".join(parser.title)) != candidate.title:
        raise DocumentError("document_title_mismatch")
    if not any(name in parser.times for name in _PUBLICATION_FIELDS) or any(
            len(values) != 1 for values in parser.times.values()):
        raise DocumentError("document_time_unavailable")
    times = {name: _publication(values[0]) for name, values in parser.times.items()}
    # Sina exposes its original publication in bytedance metadata; article
    # publication metadata can carry a later time without being an update field.
    published = next(times[name] for name in _PUBLICATION_FIELDS if name in times)
    if (normalized_text("".join(parser.date)) != published.strftime("%Y年%m月%d日 %H:%M")
            or published.date().isoformat() != candidate.published_date
            or candidate.published_at is not None and published.replace(second=0, microsecond=0)
                != timestamp(candidate.published_at).replace(second=0, microsecond=0)):
        raise DocumentError("document_time_mismatch")
    updates = tuple(times[name] for name in _UPDATE_FIELDS if name in times)
    if any(value < published for value in times.values()):
        raise DocumentError("document_time_mismatch")
    paragraphs, omitted_trailing = tuple(parser.paragraphs), 0
    if parser.omitted_tables:
        paragraphs, omitted_trailing = trim_incomplete_tail(paragraphs)
        if not paragraphs:
            raise DocumentError("document_empty_body")
    if not mentions_instrument("\n".join(paragraphs), candidate):
        raise DocumentError("document_instrument_mismatch")
    return ResearchDocument(candidate, paragraphs, retrieved_at, EXTRACTOR_VERSION,
                            published.isoformat(), max(updates).isoformat() if updates else None,
                            metadata_at=max(times.values()).isoformat(), complete=not parser.omitted_tables,
                            incomplete_reasons=("document_tables_omitted",) if parser.omitted_tables else (),
                            omitted_tables=parser.omitted_tables, omitted_trailing_characters=omitted_trailing)
