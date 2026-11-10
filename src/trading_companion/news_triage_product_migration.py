"""Explicit v2-to-v3 rule migration of one independent sheet, without relabeling."""

from copy import deepcopy
import csv
from hashlib import sha256
import io
from pathlib import Path
from uuid import uuid4

from trading_companion.news_triage_product import INSTRUCTIONS, ROOT, descriptor, digest, questions
from trading_companion.news_triage_product_annotations import CONTEXT, FIELDS, _complete, context
from trading_companion.news_triage_product_data import _validate_dataset, read_json, write_json

VERSION = "news-triage-product-rule-migration-v1"
LEGACY_PATH = ROOT / "data/evaluation/news-triage-product-rules-v2.legacy.json"
LEGACY_DESCRIPTOR_SHA256 = "0c72eaacbf18105fb7f3d971f06410c1047374188bf60af3a5bd9a6296116d37"
MAX_SHEET_BYTES = 8 * 1024 * 1024


def legacy_descriptor():
    value = read_json(LEGACY_PATH, 64 * 1024)
    if digest(value) != LEGACY_DESCRIPTOR_SHA256:
        raise ValueError("Legacy rule registration was changed")
    return value


def _sheet_snapshot(path):
    """Parse and later copy the same bounded bytes, even if the source is edited."""
    path = Path(path)
    if path.name not in ("codex.csv", "claude.csv"):
        raise ValueError("Migrate exactly one independent codex.csv or claude.csv")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SHEET_BYTES + 1)
    if len(raw) > MAX_SHEET_BYTES:
        raise ValueError("Annotation sheet exceeds size limit")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise ValueError("Annotation columns must match the blind template")
    rows = list(reader)
    if (len(rows) > 1000 or any(None in r or any(v is None for v in r.values()) for r in rows)
            or len({r["id"] for r in rows}) != len(rows)):
        raise ValueError("Invalid or duplicate annotation rows")
    for row in rows:
        _complete(row)  # Validate, but never require completeness or fill review flags.
    return raw, rows


def migrate(dataset_path, sheet_path, output_root, *, write=False):
    """Validate first; preview by default, or write a fresh, answer-preserving copy."""
    old = _validate_dataset(read_json(dataset_path), legacy_descriptor())
    current = descriptor()
    if current["rule_version"] != "news-triage-product-rules-v3":
        raise ValueError("This migration only targets rules-v3")
    new = deepcopy(old)
    new["task"] = current
    new["dataset_sha256"] = digest({k: v for k, v in new.items() if k != "dataset_sha256"})
    _validate_dataset(new, current)
    content = lambda data: {k: v for k, v in data.items() if k not in ("task", "dataset_sha256")}
    if content(old) != content(new):
        raise ValueError("Migration must preserve all dataset content")

    raw, rows = _sheet_snapshot(sheet_path)
    expected = {r["id"]: r for r in old["rows"]}
    if {r["id"] for r in rows} != expected.keys():
        raise ValueError("Annotation IDs must exactly match the dataset")
    for row in rows:
        item = expected[row["id"]]
        original = context(item, old)
        target = context(item, new)
        if original != target or any(row[k] != original[k] for k in CONTEXT):
            raise ValueError("Annotation context was changed; migration cannot repair it")

    sheet_hash = sha256(raw).hexdigest()
    manifest = {"version": VERSION, "source_dataset_sha256": old["dataset_sha256"],
                "dataset_sha256": new["dataset_sha256"], "source_task": old["task"], "task": current,
                "sheet": Path(sheet_path).name, "rows": len(rows),
                "source_sheet_sha256": sheet_hash, "sheet_sha256": sheet_hash,
                "context_sha256": digest([{k: r[k] for k in CONTEXT} for r in rows]),
                "checks": {"dataset_content_unchanged_except_task_and_digest": True,
                           "all_context_fields_unchanged": True, "sheet_bytes_unchanged": True},
                "answers_changed": 0, "review_flags_changed": 0, "model_calls": 0}
    result = {"status": "planned", "output_written": False, "dataset_sha256": new["dataset_sha256"],
              "source_dataset_sha256": old["dataset_sha256"], "sheet_sha256": sheet_hash,
              "rows": len(rows), "answers_changed": 0, "review_flags_changed": 0, "model_calls": 0}
    if not write:
        return result

    target = Path(output_root) / uuid4().hex
    target.mkdir(parents=True, exist_ok=False)
    write_json(target / "dataset.json", new)
    write_json(target / "task.json", {"task": current, "instructions": INSTRUCTIONS,
        "questions": questions(), "human_abstention": "unresolved",
        "review_policy": "independent_labels_then_user_adjudication"})
    with (target / Path(sheet_path).name).open("xb") as stream:
        stream.write(raw)
    if (target / Path(sheet_path).name).read_bytes() != raw:
        raise ValueError("Output sheet bytes differ from the validated snapshot")
    write_json(target / "migration.json", manifest)
    return {**result, "status": "migrated", "output_written": True, "directory": str(target)}
