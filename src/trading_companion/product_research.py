"""Bounded domain collection for company research and candidate comparisons."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json

from trading_companion.company_research_contracts import ResearchDataBatch, ResearchDataRequest, SHANGHAI
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import DEFAULT_INDICATORS, MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report, UNITS
from trading_companion.research_synthesis import build_research_synthesis
from trading_companion.research_tool import _build_source
from trading_companion.risk_research import build_risk_scenario
from trading_companion.turn_control import checkpoint, reserve_analysis


LABELS = {
    "company_name": "公司名称", "main_business": "主营业务", "company_profile": "公司简介",
    "industry": "行业", "csrc_industry": "证监会行业", "sw_industry": "申万行业",
    "listing_date": "上市日期", "revenue": "营业收入", "parent_net_profit": "归母净利润",
    "revenue_yoy": "营收同比", "parent_net_profit_yoy": "归母净利润同比",
    "weighted_roe": "加权净资产收益率", "announcement_title": "公告标题", "news_excerpt": "新闻摘要",
}


def _number(value):
    return format(value, ".7g")


def evidence_display(value, unit):
    if type(value) in (int, float):
        if unit == "cny":
            return f"{format(value / 100_000_000, '.2f').rstrip('0').rstrip('.')}亿元" if abs(value) >= 100_000_000 else f"{_number(value)}元"
        return _number(value) + {"percent": "%", **UNITS}.get(unit, unit)
    return str(value)


def _validate_batch(batch, request, kinds):
    if (type(batch) is not ResearchDataBatch or batch.request != request
            or len(batch.source_results) != len(kinds)
            or {section.kind for section in batch.source_results} != set(kinds)):
        raise ValueError("Company evidence belongs to another request or domain")
    identities = set()
    for section in batch.source_results:
        if len(section.items) > request.limit:
            raise ValueError("Company evidence exceeds its requested bound")
        for item in section.items:
            if (item.instrument_id != request.instrument_id or item.section != section.kind
                    or item.status != "available" or item.id in identities):
                raise ValueError("Company evidence identity mismatch")
            identities.add(item.id)
            if item.temporal_scope == "current_context":
                if not request.allow_current_background or section.kind != "profile":
                    raise ValueError("Current background cannot support historical research")
            elif item.publication_precision in ("datetime", "minute"):
                if item.published_at is None or item.published_at > request.as_of:
                    raise ValueError("Evidence was not available before cutoff")
            elif item.publication_precision == "date":
                end = datetime.combine(item.published_date, time.max, SHANGHAI)
                if end > request.as_of:
                    raise ValueError("Date-only evidence is not known before its day ends")
            else:
                raise ValueError("Unpublished evidence is not historical evidence")
            if section.kind in ("news", "announcements") and (
                    item.published_date is None or item.published_date < request.publication_start):
                raise ValueError("Document is outside the requested publication window")


@dataclass(frozen=True)
class ProductEvidence:
    id: str
    instrument_id: str
    kind: str
    label: str
    display: str
    source: str
    date: str | None = None
    url: str | None = None
    content_kind: str = "fact"
    published_at: str | None = None
    publication_precision: str = "unknown"
    report_period: str | None = None
    data_authenticity: str = "provider_reported"
    temporal_scope: str = "published_before_cutoff"
    dependencies: tuple[str, ...] = ()
    retrieved_at: str | None = None
    raw_value: float | int | str | None = None
    unit: str | None = None

    def to_dict(self):
        from dataclasses import asdict
        return {**asdict(self), "dependencies": list(self.dependencies)}


@dataclass(frozen=True)
class ProductCompany:
    instrument_id: str
    name: str
    evidence: tuple[ProductEvidence, ...]
    coverage: tuple[dict, ...]
    technical: MultiIndicatorResult | None = None
    risk: object | None = None

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "name": self.name,
                "evidence": [item.to_dict() for item in self.evidence],
                "coverage": [dict(item) for item in self.coverage],
                "risk": self.risk.to_dict() if self.risk else None}


@dataclass(frozen=True)
class ProductResearchBundle:
    plan: object
    companies: tuple[ProductCompany, ...]

    @property
    def evidence(self):
        return tuple(item for company in self.companies for item in company.evidence)

    @property
    def partial(self):
        return any(item["status"] != "available" for company in self.companies for item in company.coverage)

    def to_dict(self):
        return {"version": "product-research-bundle-v1", "plan": self.plan.to_dict(),
                "companies": [company.to_dict() for company in self.companies],
                "trading_enabled": False, "snapshot_policy": "publication_filtered_vendor_snapshots_not_versioned_pit"}


def collect_product_research(plan, *, data_service, now, development_source=None, source_factory=None):
    """No model chooses endpoints or calculations; a plan grants bounded read access only."""
    if plan.status != "ready" or not plan.securities or len(plan.securities) > 3:
        raise ValueError("A checked company research plan is required")
    companies = []
    factory = source_factory or _build_source
    kinds = tuple(kind for kind in plan.kinds if kind != "technical")
    for security in plan.securities:
        checkpoint()
        evidence, coverage, result, risk = [], [], None, None
        if kinds:
            # Current documents and completed daily bars have different cutoffs.
            document_cutoff = (datetime.combine(plan.end_date, time.max, SHANGHAI)
                               if plan.explicit_historical else now)
            publication_start = max(plan.start_date, document_cutoff.astimezone(SHANGHAI).date() - timedelta(days=366))
            request = ResearchDataRequest(security.instrument_id, security.name, document_cutoff,
                start_date=publication_start, allow_current_background=not plan.explicit_historical)
            batch = data_service.collect(request, kinds=kinds)
            checkpoint()
            _validate_batch(batch, request, kinds)
            for section in batch.source_results:
                coverage.append({"kind": section.kind, "status": section.status,
                                 "code": section.code, "message": section.message, "source": section.source,
                                 "retrieved_at": section.retrieved_at.isoformat()})
                for item in section.items:
                    if item.instrument_id != security.instrument_id or item.section != section.kind:
                        raise ValueError("Company evidence belongs to another instrument or domain")
                    evidence.append(ProductEvidence(item.id, item.instrument_id, item.section,
                        LABELS.get(item.metric, item.metric), evidence_display(item.value, item.unit), item.source,
                        item.published_date.isoformat() if item.published_date else None, item.url,
                        item.content_kind, item.published_at.isoformat() if item.published_at else None,
                        item.publication_precision, item.report_period.isoformat() if item.report_period else None,
                        temporal_scope=item.temporal_scope, retrieved_at=item.retrieved_at.isoformat(),
                        raw_value=item.value, unit=item.unit))
        if "technical" in plan.kinds or plan.wants_risk:
            checkpoint()
            request = MultiIndicatorRequest(security.instrument_id, plan.start_date, plan.end_date,
                source="csv" if development_source is not None else plan.source, indicators=plan.indicators)
            reserve_analysis()
            result = analyze_indicators(development_source or factory(request), request,
                                        allow_development_csv=development_source is not None)
            checkpoint()
            report = build_research_report(result, expected_request=request)
            usable = report.status in ("complete", "limited")
            coverage.append({"kind": "technical", "status": "available" if usable and result.synthesis_allowed else "unavailable",
                "code": report.code, "message": result.message, "source": request.source,
                "retrieved_at": result.retrieved_at.isoformat() if result.retrieved_at else None})
            if usable:
                # Numeric fields and deterministic relations retain their original evidence dependencies.
                for item in report.fields:
                    if item.status == "available" and item.fact_id:
                        evidence.append(ProductEvidence(item.fact_id, security.instrument_id, "technical", item.label,
                            evidence_display(item.value, item.unit), request.source, item.trade_date,
                            publication_precision="date", data_authenticity=result.data_authenticity,
                            temporal_scope="daily_date_filtered_not_versioned_pit",
                            retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None,
                            raw_value=item.value, unit=item.unit))
                synthesis = build_research_synthesis(result, expected_request=request)
                for index in range(max((len(d.observations) for d in synthesis.dimensions), default=0)):
                    for dimension in synthesis.dimensions:
                        if index >= len(dimension.observations):
                            continue
                        item = dimension.observations[index]
                        evidence.append(ProductEvidence(f"{request.request_id}|{item.observation_id}", security.instrument_id, "technical",
                            {"trend": "趋势关系", "momentum": "动量关系", "volume": "量能关系", "volatility": "波动关系"}.get(dimension.dimension, "已计算关系"),
                            item.text, request.source, item.trade_date, content_kind="relation",
                            publication_precision="date", data_authenticity=result.data_authenticity,
                            temporal_scope="daily_date_filtered_not_versioned_pit", dependencies=item.dependencies,
                            retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None))
            if plan.wants_risk:
                risk = build_risk_scenario(result, expected_request=request)
                coverage.append({"kind": "risk", "status": risk.status, "code": risk.code,
                                 "message": "以历史收盘价为起点的ATR做多观察情景", "source": request.source,
                                 "retrieved_at": result.retrieved_at.isoformat() if result.retrieved_at else None})
                if risk.values:
                    values = risk.values
                    text = (f"历史参考价{values.reference_entry}元；ATR{_number(float(values.atr))}元；"
                            f"波幅倍数{values.atr_multiple}倍；参考止损{values.reference_stop}元；"
                            f"参考目标{values.reference_target}元；每股风险{values.risk_per_share}元；"
                            f"舍入后收益风险比{_number(float(values.effective_reward_risk))}。"
                            "假设以最新历史收盘价做多、默认目标距离为风险距离两倍；未计费用、滑点或跳空。")
                    evidence.append(ProductEvidence(f"risk:{request.request_id}", security.instrument_id,
                        "risk", "ATR风险情景", text, request.source, risk.source_date, content_kind="scenario",
                        publication_precision="date", data_authenticity=result.data_authenticity,
                        temporal_scope="historical_long_scenario", dependencies=risk.evidence_ids,
                        retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None))
        companies.append(ProductCompany(security.instrument_id, security.name, tuple(evidence), tuple(coverage), result, risk))
    bundle = ProductResearchBundle(plan, tuple(companies))
    if len(companies) > 1:
        from trading_companion.comparison_research import build_candidate_comparison
        comparison = build_candidate_comparison(bundle)
        additions = {company.instrument_id: [] for company in companies}
        originals = {item.id: item for item in bundle.evidence}
        for metric in comparison.metrics:
            if metric.status != "comparable":
                continue
            for row in metric.rows:
                original = originals[row.evidence_id]
                display = (f"本轮{len(companies)}只候选中，{original.label}为{original.display}，"
                           f"按该项降序排第{row.rank}；报告期{original.report_period}。"
                           "仅比较这项数据，不代表整体投资价值排序。")
                additions[row.instrument_id].append(ProductEvidence(
                    "comparison:" + original.id, row.instrument_id, "financial", original.label + "比较",
                    display, "application_comparison", original.date, content_kind="relation",
                    publication_precision=original.publication_precision, report_period=original.report_period,
                    temporal_scope=original.temporal_scope, data_authenticity=original.data_authenticity,
                    dependencies=tuple(item.evidence_id for item in metric.rows), retrieved_at=original.retrieved_at))
        from dataclasses import replace
        bundle = ProductResearchBundle(plan, tuple(replace(company, evidence=company.evidence + tuple(additions[company.instrument_id]))
                                                    for company in companies))
    return bundle


def model_research_context(bundle, *, limit=14500):
    """Keep domain notes, not complete time series, in a bounded synthesis context."""
    records = []
    for index, item in enumerate(bundle.evidence, 1):
        record = {key: value for key, value in item.to_dict().items() if key not in (
            "dependencies", "url", "published_at", "retrieved_at", "raw_value")
                  and value is not None}
        record.update(id=f"E{index}", display=item.display[:480])
        records.append(record)
    payload = {"securities": [{"instrument_id": c.instrument_id, "name": c.name,
                               "coverage": [{key: item[key] for key in ("kind", "status", "code") if key in item}
                                            for item in c.coverage]} for c in bundle.companies],
               "objective": bundle.plan.objective.to_dict(),
               "daily_cutoff": bundle.plan.end_date.isoformat(),
               "explicit_historical": bundle.plan.explicit_historical,
               "evidence": records, "omitted_from_model": 0}
    # Reserve opportunities across both companies and domains; scenario data must survive later trimming.
    selected = []
    domains = ("risk", "technical", "financial", "news", "announcements", "profile")
    queues = {(company.instrument_id, kind): sorted(
        [r for r in records if r["instrument_id"] == company.instrument_id and r["kind"] == kind],
        key=lambda r: 0 if r["content_kind"] in ("relation", "scenario") else 1)
        for company in bundle.companies for kind in domains}
    while any(queues.values()):
        for kind in domains:
            for company in bundle.companies:
                queue = queues[company.instrument_id, kind]
                if not queue:
                    continue
                candidate = queue.pop(0)
                trial = {**payload, "evidence": [*selected, candidate]}
                if len(json.dumps(trial, ensure_ascii=False)) <= limit:
                    selected.append(candidate)
    payload["evidence"] = selected
    payload["omitted_from_model"] = len(records) - len(selected)
    return payload
