"""Bounded local preparation of real captures, conservative event groups and blind sheets."""

from collections import Counter, defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata
from uuid import uuid4

from trading_companion.news_triage_product import INSTRUCTIONS, descriptor, digest, identity_precheck, normalized, questions
from trading_companion.company_research_contracts import SHANGHAI
from trading_companion.news_triage_shadow import MAX_RECORD_BYTES, MAX_RECORDS, MAX_RUN_BYTES, VERSION
from trading_companion.news_triage_product_split import balance_report, split_rows
from trading_companion.security_directory import load_security_directory

DATA_VERSION = "news-triage-product-dataset-v2"
GROUP_VERSION = "news-triage-event-groups-v1"
# These are selection hints, never gold labels. No model results enter selection.
HARD_PATTERNS = {
    "negation_or_rumor": r"否认|澄清|传闻|不属实|不存在|无.*计划",
    "mixed_or_uncertain": r"但|然而|疑似|预估|是否|会.*吗|\?|？|风险评级|风险.*评级",
    "importance_ambiguous": r"合作|备忘录|进展|诉讼|受理|分行.*罚|支行.*罚",
}
EVENT_PATTERNS = {
    "buyback": r"回购", "earnings": r"季度报告|三季报|季报|净利润|业绩|营收|营业收入",
    "penalty": r"被罚|罚款|罚单|处罚|警告", "pledge": r"质押",
    "rating": r"评级|目标价", "block_trade": r"大宗交易", "patent": r"专利",
    "cooperation": r"合作|备忘录", "drug_approval": r"药.*批准|药.*受理|药品注册",
    "bond": r"债券|资本债|中期票据", "shareholder": r"股东会|股东大会",
}


def read_json(path, limit=10 * 1024 * 1024):
    raw = Path(path).read_bytes()
    if len(raw) > limit:
        raise ValueError("Local JSON exceeds the bounded input size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def load_captures(capture_dir, directory):
    requested = capture_dir if isinstance(capture_dir, (list, tuple)) else [capture_dir]
    roots = sorted(Path(root).resolve() for root in requested)
    if not 1 <= len(roots) <= 4 or len(set(roots)) != len(roots):
        raise ValueError("Use one to four distinct capture directories")
    files = []
    for root in roots:
        batch_files = sorted(root.glob("[0-9]*.json"))
        if not batch_files or len(batch_files) > MAX_RECORDS:
            raise ValueError("Each capture directory must contain bounded numbered records")
        files.extend(batch_files)
    if len(files) > 2*MAX_RECORDS or sum(f.stat().st_size for f in files) > 2*MAX_RUN_BYTES:
        raise ValueError("Use a bounded directory of numbered title captures")
    rows, inputs = [], []
    for path in files:
        data = read_json(path, MAX_RECORD_BYTES)
        if (data["version"] != VERSION or data["model_calls"] != 0 or data["origin"] not in ("collection", "research")
                or not 1 <= len(data["companies"]) <= 3 or not 0 <= data["title_count"] <= 30):
            raise ValueError("Invalid checked title capture")
        batch = "B"+digest(str(path.parent))[:16]
        inputs.append({"batch": batch, "directory_name": path.parent.name, "file": path.name,
                       "sha256": sha256(path.read_bytes()).hexdigest(), "titles": data["title_count"],
                       "instruments": sorted(c["instrument_id"] for c in data["companies"])})
        count = 0
        for company in data["companies"]:
            available = {(r["kind"], r["source"]) for r in company["source_results"]
                         if r["status"] in ("available", "partial")}
            for title in company["titles"]:
                text = title["title"]
                kind, source = title["kind"], title["source"]
                allowed = {"news": ("sina_company_news",),
                           "announcements": ("eastmoney_announcements", "cninfo_announcements")}
                published = date.fromisoformat(title["published_date"])
                cutoff = datetime.fromisoformat(data["as_of"])
                if (not isinstance(text, str) or not 0 < len(text) <= 600 or not text.isprintable()
                        or title["instrument_id"] != company["instrument_id"] or source not in allowed.get(kind, ())
                        or (kind, source) not in available or cutoff.tzinfo is None
                        or not date.fromisoformat(data["publication_start"]) <= published <= cutoff.astimezone(SHANGHAI).date()
                        or title["title_sha256"] != sha256(" ".join(
                            unicodedata.normalize("NFKC", text).casefold().split()).encode("utf-8")).hexdigest()):
                    raise ValueError("Invalid capture title or provenance")
                row = {key: title[key] for key in ("instrument_id", "kind", "source", "title", "url", "published_date",
                                                  "published_at", "publication_precision", "retrieved_at", "evidence_id")}
                row.update(company_name=company["security_name"], capture_identity_checked=True,
                           capture_file=batch+"/"+path.name, capture_batch=batch, normalized_title=normalized(text))
                row["id"] = "P" + digest({k: row[k] for k in ("instrument_id", "source", "published_date", "normalized_title")})[:20]
                row["identity"] = identity_precheck(row, directory)
                rows.append(row)
                if len(rows) > 2000:
                    raise ValueError("At most 2000 raw titles per preparation")
                count += 1
        if count != data["title_count"]:
            raise ValueError("Capture title count mismatch")
    return rows, inputs


def hard_reasons(row):
    identity = row["identity"]
    reasons = [name for name, flag in (("identity_collision", identity["collision_suspected"]),
                                       ("multi_subject", identity["multi_subject"]),
                                       ("number_collision", identity["number_collision"]),
                                       ("industry_title", identity["industry_title"]),
                                       ("identity_insufficient", not identity["credible_target_evidence"])) if flag]
    reasons.extend(name for name, pattern in HARD_PATTERNS.items() if re.search(pattern, row["title"]))
    return sorted(set(reasons))


def _event_text(row):
    text = row["normalized_title"]
    # Strip only a leading, trusted issuer prefix for same-issuer comparisons.
    for name in sorted((row["company_name"], *row["identity"]["local_aliases"]), key=len, reverse=True):
        if text.startswith(normalized(name)):
            text = text[len(normalized(name)):].lstrip(":：-—")
            break
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text)


