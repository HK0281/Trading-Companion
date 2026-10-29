from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.evaluate_intent_router import main
from trading_companion.intent_router_evaluation import evaluate
from trading_companion.intent_router_reporting import annotation_review, format_report, write_report
from trading_companion.intent_router_task import load_corpus


class IntentReportingTests(unittest.TestCase):
    def test_review_checklist_contains_all_rows_context_reasons_and_status(self):
        rows,manifest=load_corpus()
        output=annotation_review(rows,manifest)
        self.assertEqual(sum(line.startswith('| I') and line[3:6].isdigit() for line in output.splitlines()),304)
        for row in rows:
            self.assertIn(row['id']+' / '+row['family_id'],output)
        self.assertIn('latest_valid_research_kind',output)
        for status in {item['status'] for item in manifest['reviews'].values()}:
            self.assertIn(status,output)
        self.assertIn('禁止',output)

    def test_fair_tables_have_both_scopes_and_do_not_claim_model_quality(self):
        report=evaluate(call_limit=1)
        output=format_report(report)
        self.assertIn('全部准确率',output)
        self.assertIn('非反问题macro-F1',output)
        self.assertIn('gold=clarify',output)
        self.assertIn('未判定',output)
        self.assertIn('开发集仅用于选阈值',output)
        self.assertIn('不是模型准确率',output)
        report['rows'][0]['question']='<script>alert(1)</script>|```'
        self.assertNotIn('<script>',format_report(report))
        self.assertNotIn('alert(1)</script>',format_report(report))

    def test_artifacts_roundtrip_without_overwriting(self):
        report=evaluate(call_limit=1)
        with tempfile.TemporaryDirectory() as root:
            paths=write_report(report,root)
            snapshot={k:Path(p).read_bytes() for k,p in paths.items()}
            second=write_report(report,root)
            self.assertNotEqual(paths,second)
            self.assertEqual(snapshot,{k:Path(p).read_bytes() for k,p in paths.items()})
            parsed=json.loads(Path(paths['json']).read_text(encoding='utf-8'))
            self.assertEqual(parsed['plan']['split'],'dev')
            self.assertEqual(parsed['artifacts'],paths)

    def test_cli_annotations_and_export_action_flags_are_exclusive(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['--annotation-review']),0)
        self.assertEqual(sum(line.startswith('| I') and line[3:6].isdigit() for line in output.getvalue().splitlines()),304)
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            main(['--plan-only','--annotation-review'])
