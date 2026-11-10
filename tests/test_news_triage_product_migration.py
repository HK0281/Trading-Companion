from contextlib import redirect_stdout
from copy import deepcopy
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading_companion.news_triage_product import descriptor, digest
from trading_companion.news_triage_product_annotations import CONTEXT, FIELDS, _complete, _read_csv, write_blind_sheets
from trading_companion.news_triage_product_data import load_dataset, write_json
from trading_companion.news_triage_product_migration import legacy_descriptor, migrate
from tests.test_news_triage_product_data import dataset_for, fill


class RuleMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.dataset = dataset_for()
        self.dataset["task"] = legacy_descriptor()
        self.dataset_path = self.source / "dataset.json"
        self.save_dataset()
        write_blind_sheets(self.dataset, self.source)
        self.sheet = self.source / "codex.csv"
        fill(self.sheet)

    def save_dataset(self):
        self.dataset["dataset_sha256"] = digest({k: v for k, v in self.dataset.items() if k != "dataset_sha256"})
        self.dataset_path.write_text(json.dumps(self.dataset, ensure_ascii=False), encoding="utf-8")

    def save_rows(self, rows):
        with self.sheet.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_two_independent_sheets_keep_different_answers_but_identical_target_datasets(self):
        other = self.source / "claude.csv"
        fill(other, labels={"relevance": "primary", "tone": "negative", "importance": "unknown"})
        old_dataset_bytes = self.dataset_path.read_bytes()
        original = {p.name: p.read_bytes() for p in (self.sheet, other)}
        outputs = []
        for sheet in (self.sheet, other):
            result = migrate(self.dataset_path, sheet, self.output, write=True)
            target = Path(result["directory"])
            outputs.append(target)
            self.assertEqual((target / sheet.name).read_bytes(), original[sheet.name])
            self.assertFalse((target / ("claude.csv" if sheet == self.sheet else "codex.csv")).exists())
            new = load_dataset(target / "dataset.json")
            self.assertEqual(new["task"], descriptor())
            for key in self.dataset.keys() - {"task", "dataset_sha256"}:
                self.assertEqual(new[key], self.dataset[key])
            manifest = json.loads((target / "migration.json").read_text("utf-8"))
            self.assertEqual(manifest["source_sheet_sha256"], sha256(original[sheet.name]).hexdigest())
            self.assertEqual(manifest["source_sheet_sha256"], manifest["sheet_sha256"])
            self.assertEqual(manifest["answers_changed"], 0)
            self.assertEqual(manifest["review_flags_changed"], 0)
            task = json.loads((target / "task.json").read_text("utf-8"))
            self.assertEqual(task["task"], new["task"])
            self.assertEqual(task["instructions"], task["questions"]["tone"]["instructions"])
        self.assertEqual((outputs[0] / "dataset.json").read_bytes(), (outputs[1] / "dataset.json").read_bytes())
        self.assertNotEqual(original["codex.csv"], original["claude.csv"])
        self.assertEqual(self.dataset_path.read_bytes(), old_dataset_bytes)
        self.assertTrue(all((self.source / name).read_bytes() == raw for name, raw in original.items()))
        with self.assertRaises(ValueError):
            load_dataset(self.dataset_path)

    def test_preview_and_cli_do_not_write_or_print_answers(self):
        from scripts.migrate_news_triage_product import main
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--dataset", str(self.dataset_path), "--sheet", str(self.sheet),
                         "--output-root", str(self.output)])
        self.assertEqual(code, 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["output_written"])
        self.assertEqual(result["model_calls"], 0)
        self.assertNotIn("relevance", stream.getvalue())
        self.assertNotIn("测试夹具依据", stream.getvalue())
        self.assertFalse(self.output.exists())

    def test_blank_answers_abstentions_and_review_flags_are_preserved(self):
        rows = list(_read_csv(self.sheet, FIELDS).values())
        rows[0].update(tone="unresolved", reviewed="", grouping_reviewed="")
        rows[1].update(importance="", importance_rationale="", grouping_reviewed="")
        self.save_rows(rows)
        original = self.sheet.read_bytes()
        result = migrate(self.dataset_path, self.sheet, self.output, write=True)
        destination = Path(result["directory"]) / "codex.csv"
        self.assertEqual(destination.read_bytes(), original)
        self.assertTrue(all(not _complete(r) for r in _read_csv(destination, FIELDS).values()))

    def test_every_context_column_is_checked_before_any_output(self):
        original = list(_read_csv(self.sheet, FIELDS).values())
        for field in CONTEXT:
            with self.subTest(field=field):
                rows = deepcopy(original)
                rows[0][field] += "changed"
                self.save_rows(rows)
                with self.assertRaises(ValueError):
                    migrate(self.dataset_path, self.sheet, self.output, write=True)
                self.assertFalse(self.output.exists())

    def test_invalid_rows_labels_and_review_values_are_rejected(self):
        original = list(_read_csv(self.sheet, FIELDS).values())
        for change in ("duplicate", "missing", "extra", "label", "tone", "review", "rationale"):
            rows = deepcopy(original)
            if change == "duplicate": rows.append(deepcopy(rows[0]))
            if change == "missing": rows.pop()
            if change == "extra": rows.append({**rows[0], "id": "not-in-dataset"})
            if change == "label": rows[0]["importance"] = "invalid"
            if change == "tone": rows[0].update(relevance="unrelated", tone="positive")
            if change == "review": rows[0]["reviewed"] = "approved"
            if change == "rationale": rows[0]["tone_rationale"] = "line\nbreak"
            with self.subTest(change=change):
                self.save_rows(rows)
                with self.assertRaises(ValueError):
                    migrate(self.dataset_path, self.sheet, self.output, write=True)
                self.assertFalse(self.output.exists())

    def test_legacy_version_integrity_and_company_isolation_are_not_bypassed(self):
        original = deepcopy(self.dataset)
        for change in ("version", "source_hash", "company_leakage", "split", "checksum"):
            self.dataset = deepcopy(original)
            if change == "version": self.dataset["task"]["rule_version"] = "unknown-version"
            if change == "source_hash": self.dataset["task"]["source_digest"] = "0" * 64
            if change == "company_leakage": self.dataset["summary"]["company_split"]["sealed"].append("600000.SH")
            if change == "split": self.dataset["rows"][1]["split"] = "debug"
            self.save_dataset()
            if change == "checksum":
                self.dataset_path.write_text(self.dataset_path.read_text("utf-8").replace('"natural"', '"hard"'), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(ValueError):
                migrate(self.dataset_path, self.sheet, self.output, write=True)
            self.assertFalse(self.output.exists())

    def test_legacy_registration_cannot_be_substituted(self):
        fake = self.root / "legacy.json"
        write_json(fake, {**legacy_descriptor(), "definition_sha256": "0" * 64})
        with patch("trading_companion.news_triage_product_migration.LEGACY_PATH", fake), self.assertRaises(ValueError):
            migrate(self.dataset_path, self.sheet, self.output, write=True)
        self.assertFalse(self.output.exists())

    def test_current_dataset_is_not_a_legacy_input(self):
        self.dataset["task"] = descriptor()
        self.save_dataset()
        with self.assertRaises(ValueError):
            migrate(self.dataset_path, self.sheet, self.output, write=True)
        self.assertFalse(self.output.exists())

    def test_existing_directory_is_never_overwritten(self):
        original = self.sheet.read_bytes(), self.dataset_path.read_bytes()
        with patch("trading_companion.news_triage_product_migration.uuid4", return_value=SimpleNamespace(hex="source")):
            with self.assertRaises(FileExistsError):
                migrate(self.dataset_path, self.sheet, self.root, write=True)
        self.assertEqual(original, (self.sheet.read_bytes(), self.dataset_path.read_bytes()))

    def test_copy_uses_validated_snapshot_when_source_changes(self):
        from trading_companion.news_triage_product_migration import _sheet_snapshot
        original = self.sheet.read_bytes()
        def edit_after_snapshot(path):
            snapshot = _sheet_snapshot(path)
            self.sheet.write_bytes(b"changed after reading")
            return snapshot
        with patch("trading_companion.news_triage_product_migration._sheet_snapshot", side_effect=edit_after_snapshot):
            result = migrate(self.dataset_path, self.sheet, self.output, write=True)
        self.assertEqual((Path(result["directory"]) / "codex.csv").read_bytes(), original)

    def test_oversize_or_wrong_sheet_name_is_rejected(self):
        from trading_companion.news_triage_product_migration import MAX_SHEET_BYTES
        wrong_name = self.root / "adjudication.csv"
        wrong_name.write_bytes(self.sheet.read_bytes())
        with self.assertRaises(ValueError):
            migrate(self.dataset_path, wrong_name, self.output, write=True)
        self.sheet.write_bytes(b"x" * (MAX_SHEET_BYTES + 1))
        with self.assertRaises(ValueError):
            migrate(self.dataset_path, self.sheet, self.output, write=True)
        self.assertFalse(self.output.exists())
