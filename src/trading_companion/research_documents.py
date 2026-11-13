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


def document_question_terms(question, security_names=(), security_codes=()):
    """Keep topic terms without stock identities or generic requests for a summary."""
    text = question
    for name in sorted(set(security_names), key=len, reverse=True):
        if name:
            text = text.replace(name, " ")
    for code in set(security_codes):
        digits = str(code).split(".", 1)[0]
        if re.fullmatch(r"[0-9]{6}", digits):
            text = re.sub(r"(?<![A-Za-z0-9])" + digits + r"(?:\.(?:SH|SZ|BJ))?(?![A-Za-z0-9])",
                          " ", text, flags=re.I)
    text = re.sub(r"^\s*请", " ", text)
    generic = ("说了什么", "讲了什么", "主要内容", "具体内容", "什么内容", "是什么", "有什么", "告诉我", "帮我", "有没有", "怎么样",
               "最近", "最新", "近期", "今天", "现在", "当前", "请问", "哪些", "什么", "怎么", "是否", "能否",
               "说说", "看看", "分析", "解读", "概括", "总结", "介绍", "主要", "具体", "内容", "情况", "新闻", "公告",
               "这篇", "这份", "这个", "的", "了", "吗", "呢")
    text = re.sub("|".join(map(re.escape, sorted(generic, key=len, reverse=True))), " ", text).casefold()
    terms = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]{2}", text))
    terms.update(text[i:i + 2] for i in range(len(text) - 1)
                 if re.fullmatch(r"[\u3400-\u9fff]{2}", text[i:i + 2]))
    return frozenset(terms)


def _without_issuer(value, candidate):
    """Compare document labels independently of an explicitly named legal issuer."""
    value = re.sub(r"\s+", "", value)
    issuer = re.match(r"[\u3400-\u9fffA-Za-z·（）()]{2,100}(?:股份有限公司|有限责任公司|有限公司)", value)
    if issuer and not re.search(r"拟|收购|审议|通过|转让|出售|设立|授权|签署|决定|批准|合作|计划", issuer[0]):
        value = value[issuer.end():]
    for _ in range(2):
        if value.startswith(candidate.security_name):
            value = value[len(candidate.security_name):].lstrip(":：")
    return value


def _reference_heading(paragraph):
    return bool(re.fullmatch(r"(?:[一二三四五六七八九十百0-9]+[、.．]|[（(][一二三四五六七八九十百0-9]+[）)])?"
                             r"\s*备查(?:文件|资料|文献)[：:。]?", paragraph))


def _reference_entry(paragraph):
    """Only artifact lists in an explicit reference section, never ordinary business prose."""
    numbered = re.match(r"(?:[0-9一二三四五六七八九十百]+[.．、]|[（(][0-9一二三四五六七八九十百]+[）)])\s*(.+)", paragraph)
    if not numbered:
        return False
    title = numbered[1]
    return (not re.search(r"[，,：:]|已|将|拟|完成|办理|新增|获得|收到|本次|截至|占比|比例|于\s*[0-9]", title)
            and bool(re.fullmatch(r"[^。；;！？!?]{1,160}(?:通知书?|明细表|登记表|申请表|证明|说明|报告|复印件|原件|文件|议案|决议)[。；;]?", title)))


