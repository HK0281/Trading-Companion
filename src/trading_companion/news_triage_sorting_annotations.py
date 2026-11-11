"""Independent human labels, recorded adjudication, and blinded paired review."""

from datetime import datetime, timezone
from pathlib import Path
import secrets

from trading_companion.news_triage_sorting_artifacts import read_json, write_json
from trading_companion.news_triage_sorting_replay import load_manifest, paired_summary

ANNOTATION = "sorting-important-items-v1"
SEAL = "sorting-annotation-seal-v1"
BLIND = "sorting-blind-review-v1"


def evidence(case):
    if case.expected_bundle is None or case.expected_plan is None:
        raise ValueError("Use complete v2 frozen packets")
    records = [e for company in case.expected_bundle["companies"] for e in company["evidence"]]
    return {"E"+str(i): record for i, record in enumerate(records, 1)}


def _text(value, *, empty=False):
    return (type(value) is str and len(value) <= 2000 and (bool(value.strip()) or empty)
            and value.strip().lower() not in ("todo", "tbd", "待填写", "待审核", "...") and all(
        c.isprintable() or c == "\n" for c in value)
            )


def _label(row, case, digest, *, adjudication=False):
    required = {"id", "packet_sha256", "completed", "none_reason", "items"}
    if adjudication:required.add("reason")
    if (type(row) is not dict or set(row) != required or row["id"] != case.id
            or row["packet_sha256"] != digest or row["completed"] is not True
            or type(row["items"]) is not list or len(row["items"]) > 30
            or not _text(row["none_reason"], empty=bool(row["items"]))
            or adjudication and not _text(row["reason"])):
        raise ValueError("Complete every human case, including an explicit reason for no important items")
    lookup, normalized = evidence(case), []
    for item in row["items"]:
        if (type(item) is not dict or set(item) != {"description", "evidence_ids"}
                or not _text(item["description"]) or type(item["evidence_ids"]) is not list
                or not 1 <= len(item["evidence_ids"]) <= 30
                or any(type(e) is not str or e not in lookup for e in item["evidence_ids"])
                or len(set(item["evidence_ids"])) != len(item["evidence_ids"])):
            raise ValueError("Important items require a description and original E references")
        refs = item["evidence_ids"]
        if (len({lookup[e]["instrument_id"] for e in refs}) != 1
                or not any(lookup[e]["kind"] in ("news", "announcements")
                           and lookup[e]["content_kind"] == "title" for e in refs)):
            raise ValueError("An important headline item must belong to one company and cite a title")
        normalized.append(dict(description=item["description"].strip(), evidence_ids=sorted(refs)))
    normalized.sort(key=lambda item:(item["evidence_ids"], item["description"]))
    if len({(v["description"], tuple(v["evidence_ids"])) for v in normalized}) != len(normalized):
        raise ValueError("Duplicate important item")
    return dict(id=case.id, packet_sha256=digest, completed=True,
                none_reason=row["none_reason"].strip(), items=normalized)


def _manifest(path):
    cases, plan = load_manifest(path)
    raw, digest = read_json(path)
    packets = {case.id: row["sha256"] for (case, _), row in zip(cases, raw["cases"], strict=True)}
    return cases, packets, digest


def write_templates(manifest):
    cases, packets, digest = _manifest(manifest)
    root = Path(manifest).parent
    blank = [dict(id=c.id, packet_sha256=packets[c.id], completed=False, none_reason="", items=[])
             for c, _ in cases]
    for who in ("codex", "claude"):
        write_json(root/("annotations-"+who+".template.json"),
                   dict(version=ANNOTATION, annotator=who, manifest_sha256=digest, cases=blank))
    write_json(root/"evidence-guide.json", dict(version="sorting-evidence-guide-v1", manifest_sha256=digest,
        cases=[dict(id=c.id, question=c.question, evidence=[dict(E=key, **value) for key, value in evidence(c).items()],
                    plan=c.expected_plan) for c, _ in cases]))


def _annotations(path, who, cases, packets, digest):
    data, checksum = read_json(path)
    if (type(data) is not dict or set(data) != {"version", "annotator", "manifest_sha256", "cases"}
            or data["version"] != ANNOTATION or data["annotator"] != who
            or data["manifest_sha256"] != digest or type(data["cases"]) is not list or len(data["cases"]) != 30):
        raise ValueError("Human annotations do not match the frozen manifest")
    rows = {row["id"]: row for row in data["cases"]}
    if set(rows) != {c.id for c, _ in cases}:
        raise ValueError("Human cases must occur exactly once")
    return {c.id: _label(rows[c.id], c, packets[c.id]) for c, _ in cases}, checksum


