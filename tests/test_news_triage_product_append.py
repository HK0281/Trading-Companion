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
from trading_companion.news_triage_product_annotations import (
    FIELDS, load_annotations, merge, write_blind_sheets, write_csv,
)
from trading_companion.news_triage_product_append import append_split, build_append_dataset
from trading_companion.news_triage_product_data import load_dataset, prepare, write_json
from trading_companion.news_triage_product_migration import migrate_finalized
from trading_companion.news_triage_product_parent import load_parent_pair, parent_descriptor, v4_descriptor
from tests.test_news_triage_product import DIRECTORY, sample
from tests.test_news_triage_product_data import dataset_for, fill


class AppendTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent = dataset_for()
        self.parent["task"] = parent_descriptor()
        self.parent["dataset_sha256"] = digest({k: v for k, v in self.parent.items() if k != "dataset_sha256"})
        self.parent_path = self.root/"base.json"
        write_json(self.parent_path, self.parent)
        write_blind_sheets(self.parent, self.root)
        for name in ("codex", "claude"):
            fill(self.root/(name+".csv"))
        self.annotations = merge(self.parent, self.root/"codex.csv", self.root/"claude.csv")
        self.annotations["rows"][1]["review_source"] = "user_adjudication"
        self.annotations["rows"][1]["rationales"] = {q: ["测试用户裁定依据"] for q in ("relevance", "tone", "importance")}
        self.annotations["annotations_sha256"] = digest({k: v for k, v in self.annotations.items() if k != "annotations_sha256"})
        self.annotation_path = self.root/"base-annotations.json"
        write_json(self.annotation_path, self.annotations)
        self.directory = self.root/"directory.json"
        self.directory.write_text('{"is_fixture":false}', encoding="utf-8")
        self.parent_snapshot = self.parent_path.read_bytes(), self.annotation_path.read_bytes()

    def build(self, rows):
        directory = SimpleNamespace(securities=DIRECTORY.securities, is_fixture=False)
        with patch("trading_companion.news_triage_product_append.load_security_directory", return_value=directory), \
                patch("trading_companion.news_triage_product_append.load_captures", return_value=(rows, [])):
            return build_append_dataset("unused", self.directory, self.parent_path, self.annotation_path)

    def appended(self):
        return self.build([sample("浦发银行新资料甲", day="2026-11-10"),
                           sample("招商银行新资料乙", issuer="600036.SH", day="2026-11-10")])

    def test_current_loader_stays_strict_but_registered_v3_finalized_pair_is_accepted(self):
        with self.assertRaises(ValueError):
            load_dataset(self.parent_path)
        parent, annotations = load_parent_pair(self.parent_path, self.annotation_path)
        self.assertEqual(parent, self.parent)
        self.assertEqual(annotations, self.annotations)
        self.assertEqual(parent_descriptor()["definition_sha256"], descriptor()["definition_sha256"])

    def test_append_preserves_every_parent_row_group_answer_and_split(self):
        dataset = self.appended()
        self.assertEqual(dataset["rows"][:2], self.parent["rows"])
        self.assertEqual(dataset["groups"][:2], self.parent["groups"])
        self.assertEqual(len(dataset["append"]["added_ids"]), 2)
        self.assertEqual(dataset["summary"]["splits"]["debug"]["natural"]["selected"], 2)
        self.assertTrue(dataset["summary"]["balance"]["passed"])
        path = self.root/"appended.json"
        write_json(path, dataset)
        self.assertEqual(load_dataset(path), dataset)
        target = self.root/"new"
        target.mkdir()
        write_blind_sheets(dataset, target)
        paths = [target/(name+".csv") for name in ("codex", "claude")]
        with paths[0].open(encoding="utf-8-sig", newline="") as f:
            sheet = list(csv.DictReader(f))
        self.assertEqual({r["id"] for r in sheet}, set(dataset["append"]["added_ids"]))
        self.assertTrue(all(not r[q] for r in sheet for q in ("relevance", "tone", "importance", "reviewed")))
        self.assertEqual(paths[0].read_bytes(), paths[1].read_bytes())
        pending = merge(dataset, *paths, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        self.assertEqual(pending["rows"][:2], self.annotations["rows"])
        self.assertEqual(len(pending["pending"]), 2)
        fill(paths[0])
        one_review = merge(dataset, *paths, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        self.assertEqual(one_review["summary"], {"reviewed": 2, "incomplete": 2})
        for p in paths:
            fill(p, labels={"relevance": "mentioned", "tone": "neutral", "importance": "ordinary"})
        completed = merge(dataset, *paths, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        self.assertEqual(completed["rows"][:2], self.annotations["rows"])
        report = self.root/"completed.json"
        write_json(report, completed)
        self.assertEqual(load_annotations(report, dataset), completed)
        self.assertEqual(self.parent_snapshot, (self.parent_path.read_bytes(), self.annotation_path.read_bytes()))

    def test_natural_cap_expands_even_when_old_quota_is_full_and_hard_uses_remaining_candidates(self):
        parent = deepcopy(self.parent)
        # No gold is needed by the partition search.
        parent["rows"] = [{**parent["rows"][i % 2], "id": "old"+str(i), "stratum": "natural"} for i in range(260)]
        rows = [{**sample(), "id": "new"+str(i), "group_id": "newg"+str(i), "normalized_title": str(i),
                 "instrument_id": "600000.SH" if i < 155 else "600036.SH", "hard_reasons": ["mixed_or_uncertain"]}
                for i in range(310)]
        groups = [{"id": r["group_id"], "members": [r]} for r in rows]
        added, report = append_split(rows, groups, {}, parent)
        for side in ("debug", "sealed"):
            self.assertEqual(report["splits"][side]["natural"]["selected"], 280)
            self.assertEqual(report["splits"][side]["hard"]["added"], 5)
        self.assertEqual(len(added), 310)
        self.assertTrue(report["balance"]["passed"])

    def test_exact_and_same_event_duplicates_do_not_become_new_questions(self):
        old = deepcopy(self.parent["rows"][0])
        variant = sample("浦发银行:季度业绩公告", source="eastmoney_announcements")
        result = self.build([old, variant])
        self.assertFalse(result["append"]["added_ids"])
        self.assertEqual(result["summary"]["exact_old_duplicates_skipped"], 1)
        self.assertTrue(result["append"]["parent_links"])
        output = self.root/"must-not-exist"
        with patch("trading_companion.news_triage_product_append.build_append_dataset", return_value=result):
            prepared = prepare("unused", self.directory, output, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        self.assertEqual(prepared["status"], "no_new_rows")
        self.assertFalse(output.exists())

    def test_new_companies_only_are_movable_and_search_is_deterministic(self):
        row = sample("招商证券新材料", issuer="600999.SH", day="2026-11-10")
        a, b = self.build([row]), self.build([row])
        self.assertEqual(a, b)
        for side in ("debug", "sealed"):
            self.assertTrue(set(self.parent["summary"]["company_split"][side]) <= set(a["summary"]["company_split"][side]))
        self.assertEqual(len(a["append"]["added_ids"]), 1)

    def test_indirect_shared_event_conflict_excludes_whole_new_events_without_moving_old(self):
        rows = [{**sample(), "id": "a", "group_id": "one", "hard_reasons": []},
                {**sample(issuer="600999.SH"), "id": "b", "group_id": "two", "hard_reasons": []},
                {**sample(), "id": "c", "group_id": "independent", "hard_reasons": []}]
        groups = [{"id": r["group_id"], "members": [r]} for r in rows]
        added, report = append_split(rows, groups, {"one": {"600999.SH"}, "two": {"600036.SH"}}, self.parent)
        self.assertEqual(report["excluded_groups"], ["one", "two"])
        self.assertEqual([r["id"] for r in added], ["c"])
        self.assertIn("600000.SH", report["company_split"]["debug"])
        self.assertIn("600036.SH", report["company_split"]["sealed"])

    def test_imbalance_is_reported_without_writing_or_shifting_old_companies(self):
        bad = self.build([sample("浦发银行新资料甲", day="2026-11-10", source="eastmoney_announcements"),
                          sample("招商银行新资料乙", issuer="600036.SH", day="2026-11-10")])
        self.assertFalse(bad["summary"]["balance"]["passed"])
        target = self.root/"must-not-exist"
        with patch("trading_companion.news_triage_product_append.build_append_dataset", return_value=bad):
            result = prepare("unused", self.directory, target, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        self.assertEqual(result["status"], "imbalanced")
        self.assertFalse(target.exists())

    def test_merge_rejects_changed_parent_context_binding_and_mixed_old_sheet_ids(self):
        dataset = self.appended()
        target = self.root/"new"
        target.mkdir()
        write_blind_sheets(dataset, target)
        paths = [target/(n+".csv") for n in ("codex", "claude")]
        with self.assertRaises(ValueError):
            merge(dataset, *paths)
        for field in ("title", "split", "group_id", "identity"):
            changed = deepcopy(dataset)
            changed["rows"][0][field] = "changed"
            with self.subTest(field=field), self.assertRaises(ValueError):
                merge(changed, *paths, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        for key in ("base_dataset_sha256", "base_annotations_sha256", "frozen_annotation_rows_sha256"):
            changed = deepcopy(dataset)
            changed["append"][key] = "0"*64
            with self.subTest(key=key), self.assertRaises(ValueError):
                merge(changed, *paths, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        with self.assertRaises(ValueError):
            merge(dataset, self.root/"codex.csv", paths[1], base_dataset=self.parent_path, base_annotations=self.annotation_path)

    def test_frozen_answers_cannot_be_rewritten_even_with_recomputed_report_hash(self):
        dataset = self.appended()
        target = self.root/"new"
        target.mkdir()
        write_blind_sheets(dataset, target)
        report = merge(dataset, target/"codex.csv", target/"claude.csv",
                       base_dataset=self.parent_path, base_annotations=self.annotation_path)
        report["rows"][0]["gold"]["importance"] = "ordinary"
        report["annotations_sha256"] = digest({k: v for k, v in report.items() if k != "annotations_sha256"})
        path = self.root/"changed.json"
        write_json(path, report)
        with self.assertRaisesRegex(ValueError, "Frozen parent answers"):
            load_annotations(path, dataset)

    def test_finalized_rebinding_preview_and_write_preserve_all_answers_and_source_files(self):
        target = self.root/"migration"
        preview = migrate_finalized(self.parent_path, self.annotation_path, target)
        self.assertEqual(preview["status"], "planned")
        self.assertFalse(target.exists())
        result = migrate_finalized(self.parent_path, self.annotation_path, target, write=True)
        new = load_dataset(Path(result["directory"])/"dataset.json")
        annotations = load_annotations(Path(result["directory"])/"annotations.json", new)
        self.assertEqual(annotations["rows"], self.annotations["rows"])
        for key in self.parent.keys()-{"task", "dataset_sha256"}:
            self.assertEqual(new[key], self.parent[key])
        self.assertEqual(self.parent_snapshot, (self.parent_path.read_bytes(), self.annotation_path.read_bytes()))

    def test_registered_v4_append_rebind_preserves_parent_chain_splits_and_all_finalized_answers(self):
        dataset = self.appended()
        sheets = self.root/"v4-sheets"
        sheets.mkdir()
        write_blind_sheets(dataset, sheets)
        for name in ("codex", "claude"):
            fill(sheets/(name+".csv"))
        annotations = merge(dataset, sheets/"codex.csv", sheets/"claude.csv",
                            base_dataset=self.parent_path, base_annotations=self.annotation_path)
        dataset["task"] = v4_descriptor()
        self.assertEqual(dataset["task"]["definition_sha256"], descriptor()["definition_sha256"])
        dataset["dataset_sha256"] = digest({k: v for k, v in dataset.items() if k != "dataset_sha256"})
        annotations["dataset_sha256"] = dataset["dataset_sha256"]
        annotations["annotations_sha256"] = digest({k: v for k, v in annotations.items() if k != "annotations_sha256"})
        source, answers = self.root/"v4.json", self.root/"v4-annotations.json"
        write_json(source, dataset)
        write_json(answers, annotations)
        snapshots = source.read_bytes(), answers.read_bytes()
        with self.assertRaises(ValueError):
            load_dataset(source)
        target = self.root/"rebound-v4"
        preview = migrate_finalized(source, answers, target)
        self.assertEqual(preview["model_calls"], 0)
        self.assertFalse(target.exists())
        result = migrate_finalized(source, answers, target, write=True)
        new = load_dataset(Path(result["directory"])/"dataset.json")
        final = load_annotations(Path(result["directory"])/"annotations.json", new)
        for key in dataset.keys()-{"task", "dataset_sha256"}:
            self.assertEqual(new[key], dataset[key])
        for key in annotations.keys()-{"dataset_sha256", "annotations_sha256"}:
            self.assertEqual(final[key], annotations[key])
        self.assertEqual(snapshots, (source.read_bytes(), answers.read_bytes()))

    def test_v4_task_and_registration_must_match_exactly_before_migration_writes(self):
        from trading_companion.news_triage_product_parent import V4_PATH
        dataset, annotations = deepcopy(self.parent), deepcopy(self.annotations)
        dataset["task"] = v4_descriptor()
        source, answers = self.root/"v4.json", self.root/"v4-annotations.json"
        target = self.root/"must-not-exist"
        for field in ("rule_version", "definition_sha256", "source_digest"):
            bad = deepcopy(dataset)
            bad["task"][field] = "changed"
            bad["dataset_sha256"] = digest({k: v for k, v in bad.items() if k != "dataset_sha256"})
            annotations["dataset_sha256"] = bad["dataset_sha256"]
            annotations["annotations_sha256"] = digest({k: v for k, v in annotations.items() if k != "annotations_sha256"})
            source.write_text(json.dumps(bad), encoding="utf-8")
            answers.write_text(json.dumps(annotations), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ValueError):
                migrate_finalized(source, answers, target, write=True)
            self.assertFalse(target.exists())
        fake = self.root/"fake-registration.json"
        fake.write_text(V4_PATH.read_text("utf-8").replace("rules-v4", "rules-v999"), encoding="utf-8")
        with patch("trading_companion.news_triage_product_parent.V4_PATH", fake), self.assertRaises(ValueError):
            v4_descriptor()

    def test_parent_version_definition_incomplete_review_and_wrong_hash_are_rejected(self):
        for case in ("version", "definition", "source", "checksum", "incomplete"):
            dataset, annotations = deepcopy(self.parent), deepcopy(self.annotations)
            if case == "version": dataset["task"]["rule_version"] = "arbitrary-v3"
            if case == "definition": dataset["task"]["definition_sha256"] = "0"*64
            if case == "source": dataset["task"]["source_digest"] = "0"*64
            if case == "incomplete":
                annotations["rows"][0].update(status="incomplete", gold=None, review_source=None)
                annotations["status"] = "pending"
            dataset["dataset_sha256"] = digest({k: v for k, v in dataset.items() if k != "dataset_sha256"})
            annotations["dataset_sha256"] = dataset["dataset_sha256"]
            annotations["annotations_sha256"] = digest({k: v for k, v in annotations.items() if k != "annotations_sha256"})
            if case == "checksum": annotations["annotations_sha256"] = "0"*64
            self.parent_path.write_text(json.dumps(dataset), encoding="utf-8")
            self.annotation_path.write_text(json.dumps(annotations), encoding="utf-8")
            with self.subTest(case=case), self.assertRaises(ValueError):
                load_parent_pair(self.parent_path, self.annotation_path)

    def test_prepare_cli_rejects_missing_parent_argument_before_writing(self):
        from contextlib import redirect_stdout
        from scripts.prepare_news_triage_product import main
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--captures", "unused", "--base-dataset", str(self.parent_path)]), 2)

    def test_real_file_prepare_and_merge_cli_round_trip_without_network(self):
        from contextlib import redirect_stdout
        from scripts.prepare_news_triage_product import main as prepare_main
        from scripts.merge_news_triage_annotations import main as merge_main
        self.directory.write_text(json.dumps({"source": "synthetic_test_only", "is_fixture": False,
            "retrieved_at": "2026-11-10T00:00:00+00:00", "securities": [s.to_dict() for s in DIRECTORY.securities]}), encoding="utf-8")
        self.parent["directory_sha256"] = sha256(self.directory.read_bytes()).hexdigest()
        self.parent["dataset_sha256"] = digest({k: v for k, v in self.parent.items() if k != "dataset_sha256"})
        self.annotations["dataset_sha256"] = self.parent["dataset_sha256"]
        self.annotations["annotations_sha256"] = digest({k: v for k, v in self.annotations.items() if k != "annotations_sha256"})
        self.parent_path.write_text(json.dumps(self.parent), encoding="utf-8")
        self.annotation_path.write_text(json.dumps(self.annotations), encoding="utf-8")
        captures = self.root/"captures"
        captures.mkdir()
        companies = []
        for row in [sample("浦发银行新资料甲", day="2026-11-10"), sample("招商银行新资料乙", issuer="600036.SH", day="2026-11-10")]:
            title = {**row, "evidence_id": "E"+row["id"], "published_at": None, "publication_precision": "date",
                     "retrieved_at": "2026-11-10T10:00:00+08:00", "title_sha256": sha256(row["title"].encode("utf-8")).hexdigest()}
            companies.append({"instrument_id": row["instrument_id"], "security_name": row["company_name"], "titles": [title],
                              "source_results": [{"kind": "news", "source": row["source"], "status": "available"}]})
        write_json(captures/"000001.json", {"version": "news-title-capture-v1", "model_calls": 0, "origin": "collection",
            "as_of": "2026-11-10T10:00:00+08:00", "publication_start": "2026-10-11", "title_count": 2, "companies": companies})
        flags = ["--captures", str(captures), "--directory", str(self.directory), "--base-dataset", str(self.parent_path),
                 "--base-annotations", str(self.annotation_path), "--output-root", str(self.root/"output")]
        with patch("socket.getaddrinfo", side_effect=AssertionError("No DNS")), \
                patch("socket.socket.connect", side_effect=AssertionError("No HTTP")):
            preview = io.StringIO()
            with redirect_stdout(preview):
                self.assertEqual(prepare_main(flags), 0)
            self.assertEqual(json.loads(preview.getvalue())["summary"]["added"], 2)
            self.assertFalse((self.root/"output").exists())
            prepared = io.StringIO()
            with redirect_stdout(prepared):
                self.assertEqual(prepare_main([*flags, "--write"]), 0)
            run = Path(json.loads(prepared.getvalue())["directory"])
            for name in ("codex", "claude"):
                fill(run/(name+".csv"))
            merged = io.StringIO()
            with redirect_stdout(merged):
                self.assertEqual(merge_main(["--dataset", str(run/"dataset.json"), "--codex", str(run/"codex.csv"),
                    "--claude", str(run/"claude.csv"), "--base-dataset", str(self.parent_path),
                    "--base-annotations", str(self.annotation_path), "--output-root", str(self.root/"merged")]), 0)
        report = load_annotations(Path(json.loads(merged.getvalue())["directory"])/"annotations.json", load_dataset(run/"dataset.json"))
        self.assertEqual(report["rows"][:2], self.annotations["rows"])
        self.assertEqual(len(report["rows"]), 4)

    def test_append_disagreements_require_new_user_decisions_and_never_rewrite_parent(self):
        dataset = self.appended()
        target = self.root/"new"
        target.mkdir()
        write_blind_sheets(dataset, target)
        left, right = target/"codex.csv", target/"claude.csv"
        fill(left)
        fill(right, labels={"relevance": "primary", "tone": "neutral", "importance": "ordinary"})
        pending = merge(dataset, left, right, base_dataset=self.parent_path, base_annotations=self.annotation_path)
        self.assertEqual(pending["summary"], {"reviewed": 2, "disagreement": 2})
        fields = ("id", "relevance", "tone", "importance", "relevance_rationale", "tone_rationale", "importance_rationale",
                  "reviewed", "grouping_reviewed", "adjudicator", "decision_rationale")
        decisions = [{"id": r["id"], "relevance": "primary", "tone": "neutral", "importance": "ordinary",
                      **{q+"_rationale": "测试用户新题裁定" for q in ("relevance", "tone", "importance")},
                      "reviewed": "yes", "grouping_reviewed": "yes", "adjudicator": "user", "decision_rationale": "测试用户授权"}
                     for r in pending["pending"]]
        path = target/"decisions.csv"
        write_csv(path, fields, decisions)
        finalized = merge(dataset, left, right, base_dataset=self.parent_path, base_annotations=self.annotation_path,
                          adjudications=path)
        self.assertEqual(finalized["rows"][:2], self.annotations["rows"])
        self.assertTrue(all(r["review_source"] == "user_adjudication" for r in finalized["rows"][2:]))
        path.write_text(path.read_text("utf-8-sig").replace(decisions[0]["id"], self.parent["rows"][0]["id"]), encoding="utf-8-sig")
        with self.assertRaises(ValueError):
            merge(dataset, left, right, base_dataset=self.parent_path, base_annotations=self.annotation_path, adjudications=path)
