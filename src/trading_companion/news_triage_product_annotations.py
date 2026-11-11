"""Identical blind CSV sheets and explicit, provenance-bound human reconciliation."""

from collections import Counter
from copy import deepcopy
import csv
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from trading_companion.news_triage_product import OPTIONS, UNRESOLVED, digest
from trading_companion.news_triage_product_data import load_dataset, read_json, write_json

CONTEXT = ("id", "split", "stratum", "group_id", "instrument_id", "company_name", "source",
           "published_date", "title", "url", "identity_precheck", "group_members")
ANSWERS = tuple(OPTIONS)
REASONS = tuple(q + "_rationale" for q in OPTIONS)
REVIEW = ("reviewed", "grouping_reviewed")
FIELDS = (*CONTEXT, *ANSWERS, *REASONS, *REVIEW)
VERSION = "news-triage-human-merge-v1"


def context(row, dataset):
    group = next(g for g in dataset["groups"] if g["id"] == row["group_id"])
    values = {k: row[k] for k in CONTEXT if k not in ("identity_precheck", "group_members")}
    values["identity_precheck"] = json.dumps(row["identity"], ensure_ascii=False, sort_keys=True)
    values["group_members"] = json.dumps([{k: m[k] for k in ("instrument_id", "title", "source", "published_date")}
                                          for m in group["members"]], ensure_ascii=False)
    return values


def write_csv(path, fields, rows):
    with Path(path).open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_blind_sheets(dataset, directory):
    added = None if "append" not in dataset else set(dataset["append"]["added_ids"])
    rows = [{**context(row, dataset), **dict.fromkeys((*ANSWERS, *REASONS, *REVIEW), "")}
            for row in dataset["rows"] if added is None or row["id"] in added]
    for reviewer in ("codex", "claude"):
        write_csv(Path(directory) / (reviewer + ".csv"), FIELDS, rows)


def _read_csv(path, fields):
    if Path(path).stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Annotation sheet exceeds size limit")
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != tuple(fields):
            raise ValueError("Annotation columns must match the blind template")
        rows = list(reader)
    if len(rows) > 1000 or any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError("Invalid annotation rows")
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate annotation ID")
    return {r["id"]: r for r in rows}


def _complete(row):
    for q in ANSWERS:
        if row[q] and row[q] not in (*OPTIONS[q], UNRESOLVED):
            raise ValueError("Invalid human label")
        rationale = row[q + "_rationale"]
        if len(rationale) > 2000 or any(not c.isprintable() for c in rationale):
            raise ValueError("Invalid human rationale")
    if row["reviewed"] not in ("", "yes") or row["grouping_reviewed"] not in ("", "yes"):
        raise ValueError("Review fields accept blank or yes")
    if row["relevance"] == "unrelated" and row["tone"] not in ("", "neutral", UNRESOLVED):
        raise ValueError("Unrelated titles cannot have a directional target tone")
    return bool(all(row[q] and row[q+"_rationale"].strip() for q in ANSWERS)
                and all(row[field] == "yes" for field in REVIEW))


def merge(dataset, codex_path, claude_path, *, adjudications=None, base_dataset=None, base_annotations=None):
    inherited = []
    parent_binding = None
    if "append" in dataset:
        from trading_companion.news_triage_product_parent import load_parent_pair, validate_frozen
        if base_dataset is None or base_annotations is None:
            raise ValueError("Append merge requires both original parent files")
        parent, annotations = load_parent_pair(base_dataset, base_annotations)
        validate_frozen(dataset, parent, annotations)
        inherited = deepcopy(annotations["rows"])
        parent_binding = {"dataset_sha256": parent["dataset_sha256"],
                          "annotations_sha256": annotations["annotations_sha256"]}
    elif base_dataset is not None or base_annotations is not None:
        raise ValueError("Parent files require an append dataset")
    sheets = [_read_csv(path, FIELDS) for path in (codex_path, claude_path)]
    expected = {r["id"]: r for r in dataset["rows"]
                if "append" not in dataset or r["id"] in dataset["append"]["added_ids"]}
    if any(sheet.keys() != expected.keys() for sheet in sheets):
        raise ValueError("Both independent sheets must contain exactly the dataset IDs")
    decisions = {} if adjudications is None else _read_csv(adjudications, (
        "id", *ANSWERS, *REASONS, *REVIEW, "adjudicator", "decision_rationale"))
    if decisions.keys() - expected.keys():
        raise ValueError("Adjudication contains unknown IDs")
    results, pending = inherited, []
    for sid, item in expected.items():
        left, right = (sheet[sid] for sheet in sheets)
        ctx = context(item, dataset)
        if any(any(row[k] != value for k, value in ctx.items()) for row in (left, right)):
            raise ValueError("Blind context was changed; regenerate sheets for regrouping")
        completed = [_complete(row) for row in (left, right)]
        differing = [q for q in ANSWERS if left[q] != right[q]]
        status, gold, reasons, source = "incomplete", None, None, None
        if all(completed) and not differing:
            status, source = "reviewed", "independent_agreement"
            gold = {q: left[q] for q in ANSWERS}
            reasons = {q: [left[q+"_rationale"], right[q+"_rationale"]] for q in ANSWERS}
        elif all(completed):
            status = "disagreement"
            decision = decisions.get(sid)
            if decision is not None:
                if (not _complete(decision) or decision["adjudicator"] != "user"
                        or not decision["decision_rationale"].strip() or len(decision["decision_rationale"]) > 2000):
                    raise ValueError("Disagreements need explicit complete user adjudication")
                status, source = "reviewed", "user_adjudication"
                gold = {q: decision[q] for q in ANSWERS}
                reasons = {q: [decision[q+"_rationale"]] for q in ANSWERS}
        if sid in decisions and (not all(completed) or not differing):
            raise ValueError("Adjudication cannot replace missing independent work or rewrite agreement")
        results.append({"id": sid, "split": item["split"], "status": status, "gold": gold,
                        "rationales": reasons, "review_source": source, "differing_questions": differing})
        if status != "reviewed":
            pending.append({"id": sid, "title": item["title"], "company_name": item["company_name"],
                            "status": status, "differing_questions": differing,
                            "codex": {k: left[k] for k in (*ANSWERS, *REASONS, *REVIEW)},
                            "claude": {k: right[k] for k in (*ANSWERS, *REASONS, *REVIEW)}})
    report = {"version": VERSION, "dataset_sha256": dataset["dataset_sha256"],
              "status": "reviewed" if not pending else "pending",
              "sheets_sha256": {name: sha256(Path(path).read_bytes()).hexdigest()
                                for name, path in (("codex", codex_path), ("claude", claude_path))},
              "adjudications_sha256": None if adjudications is None else sha256(Path(adjudications).read_bytes()).hexdigest(),
              "summary": dict(Counter(r["status"] for r in results)), "rows": results, "pending": pending,
              "trust_scope": "Local human review declarations; hashes bind content, not the real-world identity of reviewers."}
    if parent_binding is not None:
        report["parent"] = parent_binding
    report["annotations_sha256"] = digest(report)
    return report