def seal_annotations(manifest, codex, claude, *, adjudication=None):
    cases, packets, digest = _manifest(manifest)
    left, left_hash = _annotations(codex, "codex", cases, packets, digest)
    right, right_hash = _annotations(claude, "claude", cases, packets, digest)
    conflicts = {key for key in left if left[key] != right[key]}
    resolutions, resolution_hash = {}, None
    if conflicts:
        if adjudication is None:
            raise ValueError("Human disagreement requires Claude's recorded adjudication")
        data, resolution_hash = read_json(adjudication)
        if (set(data) != {"version", "annotator", "manifest_sha256", "codex_sha256", "claude_sha256", "cases"}
                or data["version"] != "sorting-adjudication-v1" or data["annotator"] != "claude"
                or data["manifest_sha256"] != digest or data["codex_sha256"] != left_hash
                or data["claude_sha256"] != right_hash or len(data["cases"]) != len(conflicts)):
            raise ValueError("Adjudication must bind both independent inputs")
        resolutions = {row["id"]: row for row in data["cases"]}
        if set(resolutions) != conflicts:
            raise ValueError("Adjudicate exactly the disagreeing cases")
    elif adjudication is not None:
        raise ValueError("No extra adjudication when annotations already agree")
    consensus = []
    for case, _ in cases:
        consensus.append(_label(resolutions[case.id], case, packets[case.id], adjudication=True)
                         if case.id in conflicts else left[case.id])
    seal = dict(version=SEAL, manifest_sha256=digest, codex_sha256=left_hash, claude_sha256=right_hash,
        adjudication_sha256=resolution_hash, adjudications=list(resolutions.values()),
        frozen_at=datetime.now(timezone.utc).isoformat(), cases=consensus)
    if conflicts:
        with (Path(manifest).parent/"annotation-adjudication.json").open("xb") as stream:
            stream.write(Path(adjudication).read_bytes())
    write_json(Path(manifest).parent/"annotation-seal.json", seal)
    return seal


def load_seal(manifest, codex, claude):
    cases, packets, digest = _manifest(manifest)
    left, left_hash = _annotations(codex, "codex", cases, packets, digest)
    right, right_hash = _annotations(claude, "claude", cases, packets, digest)
    seal, checksum = read_json(Path(manifest).parent/"annotation-seal.json")
    if (set(seal) != {"version", "manifest_sha256", "codex_sha256", "claude_sha256", "adjudication_sha256",
                     "adjudications", "frozen_at", "cases"}
            or seal["version"] != SEAL or seal["manifest_sha256"] != digest
            or seal["codex_sha256"] != left_hash or seal["claude_sha256"] != right_hash
            or len(seal["cases"]) != 30):
        raise ValueError("Human freeze has changed")
    rows = {row["id"]: row for row in seal["cases"]}
    conflicts = {key for key in left if left[key] != right[key]}
    adjudications = {row["id"]: row for row in seal["adjudications"]}
    if (set(rows) != set(left) or set(adjudications) != conflicts
            or len(adjudications) != len(seal["adjudications"])
            or bool(conflicts) != bool(seal["adjudication_sha256"])):
        raise ValueError("Human resolution is incomplete")
    if conflicts:
        original, original_hash = read_json(Path(manifest).parent/"annotation-adjudication.json")
        if (original_hash != seal["adjudication_sha256"] or original["annotator"] != "claude"
                or original["version"] != "sorting-adjudication-v1" or original["manifest_sha256"] != digest
                or original["codex_sha256"] != left_hash or original["claude_sha256"] != right_hash
                or original["cases"] != seal["adjudications"]):
            raise ValueError("Original adjudication record changed")
    result = {}
    for case, _ in cases:
        final = _label(rows[case.id], case, packets[case.id])
        expected = _label(adjudications[case.id], case, packets[case.id], adjudication=True) if case.id in conflicts else left[case.id]
        if final != expected:
            raise ValueError("Frozen consensus differs from independent labels or adjudication")
        result[case.id] = final["items"]
    return result, checksum


def _public_arm(original):
    if not original["contexts"] or original["bundle"] is None or original["answer"] is None:
        raise ValueError("Missing answer or evidence in blind pair")
    answer = original["answer"]
    return dict(answer={k:answer[k] for k in ("text", "narratives", "decisions") if k in answer},
                selected_E_ids=sorted(r["id"] for r in original["contexts"][0]["evidence"]))


def _public_pair(pair, public_id, order):
    records = [e for c in pair["arms"]["off"]["bundle"]["companies"] for e in c["evidence"]]
    return dict(id=public_id, question=pair["question"], important_items=pair["important_items"],
                evidence=[dict(E="E"+str(j), **e) for j, e in enumerate(records, 1)],
                arms={name:_public_arm(pair["arms"][side]) for name, side in zip(("A", "B"), order, strict=True)})


