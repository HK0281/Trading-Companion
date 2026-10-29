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
            self.assertNotIn("PRIVATE", output.getvalue())

    def test_no_trace_overrides_trace_dir_and_help_documents_switch(self):
        from trading_companion.api_server import main as server
        with TemporaryDirectory() as temporary, patch("uvicorn.run"), redirect_stdout(StringIO()):
            path = Path(temporary) / "disabled"
            self.assertEqual(server(["--demo", "--trace-dir", str(path), "--no-trace"]), 0)
            self.assertFalse(path.exists())
