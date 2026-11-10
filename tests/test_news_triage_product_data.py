from copy import deepcopy
from contextlib import redirect_stdout
import csv
from hashlib import sha256
import json
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.news_triage_product import OPTIONS, descriptor, digest
from trading_companion.news_triage_product_annotations import (
    FIELDS, context, load_annotations, merge, write_blind_sheets, write_csv, write_merge,
)
from trading_companion.news_triage_product_data import (
    DATA_VERSION, build_dataset, group_rows, hard_reasons, load_captures, load_dataset, prepare, split_rows, write_json,
)
from trading_companion.news_triage_product_split import balance_report
from tests.test_news_triage_product import DIRECTORY, sample


def dataset_for(rows=None):
    rows = [sample(), sample("招商银行公布季度业绩", issuer="600036.SH")] if rows is None else deepcopy(rows)
    groups = []
    for index, row in enumerate(rows):
        row.update(group_id="G"+row["id"], stratum="natural", split="debug" if index % 2 == 0 else "sealed", hard_reasons=hard_reasons(row))
        groups.append({"id": row["group_id"], "min_date": row["published_date"], "max_date": row["published_date"],
                       "members": [deepcopy(row)]})
    company_split = {s: sorted({r["instrument_id"] for r in rows if r["split"] == s}) for s in ("debug", "sealed")}
    assignment = {code: s for s, codes in company_split.items() for code in codes}
    dataset = {"version": DATA_VERSION, "task": descriptor(), "directory_is_fixture": False, "groups": groups, "rows": rows,
               "partition": {"event_group_split": {r["group_id"]: r["split"] for r in rows}},
               "summary": {"company_split": company_split, "excluded_groups": [], "balance": balance_report(rows, assignment)}}
    dataset["dataset_sha256"] = digest(dataset)
    return dataset