def write_blind(report_path, output, *, chooser=secrets.randbelow):
    report, report_hash = read_json(report_path, limit=128*1024*1024)
    if (report.get("status") != "awaiting_blind_review" or len(report["rows"]) != 30
            or any(set(r["arms"]) != {"on", "off"} for r in report["rows"])):
        raise ValueError("A complete sixty-turn report is required for blind review")
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    rows, mappings, blanks = [], [], []
    for i, pair in enumerate(report["rows"], 1):
        public_id = "blind-"+str(i).zfill(3)
        order = ("on", "off") if chooser(2) else ("off", "on")
        rows.append(_public_pair(pair, public_id, order))
        mappings.append(dict(id=public_id, case_id=pair["id"], A=order[0], B=order[1]))
        blank = dict(completed=False, omissions=[], cross_company_errors=0, citation_errors=0, comments="")
        blanks.append(dict(id=public_id, A=dict(blank), B=dict(blank)))
    table_hash = write_json(root/"blind-table.json", dict(version=BLIND, rows=rows))
    mapping_hash = write_json(root/"private-mapping.json", dict(version=BLIND, report_sha256=report_hash,
        table_sha256=table_hash, mappings=mappings))
    write_json(root/"blind-review.template.json", dict(version=BLIND, table_sha256=table_hash,
                                                     mapping_sha256=mapping_hash, rows=blanks))
    # An ordinary browser can render the structured table without revealing private metadata.
    import html
    page = "<!doctype html><meta charset=utf-8><title>回答盲审</title>"
    for row in rows:
        page += "<h2>"+html.escape(row["id"])+"</h2><p>"+html.escape(row["question"])+"</p><table><tr>"
        import json
        for arm in ("A", "B"):
            page += "<td style='vertical-align:top;width:50%'><h3>"+arm+"</h3><pre style='white-space:pre-wrap'>"+html.escape(json.dumps(row["arms"][arm],ensure_ascii=False,indent=2))+"</pre></td>"
        page += "</tr></table>"
        page += "<details><summary>重要事项与共同原始证据</summary><pre style='white-space:pre-wrap'>"+html.escape(json.dumps(
            dict(important_items=row["important_items"], evidence=row["evidence"]),ensure_ascii=False,indent=2))+"</pre></details>"
    with (root/"blind-table.html").open("x", encoding="utf-8") as stream:stream.write(page)
    return table_hash


def summarize_blind(report_path, mapping_path, table_path, reviews_path):
    report, report_hash = read_json(report_path, limit=128*1024*1024)
    mapping, mapping_hash = read_json(mapping_path)
    table, table_hash = read_json(table_path, limit=128*1024*1024)
    reviews, _ = read_json(reviews_path)
    if (mapping["version"] != BLIND or table["version"] != BLIND or reviews["version"] != BLIND
            or mapping["report_sha256"] != report_hash or mapping["table_sha256"] != table_hash
            or reviews["table_sha256"] != table_hash or reviews["mapping_sha256"] != mapping_hash):
        raise ValueError("Blind review binding changed")
    pairs = {r["id"]: r for r in report["rows"]}
    rows = {r["id"]: r for r in reviews["rows"]}
    maps = {r["id"]: r for r in mapping["mappings"]}
    public = {r["id"]: r for r in table["rows"]}
    if (len(rows) != len(reviews["rows"]) or len(rows) != 30 or set(rows) != set(maps)
            or len(maps) != len(mapping["mappings"]) or len(pairs) != 30
            or len(public) != len(table["rows"]) or set(public) != set(maps)
            or {m["case_id"] for m in maps.values()} != set(pairs)):
        raise ValueError("Review every blind pair exactly once")
    converted = {}
    for public_id, m in maps.items():
        if {m["A"], m["B"]} != {"on", "off"}:raise ValueError("Invalid blind mapping")
        pair = pairs[m["case_id"]]
        if public[public_id] != _public_pair(pair, public_id, (m["A"],m["B"])):
            raise ValueError("Blind mapping does not reproduce the public answers and evidence")
        sides = {}
        for arm in ("A", "B"):
            item = rows[public_id][arm]
            if (set(item) != {"completed", "omissions", "cross_company_errors", "citation_errors", "comments"}
                    or item["completed"] is not True or type(item["omissions"]) is not list
                    or any(type(i) is not int or not 0 <= i < len(pair["important_items"]) for i in item["omissions"])
                    or len(set(item["omissions"])) != len(item["omissions"])
                    or any(type(item[k]) is not int or item[k]<0 for k in ("cross_company_errors", "citation_errors"))
                    or not _text(item["comments"], empty=not (item["omissions"] or item["cross_company_errors"] or item["citation_errors"]))):
                raise ValueError("Complete omissions and error review for both blinded answers")
            sides[m[arm]] = item
        before = {r["id"]:i for i, r in enumerate(pair["arms"]["off"]["contexts"][0]["evidence"])}
        after = {r["id"]:i for i, r in enumerate(pair["arms"]["on"]["contexts"][0]["evidence"])}
        promoted = sum(any(e in after and (e not in before or after[e]<before[e]) for e in refs)
                       for _, refs in pair["important_items"])
        converted[pair["id"]] = dict(baseline_omissions=[pair["important_items"][i][0] for i in sides["off"]["omissions"]],
            new_omissions=len(set(sides["on"]["omissions"])-set(sides["off"]["omissions"])),
            cross_company_errors=sum(s["cross_company_errors"] for s in sides.values()),
            citation_errors=sum(s["citation_errors"] for s in sides.values()), important_promoted=promoted)
    return dict(summary=paired_summary(report, converted), reviews=converted, enablement=False)
