"""Strictly registered v3 parents with identical question semantics; no general bypass."""

from copy import deepcopy

from trading_companion.news_triage_product import ROOT, descriptor, digest
from trading_companion.news_triage_product_data import _validate_dataset, read_json

PARENT_PATH = ROOT / "data/evaluation/news-triage-product-rules-v3.legacy.json"
PARENT_DESCRIPTOR_SHA256 = "72ee3a408692e84b4f69b603ccfba9a94718615a75c32480eab43c43a088f3d7"


def parent_descriptor():
    value = read_json(PARENT_PATH, 64*1024)
    if digest(value) != PARENT_DESCRIPTOR_SHA256:
        raise ValueError("Frozen v3 registration was changed")
    return value


def load_parent_pair(dataset_path, annotations_path):
    from trading_companion.news_triage_product_annotations import load_annotations
    raw = read_json(dataset_path)
    current = descriptor()
    expected = current if raw["task"] == current else parent_descriptor()
    if expected["definition_sha256"] != current["definition_sha256"]:
        raise ValueError("Parent label semantics differ; manual re-review is required")
    dataset = _validate_dataset(raw, expected)
    annotations = load_annotations(annotations_path, dataset)
    if annotations["status"] != "reviewed" or any(r["status"] != "reviewed" for r in annotations["rows"]):
        raise ValueError("Parent must have complete finalized human annotations")
    return dataset, annotations


def rebound(dataset, annotations):
    """Only used after load_parent_pair; preserve every title and finalized answer."""
    new_dataset, new_annotations = deepcopy(dataset), deepcopy(annotations)
    new_dataset["task"] = descriptor()
    new_dataset["dataset_sha256"] = digest({k: v for k, v in new_dataset.items() if k != "dataset_sha256"})
    new_annotations["dataset_sha256"] = new_dataset["dataset_sha256"]
    new_annotations["annotations_sha256"] = digest({k: v for k, v in new_annotations.items() if k != "annotations_sha256"})
    return new_dataset, new_annotations


def validate_frozen(dataset, parent, annotations):
    manifest = dataset["append"]
    frozen = {r["id"]: r for r in parent["rows"]}
    combined = {r["id"]: r for r in dataset["rows"]}
    groups = {g["id"]: g for g in dataset["groups"]}
    if (manifest["base_dataset_sha256"] != parent["dataset_sha256"]
            or manifest["base_annotations_sha256"] != annotations["annotations_sha256"]
            or manifest["base_task"] != parent["task"]
            or manifest["frozen_annotation_rows_sha256"] != digest(annotations["rows"])
            or manifest["frozen_ids"] != sorted(frozen)
            or set(manifest["added_ids"]) != combined.keys()-frozen.keys()
            or any(combined.get(sid) != row for sid, row in frozen.items())
            or any(groups.get(g["id"]) != g for g in parent["groups"])
            or any(not set(parent["summary"]["company_split"][s]) <= set(dataset["summary"]["company_split"][s])
                   for s in ("debug", "sealed"))
            or any(dataset["partition"]["event_group_split"].get(gid) != side
                   for gid, side in parent["partition"]["event_group_split"].items())):
        raise ValueError("Parent content, assignments or finalized provenance was changed")