def fill(path, *, labels=None):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row.update(labels or {"relevance": "primary", "tone": "neutral", "importance": "high"})
        row.update({q+"_rationale": "测试夹具依据，非真实标题标注" for q in OPTIONS})
        row.update(reviewed="yes", grouping_reviewed="yes")
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class PreparationTests(unittest.TestCase):
    def test_near_reposts_across_sources_and_issuers_stay_in_one_group(self):
        rows = [sample("招商银行泉州分行被罚，罚款50万元", issuer="600036.SH"),
                sample("招商银行泉州分行因违规被罚50万元", issuer="600036.SH", day="2026-11-02"),
                sample("招商银行:关于泉州分行受到处罚的公告", issuer="600036.SH", source="eastmoney_announcements"),
                sample("浦发银行、招商银行集体上涨"),
                sample("浦发银行、招商银行集体上涨", issuer="600036.SH")]
        representatives, groups, links = group_rows(rows)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(representatives), 3)  # One event per issuer; two issuers in shared article.
        self.assertEqual(len({g["id"] for g in groups}), 2)
        self.assertTrue(links)
        _, grouped_again, _ = group_rows(list(reversed(rows)))
        self.assertEqual(groups, grouped_again)

    def test_plain_representative_does_not_inherit_difficulty_from_other_variants(self):
        rows = [sample("浦发银行公布季度业绩"), sample("浦发银行季度业绩是否改善？", day="2026-11-02")]
        representatives, _, _ = group_rows(rows)
        self.assertEqual(len(representatives), 1)
        self.assertFalse(representatives[0]["hard_reasons"])
        self.assertEqual(representatives[0]["stratum"], "natural")

    def test_real_hard_shortage_is_not_filled_with_easy_or_synthetic_rows(self):
        rows = [{"id": str(i), "group_id": str(i), "instrument_id": "600000.SH" if i<5 else "600036.SH",
                 "published_date": "2026-11-01", "kind": "news", "source": "sina_company_news",
                 "normalized_title": str(i), "hard_reasons": []} for i in range(10)]
        selected, report = split_rows(rows, [{"id": r["id"], "members": [r]} for r in rows])
        self.assertEqual(len(selected), 10)
        self.assertEqual(sum(s["hard"]["shortfall"] for s in report["splits"].values()), 140)

    def test_capture_rejects_unchecked_or_tampered_public_titles(self):
        with TemporaryDirectory() as temporary:
            title = {**sample(), "evidence_id": "E1", "published_at": "2026-11-01T10:00:00+08:00",
                     "publication_precision": "datetime", "retrieved_at": "2026-11-10T10:00:00+08:00"}
            title["title_sha256"] = sha256(title["title"].casefold().encode("utf-8")).hexdigest()
            data = {"version": "news-title-capture-v1", "model_calls": 0, "origin": "collection", "title_count": 1,
                    "as_of": "2026-11-10T10:00:00+08:00", "publication_start": "2026-10-11",
                    "companies": [{"instrument_id": "600000.SH", "security_name": "浦发银行", "titles": [title],
                                   "source_results": [{"kind": "news", "source": "sina_company_news", "status": "available"}]}]}
            path = Path(temporary)/"000001.json"
            write_json(path, data)
            rows, _ = load_captures(temporary, DIRECTORY)
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["identity"]["issuer_verified"])
            for change in ("hash", "unavailable", "count", "wrong_issuer"):
                bad = deepcopy(data)
                if change == "hash": bad["companies"][0]["titles"][0]["title"] += "被篡改"
                if change == "unavailable": bad["companies"][0]["source_results"][0]["status"] = "unavailable"
                if change == "count": bad["title_count"] = 2
                if change == "wrong_issuer": bad["companies"][0]["instrument_id"] = "600036.SH"
                path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_captures(temporary, DIRECTORY)

    def test_dataset_hash_and_group_split_leakage_are_checked(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/"dataset.json"
            dataset = dataset_for()
            write_json(path, dataset)
            self.assertEqual(load_dataset(path), dataset)
            dataset["rows"][0]["title"] = "changed"
            path.write_text(json.dumps(dataset), encoding="utf-8")
            with self.assertRaises(ValueError): load_dataset(path)

    def test_multiple_directories_keep_same_filenames_distinct_and_deduplicate_titles(self):
        with TemporaryDirectory() as temporary:
            roots = [Path(temporary)/name for name in ("first", "second")]
            for root in roots: root.mkdir()
            a, b = sample(), sample("招商银行发布季度业绩", issuer="600036.SH")
            def capture(rows):
                companies = []
                for row in rows:
                    title = {**row, "evidence_id": "E1", "published_at": None, "publication_precision": "date",
                             "retrieved_at": "2026-11-10T10:00:00+08:00",
                             "title_sha256": sha256(row["title"].encode("utf-8")).hexdigest()}
                    companies.append({"instrument_id": row["instrument_id"], "security_name": row["company_name"],
                                      "titles": [title], "source_results": [{"kind": row["kind"], "source": row["source"], "status": "available"}]})
                return {"version": "news-title-capture-v1", "model_calls": 0, "origin": "collection", "title_count": len(rows),
                        "as_of": "2026-11-10T10:00:00+08:00", "publication_start": "2026-10-11", "companies": companies}
            write_json(roots[0]/"000001.json", capture([a]))
            write_json(roots[1]/"000001.json", capture([a, b]))
            rows, inputs = load_captures(roots, DIRECTORY)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({r["id"] for r in rows}), 2)
            self.assertEqual(len({f["batch"] for f in inputs}), 2)
            self.assertEqual({f["file"] for f in inputs}, {"000001.json"})
            self.assertEqual(load_captures(list(reversed(roots)), DIRECTORY), (rows, inputs))
            representatives, _, _ = group_rows(rows)
            self.assertEqual(len(representatives), 2)
            with self.assertRaises(ValueError): load_captures([roots[0], roots[0]], DIRECTORY)

    def test_loader_rejects_company_overlap_even_when_event_ids_differ(self):
        dataset = dataset_for()
        dataset["summary"]["company_split"]["sealed"].append(dataset["rows"][0]["instrument_id"])
        dataset.pop("dataset_sha256")
        dataset["dataset_sha256"] = digest(dataset)
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/"dataset.json"
            write_json(path, dataset)
            with self.assertRaisesRegex(ValueError, "Company leakage"): load_dataset(path)

    def test_preparation_cli_previews_without_writing_and_explicit_write_keeps_labels_empty(self):
        from scripts.prepare_news_triage_product import main
        with TemporaryDirectory() as temporary:
            output = Path(temporary)/"output"
            fake = dataset_for()
            fake["summary"]["selected"] = 2
            with patch("scripts.prepare_news_triage_product.build_dataset", return_value=fake), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--captures", "unused", "--output-root", str(output)]), 0)
            self.assertFalse(output.exists())
            with patch("trading_companion.news_triage_product_data.build_dataset", return_value=fake):
                result = prepare("unused", "unused", output)
            self.assertEqual(result["annotations_filled"], 0)
            self.assertEqual(result["model_calls"], 0)
            pending = merge(fake, Path(result["directory"])/"codex.csv", Path(result["directory"])/"claude.csv")
            self.assertEqual(pending["status"], "pending")

    def test_dataset_loader_rejects_a_group_shared_between_debug_and_sealed(self):
        dataset = dataset_for([sample(), sample("招商银行发布新产品", issuer="600036.SH", day="2026-11-10")])
        dataset["rows"][1].update(split="sealed", group_id=dataset["rows"][0]["group_id"])
        dataset.pop("dataset_sha256")
        dataset["dataset_sha256"] = digest(dataset)
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/"dataset.json"
            write_json(path, dataset)
            with self.assertRaisesRegex(ValueError, "leakage"): load_dataset(path)


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dataset = dataset_for()
        write_blind_sheets(self.dataset, self.root)
        self.paths = [self.root/(name+".csv") for name in ("codex", "claude")]

    def test_blind_sheets_are_identical_with_only_empty_labels(self):
        self.assertEqual(self.paths[0].read_bytes(), self.paths[1].read_bytes())
        text = self.paths[0].read_text("utf-8-sig")
        self.assertNotIn("gold", text)
        result = merge(self.dataset, *self.paths)
        self.assertEqual(result["summary"], {"incomplete": 2})
        self.assertIsNone(result["rows"][0]["gold"])

    def test_consensus_requires_both_independent_reviews_and_group_confirmation(self):
        fill(self.paths[0])
        self.assertEqual(merge(self.dataset, *self.paths)["status"], "pending")
        fill(self.paths[1])
        report = merge(self.dataset, *self.paths)
        self.assertEqual(report["status"], "reviewed")
        self.assertEqual(report["rows"][0]["review_source"], "independent_agreement")
        text = self.paths[1].read_text("utf-8-sig").replace("yes,yes", "yes,")
        self.paths[1].write_text(text, encoding="utf-8-sig")
        self.assertEqual(merge(self.dataset, *self.paths)["status"], "pending")

    def test_disagreement_needs_user_adjudication_and_all_items_are_kept(self):
        fill(self.paths[0])
        fill(self.paths[1], labels={"relevance": "primary", "tone": "neutral", "importance": "unknown"})
        report = merge(self.dataset, *self.paths)
        self.assertEqual(report["rows"][0]["status"], "disagreement")
        self.assertEqual(report["pending"][0]["differing_questions"], ["importance"])
        fields = ("id", *OPTIONS, *(q+"_rationale" for q in OPTIONS), "reviewed", "grouping_reviewed", "adjudicator", "decision_rationale")
        decision = {"id": self.dataset["rows"][0]["id"], "relevance": "primary", "tone": "neutral", "importance": "unknown",
                    **{q+"_rationale": "测试裁定依据" for q in OPTIONS}, "reviewed": "yes", "grouping_reviewed": "yes",
                    "adjudicator": "user", "decision_rationale": "测试用户裁定"}
        path = self.root/"decisions.csv"
        write_csv(path, fields, [{**decision, "id": r["id"]} for r in report["pending"]])
        done = merge(self.dataset, *self.paths, adjudications=path)
        self.assertEqual(done["rows"][0]["review_source"], "user_adjudication")
        self.assertEqual(done["status"], "reviewed")

    def test_changed_context_duplicate_rows_and_unknown_labels_are_rejected(self):
        original = self.paths[0].read_text("utf-8-sig")
        for changed in (original.replace("浦发银行", "假公司"), original+original.splitlines()[1]+"\n"):
            self.paths[0].write_text(changed, encoding="utf-8-sig")
            with self.assertRaises(ValueError): merge(self.dataset, *self.paths)
        self.paths[0].write_text(original, encoding="utf-8-sig")
        fill(self.paths[0], labels={"relevance": "buy", "tone": "neutral", "importance": "high"})
        with self.assertRaises(ValueError): merge(self.dataset, *self.paths)

    def test_annotation_digest_binds_dataset_and_never_fills_blanks(self):
        report = merge(self.dataset, *self.paths)
        path = self.root/"annotations.json"
        write_json(path, report)
        self.assertEqual(load_annotations(path, self.dataset)["status"], "pending")
        report["status"] = "reviewed"
        path.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaises(ValueError): load_annotations(path, self.dataset)
