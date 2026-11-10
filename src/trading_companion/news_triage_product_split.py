"""Deterministic company-component stratification, independent of dates and gold labels."""

from collections import Counter
import random

from trading_companion.news_triage_product import digest

SPLIT_VERSION = "news-triage-company-split-v1"
MAX_RATIO_GAP = .15
TARGETS = {"natural": 130, "hard": 70}
SPLITS = ("debug", "sealed")


def board_for(instrument):
    if instrument.endswith(".SH"):
        return "star" if instrument.startswith(("688", "689")) else "sh_main"
    if instrument.endswith(".SZ"):
        return "chinext" if instrument.startswith(("300", "301")) else "sz_main"
    raise ValueError("Unsupported pilot exchange board")


def company_components(companies, groups, excluded):
    parent = {code: code for code in companies}
    def root(code):
        while code != parent[code]:
            parent[code] = parent[parent[code]]
            code = parent[code]
        return code
    for group in groups:
        if group["id"] in excluded:
            continue
        issuers = sorted({m["instrument_id"] for m in group["members"]})
        for code in issuers[1:]:
            parent[root(code)] = root(issuers[0])
    units = {}
    for code in companies:
        units.setdefault(root(code), []).append(code)
    return sorted((sorted(unit) for unit in units.values()), key=lambda unit: digest(unit))


def select_rows(ordered_rows, assignment, excluded):
    selected, report = [], {}
    for split in SPLITS:
        pool = [r for r in ordered_rows if r["group_id"] not in excluded and assignment[r["instrument_id"]] == split]
        natural = pool[:TARGETS["natural"]]
        taken = {r["id"] for r in natural}
        hard_pool = [r for r in pool if r["id"] not in taken and r["hard_reasons"]]
        report[split] = {}
        for stratum, available, chosen in (("natural", pool, natural), ("hard", hard_pool, hard_pool[:TARGETS["hard"]])):
            selected.extend({**r, "stratum": stratum, "split": split} for r in chosen)
            report[split][stratum] = {"target": TARGETS[stratum], "available": len(available),
                                       "selected": len(chosen), "shortfall": max(0, TARGETS[stratum]-len(chosen))}
    return selected, report


def distribution(values):
    counts = Counter(values)
    total = sum(counts.values())
    return {"counts": dict(sorted(counts.items())), "total": total,
            "ratios": {key: value/total for key, value in sorted(counts.items())}}


def balance_report(rows, assignment):
    per_split = {}
    for split in SPLITS:
        chosen = [r for r in rows if r["split"] == split]
        codes = sorted(code for code, side in assignment.items() if side == split)
        per_split[split] = {"company_count": len(codes), "titles": len(chosen),
                            "kinds": distribution(r["kind"] for r in chosen),
                            "sources": distribution(r["source"] for r in chosen),
                            "company_boards": distribution(board_for(code) for code in codes),
                            "title_boards": distribution(board_for(r["instrument_id"]) for r in chosen)}
    gaps = {}
    for field in ("kinds", "sources", "company_boards", "title_boards"):
        a, b = (per_split[split][field]["ratios"] for split in SPLITS)
        gaps[field] = max((abs(a.get(key, 0)-b.get(key, 0)) for key in a.keys() | b.keys()), default=0)
    populated = all(per_split[s]["titles"] and per_split[s]["company_count"] for s in SPLITS)
    return {"passed": bool(populated and all(gap <= MAX_RATIO_GAP+1e-12 for gap in gaps.values())),
            "max_allowed_gap": MAX_RATIO_GAP, "gaps": gaps, "splits": per_split,
            "policy": "fixed_15_percentage_points_selected_kinds_sources_company_boards_title_boards"}


