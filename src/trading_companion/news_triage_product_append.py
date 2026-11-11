"""Append real candidates with frozen parents and bounded, label-blind company search."""

from collections import Counter, defaultdict
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import random

from trading_companion.news_triage_product import descriptor, digest
from trading_companion.news_triage_product_data import (
    DATA_VERSION, _validate_dataset, event_features, event_link, group_rows, load_captures,
)
from trading_companion.news_triage_product_parent import load_parent_pair, validate_frozen
from trading_companion.news_triage_product_split import (
    MAX_RATIO_GAP, SPLITS, TARGETS, balance_report, board_for, company_components,
)
from trading_companion.security_directory import load_security_directory

APPEND_VERSION = "news-triage-product-append-v1"
NEW_NATURAL_LIMIT = 150


def _link_parents(rows, groups, parent):
    old = {m["id"]: (m, g) for g in parent["groups"] for m in g["members"]}
    features = {sid: event_features(m) for sid, (m, _) in old.items()}
    membership = {m["id"]: g["id"] for g in groups for m in g["members"]}
    linked, covered, links = defaultdict(set), set(), []
    selected = {(r["group_id"], r["instrument_id"]) for r in parent["rows"]}
    for row in rows:
        current = event_features(row)
        for sid, (prior, group) in old.items():
            reason = event_link(row, prior, current, features[sid])
            if reason:
                gid = membership[row["id"]]
                linked[gid].update(m["instrument_id"] for m in group["members"])
                covered.update((gid, code) for group_id, code in selected if group_id == group["id"])
                links.append({"new_id": row["id"], "parent_id": sid, "parent_group_id": group["id"], "reason": reason})
    return linked, covered, links


def _select(ordered, assignment, excluded, frozen):
    added, counts = [], {}
    for side in SPLITS:
        old_counts = Counter(r["stratum"] for r in frozen if r["split"] == side)
        pool = [r for r in ordered if r["group_id"] not in excluded and assignment[r["instrument_id"]] == side]
        natural = pool[:NEW_NATURAL_LIMIT]
        taken = {r["id"] for r in natural}
        hard_pool = [r for r in pool if r["id"] not in taken and r["hard_reasons"]]
        hard_limit = max(0, TARGETS["hard"]-old_counts["hard"])
        hard = hard_pool[:hard_limit]
        for stratum, chosen in (("natural", natural), ("hard", hard)):
            added.extend({**r, "stratum": stratum, "split": side} for r in chosen)
        counts[side] = {
            "natural": {"parent": old_counts["natural"], "new_limit": NEW_NATURAL_LIMIT,
                        "available_new": len(pool), "added": len(natural), "selected": old_counts["natural"]+len(natural)},
            "hard": {"target": TARGETS["hard"], "parent": old_counts["hard"], "available_new": len(hard_pool),
                     "added": len(hard), "selected": old_counts["hard"]+len(hard),
                     "shortfall": max(0, hard_limit-len(hard))}}
    return added, counts


def append_split(representatives, groups, bindings, parent):
    frozen = parent["rows"]
    fixed = {code: side for side in SPLITS for code in parent["summary"]["company_split"][side]}
    companies = sorted(set(fixed) | {m["instrument_id"] for g in groups for m in g["members"]})
    constraints = [{"id": g["id"], "members": [{"instrument_id": code} for code in sorted(
                    {m["instrument_id"] for m in g["members"]} | bindings.get(g["id"], set()))]} for g in groups]
    new_group_ids = {g["id"] for g in groups}
    constraints.extend(g for g in parent["groups"] if g["id"] not in parent["summary"]["excluded_groups"])
    units = company_components(companies, constraints, set())
    conflicting = {code for unit in units if len({fixed[c] for c in unit if c in fixed}) > 1 for code in unit}
    excluded = {g["id"] for g in constraints if g["id"] in new_group_ids and len(g["members"]) > 1
                and any(m["instrument_id"] in conflicting for m in g["members"])}
    units = company_components(companies, constraints, excluded)
    forced = {}
    for index, unit in enumerate(units):
        sides = {fixed[c] for c in unit if c in fixed}
        if len(sides) > 1:
            raise ValueError("Conflicting fixed company component")
        if sides:
            forced[index] = SPLITS.index(sides.pop())
    free = [i for i in range(len(units)) if i not in forced]
    ordered = sorted(representatives, key=lambda r: (digest(r["normalized_title"]), r["instrument_id"], r["published_date"], r["id"]))
    features = []
    for unit in units:
        values = Counter({"companies": len(unit)})
        values.update("board:"+board_for(c) for c in unit)
        values.update("source:"+r["source"] for r in ordered if r["instrument_id"] in unit and r["group_id"] not in excluded)
        features.append(values)
    total = sum(features, Counter())

    def candidate(bits):
        assignment = {code: SPLITS[bits[i]] for i, unit in enumerate(units) for code in unit}
        added, counts = _select(ordered, assignment, excluded, frozen)
        balance = balance_report([*frozen, *added], assignment)
        gaps = balance["gaps"]
        score = (not balance["passed"], round(sum(max(0, g-MAX_RATIO_GAP) for g in gaps.values()), 10),
                 abs(sum(s == "debug" for s in assignment.values())-len(companies)/2),
                 round(gaps["company_boards"], 10), round(sum(gaps.values()), 10), -len(added))
        return score, added, counts, balance, assignment

    rng, best = random.Random(20261110), None
    for _ in range(64):
        bits, bins = [0]*len(units), [Counter(), Counter()]
        for index, side in forced.items():
            bits[index] = side
            bins[side].update(features[index])
        order = free.copy()
        rng.shuffle(order)
        order.sort(key=lambda i: -len(units[i]))
        for index in order:
            losses = [sum((((bins[s]+features[index])[k]-bins[1-s][k])/max(1, total[k]))**2 for k in total)
                      for s in (0, 1)]
            side = 0 if losses[0] < losses[1] else 1 if losses[1] < losses[0] else rng.randrange(2)
            bits[index] = side
            bins[side].update(features[index])
        result = candidate(bits)
        if best is None or result[0] < best[0]:
            best, best_bits = result, bits
    for _ in range(512 if free else 0):
        bits = best_bits.copy()
        for index in rng.sample(free, min(len(free), rng.choice((1, 2)))):
            bits[index] = 1-bits[index]
        result = candidate(bits)
        if result[0] < best[0]:
            best, best_bits = result, bits
    _, added, counts, balance, assignment = best
    group_assignment = {g["id"]: "excluded" if g["id"] in excluded else assignment[g["members"][0]["instrument_id"]]
                        for g in groups}
    return added, {"split_version": "news-triage-company-append-split-v1",
                   "company_split": {s: sorted(c for c, side in assignment.items() if side == s) for s in SPLITS},
                   "splits": counts, "balance": balance, "company_components": units,
                   "event_group_split": group_assignment, "excluded_groups": sorted(excluded),
                   "exclusion_reason": "new_shared_event_conflicts_with_fixed_parent_sides" if excluded else None,
                   "excluded_representatives": sum(r["group_id"] in excluded for r in representatives),
                   "policy": "frozen_parent_new_natural_up_to_150_then_remaining_real_hard_no_gold_selection"}


