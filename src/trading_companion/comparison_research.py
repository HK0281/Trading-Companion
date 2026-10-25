"""Compare named candidates by separate financial metrics, never by an overall trade score."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from math import isfinite

from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle


VERSION = "candidate-comparison-v1"
METHOD = "financial_metrics_descending_competition_rank_v1"
METRICS = (("revenue_yoy", "营收同比"), ("parent_net_profit_yoy", "归母净利润同比"),
           ("weighted_roe", "加权净资产收益率"))


@dataclass(frozen=True)
class ComparisonRow:
    instrument_id: str
    name: str
    value: int | float | None = None
    display: str | None = None
    rank: int | None = None
    evidence_id: str | None = None
    unit: str | None = None
    report_period: str | None = None
    status: str = "unavailable"
    code: str = "metric_evidence_missing"

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "name": self.name, "value": self.value,
                "display": self.display, "rank": self.rank, "evidence_id": self.evidence_id,
                "unit": self.unit, "report_period": self.report_period, "status": self.status, "code": self.code}


@dataclass(frozen=True)
class MetricComparison:
    metric: str
    label: str
    status: str
    code: str
    rows: tuple[ComparisonRow, ...]
    report_period: str | None = None
    unit: str | None = None

    def to_dict(self):
        return {"metric": self.metric, "label": self.label, "status": self.status, "code": self.code,
                "report_period": self.report_period, "unit": self.unit,
                "rows": [row.to_dict() for row in self.rows],
                "coverage": {"candidate_count": len(self.rows),
                             "eligible_candidates": sum(row.status == "available" for row in self.rows),
                             "unavailable_candidates": [row.instrument_id for row in self.rows if row.status != "available"]}}


@dataclass(frozen=True)
class CandidateComparison:
    status: str
    code: str
    metrics: tuple[MetricComparison, ...]
    candidate_count: int
    scope_bound: bool
    method: str = METHOD

    def to_dict(self):
        comparable = sum(metric.status == "comparable" for metric in self.metrics)
        return {"version": VERSION, "status": self.status, "code": self.code,
                "method": self.method, "metrics": [metric.to_dict() for metric in self.metrics],
                "coverage": {"candidate_count": self.candidate_count, "scope_bound": self.scope_bound,
                             "comparable_metrics": comparable, "not_comparable_metrics": len(self.metrics) - comparable},
                "ranking_policy": {"direction": "higher_raw_value_first_within_each_metric",
                    "ties": "exact_raw_value_competition_ranks_1_1_3",
                    "periods": "all_candidates_same_report_period_within_metric",
                    "units": "canonical_percent_values_not_fractional_ratios",
                    "missing_values": "never_zero_filled_or_ranked",
                    "display": "source_display_preserved_raw_value_used_for_ordering"},
                "scope": "fixed_named_candidates_not_market_screening_or_cross_metric_quality_score",
                "limitations": ["Higher growth or ROE alone does not establish company quality or future performance.",
                                "Equal report periods and units do not establish identical accounting policies or industry economics."],
                "overall_winner": None, "trading_enabled": False}


def _text(value, maximum=512):
    return type(value) is str and bool(value.strip()) and len(value) <= maximum and value.isprintable()


def _finite(value):
    return (type(value) is float and isfinite(value)
            or type(value) is int and value.bit_length() <= 1024)


def _period(value, cutoff):
    if type(value) is not str:
        return False
    try:
        parsed = date.fromisoformat(value)
        return parsed.isoformat() == value and (cutoff is None or parsed <= cutoff)
    except ValueError:
        return False


def _row(company, label, cutoff):
    result = ComparisonRow(company.instrument_id, company.name)
    records = [item for item in company.evidence if item.kind == "financial" and item.label == label]
    if not records:
        return result
    if len(records) != 1:
        return replace(result, code="metric_evidence_ambiguous")
    record = records[0]
    if record.instrument_id != company.instrument_id or not _text(record.id):
        return replace(result, code="metric_evidence_identity_mismatch")
    result = replace(result, evidence_id=record.id)
    if not _finite(record.raw_value):
        return replace(result, code="metric_raw_value_invalid")
    if record.unit != "percent":
        return replace(result, code="metric_unit_not_percent")
    if not _period(record.report_period, cutoff):
        return replace(result, code="metric_report_period_invalid")
    if not _text(record.display, 480):
        return replace(result, code="metric_display_invalid")
    if record.content_kind != "fact":
        return replace(result, code="metric_evidence_not_fact")
    return replace(result, value=record.raw_value, display=record.display, unit=record.unit,
                   report_period=record.report_period, status="available", code="metric_evidence_available")


def _metric(metric, label, companies, *, cutoff, blocked=None):
    rows = tuple(_row(company, label, cutoff) for company in companies)
    if blocked:
        return MetricComparison(metric, label, "not_comparable", blocked, rows)
    if len(rows) < 2:
        return MetricComparison(metric, label, "not_comparable", "at_least_two_candidates_required", rows)
    if any(row.status != "available" for row in rows):
        return MetricComparison(metric, label, "not_comparable", "candidate_metric_missing_or_invalid", rows)
    if len({row.evidence_id for row in rows}) != len(rows):
        return MetricComparison(metric, label, "not_comparable", "candidate_evidence_ids_not_unique", rows)
    if len({row.report_period for row in rows}) != 1:
        return MetricComparison(metric, label, "not_comparable", "candidate_report_periods_differ", rows)
    if len({row.unit for row in rows}) != 1:
        return MetricComparison(metric, label, "not_comparable", "candidate_units_differ", rows)
    # Decimal strings preserve the supplied decimal values without display rounding.
    ordered = sorted(sorted(rows, key=lambda row: row.instrument_id),
                     key=lambda row: Decimal(str(row.value)), reverse=True)
    ranked, previous, rank = [], None, 0
    for position, row in enumerate(ordered, 1):
        number = Decimal(str(row.value))
        if number != previous:
            rank = position
        ranked.append(replace(row, rank=rank))
        previous = number
    return MetricComparison(metric, label, "comparable", "same_period_metric_ranked", tuple(ranked),
                            rows[0].report_period, rows[0].unit)


def build_candidate_comparison(bundle: ProductResearchBundle) -> CandidateComparison:
    """Rank three separate metrics only when every named candidate has compatible evidence.

    This function performs no model calls, data reads, market-wide selection, imputation
    or cross-metric scoring. Negative and zero values are actual observations; a missing
    value remains unavailable. Accounting comparability beyond period/unit is not proved.
    """
    companies, cutoff, blocked, bound, requested_count = (), None, None, False, 0
    if type(bundle) is ProductResearchBundle and type(bundle.companies) is tuple:
        requested_count = len(bundle.companies)
    if type(bundle) is not ProductResearchBundle or type(bundle.companies) is not tuple:
        blocked = "comparison_bundle_invalid"
    elif not 1 <= len(bundle.companies) <= 3:
        blocked = "comparison_candidate_limit"
    elif any(type(company) is not ProductCompany or type(company.evidence) is not tuple
             or any(type(item) is not ProductEvidence for item in company.evidence) for company in bundle.companies):
        blocked = "comparison_bundle_invalid"
    else:
        companies = bundle.companies
        if (any(not _text(company.instrument_id, 32) or not _text(company.name, 80) for company in companies)
                or len({company.instrument_id for company in companies}) != len(companies)):
            blocked = "comparison_candidate_identity_invalid"
            companies = ()
        else:
            try:
                expected = tuple((item.instrument_id, item.name) for item in bundle.plan.securities)
                actual = tuple((company.instrument_id, company.name) for company in companies)
                bound = len(expected) == len(actual) and set(expected) == set(actual)
                if type(bundle.plan.end_date) is date:
                    cutoff = bundle.plan.end_date
                if not bound or bundle.plan.status != "ready" or cutoff is None:
                    blocked = "comparison_plan_scope_mismatch"
            except (TypeError, AttributeError):
                blocked = "comparison_plan_scope_mismatch"
    metrics = tuple(_metric(metric, label, companies, cutoff=cutoff, blocked=blocked) for metric, label in METRICS)
    count = sum(item.status == "comparable" for item in metrics)
    status = "comparable" if count == len(METRICS) else "partial" if count else "not_comparable"
    code = blocked or {"comparable": "candidate_metrics_compared", "partial": "candidate_metrics_partially_compared",
                       "not_comparable": "candidate_metrics_not_comparable"}[status]
    return CandidateComparison(status, code, metrics, requested_count, bound)
