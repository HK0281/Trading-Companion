"""Optional current-document reads with an independent, revocable source ledger."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
import json
import re
from time import monotonic

from trading_companion.collection_execution import CollectionScope, CollectionUnavailable, shared_collection_executor
from trading_companion.research_documents import (
    DocumentCandidate, DocumentError, POLICY_VERSION, announcement_code, document_question_terms,
    supported_news_url, timestamp,
)
from trading_companion.company_news_body import parse_sina_article
from trading_companion.company_announcement_body import parse_eastmoney_announcement
from trading_companion.product_research import ProductEvidence
from trading_companion.turn_control import TurnStopped, checkpoint, current_control
from trading_companion.turn_trace import trace_documents


DETAIL_ENDPOINT = 'https://np-cnotice-stock.eastmoney.com/api/content/ann'
SKIP_REASONS = frozenset(('documents_off', 'documents_not_requested', 'documents_historical',
    'documents_development', 'documents_insufficient_time', 'documents_no_candidates',
    'documents_timeout', 'documents_context_capacity', 'documents_unavailable'))

_CHARSETS = {'utf-8': 'utf-8-sig', 'utf8': 'utf-8-sig', 'gb2312': 'gb2312',
             'gbk': 'gbk', 'gb18030': 'gb18030'}


def _charset(value):
    if type(value) is not str or value.strip().lower() not in _CHARSETS:
        raise ValueError('unsupported document charset')
    return _CHARSETS[value.strip().lower()]


def _header_charset(content_type):
    header = Message()
    header['Content-Type'] = content_type
    declared = [_charset(value) for name, value in header.get_params(failobj=[])[1:]
                if name.lower() == 'charset']
    if len(set(declared)) > 1:
        raise ValueError('conflicting document charsets')
    return declared[0] if declared else None


class _MetaCharset(HTMLParser):
    def __init__(self):
        super().__init__()
        self.declared = []

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        values = dict(attrs)
        if len(values) != len(attrs):
            raise ValueError('duplicate document charset attributes')
        if 'charset' in values:
            self.declared.append(_charset(values['charset']))
        if (values.get('http-equiv') or '').strip().lower() == 'content-type':
            declared = _header_charset(values.get('content') or '')
            if declared is not None:
                self.declared.append(declared)


def decode_document_payload(body, kind, content_type):
    """Decode bounded source bytes strictly; declarations never select arbitrary codecs."""
    if (type(body) is not bytes or not 0 < len(body) <= 512000 or type(content_type) is not str
            or kind not in ('news', 'announcements')):
        raise ValueError('invalid document payload')
    media = content_type.split(';', 1)[0].strip().lower()
    if media not in (('text/html',) if kind == 'news' else ('application/json', 'text/json')):
        raise ValueError('invalid document media type')
    encoding = _header_charset(content_type)
    if kind == 'announcements':
        if encoding not in (None, 'utf-8-sig'):
            raise ValueError('JSON requires UTF-8')
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        return json.loads(body.decode('utf-8-sig'), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    if encoding is None:
        metadata = _MetaCharset()
        metadata.feed(body[:2048].decode('latin-1'))
        if len(set(metadata.declared)) > 1:
            raise ValueError('conflicting document charsets')
        encoding = metadata.declared[0] if metadata.declared else None
    if encoding is not None:
        return body.decode(encoding)
    try:
        return body.decode('utf-8-sig')
    except UnicodeDecodeError:
        return body.decode('gb18030')


def document_trigger(plan, question):
    if plan.explicit_historical:
        return 'documents_historical'
    if not set(plan.kinds).intersection(('news', 'announcements')):
        return 'documents_not_requested'
    if re.search(r'(?:不要|不用|不必|无需|别|不)(?:读取?|阅读|展开|看)(?:正文|全文|原文|内容)', question):
        return 'documents_not_requested'
    from trading_companion.product_scope import document_request_scope
    positive, excluded, explicit = document_request_scope(question)
    allowed = set(plan.kinds).intersection(('news', 'announcements')).difference(excluded)
    return None if explicit.intersection(allowed) or allowed and re.search(r'原文|正文|全文', positive) else 'documents_not_requested'


def _candidate_slots(bundle, question):
    from trading_companion.product_scope import document_request_scope
    _, excluded, _ = document_request_scope(question)
    names = tuple(company.name for company in bundle.companies)
    codes = tuple(company.instrument_id for company in bundle.companies)
    terms = document_question_terms(question, security_names=names, security_codes=codes)
    groups = []
    for company in bundle.companies:
        slots = []
        for kind in ('news', 'announcements'):
            if kind not in bundle.plan.kinds or kind in excluded:
                continue
            ranked = []
            for parent in company.evidence:
                if (parent.kind != kind or parent.instrument_id != company.instrument_id or parent.content_kind != 'title'
                        or parent.data_authenticity != 'provider_reported'):
                    continue
                try:
                    candidate = DocumentCandidate(parent.id, parent.instrument_id, company.name, kind,
                        parent.source, str(parent.raw_value) if type(parent.raw_value) is str else parent.display,
                        parent.url, parent.date, parent.published_at)
                except DocumentError:
                    continue
                score = len(terms.intersection(document_question_terms(candidate.title,
                    security_names=names, security_codes=codes)))
                ranked.append((score, candidate, parent))
            date_only_groups = {(score, candidate.published_date) for score, candidate, _ in ranked
                                if candidate.published_at is None}
            # An unknown time cannot be ordered against another same-day time.
            # Preserve source order for that whole tied group instead of making
            # a date-only publication look as though it occurred at midnight.
            ranked.sort(key=lambda row: (row[0], row[1].published_date,
                datetime.min.replace(tzinfo=timezone.utc) if (row[0], row[1].published_date) in date_only_groups
                else timestamp(row[1].published_at)), reverse=True)
            selected, seen_ids, seen_urls = [], set(), set()
            for score, candidate, parent in ranked:
                if selected and ranked[0][0] > 0 and score == 0:
                    break
                if candidate.parent_id in seen_ids or candidate.url in seen_urls:
                    continue
                selected.append((candidate, parent))
                seen_ids.add(candidate.parent_id)
                seen_urls.add(candidate.url)
            if selected:
                slots.append(tuple(selected))
        groups.append(slots)
    # Every company gets a first opportunity before a single company gets another domain.
    chosen = tuple(group[round_] for round_ in range(2 if len(groups) == 1 else 1)
                   for group in groups if len(group) > round_)[:3]
    slots, primary_ids, primary_urls = [], set(), set()
    for slot in chosen:
        independent = tuple(pair for pair in slot
                            if pair[0].parent_id not in primary_ids and pair[0].url not in primary_urls)
        # A duplicate primary must not consume the company's opportunity. Promote
        # its next authorized candidate before any requests or fallback counts.
        promoted = (independent or slot)[:2]
        slots.append(promoted)
        primary_ids.add(promoted[0][0].parent_id)
        primary_urls.add(promoted[0][0].url)
    return tuple(slots)


def _candidates(bundle, question=''):
    """Keep the original primary-candidate interface; alternates remain internal."""
    return tuple(slot[0] for slot in _candidate_slots(bundle, question))


def _document_item(candidate):
    return dict(instrument_id=candidate.instrument_id, kind=candidate.kind,
        parent_title_id=candidate.parent_id, title=candidate.title, url=candidate.url,
        source=candidate.source, status='unavailable', code='document_not_read')


class DocumentCollector:
    """One bounded alternate per slot; no repeated URLs, attachments, or model calls."""

    def __init__(self, *, session=None, clock=monotonic, executor=None, wall_clock=None):
        self.session, self.clock = session, clock
        self.executor = executor or shared_collection_executor()
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))

    def _read(self, candidate, stage, request_scope):
        import requests
        response = None
        try:
            request_scope.check()
            stage.reserve_request()
            timeout = min(4.0, request_scope.remaining(), stage.remaining())
            if candidate.kind == 'news' and supported_news_url(candidate.url):
                url, params, accept = candidate.url, None, 'text/html'
            elif candidate.kind == 'announcements' and announcement_code(candidate.url, candidate.instrument_id):
                url, params, accept = DETAIL_ENDPOINT, {
                    'art_code': announcement_code(candidate.url, candidate.instrument_id),
                    'page_index': 1, 'client_source': 'web'}, 'application/json'
            else:
                raise DocumentError('document_source_unverified')
            client = self.session if self.session is not None else requests
            response = client.request('GET', url, params=params, timeout=(min(3.0, timeout), timeout),
                allow_redirects=False, stream=True,
                headers={'User-Agent': 'TradingCompanion/0.1 public-research', 'Accept': accept})
            request_scope.check()
            stage.check()
            if response.status_code != 200:
                return None, 'document_redirect_blocked' if 300 <= response.status_code < 400 else 'document_http_error'
            media = response.headers.get('Content-Type', '').lower()
            media_type = media.split(';', 1)[0].strip()
            if not ((media_type == 'text/html' and candidate.kind == 'news') or
                    (media_type in ('application/json', 'text/json') and candidate.kind == 'announcements')):
                return None, 'document_content_type_invalid'
            declared = response.headers.get('Content-Length')
            if declared is not None and (not declared.isascii() or not declared.isdigit() or int(declared) > 512000):
                return None, 'document_too_large'
            body = bytearray()
            for chunk in response.iter_content(8192):
                request_scope.check()
                stage.check()
                stage.consume(len(chunk))
                if len(body) + len(chunk) > 512000:
                    return None, 'document_too_large'
                body.extend(chunk)
            request_scope.check()
            stage.check()
            return decode_document_payload(bytes(body), candidate.kind, media), None
        except (requests.RequestException, OSError):
            return None, 'document_connection_failed'
        except (UnicodeError, ValueError, RecursionError):
            return None, 'document_invalid_response'
        finally:
            if response is not None:
                response.close()

    def enrich(self, bundle, *, question, now, sorting=False):
        reason = document_trigger(bundle.plan, question)
        chosen = _candidate_slots(bundle, question) if reason is None else ()
        if reason is None and not chosen:
            reason = 'documents_no_candidates'
        control = current_control()
        remaining = control.timeout_for(90) if control is not None else 90
        allowance = min(12.0, max(0.0, remaining - 20 - (5 if sorting else 0)))
        if reason is None and allowance <= 0:
            reason = 'documents_insufficient_time'
        if reason is not None:
            trace_documents(dict(documents_skipped_reason=reason, requests=0, response_bytes=0,
                                 elapsed_ms=0, available=0, attempted=0))
            # Non-event questions are byte-for-byte compatible with the default path.
            return bundle
        started = self.clock()
        stage = CollectionScope(deadline=started + allowance, requests=4, response_bytes=2048000, clock=self.clock)
        items = [_document_item(slot[0][0]) for slot in chosen]
        fragments, seen_ids, seen_urls, primary_attempted = [], set(), set(), set()
        attempted = fallback_attempted = fallback_available = no_substantive_excerpts = 0
        budget_stopped = False
        skipped = None
        try:
            # Complete every company's primary opportunity before any alternate.
            schedule = tuple((index, alternative) for alternative in range(2) for index in range(len(chosen)))
            for index, alternative in schedule:
                checkpoint()
                if (alternative and (items[index]['status'] == 'available' or len(chosen[index]) < 2
                                     or index not in primary_attempted)
                        or budget_stopped):
                    continue
                candidate, parent = chosen[index][alternative]
                item, entered = items[index], False
                try:
                    stage.check()
                    if attempted >= 4 or stage.usage()['requests'] >= 4:
                        continue
                    if candidate.parent_id in seen_ids or candidate.url in seen_urls:
                        continue
                    seen_ids.add(candidate.parent_id)
                    seen_urls.add(candidate.url)
                    item = items[index] = _document_item(candidate)
                    entered = True
                    attempted += 1
                    fallback_attempted += alternative
                    if not alternative:
                        primary_attempted.add(index)
                    deadline = min(stage.deadline, self.clock() + 4)
                    scope = CollectionScope(deadline=deadline, requests=1, response_bytes=512000, clock=self.clock)
                    def read(c=candidate, s=scope):
                        try:
                            return self._read(c, stage, s)
                        except CollectionUnavailable as error:
                            return None, error.code
                    result, = self.executor.run((read,), scope=scope, max_workers=1)
                    if result is None:
                        item['code'] = 'document_timeout'
                        skipped = 'documents_timeout'
                        continue
                    payload, error = result
                    if error:
                        if error in ('research_request_budget_exhausted', 'research_byte_budget_exhausted'):
                            budget_stopped, skipped = True, 'documents_unavailable'
                            item['code'] = 'document_not_read'
                        else:
                            item['code'] = 'document_timeout' if error == 'research_data_deadline_exceeded' else error
                        if item['code'] == 'document_timeout':
                            skipped = 'documents_timeout'
                        continue
                    parser = parse_sina_article if candidate.kind == 'news' else parse_eastmoney_announcement
                    document = parser(payload, candidate, retrieved_at=self.wall_clock())
                    cutoff = timestamp(now)
                    if (candidate.published_date > cutoff.date().isoformat()
                            or document.published_at is not None and timestamp(document.published_at) > cutoff
                            or document.updated_at is not None and timestamp(document.updated_at) > cutoff
                            or document.metadata_at is not None and timestamp(document.metadata_at) > cutoff):
                        raise DocumentError('document_time_mismatch')
                    stage.check()
                    excerpts = document.excerpts(question)
                    stage.check()
                    # Do not promote another explicitly named issuer's paragraph into this issuer's evidence.
                    excerpts = tuple(e for e in excerpts if not any(
                        company.instrument_id != candidate.instrument_id and
                        (company.name in e.text or company.instrument_id[:6] in e.text)
                        for company in bundle.companies))
                    item.update(status='available', code='document_read' if document.complete else document.incomplete_reasons[0],
                        **document.coverage_metadata(), document_id=document.id,
                        sha256=document.content_sha256, extractor_version=document.extractor_version,
                        extracted_characters=len(document.text), paragraph_count=len(document.paragraphs),
                        retrieved_at=document.retrieved_at, published_date=candidate.published_date,
                        publication_precision=parent.publication_precision,
                        excerpts=[dict(paragraph_index=e.paragraph_index, text=e.text, sha256=e.sha256) for e in excerpts])
                    no_substantive_excerpts += not excerpts
                    fallback_available += alternative
                    for excerpt in excerpts:
                        metadata = dict(document_id=document.id, parent_title_id=parent.id,
                            sha256=document.content_sha256, paragraph_index=excerpt.paragraph_index,
                            excerpt_sha256=excerpt.sha256, extractor_version=document.extractor_version,
                            document_complete=document.complete)
                        fragments.append(ProductEvidence(f'{document.id}:{excerpt.paragraph_index}:{excerpt.sha256[:16]}',
                            parent.instrument_id, parent.kind, '正文片段', excerpt.text, parent.source,
                            parent.date, parent.url, 'body_excerpt', parent.published_at, parent.publication_precision,
                            data_authenticity=parent.data_authenticity, temporal_scope=parent.temporal_scope,
                            dependencies=(parent.id,), retrieved_at=document.retrieved_at,
                            raw_value=None, unit='text', document=metadata))
                except DocumentError as error:
                    item['code'] = error.code
                except CollectionUnavailable:
                    if entered or not alternative:
                        item['code'] = 'document_timeout'
                    skipped = 'documents_timeout'
                except TurnStopped:
                    raise
                except Exception:
                    checkpoint()
                    item['code'] = 'document_invalid_response'
            checkpoint()
        finally:
            stage.close()
            if all(item['status'] == 'available' for item in items):
                skipped = None
            trace_documents(dict(documents_skipped_reason=skipped, **stage.usage(),
                elapsed_ms=round((self.clock() - started) * 1000), attempted=attempted,
                fallback_attempted=fallback_attempted, fallback_available=fallback_available,
                no_substantive_excerpts=no_substantive_excerpts,
                available=sum(i['status'] == 'available' for i in items),
                complete=sum(i.get('complete') is True for i in items),
                partial=sum(i.get('complete') is False for i in items),
                omitted_tables=sum(i.get('omitted_tables', 0) for i in items),
                private_use_characters_replaced=sum(i.get('private_use_characters_replaced', 0) for i in items),
                omitted_trailing_characters=sum(i.get('omitted_trailing_characters', 0) for i in items)))
        available = sum(i['status'] == 'available' for i in items)
        status = ('available' if available == len(items) and all(i['complete'] for i in items)
                  else 'partial' if available else 'unavailable')
        # Round-robin first excerpts before a second excerpt from the same document.
        counts, ranks = {}, {}
        for fragment in fragments:
            key = fragment.document['document_id']
            ranks[fragment.id] = counts.get(key, 0)
            counts[key] = ranks[fragment.id] + 1
        fragments.sort(key=lambda e: ranks[e.id])
        return replace(bundle, supplemental_evidence=tuple(fragments),
            documents=dict(version=POLICY_VERSION, status=status,
                code='documents_read' if status == 'available' else 'documents_' + status, items=items))


def documents_for_context(bundle, context):
    """Publish only excerpts actually offered to the answering model, never full text."""
    if bundle.documents is None:
        return None
    result = deepcopy(bundle.documents)
    used = {(r['document']['document_id'], r['document']['paragraph_index'], r['document']['excerpt_sha256'])
            for r in context.get('evidence', ())
            if r.get('content_kind') == 'body_excerpt' and r.get('document')}
    for item in result['items']:
        if item['status'] == 'available':
            # Saved summaries describe the previous answer; candidates remain
            # available for a later answer with a different context selection.
            item['excerpts'] = [dict(paragraph_index=e.document['paragraph_index'],
                text=e.display, sha256=e.document['excerpt_sha256']) for e in bundle.supplemental_evidence
                if e.document and e.document['document_id'] == item['document_id']
                and (e.document['document_id'], e.document['paragraph_index'], e.document['excerpt_sha256']) in used]
    if bundle.supplemental_evidence and not used:
        trace_documents(dict(documents_skipped_reason='documents_context_capacity'))
    return result