def _search(rows, groups, companies, excluded):
    units = company_components(companies, groups, excluded)
    by_company = {code: [r for r in rows if r["instrument_id"] == code and r["group_id"] not in excluded]
                  for code in companies}
    features = []
    for unit in units:
        counter = Counter({"companies": len(unit)})
        counter.update("board:"+board_for(code) for code in unit)
        counter.update("source:"+r["source"] for code in unit for r in by_company[code])
        counter["titles"] = sum(len(by_company[code]) for code in unit)
        features.append(counter)
    total = sum(features, Counter())
    ordered = sorted(rows, key=lambda r: (digest(r["normalized_title"]), r["instrument_id"], r["published_date"], r["id"]))
    def candidate(bits):
        assignment = {code: SPLITS[bits[i]] for i, unit in enumerate(units) for code in unit}
        selected, counts = select_rows(ordered, assignment, excluded)
        balance = balance_report(selected, assignment)
        excess = sum(max(0, gap-MAX_RATIO_GAP) for gap in balance["gaps"].values())
        deficit = sum(v["shortfall"] for report in counts.values() for v in report.values())
        company_gap = abs(sum(len(unit) for i, unit in enumerate(units) if bits[i] == 0)-len(companies)/2)
        score = (not balance["passed"], round(excess, 10), company_gap,
                 round(balance["gaps"]["company_boards"], 10),
                 round(sum(balance["gaps"].values()), 10), deficit,
                 abs(sum(r["split"] == "debug" for r in selected)-len(selected)/2))
        return score, selected, counts, balance, assignment
    rng = random.Random(20261110)
    best = None
    for trial in range(64):
        order = list(range(len(units)))
        rng.shuffle(order)
        order.sort(key=lambda i: -len(units[i]))
        bins, bits = [Counter(), Counter()], [0]*len(units)
        for index in order:
            losses = []
            for side in (0, 1):
                proposed = bins[side]+features[index]
                # Signed distance after assigning the unit, normalized by each feature.
                losses.append(sum(((proposed[k]-bins[1-side][k])/max(1, total[k]))**2 for k in total))
            side = 0 if losses[0] < losses[1] else 1 if losses[1] < losses[0] else rng.randrange(2)
            bits[index] = side
            bins[side].update(features[index])
        result = candidate(bits)
        if best is None or result[0] < best[0]:
            best, best_bits = result, bits
    for _ in range(512):
        changed = best_bits.copy()
        if not changed:
            break
        for index in rng.sample(range(len(changed)), min(len(changed), rng.choice((1, 2)))):
            changed[index] = 1-changed[index]
        result = candidate(changed)
        if result[0] < best[0]:
            best, best_bits = result, changed
    return best, units


def split_rows(rows, groups):
    companies = sorted({m["instrument_id"] for g in groups for m in g["members"]})
    cross = {g["id"] for g in groups if len({m["instrument_id"] for m in g["members"]}) > 1}
    excluded = set()
    result, units = _search(rows, groups, companies, excluded)
    # First try binding every cross-company article and its companies. If the
    # bounded deterministic balance search fails, exclude WHOLE shared groups;
    # individual issuer variants are never separated to rescue a split.
    exclusion_reason = None
    if not result[3]["passed"] and cross:
        excluded = cross
        exclusion_reason = "cross_company_binding_balance_search_failed"
        result, units = _search(rows, groups, companies, excluded)
    _, selected, counts, balance, assignment = result
    group_assignment = {}
    for group in groups:
        sides = {assignment[m["instrument_id"]] for m in group["members"]}
        if group["id"] in excluded:
            group_assignment[group["id"]] = "excluded"
        elif len(sides) != 1:
            raise ValueError("Cross-company article leakage")
        else:
            group_assignment[group["id"]] = sides.pop()
    return selected, {"split_version": SPLIT_VERSION, "targets_per_split": TARGETS, "splits": counts,
                      "company_split": {s: sorted(code for code, side in assignment.items() if side == s) for s in SPLITS},
                      "company_components": units, "event_group_split": group_assignment,
                      "balance": balance, "excluded_groups": sorted(excluded),
                      "excluded_representatives": sum(r["group_id"] in excluded for r in rows),
                      "exclusion_reason": exclusion_reason,
                      "policy": "company_components_stratified_seeded_search_natural_from_all_then_extra_real_hard",
                      "date_policy": "dates_reported_only_do_not_determine_sealed_membership"}