def group_rows(rows):
    """Union conservative links across sources/issuers; variants never straddle splits."""
    unique = {r["id"]: r for r in rows}
    records = sorted(unique.values(), key=lambda r: r["id"])
    parent = list(range(len(records)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    features = []
    for row in records:
        text = _event_text(row)
        features.append((text, {text[i:i+2] for i in range(len(text)-1)},
                         {k for k, p in EVENT_PATTERNS.items() if re.search(p, text)},
                         {row["instrument_id"], *row["identity"]["other_instruments"]}))
    links = []
    for i, left in enumerate(records):
        a, grams_a, events_a, issuers_a = features[i]
        for j in range(i):
            right = records[j]
            b, grams_b, events_b, issuers_b = features[j]
            days = abs((date.fromisoformat(left["published_date"]) - date.fromisoformat(right["published_date"])).days)
            same_issuer = left["instrument_id"] == right["instrument_id"]
            reason = None
            if left["normalized_title"] == right["normalized_title"]:
                reason = "identical_title_all_dates"
            elif left["url"] == right["url"]:
                reason = "same_source_article"
            elif days <= 7 and same_issuer and events_a & events_b:
                # Intentionally overgroup repeated corporate-event families. This loses
                # sample count rather than treating rewrites as independent evidence.
                reason = "same_issuer_event_family_7d"
            elif days <= 7 and (same_issuer or issuers_a & issuers_b):
                overlap = len(grams_a & grams_b) / max(1, min(len(grams_a), len(grams_b)))
                if min(len(a), len(b)) >= 10 and (overlap >= .64 or SequenceMatcher(None, a, b).ratio() >= .70):
                    reason = "near_title_shared_issuer_7d"
            if reason:
                parent[root(i)] = root(j)
                links.append({"left": left["id"], "right": right["id"], "reason": reason})
    groups = defaultdict(list)
    for i, row in enumerate(records):
        groups[root(i)].append(row)
    output, representatives = [], []
    for members in groups.values():
        gid = "G" + digest(sorted(r["id"] for r in members))[:20]
        by_issuer = defaultdict(list)
        for row in members:
            by_issuer[row["instrument_id"]].append(row)
        for issuer_members in by_issuer.values():
            chosen = sorted(issuer_members, key=lambda r: (r["published_date"], r["source"], r["id"]))[0].copy()
            # A hard variant must not turn a plain representative into a hard title.
            hints = hard_reasons(chosen)
            chosen.update(group_id=gid, hard_reasons=hints, stratum="hard" if hints else "natural",
                          variant_ids=sorted(r["id"] for r in issuer_members))
            representatives.append(chosen)
        output.append({"id": gid, "min_date": min(r["published_date"] for r in members),
                       "max_date": max(r["published_date"] for r in members),
                       "members": sorted(members, key=lambda r: r["id"]),
                       "representatives": len(by_issuer)})
    return sorted(representatives, key=lambda r: r["id"]), sorted(output, key=lambda g: g["id"]), links


def build_dataset(capture_dir, directory_path):
    directory = load_security_directory(directory_path)
    rows, inputs = load_captures(capture_dir, directory)
    if not rows:
        raise ValueError("No real titles available")
    representatives, groups, links = group_rows(rows)
    selected, split = split_rows(representatives, groups)
    partition = {key: split.pop(key) for key in ("company_components", "event_group_split")}
    categories = ("identity_collision", "number_collision", "multi_subject", "industry_title", "identity_insufficient",
                  *HARD_PATTERNS)
    hints = Counter(h for r in representatives for h in r["hard_reasons"])
    batches = []
    for batch in sorted({f["batch"] for f in inputs}):
        records = [f for f in inputs if f["batch"] == batch]
        batches.append({"id": batch, "directory_name": records[0]["directory_name"], "files": len(records),
                        "raw_titles": sum(f["titles"] for f in records),
                        "companies": sorted({code for f in records for code in f["instruments"]})})
    dataset = {"version": DATA_VERSION, "task": descriptor(), "group_version": GROUP_VERSION,
               "authenticity": "captured_public_titles", "directory_is_fixture": directory.is_fixture,
               "directory_sha256": sha256(Path(directory_path).read_bytes()).hexdigest(),
               "inputs": inputs, "capture_batches": batches, "groups": groups, "group_links": links,
               "partition": partition, "rows": selected,
               "summary": {"raw_titles": len(rows), "unique_source_date_issuer_titles": len({r["id"] for r in rows}),
                           "capture_batches": len(batches),
                           "event_groups": len(groups), "available_representatives": len(representatives),
                           "available_hard": sum(r["stratum"] == "hard" for r in representatives),
                           "available_natural": sum(r["stratum"] == "natural" for r in representatives),
                           "selected": len(selected), "shortfall_from_400": 400-len(selected),
                           "hard_selection_hints": {category: hints[category] for category in categories},
                           "selected_hard_hints": {s: {category: sum(category in r["hard_reasons"] for r in selected if r["split"] == s)
                                                       for category in categories} for s in ("debug", "sealed")},
                           "human_gate_support": {key: {"required": need, "verified": 0, "verified_shortfall": need,
                                                         "available_after_annotation": None, "status": "not_annotated"}
                                                  for key, need in (("importance_high", 30), ("tone_positive", 30),
                                                                    ("tone_negative", 30), ("proposed_filter", 50))},
                           "program_mismatch_candidates": sum(bool(r["identity"]["mismatch_basis"]) for r in representatives),
                           **split},
               "group_review": "pending_independent_human_confirmation",
               "limitations": ["Conservative event-family grouping may merge distinct events.",
                               "Heuristic grouping cannot certify semantic equivalence; both reviewers must check groups.",
                               "Hard strata are program selection hints, not human labels or guaranteed label support.",
                               "Company/board/source stratification is heuristic, not a whole-market prevalence sample.",
                               "Human gate support is unknown; program hints are not gold labels or per-category quotas."]}
    dataset["dataset_sha256"] = digest(dataset)
    return dataset


def load_dataset(path):
    return _validate_dataset(read_json(path), descriptor())


def _validate_dataset(dataset, expected_task):
    """Shared integrity checks; the public loader always requires the current task."""
    body = {k: v for k, v in dataset.items() if k != "dataset_sha256"}
    if (dataset["version"] != DATA_VERSION or dataset["dataset_sha256"] != digest(body)
            or dataset["task"] != expected_task or not dataset["rows"]
            or len({r["id"] for r in dataset["rows"]}) != len(dataset["rows"])):
        raise ValueError("Dataset integrity or rule version mismatch; prepare again")
    debug = {r["group_id"] for r in dataset["rows"] if r["split"] == "debug"}
    sealed = {r["group_id"] for r in dataset["rows"] if r["split"] == "sealed"}
    if debug & sealed:
        raise ValueError("Event group leakage")
    company_split = dataset["summary"]["company_split"]
    if set(company_split["debug"]) & set(company_split["sealed"]):
        raise ValueError("Company leakage")
    assignment = {code: side for side in ("debug", "sealed") for code in company_split[side]}
    group_assignment = dataset["partition"]["event_group_split"]
    excluded = set(dataset["summary"]["excluded_groups"])
    if (group_assignment.keys() != {g["id"] for g in dataset["groups"]}
            or set(assignment) != {m["instrument_id"] for g in dataset["groups"] for m in g["members"]}):
        raise ValueError("Incomplete company or event assignments")
    for group in dataset["groups"]:
        sides = {assignment[m["instrument_id"]] for m in group["members"]}
        if group["id"] in excluded:
            valid = group_assignment[group["id"]] == "excluded"
        else:
            valid = len(sides) == 1 and group_assignment[group["id"]] in sides
        if not valid:
            raise ValueError("Cross-company event assignment mismatch")
    for row in dataset["rows"]:
        if (assignment.get(row["instrument_id"]) != row["split"]
                or group_assignment[row["group_id"]] != row["split"] or row["group_id"] in excluded):
            raise ValueError("Company assignment mismatch")
    if not balance_report(dataset["rows"], assignment)["passed"]:
        raise ValueError("Source or board imbalance")
    return dataset


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def prepare(capture_dir, directory_path, output_root):
    from trading_companion.news_triage_product_annotations import write_blind_sheets
    dataset = build_dataset(capture_dir, directory_path)
    if not dataset["summary"]["balance"]["passed"]:
        return {"status": "imbalanced", "summary": dataset["summary"], "output_written": False,
                "model_calls": 0, "annotations_filled": 0}
    target = Path(output_root) / uuid4().hex
    target.mkdir(parents=True, exist_ok=False)
    write_json(target / "dataset.json", dataset)
    write_json(target / "task.json", {"task": dataset["task"], "instructions": INSTRUCTIONS,
        "questions": questions(),
        "human_abstention": "unresolved", "review_policy": "independent_labels_then_user_adjudication"})
    write_blind_sheets(dataset, target)
    return {"status": "prepared", "directory": str(target), "dataset_sha256": dataset["dataset_sha256"],
            "summary": dataset["summary"], "model_calls": 0, "annotations_filled": 0}
