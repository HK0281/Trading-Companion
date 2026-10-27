"""Small deterministic lexical retrieval over versioned, maintainer-owned notes."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
import math
import re
import unicodedata
from urllib.parse import urlsplit

from trading_companion import knowledge_data


RETRIEVAL_VERSION = "local-knowledge-retrieval-v1"
RETRIEVAL_ALGORITHM = "alias-gated-cjk-bigram-bm25-v1"
MAX_HITS = 4
MAX_QUERY_CHARACTERS = 2000
_CATEGORIES = frozenset(("market_rules", "financial", "risk", "technical", "research_methods"))
_TOKEN = re.compile(r"[a-z][a-z0-9+.-]*|[\u3400-\u9fff]+|[0-9]+")
_ID = re.compile(r"[a-z][a-z0-9_]{1,63}")


def _normalized(text):
    value = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"(?<![a-z])p\s*/\s*([eb])(?![a-z])", r"p\1",
                  re.sub(r"t\s*\+\s*([01])", r"t+\1", value))


def _tokens(text):
    output = []
    for token in _TOKEN.findall(_normalized(text)):
        if "\u3400" <= token[0] <= "\u9fff":
            output.extend(token[index:index + 2] for index in range(max(1, len(token) - 1)))
        else:
            output.append(token)
    return output


def _alias_match(alias, query):
    alias = _normalized(alias)
    if re.search(r"[a-z]", alias):
        return re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", query) is not None
    return alias in query


@dataclass(frozen=True)
class KnowledgeSource:
    title: str
    publisher: str
    source_type: str
    reference_version: str
    url: str

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (self.source_type not in ("official_reference", "project_reference")
                or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port is not None or parsed.fragment
                or any(not isinstance(value, str) or not value or len(value) > 500
                       or not value.isprintable() for value in (self.title, self.publisher, self.reference_version, self.url))):
            raise ValueError("Invalid local knowledge reference")


@dataclass(frozen=True)
class KnowledgeEntry:
    entry_id: str
    category: str
    title: str
    aliases: tuple[str, ...]
    excerpt: str
    source: KnowledgeSource
    version: str = knowledge_data.CORPUS_VERSION
    maintained_on: str = knowledge_data.MAINTAINED_ON

    def __post_init__(self):
        if (type(self.entry_id) is not str or _ID.fullmatch(self.entry_id) is None
                or self.category not in _CATEGORIES or type(self.source) is not KnowledgeSource
                or type(self.aliases) is not tuple or not 1 <= len(self.aliases) <= 16
                or any(type(alias) is not str or not alias or len(alias) > 50 or not alias.isprintable() for alias in self.aliases)
                or any(type(value) is not str or not value or not value.isprintable()
                       for value in (self.title, self.excerpt, self.version))
                or len(self.title) > 100 or len(self.excerpt) > 500 or len(self.version) > 100):
            raise ValueError("Invalid local knowledge entry")
        date.fromisoformat(self.maintained_on)


@dataclass(frozen=True)
class KnowledgeHit:
    alias: str
    entry: KnowledgeEntry
    score: float = field(repr=False)

    def to_dict(self):
        source = self.entry.source
        return {"id": self.alias, "snippet_id": self.entry.entry_id, "title": self.entry.title,
                "version": self.entry.version, "maintained_on": self.entry.maintained_on,
                "excerpt": self.entry.excerpt, "content_origin": "maintainer_explanation",
                "source_type": source.source_type, "source_title": source.title,
                "publisher": source.publisher, "url": source.url,
                "reference_version": source.reference_version, "live_verified": False}


@dataclass(frozen=True)
class KnowledgeRetrieval:
    hits: tuple[KnowledgeHit, ...] = ()
    corpus_version: str = knowledge_data.CORPUS_VERSION
    corpus_size: int = len(knowledge_data.ENTRIES)
    categories: tuple[str, ...] = tuple(sorted(_CATEGORIES))

    def __post_init__(self):
        if (type(self.hits) is not tuple or len(self.hits) > MAX_HITS
                or any(type(hit) is not KnowledgeHit or hit.alias != "K" + str(index + 1)
                       or type(hit.entry) is not KnowledgeEntry or not math.isfinite(hit.score) or hit.score <= 0
                       for index, hit in enumerate(self.hits))
                or len({hit.entry.entry_id for hit in self.hits}) != len(self.hits)
                or type(self.corpus_size) is not int or not 0 <= self.corpus_size <= 200
                or self.corpus_size < len(self.hits) or type(self.corpus_version) is not str
                or not 1 <= len(self.corpus_version) <= 100 or not self.corpus_version.isprintable()
                or type(self.categories) is not tuple or set(self.categories) - _CATEGORIES):
            raise ValueError("Invalid local knowledge retrieval")

    def to_dict(self):
        return {"version": RETRIEVAL_VERSION, "algorithm": RETRIEVAL_ALGORITHM,
                "status": "matched" if self.hits else "no_match", "corpus_version": self.corpus_version,
                "corpus_size": self.corpus_size, "searched_categories": list(self.categories),
                "returned": len(self.hits), "snippet_ids": [hit.entry.entry_id for hit in self.hits],
                "network_requested": False, "live_verified": False,
                "scope": "local_maintainer_notes_not_link_fulltext_or_current_rule_verification"}

    def model_context(self):
        return {**self.to_dict(), "snippets": [hit.to_dict() for hit in self.hits]}


class KnowledgeRetriever:
    """BM25 ranking with exact curated aliases to avoid weak incidental matches."""

    def __init__(self, entries):
        entries = tuple(entries)
        if (len(entries) > 200 or any(type(entry) is not KnowledgeEntry for entry in entries)
                or len({entry.entry_id for entry in entries}) != len(entries)):
            raise ValueError("Invalid local knowledge corpus")
        self._entries = entries
        self._counts = tuple(Counter(_tokens(" ".join((entry.title, *entry.aliases, *entry.aliases, entry.excerpt))))
                             for entry in entries)
        self._lengths = tuple(sum(count.values()) for count in self._counts)
        self._average = sum(self._lengths) / len(entries) if entries else 1.0
        self._frequencies = Counter(token for count in self._counts for token in count)

    def search(self, question: str, *, limit: int = MAX_HITS) -> KnowledgeRetrieval:
        if (type(question) is not str or not question.strip() or len(question) > MAX_QUERY_CHARACTERS
                or any(not character.isprintable() and character not in "\n\r\t" for character in question)
                or type(limit) is not int or not 1 <= limit <= MAX_HITS):
            raise ValueError("Invalid local knowledge query")
        query, tokens = _normalized(question), frozenset(_tokens(question))
        ranked = []
        for entry, counts, length in zip(self._entries, self._counts, self._lengths):
            aliases = sum(_alias_match(alias, query) for alias in entry.aliases)
            if not aliases:
                continue
            score = float(aliases)
            for token in sorted(tokens):
                frequency = counts.get(token, 0)
                if frequency:
                    inverse = math.log(1 + (len(self._entries) - self._frequencies[token] + 0.5)
                                       / (self._frequencies[token] + 0.5))
                    score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / self._average))
            ranked.append((score, entry.entry_id, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        hits = tuple(KnowledgeHit("K" + str(index + 1), item[2], item[0])
                     for index, item in enumerate(ranked[:limit]))
        return KnowledgeRetrieval(hits, corpus_size=len(self._entries),
                                  categories=tuple(sorted({entry.category for entry in self._entries})))


@lru_cache(maxsize=1)
def default_knowledge_retriever() -> KnowledgeRetriever:
    sources = {key: KnowledgeSource(title, publisher, authority, version, url)
               for key, title, publisher, authority, version, url in knowledge_data.SOURCES}
    return KnowledgeRetriever(KnowledgeEntry(identifier, category, title, aliases, excerpt, sources[source])
                              for identifier, category, title, aliases, excerpt, source in knowledge_data.ENTRIES)


def retrieve_knowledge(question: str, *, limit: int = MAX_HITS) -> KnowledgeRetrieval:
    return default_knowledge_retriever().search(question, limit=limit)
