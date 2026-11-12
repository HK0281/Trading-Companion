"""Local replay diagnostics use generated samples and never depend on ignored live files."""

from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import replay_research_documents
from scripts.replay_research_documents import main
from tests.test_company_announcement_body import detail_payload
from tests.test_company_news_body import article_html
from tests.test_research_documents import NOW, announcement_candidate, news_candidate


class LocalDocumentReplayTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch('socket.socket.connect', side_effect=AssertionError('No network')),
                      patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')),
                      patch('requests.request', side_effect=AssertionError('No HTTP')),
                      patch('trading_companion.local_env._read_local_values', side_effect=AssertionError('No .env'))):
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def run_cli(path, *options):
        output = StringIO()
        with redirect_stdout(output):
            code = main(['--samples', str(path), *options])
        return code, [json.loads(line) for line in output.getvalue().splitlines()], output.getvalue()

    @staticmethod
    def row(candidate, filename, **options):
        values = asdict(candidate)
        values.update(file=filename, evidence_id=values.pop('parent_id'), publication_precision='datetime',
                      retrieved_at='1900-01-01T00:00:00Z', **options)
        return values

    @staticmethod
    def write_samples(root, rows, payloads):
        (root / 'candidates.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
        for filename, body in payloads.items():
            path = root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            stamp = datetime.fromisoformat(NOW).timestamp()
            os.utime(path, (stamp, stamp))

    def test_absent_optional_samples_exit_successfully_without_loading_a_decoder(self):
        with TemporaryDirectory() as temporary, \
                patch('trading_companion.document_collection.decode_document_payload', side_effect=AssertionError('No samples')):
            missing = Path(temporary) / 'not-installed'
            code, rows, _ = self.run_cli(missing)
            self.assertEqual(code, 0)
            self.assertEqual(rows, [dict(status='skipped', code='replay_samples_missing', historical_acceptance=False)])
            code, rows, _ = self.run_cli(Path(temporary))
            self.assertEqual(code, 0)
            self.assertEqual(rows[0]['code'], 'replay_manifest_missing')

    def test_production_decode_parse_and_fixed_failure_codes_never_print_source_text(self):
        from trading_companion.document_collection import decode_document_payload
        marker = 'PRIVATE_REPLAY_BODY_SENTINEL'
        news = self.row(news_candidate(), 'news/good.html', headers={'Content-Type': 'text/html; charset=utf-8'})
        ann = self.row(announcement_candidate(), 'ann/good.json', content_type='application/json')
        broken = self.row(news_candidate(), 'news/layout.html')
        invalid = self.row(news_candidate(), 'news/encoding.html', content_type='text/html; charset=utf-8')
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_samples(root, [news, ann, broken, invalid], {
                news['file']: article_html(paragraphs=('平安银行000001' + marker,)).encode('utf-8'),
                ann['file']: json.dumps(detail_payload(), ensure_ascii=False).encode('utf-8'),
                broken['file']: b'<html><body>PRIVATE_LAYOUT_SENTINEL</body></html>',
                invalid['file']: b'\xffPRIVATE_DECODE_SENTINEL',
            })
            with patch('trading_companion.document_collection.decode_document_payload', wraps=decode_document_payload) as decode:
                code, rows, output = self.run_cli(root / 'candidates.json')
            self.assertEqual(decode.call_count, 4)
        self.assertEqual(code, 0)
        self.assertEqual([row['code'] for row in rows[:-1]],
                         ['document_read', 'document_read', 'document_layout_invalid', 'document_invalid_response'])
        self.assertTrue(all(row['time_basis'] == 'file_mtime' for row in rows[:-1]))
        self.assertTrue(all(row['historical_acceptance'] is False for row in rows))
        self.assertGreater(rows[0]['characters'], 0)
        self.assertGreater(rows[0]['excerpts'], 0)
        self.assertEqual(rows[2]['characters'], 0)
        self.assertEqual(rows[-1]['available'], 2)
        self.assertEqual(rows[-1]['complete'], 2)
        self.assertEqual(rows[-1]['partial'], 0)
        for row in rows[:2]:
            self.assertIs(row['complete'], True)
            self.assertEqual((row['omitted_tables'], row['page_count'], row['private_use_characters_replaced'],
                              row['omitted_trailing_characters']), (0, 1, 0, 0))
        self.assertNotIn('complete', rows[2])
        for private in (marker, 'PRIVATE_LAYOUT_SENTINEL', 'PRIVATE_DECODE_SENTINEL', news['title']):
            self.assertNotIn(private, output)

    def test_partial_news_and_announcement_report_coverage_without_becoming_complete(self):
        news = self.row(news_candidate(), 'news/partial.html')
        ann = self.row(announcement_candidate(), 'ann/partial.json')
        broken = self.row(news_candidate(), 'news/broken.html')
        news_body = article_html().replace('</div><!-- 正文 end -->',
            '<table><tr><td>PRIVATE_TABLE_SENTINEL12345万元</td></tr></table>'
            '<p>PRIVATE_UNFINISHED_NEWS_SENTINEL12345</p></div><!-- 正文 end -->')
        ann_body = detail_payload(page_size=2,
            notice_content='平安银行人工\ue001公告正文。\nPRIVATE_UNFINISHED_ANN_SENTINEL12345')
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_samples(root, [news, ann, broken], {
                news['file']: news_body.encode('utf-8'),
                ann['file']: json.dumps(ann_body, ensure_ascii=False).encode('utf-8'),
                broken['file']: b'<html><body>PRIVATE_LAYOUT_SENTINEL</body></html>',
            })
            code, rows, output = self.run_cli(root)
        self.assertEqual(code, 0)
        self.assertEqual([row['code'] for row in rows[:-1]],
                         ['document_tables_omitted', 'document_first_page_only', 'document_layout_invalid'])
        for row in rows[:2]:
            self.assertEqual(row['status'], 'available')
            self.assertIs(row['complete'], False)
            self.assertGreater(row['characters'], 0)
            self.assertGreater(row['excerpts'], 0)
            self.assertGreater(row['omitted_trailing_characters'], 0)
        self.assertEqual((rows[0]['omitted_tables'], rows[0]['page_count'], rows[0]['private_use_characters_replaced']),
                         (1, 1, 0))
        self.assertEqual((rows[1]['omitted_tables'], rows[1]['page_count'], rows[1]['private_use_characters_replaced']),
                         (0, 2, 1))
        self.assertNotIn('complete', rows[2])
        self.assertEqual((rows[-1]['available'], rows[-1]['complete'], rows[-1]['partial'], rows[-1]['unavailable']),
                         (2, 0, 2, 1))
        for private in ('PRIVATE_TABLE_SENTINEL', 'PRIVATE_UNFINISHED_NEWS_SENTINEL',
                        'PRIVATE_UNFINISHED_ANN_SENTINEL', 'PRIVATE_LAYOUT_SENTINEL', news['title'], ann['title']):
            self.assertNotIn(private, output)

    def test_explicit_observation_time_overrides_file_mtime_and_does_not_certify_history(self):
        row = self.row(news_candidate(), 'news.html')
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_samples(root, [row], {'news.html': article_html().encode('utf-8')})
            code, rows, _ = self.run_cli(root, '--retrieved-at', '2020-01-01T00:00:00Z')
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]['time_basis'], 'cli_override')
        self.assertEqual(rows[0]['code'], 'document_time_mismatch')
        self.assertFalse(rows[0]['historical_acceptance'])

    def test_manifest_cannot_select_parent_absolute_windows_or_configuration_paths(self):
        bad_paths = ('../outside.html', '/outside.html', 'C:\\outside.html', '\\\\server\\outside.html', '.env')
        rows = [self.row(news_candidate(), value) for value in bad_paths]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_samples(root, rows, {})
            with patch('scripts.replay_research_documents._read_bytes', wraps=replay_research_documents._read_bytes) as read:
                code, results, _ = self.run_cli(root)
            self.assertEqual(read.call_count, 1, 'Only the manifest may be read')
            configuration = root / '.env'
            configuration.write_text('PRIVATE_CONFIG_SENTINEL', encoding='utf-8')
            with patch('scripts.replay_research_documents._read_bytes', side_effect=AssertionError('No configuration reads')):
                blocked, _, output = self.run_cli(configuration)
            self.assertEqual(blocked, 1)
            self.assertNotIn('PRIVATE_CONFIG_SENTINEL', output)
        self.assertEqual(code, 0)
        self.assertTrue(all(row['code'] == 'replay_path_blocked' for row in results[:-1]))

    def test_invalid_manifest_has_a_fixed_failure_without_echoing_contents(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'candidates.json').write_text('{"PRIVATE_MANIFEST_SENTINEL":', encoding='utf-8')
            code, rows, output = self.run_cli(root)
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]['code'], 'replay_invalid_manifest')
        self.assertNotIn('PRIVATE_MANIFEST_SENTINEL', output)


if __name__ == '__main__':
    unittest.main()