def write_merge(dataset_path, codex_path, claude_path, output_root, *, adjudications=None,
                base_dataset=None, base_annotations=None):
    dataset = load_dataset(dataset_path)
    report = merge(dataset, codex_path, claude_path, adjudications=adjudications,
                   base_dataset=base_dataset, base_annotations=base_annotations)
    target = Path(output_root) / uuid4().hex
    target.mkdir(parents=True, exist_ok=False)
    write_json(target / "annotations.json", report)
    write_json(target / "pending.json", report["pending"])
    fields = ("id", *ANSWERS, *REASONS, *REVIEW, "adjudicator", "decision_rationale")
    write_csv(target / "adjudication.csv", fields,
              [{**dict.fromkeys(fields, ""), "id": r["id"]} for r in report["pending"] if r["status"] == "disagreement"])
    return {"status": report["status"], "directory": str(target), "summary": report["summary"],
            "annotations_sha256": report["annotations_sha256"], "model_calls": 0}


def load_annotations(path, dataset):
    report = read_json(path)
    if (report["version"] != VERSION or report["dataset_sha256"] != dataset["dataset_sha256"]
            or report["annotations_sha256"] != digest({k: v for k, v in report.items() if k != "annotations_sha256"})
            or len(report["rows"]) != len(dataset["rows"])
            or {r["id"] for r in report["rows"]} != {r["id"] for r in dataset["rows"]}):
        raise ValueError("Annotation integrity or dataset binding mismatch")
    for row in report["rows"]:
        item = next(r for r in dataset["rows"] if r["id"] == row["id"])
        if row["split"] != item["split"]:
            raise ValueError("Annotation split binding mismatch")
        if row["status"] == "reviewed":
            if (row["review_source"] not in ("independent_agreement", "user_adjudication")
                    or type(row["gold"]) is not dict or row["gold"].keys() != OPTIONS.keys()
                    or any(row["gold"][q] not in (*OPTIONS[q], UNRESOLVED) for q in OPTIONS)
                    or type(row["rationales"]) is not dict or row["rationales"].keys() != OPTIONS.keys()
                    or not all(type(row["rationales"][q]) is list and row["rationales"][q]
                               and all(type(s) is str and s.strip() for s in row["rationales"][q]) for q in OPTIONS)
                    or row["gold"]["relevance"] == "unrelated" and row["gold"]["tone"] not in ("neutral", UNRESOLVED)):
                raise ValueError("Invalid reviewed annotations")
        elif row["status"] not in ("incomplete", "disagreement"):
            raise ValueError("Invalid annotation status")
    reviewed = all(r["status"] == "reviewed" for r in report["rows"])
    if (report["status"] == "reviewed") != reviewed:
        raise ValueError("Annotation overall status mismatch")
    if "append" in dataset:
        manifest = dataset["append"]
        frozen = set(manifest["frozen_ids"])
        if (report.get("parent") != {"dataset_sha256": manifest["base_dataset_sha256"],
                                    "annotations_sha256": manifest["base_annotations_sha256"]}
                or digest([r for r in report["rows"] if r["id"] in frozen]) != manifest["frozen_annotation_rows_sha256"]):
            raise ValueError("Frozen parent answers were changed")
    return report
