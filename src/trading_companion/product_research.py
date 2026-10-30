"""Bounded domain collection for company research and candidate comparisons."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import re

from trading_companion.company_research_contracts import ResearchDataBatch, ResearchDataRequest, SHANGHAI
from trading_companion.company_research import ResearchDataService
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import DEFAULT_INDICATORS, MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.research_report import build_research_report, FIELD_LABELS, UNITS
from trading_companion.research_synthesis import build_research_synthesis
from trading_companion.research_tool import _build_source
from trading_companion.risk_research import build_risk_scenario
from trading_companion.trade_plan import build_trade_plans, plan_display
from trading_companion.research_decision import excludes_plan
from trading_companion.technical_insights import TechnicalInsights, build_technical_insights
from trading_companion.turn_control import checkpoint, reserve_analysis
from trading_companion.turn_trace import trace_call


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
        return (str(value) if type(value) is int else _number(value)) + {"percent": "%", "count": "个", **UNITS}.get(unit, unit)
    return str(value)


@trace_call("check", "company_batch")
def _validate_batch(batch, request, kinds):
    if (type(batch) is not ResearchDataBatch or batch.request != request
            or len(batch.source_results) != len(kinds)
            or {section.kind for section in batch.source_results} != set(kinds)):
        raise ValueError("Company evidence belongs to another request or domain")
    identities = set()
    for section in batch.source_results:
        if len(section.items) > (25 if section.kind == "financial" else request.limit):
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
    calculation_basis: tuple[tuple[str, str], ...] = ()

    def to_dict(self):
        from dataclasses import asdict
        value = {**asdict(self), "dependencies": list(self.dependencies)}
        value.pop("calculation_basis")
        if self.calculation_basis:
            value["calculation_basis"] = dict(self.calculation_basis)
        return value


@dataclass(frozen=True)
class ProductCompany:
    instrument_id: str
    name: str
    evidence: tuple[ProductEvidence, ...]
    coverage: tuple[dict, ...]
    technical: MultiIndicatorResult | None = None
    risk: object | None = None
    technical_insights: TechnicalInsights | None = None
    historical_replay: object | None = None
    market_background: dict | None = None
    trade_plans: tuple = ()

    def to_dict(self):
        result = {"instrument_id": self.instrument_id, "name": self.name,
                "evidence": [item.to_dict() for item in self.evidence],
                "coverage": [dict(item) for item in self.coverage],
                "risk": self.risk.to_dict() if self.risk else None}
        if self.trade_plans:
            result["trade_plans"] = [item.to_dict() for item in self.trade_plans]
        return result


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


def collect_product_research(plan, *, data_service, now, development_source=None, source_factory=None,
                             market_background_source=None, directory=None, background_clock=None):
    """No model chooses endpoints or calculations; a plan grants bounded read access only."""
    if plan.status != "ready" or not plan.securities or len(plan.securities) > 3:
        raise ValueError("A checked company research plan is required")
    plan.scope.validate(plan.kinds, plan.indicators)
    companies = []
    factory = source_factory or _build_source
    kinds = tuple(kind for kind in plan.kinds if kind != "technical")
    background_snapshot = None
    background_requested = (market_background_source is not None and not plan.explicit_historical
        and ("profile" in kinds or re.search(r"ST|上市|板块|交易规则|涨跌幅限制", plan.user_question, re.I)))
    if background_requested:
        try:
            background_snapshot = trace_call("source", "market_background", market_background_source.read_snapshot, directory=directory,
                instrument_ids=tuple(security.instrument_id for security in plan.securities))
        except Exception:
            # Optional exchange background cannot discard other available research domains.
            pass
        checkpoint()
    requests, batches = (), None
    if kinds:
        # Documents use one cutoff even though completed daily bars have an earlier one.
        document_cutoff = (datetime.combine(plan.end_date, time.max, SHANGHAI) if plan.explicit_historical else now)
        publication_start = max(plan.start_date, document_cutoff.astimezone(SHANGHAI).date() - timedelta(days=366))
        requests = tuple(ResearchDataRequest(security.instrument_id, security.name, document_cutoff,
            start_date=publication_start, allow_current_background=not plan.explicit_historical)
            for security in plan.securities)
        if type(data_service) is ResearchDataService and len(requests) > 1:
            checkpoint()
            batches = trace_call("source", "company_collect", data_service.collect_many, requests, kinds=kinds)
            checkpoint()
            if type(batches) is not tuple or len(batches) != len(requests):
                raise ValueError("Company evidence batches must match the checked request sequence")
            # Validate the entire prefetch before any daily analysis spends the shared turn budget.
            for batch, request in zip(batches, requests, strict=True):
                _validate_batch(batch, request, kinds)
    for company_index, security in enumerate(plan.securities):
        checkpoint()
        evidence, coverage, result, risk, insights, replay, background = [], [], None, None, None, None, None
        if background_snapshot is not None:
            try:
                background, background_evidence = _market_background_evidence(background_snapshot, security, plan,
                    background_clock() if background_clock is not None else now)
                evidence.extend(background_evidence)
            except Exception:
                background = None
        if background_requested:
            available = background is not None and background.get("board") not in (None, "unknown")
            coverage.append({"kind": "market_context", "status": "available" if available else "unavailable",
                "code": "exchange_current_context_available" if available else "exchange_current_context_unavailable",
                "message": "当前交易所背景已取得；历史状态仍按字段说明。" if available else
                           "当前交易所板块和上市背景未取得；行业资料不能替代交易所板块。",
                "source": "sse_szse", "retrieved_at": background_snapshot.retrieved_at.isoformat()
                    if background_snapshot is not None else None})
        if kinds:
            if batches is not None:
                batch = batches[company_index]
            else:
                request = requests[company_index]
                batch = trace_call("source", "company_collect", data_service.collect, request, kinds=kinds)
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
            coverage.append({"kind": "technical", "status": ("limited" if plan.indicators_explicit and report.status == "limited" else "available")
                if usable and result.synthesis_allowed else "unavailable",
                "code": report.code, "message": result.message, "source": request.source,
                "retrieved_at": result.retrieved_at.isoformat() if result.retrieved_at else None})
            if usable:
                insights = build_technical_insights(result, expected_request=request)
                if insights.status in ("complete", "limited"):
                    evidence.extend(_insight_evidence(insights, result))
                if re.search(r"回测|历史回放|策略回放", plan.user_question):
                    from trading_companion.historical_replay import replay_config_from_question, build_historical_replay
                    try:
                        replay_config = replay_config_from_question(plan.user_question, plan.indicators,
                            indicators_explicit=plan.indicators_explicit)
                        replay = build_historical_replay(result, expected_request=request, config=replay_config)
                        evidence.extend(_replay_evidence(replay, result))
                    except ValueError:
                        evidence.append(ProductEvidence(f"{request.request_id}|replay:unsupported", security.instrument_id,
                            "technical", "回放范围", "当前回放支持单个SMA的多头或现金敞口比较；成本需使用明确百分比或基点。本次条件未执行策略回放。",
                            request.source, plan.end_date.isoformat(), content_kind="relation",
                            data_authenticity=result.data_authenticity, temporal_scope="historical_exposure_replay",
                            retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None))
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
        plans = ()
        if plan.objective.kind == "decision":
            blocked = excludes_plan(plan.user_question) or "technical" not in plan.kinds
            plans = build_trade_plans(result if result is not None else security.instrument_id,
                expected_request=result.request if result is not None else None,
                unavailable_code="plan_excluded" if blocked else None)
            for candidate in plans:
                if candidate.status == "available":
                    evidence.append(ProductEvidence(candidate.plan_id, security.instrument_id, "risk", "ATR参考计划",
                        plan_display(candidate), candidate.source, candidate.source_date, content_kind="trade_plan",
                        publication_precision="date", data_authenticity=candidate.data_authenticity,
                        temporal_scope="conditional_atr_reference_plan", dependencies=candidate.evidence_ids,
                        retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None))
        from trading_companion.financial_periods import quarterly_evidence
        evidence.extend(quarterly_evidence(evidence))
        if result is not None:
            from trading_companion.research_price_context import price_context_evidence
            evidence.extend(price_context_evidence(result, allow_extra_means=not plan.indicators_explicit))
        companies.append(ProductCompany(security.instrument_id, security.name, tuple(evidence), tuple(coverage), result, risk, insights, replay, background, plans))
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


def _market_background_evidence(snapshot, security, plan, now):
    from trading_companion.market_context import current_security_background
    from trading_companion.market_rules import explain_market_rules
    background = current_security_background(snapshot, instrument_id=security.instrument_id,
        analysis_date=plan.end_date, reference_at=now)
    if background.get("board") is None or background.get("board") == "unknown":
        return background, []
    listing = background.get("listing_date")
    from datetime import date
    rules = explain_market_rules(board=background["board"], st_status=background.get("analysis_date_st_status", "unknown"),
        as_of_date=plan.end_date, listing_date=date.fromisoformat(listing) if listing else None)
    background = {**background, "rules": rules}
    source = background.get("source", "exchange_metadata")
    observed = background.get("observed_at")
    day = background.get("observation_date")
    common = dict(instrument_id=security.instrument_id, kind="profile", source=source, date=day,
                  publication_precision="unknown", temporal_scope="current_context", retrieved_at=observed)
    references = background.get("source_records", [])
    url = next((record.get("source_url") for record in references if type(record) is dict and record.get("source_url")), None)
    boards = {"sse_main": "沪市主板", "szse_main": "深市主板", "star": "科创板", "chinext": "创业板", "bse": "北交所"}
    prefix = "exchange:" + snapshot.snapshot_id + "|" + security.instrument_id
    items = [ProductEvidence(id=prefix + "|board", label="交易所当前板块",
        display=boards.get(background["board"], "未知"), url=url, **common)]
    if listing:
        items.append(ProductEvidence(id=prefix + "|listing", label="交易所上市日期", display=listing, raw_value=listing, url=url, **common))
    observed_st = background.get("observed_st_status", "unknown")
    st_label = {"st": "ST", "star_st": "*ST", "not_st": "非ST", "unknown": "未核实"}.get(observed_st, "未核实")
    items.append(ProductEvidence(id=prefix + "|st", label="当前风险警示观测",
        display=f"本次当前状态：{st_label}；分析截止日状态："
                + ("未核实，不能用当前观测回填历史。" if background.get("analysis_date_st_status") == "unknown" else st_label + "。"),
        content_kind="relation", url=url, **common))
    if rules["sources"]:
        rule_source = rules["sources"][0]
        items.append(ProductEvidence(id=prefix + "|rules", instrument_id=security.instrument_id, kind="profile",
            label="有版本的普通A股交易规则", display=f"规则版本{rules['rule_version']}；生效起日{rules['policy_effective_from']}"
                + (f"，结束日{rules['policy_effective_to']}。" if rules["policy_effective_to"] else "。")
                + " ".join(rules["explanations"])
                + "这是条件规则说明，不是该证券当日可交易性判定。", source="application_market_rules",
            date=rule_source["published_date"], url=rule_source["url"], content_kind="relation",
            publication_precision="date", temporal_scope="market_rule_explanation", retrieved_at=observed))
    return background, items


def _replay_evidence(replay, result):
    """Publish bounded replay summaries, keeping the full path outside model context."""
    common = dict(instrument_id=result.request.instrument_id, kind="technical", source=result.source,
                  date=result.latest_trade_date.isoformat(), data_authenticity=result.data_authenticity,
                  temporal_scope="historical_exposure_replay",
                  retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None)
    prefix = result.request.request_id + "|replay:"
    if replay.status != "available":
        return [ProductEvidence(id=prefix + "availability", label="历史回放范围",
            display="当前快照不足以形成预热之后的可比较历史回放区间。", content_kind="relation", **common)]
    output = [ProductEvidence(id=prefix + "method", label=f"SMA({replay.config.window})历史回放方法",
        display=f"回放区间{replay.start_date}至{replay.end_date}；前一条收盘价高于SMA({replay.config.window})时，"
                "下一条开盘切为多头敞口，否则为空仓；与同区间持有比较，末日开盘结束。"
                "只是标准化敞口收益回放，未建模实际成交、涨跌停、手数和公司行动，未选择最优参数。",
        content_kind="relation", **common)]
    metrics = (("total_return_percent", "历史回放净收益", "percent"),
               ("benchmark_return_percent", "同期持有净收益", "percent"),
               ("excess_return_percentage_points", "相对持有差值", "percentage_points"),
               ("max_drawdown_percent", "历史回放最大回撤", "percent"),
               ("benchmark_max_drawdown_percent", "同期持有最大回撤", "percent"),
               ("turnover_count", "敞口切换次数（含末日平仓）", "count"))
    for field, label, unit in metrics:
        value = getattr(replay, field)
        output.append(ProductEvidence(id=prefix + field, label=label,
            display=evidence_display(value, unit) if unit != "percentage_points" else _number(value) + "个百分点",
            raw_value=value, unit=unit, **common))
    gap = replay.excess_return_percentage_points
    output.append(ProductEvidence(id=prefix + "relative_gap_magnitude",
        label="领先同期持有幅度" if gap >= 0 else "落后同期持有幅度",
        display=_number(abs(gap)) + "个百分点", raw_value=abs(gap), unit="percentage_points", **common))
    cost = replay.config.cost_bps / 100
    output.append(ProductEvidence(id=prefix + "cost", label="假设单次敞口切换成本",
        display=evidence_display(cost, "percent"), raw_value=cost, unit="percent", **common))
    return output


def _insight_evidence(insights, result):
    """Publish only derived conclusions; their complete input graph stays in the archive."""
    labels = {"divergence": "背离规则候选", "levels": "历史价格区域", "weekly": "周线观察", "alignment": "日周方向核对"}
    scopes = {"daily": "daily_signal_derived", "weekly": "completed_week_derived", "cross_timeframe": "cross_timeframe_derived"}
    facts = {item.fact_id: item for item in insights.facts}
    output = []
    for item in insights.observations:
        numeric = type(item.value) in (int, float)
        field_label = (FIELD_LABELS.get(item.field, "收盘价" if item.field == "close_price" else item.field)
                       if item.timeframe == "weekly" and item.field not in ("value", "observation") else "")
        label = labels[item.category] + (" · " + item.indicator_id if item.indicator_id != "technical_insights" else "")
        if field_label:
            label += " · " + field_label
        identifier = (item.dependencies[0] if item.status == "available" else
                      f"{result.request.request_id}|technical-missing|{item.key}")
        if item.status == "available":
            fact = facts[identifier]
            if fact.value != item.value or fact.unit != item.unit:
                raise ValueError("Derived technical evidence does not match its numeric fact")
        display = item.text + ((" " + field_label + "：" if field_label else " ")
                               + evidence_display(item.value, item.unit) if numeric else "")
        output.append(ProductEvidence(identifier, item.instrument_id, "technical",
            label,
            display, result.source, item.trade_date.isoformat(), content_kind="fact" if numeric else "relation",
            publication_precision="date", data_authenticity=result.data_authenticity,
            temporal_scope=scopes[item.timeframe],
            dependencies=facts[identifier].dependencies if item.status == "available" else (),
            retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None,
            raw_value=item.value, unit=item.unit if numeric else None))
    return output


def model_research_context(bundle, *, limit=14500):
    """Keep domain notes, not complete time series, in a bounded synthesis context."""
    records = []
    for index, item in enumerate(bundle.evidence, 1):
        record = {key: value for key, value in item.to_dict().items() if key not in (
            "dependencies", "url", "published_at", "retrieved_at", "raw_value")
                  and value is not None}
        record.update(id=f"E{index}", display=item.display if item.content_kind == "trade_plan" else item.display[:480])
        if item.temporal_scope == "daily_signal_derived" and type(item.raw_value) is str:
            record["signal_state"] = item.raw_value
        records.append(record)
    payload = {"securities": [{"instrument_id": c.instrument_id, "name": c.name,
                               "coverage": [{key: item[key] for key in ("kind", "status", "code") if key in item}
                                            for item in c.coverage]} for c in bundle.companies],
               "objective": bundle.plan.objective.to_dict(),
               "daily_cutoff": bundle.plan.end_date.isoformat(),
               "explicit_historical": bundle.plan.explicit_historical,
               "evidence": records, "omitted_from_model": 0}
    pinned = []
    if bundle.plan.objective.kind == "decision":
        # Full public plans repeat provenance and display metadata already present
        # in the pinned evidence. The model only selects a candidate; it does not
        # serialize the plan back to the application.
        payload["trade_plans"] = [
            {"plan_id": p.plan_id, "instrument_id": p.instrument_id,
             "purpose": p.purpose, "status": p.status, "code": p.code}
            for c in bundle.companies for p in c.trade_plans]
        pinned = [r for r in records if r["content_kind"] == "trade_plan"
                  or r["label"] in ("营收同比", "归母净利润同比")]
        if len(json.dumps({**payload, "evidence": pinned}, ensure_ascii=False)) > limit:
            raise ValueError("decision_context_too_large")
    # Reserve opportunities across both companies and domains; scenario data must survive later trimming.
    selected = list(pinned)
    domains = ("risk", "technical", "financial", "news", "announcements", "profile")

    def priority(record):
        scope = record.get("temporal_scope")
        if re.search(r"周线|周均|日周", bundle.plan.user_question) and scope in ("cross_timeframe_derived", "completed_week_derived"):
            return -4 if scope == "cross_timeframe_derived" else -3
        if scope in ("daily_price_context", "financial_quarter_derived"):
            return -2
        if scope == "historical_exposure_replay":
            return -1
        focus = next((item.rsplit(":", 1)[-1] for item in bundle.plan.applied_defaults
                      if item.startswith("indicators:profile:")), "balanced")
        label = record.get("label", "")
        if (focus == "volume_price" and any(term in label for term in ("OBV", "VWMA", "量能"))
                or focus == "trend_strength" and "ADX" in label):
            return 0
        if record.get("signal_state") in ("bullish_candidate", "bearish_candidate"):
            return 0
        if scope == "cross_timeframe_derived":
            return 1
        if scope == "daily_signal_derived" and record["content_kind"] == "fact":
            return 2
        return 3 if record["content_kind"] in ("relation", "scenario") else 4

    queues = {(company.instrument_id, kind): sorted(
        [r for r in records if r not in pinned and r["instrument_id"] == company.instrument_id and r["kind"] == kind],
        key=priority)
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
