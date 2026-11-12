"""Bounded source documents and exact excerpts; full text never becomes model metadata."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import re


POLICY_VERSION = "research-documents-v1"
MAX_DOCUMENT_BYTES = 512_000
MAX_NEWS_CHARACTERS = 16_000
MAX_ANNOUNCEMENT_CHARACTERS = 32_000
MAX_DOCUMENT_PARAGRAPHS = 128
MAX_ANNOUNCEMENT_PARAGRAPHS = 400
MAX_EXCERPTS = 2
MAX_EXCERPT_CHARACTERS = 480
SHANGHAI = timezone(timedelta(hours=8))
TEMPORAL_SCOPE = "current_document_not_versioned_pit"

_ERROR_CODES = frozenset((
    "document_invalid_candidate", "document_invalid_document", "document_invalid_excerpt",
    "document_source_unverified", "document_identity_mismatch", "document_title_mismatch",
    "document_time_mismatch", "document_time_unavailable", "document_layout_invalid",
    "document_instrument_mismatch", "document_too_large", "document_attachment_only",
    "document_empty_body", "document_updated_after_retrieval", "document_invalid_response",
    "document_incomplete_body",
))


class DocumentError(ValueError):
    """Only a fixed code escapes a parser, never source text or arbitrary exception details."""

    def __init__(self, code):
        if code not in _ERROR_CODES:
            raise ValueError("Unknown document error code")
        self.code = code
        super().__init__(code)


def normalized_text(value):
    return " ".join(value.split())


def document_paragraph_limit(kind):
    return MAX_ANNOUNCEMENT_PARAGRAPHS if kind == "announcements" else MAX_DOCUMENT_PARAGRAPHS


def coverage_code(kind, complete, omitted_tables, page_count,
                  private_use_characters_replaced, omitted_trailing_characters):
    """Validate source-specific coverage; usable text is not necessarily a complete document."""
    counts = (omitted_tables, page_count, private_use_characters_replaced, omitted_trailing_characters)
    maximum = MAX_NEWS_CHARACTERS if kind == "news" else MAX_ANNOUNCEMENT_CHARACTERS
    if (kind not in ("news", "announcements") or type(complete) is not bool
            or any(type(value) is not int or not 0 <= value <= MAX_DOCUMENT_BYTES for value in counts)
            or page_count < 1 or omitted_trailing_characters > maximum
            or kind == "news" and (page_count != 1 or private_use_characters_replaced != 0)
            or kind == "announcements" and omitted_tables != 0):
        raise DocumentError("document_invalid_document")
    reason = ("document_tables_omitted" if omitted_tables else "document_read") if kind == "news" else (
        "document_first_page_only" if page_count > 1 else "document_read")
    if complete != (reason == "document_read") or complete and omitted_trailing_characters:
        raise DocumentError("document_invalid_document")
    return reason


def trim_incomplete_tail(paragraphs):
    """Drop the whole unreadable suffix, retaining only a trusted sentence boundary."""
    pairs = {"（": "）", "(": ")", "[": "]", "【": "】", "{": "}", "《": "》", "〈": "〉",
             "“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}
    closers = set(pairs.values()) | {"'"}
    stack, boundary, sentence_end = [], None, False
    for index, paragraph in enumerate(paragraphs):
        # ASCII query punctuation belongs to the link, not to the surrounding prose.
        urls = {position for match in re.finditer(
            r'(?:https?://|www\.)[^\s<>"“”「」『』（）【】。！？]+', paragraph, flags=re.I)
                for position in range(match.start(), match.end())}
        for offset, character in enumerate(paragraph):
            if offset in urls:
                sentence_end = False
            elif character in ("'", "’") and 0 < offset < len(paragraph) - 1 and all(
                    c.isascii() and c.isalnum() for c in (paragraph[offset - 1], paragraph[offset + 1])):
                sentence_end = False  # A word-internal apostrophe cannot close an outer quote.
            elif stack and character == stack[-1]:
                stack.pop()
                if sentence_end and not stack:
                    boundary = (index, offset + 1)
            elif character in pairs:
                stack.append(pairs[character])
                sentence_end = False
            elif character in closers:
                if sentence_end and not stack:
                    boundary = (index, offset + 1)
                else:
                    sentence_end = False
            elif character in "。！？!?":
                sentence_end = True
                if not stack:
                    boundary = (index, offset + 1)
            elif not character.isspace():
                sentence_end = False
    if boundary is None:
        raise DocumentError("document_empty_body")
    index, end = boundary
    kept = (*paragraphs[:index], paragraphs[index][:end])
    return kept, len("\n\n".join(paragraphs)) - len("\n\n".join(kept))


def timestamp(value, *, code="document_time_unavailable"):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise DocumentError(code) from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise DocumentError(code)
    return value.astimezone(SHANGHAI)


def supported_news_url(value):
    return type(value) is str and bool(re.fullmatch(
        r"https://finance\.sina\.com\.cn/(?:[A-Za-z0-9_-]+/)*doc-[a-z0-9]+\.shtml", value))


def announcement_code(value, instrument_id=None):
    match = re.fullmatch(
        r"https://data\.eastmoney\.com/notices/detail/([0-9]{6})/(AN[0-9]{12,40})\.html", value
    ) if type(value) is str else None
    if match is None or instrument_id is not None and match[1] != instrument_id[:6]:
        return None
    return match[2]


def mentions_instrument(value, candidate):
    return candidate.security_name in value or bool(re.search(
        r"(?<![0-9])" + candidate.instrument_id[:6] + r"(?![0-9])", value))


@dataclass(frozen=True)
class DocumentCandidate:
    parent_id: str
    instrument_id: str
    security_name: str
    kind: str
    source: str
    title: str
    url: str
    published_date: str
    published_at: str | None = None

    def __post_init__(self):
        match = re.fullmatch(r"([0-9]{6})\.(SH|SZ|BJ)", self.instrument_id) if type(self.instrument_id) is str else None
        prefixes = {"SH": ("6",), "SZ": ("0", "3"), "BJ": ("4", "8", "9")}
        if (match is None or not match[1].startswith(prefixes[match[2]])
                or type(self.parent_id) is not str or not 1 <= len(self.parent_id) <= 500
                or not self.parent_id.isprintable() or type(self.security_name) is not str
                or not 1 <= len(self.security_name) <= 80 or not self.security_name.isprintable()
                or self.security_name != normalized_text(self.security_name)
                or type(self.title) is not str or not 1 <= len(self.title) <= 600
                or not self.title.isprintable() or self.title != normalized_text(self.title)
                or (self.kind, self.source) not in (("news", "sina_company_news"),
                                                     ("announcements", "eastmoney_announcements"))):
            raise DocumentError("document_invalid_candidate")
        if (self.kind == "news" and (match[2] == "BJ" or not supported_news_url(self.url))
                or self.kind == "announcements" and (match[2] == "BJ" or announcement_code(self.url, self.instrument_id) is None)):
            raise DocumentError("document_source_unverified")
        try:
            day = date.fromisoformat(self.published_date) if type(self.published_date) is str else None
        except ValueError:
            day = None
        if day is None or day.isoformat() != self.published_date:
            raise DocumentError("document_invalid_candidate")
        if self.published_at is not None:
            stamp = timestamp(self.published_at)
            if stamp.date() != day:
                raise DocumentError("document_time_mismatch")
            object.__setattr__(self, "published_at", stamp.isoformat())


@dataclass(frozen=True)
class DocumentExcerpt:
    document_id: str
    content_sha256: str
    paragraph_index: int
    start: int
    end: int
    text: str
    kind: str = "news"

    def __post_init__(self):
        if (type(self.document_id) is not str or not re.fullmatch(r"document:[0-9a-f]{64}", self.document_id)
                or type(self.content_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256)
                or type(self.kind) is not str or self.kind not in ("news", "announcements")
                or type(self.paragraph_index) is not int or not 1 <= self.paragraph_index <= document_paragraph_limit(self.kind)
                or type(self.start) is not int or type(self.end) is not int
                or not 0 <= self.start < self.end <= MAX_ANNOUNCEMENT_CHARACTERS
                or type(self.text) is not str or not self.text.isprintable()
                or not 1 <= len(self.text) <= MAX_EXCERPT_CHARACTERS or self.end - self.start != len(self.text)):
            raise DocumentError("document_invalid_excerpt")

    @property
    def sha256(self):
        return sha256(self.text.encode("utf-8")).hexdigest()

    def to_dict(self):
        return {"document_id": self.document_id, "content_sha256": self.content_sha256,
                "paragraph_index": self.paragraph_index, "start": self.start, "end": self.end,
                "text": self.text, "sha256": self.sha256}


@dataclass(frozen=True)
class ResearchDocument:
    candidate: DocumentCandidate
    paragraphs: tuple[str, ...] = field(repr=False)
    retrieved_at: str
    extractor_version: str
    published_at: str | None = None
    updated_at: str | None = None
    metadata_at: str | None = None
    complete: bool = True
    incomplete_reasons: tuple[str, ...] = ()
    omitted_tables: int = 0
    page_count: int = 1
    private_use_characters_replaced: int = 0
    omitted_trailing_characters: int = 0

    def __post_init__(self):
        if (type(self.candidate) is not DocumentCandidate or type(self.paragraphs) is not tuple
                or not self.paragraphs
                or any(type(p) is not str or not p or not p.isprintable() or p != normalized_text(p) for p in self.paragraphs)
                or type(self.extractor_version) is not str or self.extractor_version not in (
                    "sina-artibody-v3", "eastmoney-announcement-body-v2")):
            raise DocumentError("document_invalid_document")
        code = coverage_code(self.candidate.kind, self.complete, self.omitted_tables, self.page_count,
                             self.private_use_characters_replaced, self.omitted_trailing_characters)
        if type(self.incomplete_reasons) is not tuple or self.incomplete_reasons != (
                () if self.complete else (code,)):
            raise DocumentError("document_invalid_document")
        if len(self.paragraphs) > document_paragraph_limit(self.candidate.kind):
            raise DocumentError("document_too_large")
        expected = "sina-artibody-v3" if self.candidate.kind == "news" else "eastmoney-announcement-body-v2"
        if self.extractor_version != expected:
            raise DocumentError("document_invalid_document")
        maximum = MAX_NEWS_CHARACTERS if self.candidate.kind == "news" else MAX_ANNOUNCEMENT_CHARACTERS
        if len(self.text) + self.omitted_trailing_characters > maximum:
            raise DocumentError("document_too_large")
        retrieved = timestamp(self.retrieved_at)
        if self.candidate.published_date > retrieved.date().isoformat():
            raise DocumentError("document_time_mismatch")
        published = timestamp(self.published_at or self.candidate.published_at) if self.published_at or self.candidate.published_at else None
        if published is not None:
            if published.date().isoformat() != self.candidate.published_date or published > retrieved:
                raise DocumentError("document_time_mismatch")
            if self.candidate.published_at is not None and published.replace(second=0, microsecond=0) != timestamp(
                    self.candidate.published_at).replace(second=0, microsecond=0):
                raise DocumentError("document_time_mismatch")
            object.__setattr__(self, "published_at", published.isoformat())
        if self.updated_at is not None:
            updated = timestamp(self.updated_at)
            if updated > retrieved:
                raise DocumentError("document_updated_after_retrieval")
            if published is not None and updated < published:
                raise DocumentError("document_time_mismatch")
            object.__setattr__(self, "updated_at", updated.isoformat())
        if self.metadata_at is not None:
            metadata = timestamp(self.metadata_at)
            if metadata > retrieved or published is not None and metadata < published:
                raise DocumentError("document_time_mismatch")
            object.__setattr__(self, "metadata_at", metadata.isoformat())
        object.__setattr__(self, "retrieved_at", retrieved.isoformat())

    @property
    def text(self):
        return "\n\n".join(self.paragraphs)

    @property
    def content_sha256(self):
        return sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def id(self):
        values = [self.candidate.instrument_id, self.candidate.parent_id, self.candidate.source,
                  self.candidate.url, self.candidate.title, self.candidate.published_date,
                  self.published_at, self.content_sha256, self.extractor_version,
                  self.complete, self.incomplete_reasons, self.omitted_tables, self.page_count,
                  self.private_use_characters_replaced, self.omitted_trailing_characters]
        return "document:" + sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()

    def excerpts(self, question, max_count=MAX_EXCERPTS, max_chars=MAX_EXCERPT_CHARACTERS):
        if (type(question) is not str or len(question) > 20_000 or type(max_count) is not int
                or not 1 <= max_count <= MAX_EXCERPTS or type(max_chars) is not int
                or not 1 <= max_chars <= MAX_EXCERPT_CHARACTERS):
            raise DocumentError("document_invalid_excerpt")
        terms = set(re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]{2}", question))
        terms.update(question[i:i + 2] for i in range(len(question) - 1)
                     if re.fullmatch(r"[\u3400-\u9fff]{2}", question[i:i + 2]))
        candidates = []
        for index, paragraph in enumerate(self.paragraphs, 1):
            for start, text in _sentence_excerpts(paragraph, max_chars):
                candidates.append((sum(term in text for term in terms), index, start, text))
        chosen = sorted(sorted(candidates, key=lambda p: (-p[0], p[1], p[2]))[:max_count], key=lambda p: (p[1], p[2]))
        return tuple(DocumentExcerpt(self.id, self.content_sha256, index, start, start + len(text), text,
                                     kind=self.candidate.kind)
                     for _, index, start, text in chosen)

    def public_summary(self):
        c = self.candidate
        return {"document_id": self.id, "parent_id": c.parent_id, "instrument_id": c.instrument_id,
                "kind": c.kind, "source": c.source, "title": c.title, "url": c.url,
                "published_date": c.published_date, "published_at": self.published_at,
                "retrieved_at": self.retrieved_at, "extractor_version": self.extractor_version,
                "content_sha256": self.content_sha256, "paragraph_count": len(self.paragraphs),
                "character_count": len(self.text), "status": "complete" if self.complete else "partial",
                "temporal_scope": TEMPORAL_SCOPE, "updated_at": self.updated_at,
                "code": "document_read" if self.complete else self.incomplete_reasons[0],
                **self.coverage_metadata()}

    def coverage_metadata(self):
        return dict(complete=self.complete, omitted_tables=self.omitted_tables, page_count=self.page_count,
                    private_use_characters_replaced=self.private_use_characters_replaced,
                    omitted_trailing_characters=self.omitted_trailing_characters)


def _sentence_excerpts(paragraph, maximum):
    """Keep whole paragraphs or sentences; never manufacture a number by slicing it."""
    if len(paragraph) <= maximum:
        return ((0, paragraph),)
    result, start, text = [], None, ""
    for match in re.finditer(r".+?(?:[。！？!?]+[”’」』）)]*|$)", paragraph):
        sentence = match[0]
        if len(sentence) > maximum:
            if text:
                result.append((start, text))
            start, text = None, ""
            continue
        if text and len(text) + len(sentence) > maximum:
            result.append((start, text))
            start, text = None, ""
        if start is None:
            start = match.start()
        text += sentence
    if text:
        result.append((start, text))
    return tuple(result)