def _boilerplate_paragraph(paragraph, candidate):
    """Recognize administrative lines or sentences, never rewrite source text."""
    if len(paragraph) < 40 and re.match(r"[（(]?(?:来源|作者|记者|编辑|责任编辑)\s*[:：]", paragraph):
        return True
    compact = re.sub(r"\s+", "", paragraph)
    title = re.sub(r"\s+", "", candidate.title)
    titles = {title}
    for separator in (":", "："):
        prefix, found, rest = title.partition(separator)
        if found and prefix == candidate.security_name:
            titles.add(rest)
    sentence_end = bool(re.search(r"[。！？!?][”’」』）)]*$", paragraph))
    line = compact.rstrip("。！？!?:：")
    if line in titles:
        return True
    if not sentence_end:
        label = re.sub(r"(?:之[一二三四五六七八九十百0-9]+|[（(][一二三四五六七八九十百0-9]+[）)])$", "",
                       _without_issuer(line, candidate))
        title_labels = {_without_issuer(value, candidate) for value in titles}
        if (line and any(line in title for title in titles)
                or label and any(label in title for title in title_labels)):
            return True
    if len(re.findall(r"(?:\.{3,}|…{2,}|·{3,})\s*[0-9]{1,4}(?![0-9])", paragraph)) >= 2:
        return True
    decision = bool(re.search(r"议案|审议通过|表决|同意\s*[0-9〇零一二三四五六七八九十百]+\s*票", paragraph))
    if (not decision and re.fullmatch(
            r"[\u3400-\u9fffA-Za-z·（）()]{2,100}(?:股份有限公司|有限责任公司|有限公司)(?:董事会|监事会)?", compact)):
        return True
    if (len(paragraph) < 30 and not sentence_end and not decision
            and re.match(r"^(?:[（(]?[一二三四五六七八九十百0-9]+[）)、.．、]|第[一二三四五六七八九十百0-9]+[章节部分])", paragraph)):
        return True
    pieces = paragraph.split()
    numeric_cells = sum(bool(re.fullmatch(r"[-+]?\d[\d,.%/]*", piece)) for piece in pieces)
    fragmented = (bool(re.search(r"[\u3400-\u9fff]", paragraph))
                  and sum(len(piece) for piece in pieces if len(piece) <= 4) * 2 > sum(map(len, pieces))
                  or len(pieces) >= 6 and numeric_cells >= 3
                  and (sum(len(piece) <= 4 for piece in pieces) * 2 > len(pieces) or numeric_cells * 2 >= len(pieces)))
    fragmented = fragmented and not re.search(
        r"公司(?:已|拟|将)|股东(?:已|拟|将)|本次(?:解除|质押)|尚余|已(?:解除|质押|办理)|完成|截至", paragraph)
    if (len(pieces) >= 4 and not sentence_end
            and not re.search(r"同意\s*[0-9〇零一二三四五六七八九十百]+\s*票", paragraph)
            and fragmented):
        return True
    code = re.escape(candidate.instrument_id[:6])
    name = re.escape(candidate.security_name)
    identity = (r"(?:(?:证券|股票)代码[:：]?" + code + r"(?:\.(?:SH|SZ|BJ))?[，,；;]?|"
                r"(?:证券|股票)简称[:：]?" + name + r"[，,；;]?|"
                r"公告编号[:：]?(?:临)?[A-Za-z0-9—－-]+[，,；;]?){2,3}")
    if re.fullmatch(identity, compact.rstrip("。")) and "代码" in compact and "简称" in compact:
        return True
    guarantees = (
        "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，"
        "并对其内容的真实性、准确性和完整性承担法律责任。",
        "本公司及董事会全体成员保证信息披露的内容真实、准确、完整，没有虚假记载、误导性陈述或重大遗漏。",
        "本公司董事会及全体董事保证本公告内容的真实性、准确性和完整性，"
        "不存在虚假记载、误导性陈述或重大遗漏，并承担个别及连带法律责任。",
    )
    def plain(value):
        return re.sub(r"[，,、。；;：:]", "", value).replace("或者", "或")
    def guarantee_only(value):
        guarantee = plain(value)
        return len(guarantee) >= 24 and any(plain(template).startswith(guarantee) for template in guarantees)
    if guarantee_only(compact):
        return True  # Also covers source paragraphs ending midway through a standard guarantee.
    header = re.match(identity, compact)
    if (header is not None and "代码" in header[0] and "简称" in header[0]
            and guarantee_only(compact[header.end():])):
        return True
    date_text = r"(?:[0-9]{4}|[〇零一二三四五六七八九]{4})年[0-9〇零一二三四五六七八九十]{1,3}月[0-9〇零一二三四五六七八九十]{1,3}日"
    pure = (
        r"特此公告[。！!]?",
        date_text + r"[。]?",
        name + r"(?:股份有限公司|有限公司)?(?:董事会)?[。]?",
        r"(?:本报告|本公告|本文)(?:可能)?(?:载有|包含|含有)(?:（除历史数据外）)?前瞻性陈述[。]?",
        r"该等前瞻性陈述乃根据" + name + r"对未来事件或绩效的现行假设、期望、信念、计划、目标及预测而作出[。]?",
        r"(?:上述|相关|该等|本报告中的?)前瞻性陈述(?:并不构成|不构成)(?:本公司|公司)?(?:对投资者的)?(?:实质性)?(?:承诺|业绩承诺|盈利预测)[。]?",
    )
    digits = r"[0-9〇零一二三四五六七八九十百]+"
    day = digits + "年" + digits + "月" + digits + "日"
    meeting = (r"(?:本次|此次|本公司|公司)?(?:第" + digits + r"届)?(?:董事会|监事会|股东会|股东大会)?(?:第"
               + digits + r"次)?会议")
    methods = r"(?:传真|电子邮件|邮件|专人|书面|电话|短信|通讯|网络|等|、|和|及|方式|形式|向|全体|董事|监事)+"
    attendance = r"(?:应|实际|亲自|委托|收到|参加|出席|本次|会议|董事|监事|有效|表决票|的|名|人|全体|共|[0-9〇零一二三四五六七八九十百])+"
    procedure = (
        meeting + r"通知于" + day + r"(?:通过|以)" + methods + r"(?:送达|发出|发送)[。]?",
        meeting + r"于" + day + r"(?:采用|以|通过)(?:通讯|现场|线上|线下|网络|视频|书面|结合|和|及|、|方式)+召开"
        + r"(?:[，,]" + attendance + r")*[。]?",
        r"(?:本次)?会议的?(?:召集[、,，及和]?召开|召集|召开)(?:程序)?符合(?:《[^》]{1,40}》(?:及|和|、)?)+的?(?:有关|相关)?规定[。]?",
        r"(?:经(?:与会|全体)?(?:董事|监事|股东)(?:审议|讨论)[，,]?)?(?:会议|董事会|监事会)?(?:审议|表决)?"
        r"(?:通过|形成|作出)了?(?:以下|如下)(?:[一二三四五六七八九十0-9]+项)?(?:决议|议案|事项)[：:。]?",
        r"(?:决议|议案|事项)(?:如下|如下所示)[：:。]?",
    )
    sentences = re.findall(r"[^。！？!?]+[。！？!?]*", compact)
    return bool(sentences) and all(any(re.fullmatch(pattern, sentence) for pattern in pure)
        or any(re.fullmatch(pattern, _without_issuer(sentence, candidate)) for pattern in procedure)
        for sentence in sentences)


