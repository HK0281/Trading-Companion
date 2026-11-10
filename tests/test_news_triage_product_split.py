from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.news_triage_product_data import prepare
from trading_companion.news_triage_product_split import MAX_RATIO_GAP, balance_report, split_rows


def fixture_rows(codes, *, pairs=4, hard=True):
    rows, groups = [], []
    for code in codes:
        for i in range(2*pairs):
            sid = code+":"+str(i)
            kind = "news" if i % 2 else "announcements"
            row = {"id": sid, "group_id": sid, "instrument_id": code, "kind": kind,
                   "source": "sina_company_news" if kind == "news" else "eastmoney_announcements",
                   "normalized_title": sid, "published_date": "2026-11-10" if kind == "news" else "2026-10-15",
                   "hard_reasons": ["test_hint"] if hard else []}
            rows.append(row)
            groups.append({"id": sid, "members": [row]})
    return rows, groups


class CompanySplitTests(unittest.TestCase):
    def test_company_and_event_isolation_source_and_board_balance_and_reproducibility(self):
        codes = ([f"{600000+i:06d}.SH" for i in range(15)] + [f"{i:06d}.SZ" for i in range(15)]
                 + [f"{300000+i:06d}.SZ" for i in range(10)] + [f"{688000+i:06d}.SH" for i in range(10)])
        rows, groups = fixture_rows(codes, pairs=5)
        chosen, report = split_rows(rows, groups)
        self.assertEqual(len(chosen), 400)
        self.assertTrue(report["balance"]["passed"])
        self.assertEqual([len(report["company_split"][s]) for s in ("debug", "sealed")], [25, 25])
        self.assertFalse(set(report["company_split"]["debug"]) & set(report["company_split"]["sealed"]))
        self.assertFalse({r["group_id"] for r in chosen if r["split"] == "debug"}
                         & {r["group_id"] for r in chosen if r["split"] == "sealed"})
        self.assertTrue(all(gap <= .15+1e-12 for gap in report["balance"]["gaps"].values()))
        self.assertTrue(all(r["hard_reasons"] for r in chosen if r["stratum"] == "natural"))
        for split in ("debug", "sealed"):
            self.assertEqual({r["published_date"] for r in chosen if r["split"] == split}, {"2026-10-15", "2026-11-10"})
        again, other = split_rows(list(reversed(rows)), list(reversed(groups)))
        self.assertEqual(chosen, again)
        self.assertEqual(report, other)

    def test_shared_article_binds_companies_and_all_their_other_events(self):
        codes = ["600000.SH", "600036.SH", "600016.SH", "600030.SH"]
        rows, groups = fixture_rows(codes)
        shared = [{**rows[i*8], "id": "shared"+str(i), "group_id": "shared", "kind": "news", "source": "sina_company_news"}
                  for i in (0, 1)]
        rows.extend(shared)
        groups.append({"id": "shared", "members": shared})
        selected, report = split_rows(rows, groups)
        self.assertTrue(report["balance"]["passed"])
        self.assertFalse(report["excluded_groups"])
        self.assertEqual(len({r["split"] for r in selected if r["instrument_id"] in codes[:2]}), 1)

    def test_binding_conflict_excludes_entire_shared_article_and_reports_count(self):
        codes = ["600000.SH", "600036.SH", "600016.SH", "600030.SH"]
        rows, groups = fixture_rows(codes)
        shared = [{**rows[i*8], "id": "shared"+str(i), "group_id": "shared"} for i in range(4)]
        rows.extend(shared)
        groups.append({"id": "shared", "members": shared})
        selected, report = split_rows(rows, groups)
        self.assertTrue(report["balance"]["passed"])
        self.assertEqual(report["excluded_groups"], ["shared"])
        self.assertEqual(report["excluded_representatives"], 4)
        self.assertFalse(any(r["group_id"] == "shared" for r in selected))

    def test_fixed_source_gap_threshold_refuses_blind_sheet_writes(self):
        rows = []
        for code, news in (("600000.SH", 50), ("600036.SH", 66)):
            rows.extend({"instrument_id": code, "split": "debug" if code == "600000.SH" else "sealed",
                         "kind": "news" if i<news else "announcements",
                         "source": "sina_company_news" if i<news else "eastmoney_announcements"} for i in range(100))
        assignment = {"600000.SH": "debug", "600036.SH": "sealed"}
        balance = balance_report(rows, assignment)
        self.assertEqual(MAX_RATIO_GAP, .15)
        self.assertFalse(balance["passed"])
        self.assertAlmostEqual(balance["gaps"]["kinds"], .16)
        with TemporaryDirectory() as temporary:
            output = Path(temporary)/"must_not_exist"
            dataset = {"summary": {"balance": balance}}
            with patch("trading_companion.news_triage_product_data.build_dataset", return_value=dataset):
                result = prepare(["first", "second"], "directory", output)
            self.assertEqual(result["status"], "imbalanced")
            self.assertFalse(output.exists())
            from scripts.prepare_news_triage_product import main
            with patch("trading_companion.news_triage_product_data.build_dataset", return_value=dataset), redirect_stdout(StringIO()):
                self.assertEqual(main(["--captures", "first", "--captures", "second", "--write", "--output-root", str(output)]), 1)
            self.assertFalse(output.exists())