def build_append_dataset(capture_dir, directory_path, base_dataset, base_annotations):
    parent, annotations = load_parent_pair(base_dataset, base_annotations)
    directory = load_security_directory(directory_path)
    directory_digest = sha256(Path(directory_path).read_bytes()).hexdigest()
    if (directory.is_fixture != parent["directory_is_fixture"]
            or parent.get("directory_sha256", directory_digest) != directory_digest):
        raise ValueError("Append must use the same trusted security directory")
    raw, inputs = load_captures(capture_dir, directory)
    old_ids = {m["id"] for g in parent["groups"] for m in g["members"]}
    unique = {r["id"]: r for r in raw if r["id"] not in old_ids}
    new = sorted(unique.values(), key=lambda r: r["id"])
    representatives, groups, links = group_rows(new)
    bindings, covered, parent_links = _link_parents(new, groups, parent)
    original_representatives = len(representatives)
    representatives = [r for r in representatives if (r["group_id"], r["instrument_id"]) not in covered]
    if {g["id"] for g in groups} & {g["id"] for g in parent["groups"]}:
        raise ValueError("New group ID collides with frozen parent")
    added, report = append_split(representatives, groups, bindings, parent)
    output = deepcopy(parent)
    output.update(version=DATA_VERSION, task=descriptor(), rows=[*deepcopy(parent["rows"]), *added],
                  groups=[*deepcopy(parent["groups"]), *groups], group_links=[*parent.get("group_links", []), *links],
                  directory_is_fixture=directory.is_fixture)
    output["inputs"] = [*parent.get("inputs", []), *inputs]
    batches = []
    for batch in sorted({item["batch"] for item in output["inputs"]}):
        records = [item for item in output["inputs"] if item["batch"] == batch]
        batches.append({"id": batch, "directory_name": records[0]["directory_name"], "files": len(records),
                        "raw_titles": sum(item["titles"] for item in records),
                        "companies": sorted({code for item in records for code in item["instruments"]})})
    output["capture_batches"] = batches
    output["partition"] = {"company_components": report.pop("company_components"),
                           "event_group_split": {**parent["partition"]["event_group_split"], **report.pop("event_group_split")}}
    report["excluded_groups"] = sorted(set(report["excluded_groups"]) | set(parent["summary"]["excluded_groups"]))
    report["excluded_representatives"] += parent["summary"].get("excluded_representatives", 0)
    output["summary"] = {"selected": len(output["rows"]), "parent_selected": len(parent["rows"]),
        "added": len(added), "raw_titles_new": len(raw), "unique_new_titles": len(new),
        "raw_titles": parent["summary"].get("raw_titles", len(old_ids))+len(raw), "capture_batches": len(batches),
        "event_groups": len(output["groups"]),
        "exact_old_duplicates_skipped": sum(r["id"] in old_ids for r in raw),
        "covered_representatives_skipped": original_representatives-len(representatives), "new_event_groups": len(groups),
        "human_gate_support": {key: {"required": 30, "available_after_annotation": None, "status": "await_new_annotation"}
                               for key in ("importance_high", "tone_positive", "tone_negative")}, **report}
    output["append"] = {"version": APPEND_VERSION, "base_dataset_sha256": parent["dataset_sha256"],
        "base_annotations_sha256": annotations["annotations_sha256"], "base_task": parent["task"],
        "frozen_annotation_rows_sha256": digest(annotations["rows"]),
        "frozen_ids": sorted(r["id"] for r in parent["rows"]), "added_ids": sorted(r["id"] for r in added),
        "parent_links": parent_links, "new_natural_limit_per_split": NEW_NATURAL_LIMIT,
        "new_company_bindings": {gid: sorted(codes) for gid, codes in sorted(bindings.items())}}
    output["group_review"] = "frozen_parent_new_groups_pending_independent_confirmation"
    output["dataset_sha256"] = digest({k: v for k, v in output.items() if k != "dataset_sha256"})
    validate_frozen(output, parent, annotations)
    if report["balance"]["passed"]:
        _validate_dataset(output, descriptor())
    return output
