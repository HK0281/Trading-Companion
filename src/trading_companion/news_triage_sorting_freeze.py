"""One bounded public-data recording, followed by exact offline packet rebuilding."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from time import monotonic, sleep

from trading_companion.company_research import ResearchDataService, _ENDPOINTS
from trading_companion.company_research_contracts import SHANGHAI
from trading_companion.decision_horizon import gate
from trading_companion.errors import DataSourceError
from trading_companion.multi_analysis import bind_retrieval_clock
from trading_companion.news_triage_sorting_artifacts import canonical, write_json
from trading_companion.news_triage_sorting_replay import DailyPacket, FrozenSources, ReplayCase, VERSION, freeze_case
from trading_companion.product_research import collect_product_research, bind_research_request_ids, model_research_context
from trading_companion.product_narrative_answer import prepare_answer_context
from trading_companion.research_intent_adapter import build_product_plan
from trading_companion.research_intent_context import build_intent_context
from trading_companion.research_intent_v2 import CheckedIntent
from trading_companion.research_session import ResearchSession
from trading_companion.tencent_adapter import TencentDailyBarSource, TENCENT_DAILY_URL

ALL_KINDS = ("profile", "financial", "news", "announcements", "technical")
MAX_SOURCE_REQUESTS = 256
_URLS = frozenset((TENCENT_DAILY_URL,
    "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php",
    *(_ENDPOINTS[k][1] for k in ("profile", "financial", "announcements"))))


@dataclass(frozen=True)
class CaseSpec:
    id: str
    group: str
    codes: tuple
    reason: str
    kinds: tuple = ALL_KINDS


def prepare_case(spec, directory, now):
    names = []
    for code in spec.codes:
        match = directory.lookup(code)
        if match.status != "resolved":
            raise ValueError("Fixed company missing from current directory")
        names.append(match.candidates[0].name+"（"+code+"）")
    decision = spec.group == "decision"
    boundary = spec.group.startswith("capacity")
    scope = "仅新闻" if spec.kinds == ("news",) else "仅公告" if spec.kinds == ("announcements",) else "公司、财务、新闻、公告及技术面"
    question = "分析"+"、".join(names)+"的"+scope
    if decision:
        question += "，给出未来20个交易日的买卖评级和参考计划"
    if boundary:
        question += "，对照日线与周线的SMA20、ATR、MACD、RSI和BOLL，列出相反证据与风险"
    checked = CheckedIntent("stock_research", objective_kind="decision" if decision else "analysis",
        wants_plan=decision, subject_source="current_turn", bound_subjects=spec.codes,
        horizon="未来20个交易日" if decision else None,
        research_kinds=spec.kinds, strict_scope=spec.kinds in (("news",), ("announcements",)),
        indicator_mentions=("SMA20", "ATR", "MACD", "RSI", "BOLL") if boundary else ())
    context = build_intent_context(session=ResearchSession(), directory=directory, now=now, source="tencent")
    plan = execution_plan(checked, question, context)
    return question, checked, plan


def execution_plan(checked, question, context):
    # The product executor applies the same horizon/price permission gate after planning.
    return gate(build_product_plan(checked, question, context), now=context.now,
                reference_date=context.reference_date, development=context.development)


def collection_plan(specs, directory, now):
    companies = daily = 0
    rows = []
    for index, spec in enumerate(specs):
        question, _, plan = prepare_case(spec, directory, now)
        sections = sum(k != "technical" for k in plan.kinds)*len(plan.securities)
        slices = (plan.end_date.year-plan.start_date.year+1)*len(plan.securities) if "technical" in plan.kinds else 0
        companies += sections
        daily += slices
        rows.append(dict(id=spec.id, group=spec.group, companies=list(spec.codes), reason=spec.reason,
                         question=question, first="on" if index < 15 else "off",
                         source_request_upper_bound=sections+slices))
    return dict(version="news-triage-freeze-plan-v1", mode="plan_only", cases=rows,
        pairs=len(specs), company_http_upper_bound=companies, daily_http_upper_bound=daily,
        source_http_upper_bound=companies+daily, model_requests=0, intent_requests=0,
        source_policy="single_attempt_per_endpoint_slice_exact_cache_no_refetch",
        min_request_interval_seconds=1, endpoints=sorted(_URLS))


class SourceTransport:
    def __init__(self, client, limit, *, clock=monotonic, sleeper=sleep):
        if type(limit) is not int or not 1 <= limit <= MAX_SOURCE_REQUESTS:
            raise ValueError("Use a positive public HTTP budget")
        self.client, self.limit, self.used = client, limit, 0
        self.clock, self.sleeper, self.last, self.lock = clock, sleeper, None, Lock()

    def request(self, method, url, **kwargs):
        if method != "GET" or url not in _URLS:
            raise ValueError("Only fixed public collection endpoints are allowed")
        with self.lock:
            if self.used >= self.limit:
                raise DataSourceError("Public collection request allowance exhausted")
            if self.last is not None:
                self.sleeper(max(0, 1-(self.clock()-self.last)))
            self.used += 1
            self.last = self.clock()
        kwargs.update(allow_redirects=False, stream=True)
        response = self.client.request(method, url, **kwargs)
        try:
            if response.status_code != 200:
                raise DataSourceError("Public source did not return HTTP 200")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if len(raw)+len(chunk) > 512000:
                    raise DataSourceError("Public source response exceeded its bound")
                raw.extend(chunk)
            body = bytes(raw)
            content_type = response.headers.get("Content-Type", "")
            class Buffered:
                status_code = 200
                headers = {"Content-Length": str(len(body)),
                           "Content-Type": content_type if type(content_type) is str else ""}
                def json(self):return json.loads(body.decode("utf-8"))
                def iter_content(self, *args, **kwargs):yield body
                def raise_for_status(self):return None
                def close(self):return None
            return Buffered()
        finally:
            response.close()

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class RecordingSources:
    def __init__(self, service, daily_factory, *, clock=lambda:datetime.now(timezone.utc)):
        self.service, self.daily_factory, self.clock = service, daily_factory, clock
        self.company_cache, self.daily_cache = {}, {}
        self.retrieved_at = None
        self.batches, self.daily_packets = [], []

    def collect(self, request, kinds):
        key = (request, kinds)
        if key not in self.company_cache:
            try:self.company_cache[key] = self.service.collect(request, kinds=kinds)
            except Exception:self.company_cache[key] = None
        batch = self.company_cache[key]
        if batch is None:raise DataSourceError("Recorded company request failed; no refetch")
        self.batches.append(batch)
        return batch

    def daily(self, request):
        owner = self
        source = self.daily_factory(request)
        class Recorded:
            source_name = source.source_name
            adjustment = source.adjustment
            instrument_id = request.instrument_id
            start_date = request.start_date
            end_date = request.as_of_date
            def read_daily_bars(self):
                key = replace(request, request_id="sorting-cache")
                if key not in owner.daily_cache:
                    # Retain failure too: another case must not refetch the same request.
                    try:
                        bars = tuple(source.read_daily_bars())
                        owner.daily_cache[key] = DailyPacket(request, bars, owner.clock())
                    except Exception:
                        owner.daily_cache[key] = None
                packet = owner.daily_cache[key]
                if packet is None:
                    raise DataSourceError("Recorded daily request failed; no refetch")
                owner.retrieved_at = packet.retrieved_at
                owner.daily_packets.append(replace(packet, request=request))
                return list(packet.bars)
        return Recorded()


def rebuild_case(case):
    sources = FrozenSources(case)
    context = build_intent_context(session=ResearchSession(), directory=case.directory, now=case.now, source=case.source)
    plan = execution_plan(case.checked, case.question, context)
    with bind_retrieval_clock(lambda:sources.retrieved_at), bind_research_request_ids(
            p.request.request_id for p in case.daily):
        bundle = collect_product_research(plan, data_service=sources, source_factory=sources.daily, now=case.now)
    if (canonical(plan.to_dict()) != case.expected_plan or canonical(bundle.to_dict()) != case.expected_bundle):
        raise ValueError("Frozen plan, evidence, timestamps or E references changed")
    return bundle


def freeze_batch(specs, directory, now, output, *, recorder, allowance=None):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    rows, statuses = [], []
    write_json(root/"collection-start.json", dict(version="sorting-collection-start-v1",
        started_at=now.isoformat(), case_ids=[s.id for s in specs], model_requests=0))
    def snapshot():
        return dict(version="news-triage-freeze-report-v1", status="completed" if len(rows)==30 else "partial",
            source_requests=allowance.used if allowance is not None else None, model_requests=0,
            cases=list(statuses), enablement=False)
    for index, spec in enumerate(specs):
        if allowance is not None and allowance.used >= allowance.limit:
            statuses.append(dict(id=spec.id, status="not_run", reason="source_budget"))
            continue
        recorder.batches, recorder.daily_packets = [], []
        try:
            question, checked, plan = prepare_case(spec, directory, now)
            with bind_retrieval_clock(lambda:recorder.retrieved_at), bind_research_request_ids(
                    "sorting-"+spec.id+"-daily-"+str(i) for i in range(len(plan.securities))):
                bundle = collect_product_research(plan, data_service=recorder, source_factory=recorder.daily, now=now)
            case = ReplayCase(spec.id, question, checked, now, directory,
                tuple(recorder.batches), tuple(recorder.daily_packets),
                expected_plan=canonical(plan.to_dict()), expected_bundle=canonical(bundle.to_dict()),
                case_group=spec.group, selection_reason=spec.reason)
            packet = root/(spec.id+".json")
            digest = freeze_case(case, packet)
            incomplete = any(s["status"] in ("unavailable", "partial") for c in bundle.companies for s in c.coverage)
            incomplete |= plan.objective.kind == "decision" and any(
                p.status != "available" for c in bundle.companies for p in c.trade_plans)
            if not incomplete:
                rebuild_case(case)
            original = model_research_context(bundle)
            final = prepare_answer_context(question, bundle)
            metrics = dict(full_evidence_count=len(bundle.evidence), selector_evidence_count=len(original["evidence"]),
                selector_json_chars=len(json.dumps(original,ensure_ascii=False)),
                final_json_chars=len(json.dumps(final,ensure_ascii=False)), final_E_ids=[e["id"] for e in final["evidence"]],
                selector_omitted=original["omitted_from_model"], final_omitted=final["omitted_from_model"],
                trimmed_after_enrichment=len(original["evidence"])-len(final["evidence"]),
                capacity_observation="reached" if final["omitted_from_model"] else "not_reached")
            if not incomplete:
                rows.append(dict(file=packet.name, sha256=digest, first="on" if index<15 else "off"))
            statuses.append(dict(id=spec.id, status="partial" if incomplete else "frozen",
                                 file=packet.name, sha256=digest, context_metrics=metrics))
        except Exception:
            statuses.append(dict(id=spec.id, status="partial", reason="packet_recording_failed"))
        except BaseException:
            statuses.append(dict(id=spec.id, status="partial", reason="collection_interrupted"))
            write_json(root/"collection-report.json", snapshot())
            raise
        finally:
            write_json(root/("collection-progress-"+str(index+1).zfill(2)+".json"), snapshot())
    report = snapshot()
    if len(rows)==30:
        report["manifest_sha256"] = write_json(root/"manifest.json", dict(version=VERSION, cases=rows))
        from trading_companion.news_triage_sorting_annotations import write_templates
        write_templates(root/"manifest.json")
    write_json(root/"collection-report.json", report)
    return report


def configured_recorder(transport):
    service = ResearchDataService(session=transport, max_workers=1)
    return RecordingSources(service, lambda request:TencentDailyBarSource(
        request.instrument_id, request.start_date, request.as_of_date, http_get=transport.get))
