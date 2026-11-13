"""Document trace summaries retain fixed skip reasons and counts, never source text."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_companion.turn_trace import JsonlTraceSink, trace_documents, trace_message
from trading_companion.trace_cli import parse_record


class DocumentTraceTests(unittest.TestCase):
    def test_fallback_attempt_counts_are_bounded_and_do_not_export_source_details(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            counts = dict(requests=4, attempted=4, available=3, complete=2, partial=1,
                fallback_attempted=1, fallback_available=1, no_substantive_excerpts=1)
            with trace_message(JsonlTraceSink(root), mode='normal', provider='fake', history=False):
                trace_documents({**counts, 'title': 'private chosen title', 'parent_title_id': 'private parent'})
                trace_documents(dict(documents_skipped_reason='documents_context_capacity'))
            record = json.loads(next(root.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
            summary = record['research_documents']
            self.assertEqual({key: summary[key] for key in counts}, counts)
            self.assertNotIn('private', json.dumps(record))
            parse_record(json.dumps(record).encode())

    def test_invalid_or_inconsistent_document_counters_do_not_replace_valid_counts(self):
        counts = dict(requests=4, attempted=4, available=2, complete=2, partial=0,
            fallback_attempted=2, fallback_available=2, no_substantive_excerpts=0)
        for invalid in (dict(requests=5), dict(attempted=True), dict(fallback_attempted=-1),
                        dict(fallback_attempted=3), dict(fallback_available=3),
                        dict(no_substantive_excerpts=3), dict(complete=1), dict(response_bytes=2048001),
                        dict(fallback_available='private counter')):
            with self.subTest(invalid=invalid), TemporaryDirectory() as temporary:
                root = Path(temporary)
                with trace_message(JsonlTraceSink(root), mode='normal', provider='fake', history=False):
                    trace_documents(counts)
                    trace_documents(invalid)
                record = json.loads(next(root.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
                summary = record['research_documents']
                self.assertEqual({key: summary[key] for key in counts}, counts)
                self.assertNotIn('private', json.dumps(record))
                parse_record(json.dumps(record).encode())

    def test_complete_and_partial_counts_survive_without_source_details(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with trace_message(JsonlTraceSink(root), mode='normal', provider='fake', history=False):
                trace_documents(dict(available=2, complete=1, partial=1, omitted_tables=2,
                    private_use_characters_replaced=1, omitted_trailing_characters=13,
                    text='private body', url='https://private.invalid/', sha256='private digest'))
                trace_documents(dict(documents_skipped_reason='documents_context_capacity'))
            record = json.loads(next(root.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
            summary = record['research_documents']
            self.assertEqual((summary['available'], summary['complete'], summary['partial']), (2, 1, 1))
            self.assertEqual(summary['omitted_tables'], 2)
            self.assertEqual(summary['private_use_characters_replaced'], 1)
            self.assertEqual(summary['omitted_trailing_characters'], 13)
            for secret in ('private body', 'https://private.invalid/', 'private digest'):
                self.assertNotIn(secret, json.dumps(record))
            parse_record(json.dumps(record).encode())

    def test_fixed_timeout_reason_and_counts_survive_context_capacity_update(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with trace_message(JsonlTraceSink(root), mode='normal', provider='fake', history=False):
                trace_documents(dict(documents_skipped_reason='documents_timeout', requests=3,
                    response_bytes=1024, elapsed_ms=12000, available=0, attempted=3,
                    text='private body text', url='https://private.invalid/'))
            records = [json.loads(line) for path in root.glob('*.jsonl')
                       for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record['research_documents'], dict(version='research-documents-v1',
                documents_skipped_reason='documents_timeout', requests=3, response_bytes=1024,
                elapsed_ms=12000, available=0, attempted=3))
            self.assertNotIn('private', json.dumps(record))
            parse_record(json.dumps(record).encode())

    def test_disabled_documents_do_not_add_trace_fields_and_untrusted_reason_is_discarded(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with trace_message(JsonlTraceSink(root), mode='normal', provider='fake', history=False):
                pass
            record = json.loads(next(root.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
            self.assertNotIn('research_documents', record)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with trace_message(JsonlTraceSink(root), mode='normal', provider='fake', history=False):
                trace_documents(dict(documents_skipped_reason='private source failure'))
            record = json.loads(next(root.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
            self.assertIsNone(record['research_documents']['documents_skipped_reason'])
            self.assertNotIn('private', json.dumps(record))