def _substantive_excerpt(text):
    if re.search(r"议案|审议通过|表决|同意\s*[0-9〇零一二三四五六七八九十百]+\s*票", text):
        return True
    return len(text) >= 30 and bool(re.search(r"[0-9]|[。！？!?][”’」』）)]*$", text))


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
                    "sina-artibody-v3", "eastmoney-announcement-body-v2", "eastmoney-announcement-body-v3")):
            raise DocumentError("document_invalid_document")
        code = coverage_code(self.candidate.kind, self.complete, self.omitted_tables, self.page_count,
                             self.private_use_characters_replaced, self.omitted_trailing_characters)
        if type(self.incomplete_reasons) is not tuple or self.incomplete_reasons != (
                () if self.complete else (code,)):
            raise DocumentError("document_invalid_document")
        if len(self.paragraphs) > document_paragraph_limit(self.candidate.kind):
            raise DocumentError("document_too_large")
        expected = (("sina-artibody-v3",) if self.candidate.kind == "news" else
                    ("eastmoney-announcement-body-v2", "eastmoney-announcement-body-v3"))
        if self.extractor_version not in expected:
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
        terms = document_question_terms(question, (self.candidate.security_name,), (self.candidate.instrument_id,))
        candidates = []
        references = False
        for index, paragraph in enumerate(self.paragraphs, 1):
            if _reference_heading(paragraph):
                references = True
                continue
            if references and re.match(r"^[一二三四五六七八九十百]+[、.．]", paragraph):
                references = False
            if references and _reference_entry(paragraph):
                continue
            if _boilerplate_paragraph(paragraph, self.candidate):
                continue
            for start, text in _sentence_excerpts(paragraph, max_chars, self.candidate):
                candidates.append((sum(term in text.casefold() for term in terms), index, start, text))
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


def _sentence_excerpts(paragraph, maximum, candidate):
    """Keep continuous substantive source spans, without joining across skipped sentences."""
    result, start, end = [], None, None
    def emit():
        if start is not None and _substantive_excerpt(paragraph[start:end]):
            result.append((start, paragraph[start:end]))
    for match in re.finditer(r".+?(?:[。！？!?]+[”’」』）)]*|$)", paragraph):
        left = match.start() + len(match[0]) - len(match[0].lstrip())
        sentence = paragraph[left:match.end()]
        if len(sentence) > maximum or _boilerplate_paragraph(sentence, candidate):
            emit()
            start, end = None, None
            continue
        if start is not None and match.end() - start > maximum:
            emit()
            start, end = None, None
        if start is None:
            start = left
        end = match.end()
    emit()
    return tuple(result)
