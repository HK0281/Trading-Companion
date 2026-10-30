"""Answer diagnostics remain metadata through storage, parsing and run lookup."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.trace_cli import main
from trading_companion.turn_trace import JsonlTraceSink, trace_message, trace_answer_validation
from tests.test_turn_trace import read_records


class NarrativeTraceTests(unittest.TestCase):
    def test_storage_and_cli_filter_arbitrary_text_and_keep_locations(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary)
            with trace_message(JsonlTraceSink(path), mode="demo", provider="fake", history=False):
                trace_answer_validation({"state": "partial", "code": "answer_partial", "generated_sentences": 9,
                    "deleted_sentences": 1, "repair_attempts": 1, "structure_failures": 1,
                    "local_structure_recoveries": 1, "partial_structure_recoveries": 1,
                    "repair_all_passed": False, "repair_rating_restored": True, "repair_content_added": True,
                    "question": "PRIVATE-QUESTION", "answer": "PRIVATE-ANSWER",
                    "reasons": [{"code": "answer_numeric_unmatched", "section": "current", "index": 2,
                                 "text": "PRIVATE-REJECTED"}, {"code": "PRIVATE-CODE", "section": "PRIVATE-SECTION", "index": -1}]})
            record = read_records(path)[0]
            self.assertNotIn("PRIVATE", json.dumps(record))
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--trace-dir", str(path), "--run-id", record["run_id"], "--format", "json"])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            meta = report["answer_validation"][0]
            self.assertEqual(meta["reasons"][0], {"code": "answer_numeric_unmatched", "section": "current", "index": 2})
            self.assertEqual(meta["structure_failures"], 1)
            self.assertEqual(meta["local_structure_recoveries"], 1)
            self.assertTrue(meta["repair_rating_restored"])
            self.assertFalse(meta["repair_all_passed"])
            self.assertNotIn("PRIVATE", output.getvalue())

    def test_recovery_metadata_only_accepts_bounded_numbers_and_booleans(self):
        from trading_companion.turn_trace import safe_answer_validation
        value=safe_answer_validation(dict(local_structure_recoveries=10001,partial_structure_recoveries=True,
            repair_all_passed='PRIVATE',repair_rating_restored=1,repair_content_added=False,model_draft='PRIVATE'))
        self.assertEqual(value['local_structure_recoveries'],10000)
        self.assertNotIn('partial_structure_recoveries',value)
        self.assertNotIn('repair_all_passed',value)
        self.assertNotIn('repair_rating_restored',value)
        self.assertFalse(value['repair_content_added'])
        self.assertNotIn('PRIVATE',json.dumps(value))

    def test_numeric_metadata_is_bounded_and_keeps_no_bindings_or_raw_context(self):
        from trading_companion.turn_trace import safe_answer_validation
        value = safe_answer_validation(dict(numeric_validation_version='source-bindings-v1', numeric_tokens=10001,
            bound_numbers=True, unbound_matched=3, numbers=[{'text':'PRIVATE'}], context='PRIVATE'))
        self.assertEqual(value['numeric_validation_version'],'source-bindings-v1')
        self.assertEqual(value['numeric_tokens'],10000)
        self.assertEqual(value['unbound_matched'],3)
        self.assertNotIn('bound_numbers',value)
        self.assertNotIn('PRIVATE',json.dumps(value))

    def test_no_trace_overrides_trace_dir_and_help_documents_switch(self):
        from trading_companion.api_server import main as server
        with TemporaryDirectory() as temporary, patch("uvicorn.run"), redirect_stdout(StringIO()):
            path = Path(temporary) / "disabled"
            self.assertEqual(server(["--demo", "--trace-dir", str(path), "--no-trace"]), 0)
            self.assertFalse(path.exists())

    def test_issue_and_deduplication_counts_are_versioned_bounded_metadata(self):
        from trading_companion.turn_trace import safe_answer_validation
        value = safe_answer_validation(dict(issue_count_version='answer-issues-v1',
            draft_issue_counts=[{'answer_stance_mismatch': 2, 'answer_numeric_unmatched': 3}, {}],
            deduplicated_drivers=1, deduplicated_sentences=2))
        self.assertEqual(value['draft_issue_counts'][0]['answer_stance_mismatch'], 2)
        self.assertEqual(value['deduplicated_drivers'], 1)
        for drafts in ([{'PRIVATE': 1}], [{'answer_numeric_unmatched': True}],
                       [{'answer_numeric_unmatched': -1}], [{}] * 3):
            value = safe_answer_validation(dict(issue_count_version='answer-issues-v1', draft_issue_counts=drafts))
            self.assertNotIn('draft_issue_counts', value)
            self.assertNotIn('PRIVATE', json.dumps(value))
