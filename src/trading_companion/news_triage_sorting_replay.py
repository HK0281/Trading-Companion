"""Fixed source packets through the checked product executor, no live source reads.

JSON uses a closed dataclass registry, never pickle, eval, or an artifact-supplied import.
The command only previews. A separately reviewed live driver calls run_pairs explicitly.
"""

from dataclasses import dataclass, fields, is_dataclass, asdict
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
from statistics import median
from threading import Lock
from time import monotonic

from trading_companion.company_research_contracts import (
    ResearchDataBatch, ResearchDataRequest, ResearchDataSection, ResearchEvidence,
)
from trading_companion.agent import _controlled
from trading_companion.daily_bar import DailyBar
from trading_companion.model import ModelCallError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.news_triage_sorting import NewsTriageSorter
from trading_companion.news_triage_sorting_control import token_usage
from trading_companion.news_triage_sorting_artifacts import canonical
from trading_companion.research_intent_adapter import execute_checked_intent
from trading_companion.research_intent_context import build_intent_context
from trading_companion.research_intent_v2 import CheckedIntent
from trading_companion.research_session import ResearchSession
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_control import company_turn_control

VERSION = "news-triage-sorting-pairs-v2"
LEGACY_VERSION = "news-triage-sorting-pairs-v1"
MAX_PACKET_BYTES = 16*1024*1024


@dataclass(frozen=True)
class ReplayCase:
    id: str
    question: str
    checked: CheckedIntent
    now: datetime
    directory: SecurityDirectory
    batches: tuple
    daily: tuple = ()  # (MultiIndicatorRequest, tuple[DailyBar]) pairs
    important_items: tuple = ()  # human-authored (description, tuple[E references])
    source: str = "tencent"
    expected_plan: dict | None = None
    expected_bundle: dict | None = None
    case_group: str | None = None
    selection_reason: str | None = None


@dataclass(frozen=True)
class DailyPacket:
    request: MultiIndicatorRequest
    bars: tuple
    retrieved_at: datetime


_TYPES = {c.__name__: c for c in (ReplayCase, DailyPacket, CheckedIntent, Security, SecurityDirectory,
    ResearchDataBatch, ResearchDataRequest, ResearchDataSection, ResearchEvidence,
    DailyBar, IndicatorSpec, MultiIndicatorRequest)}


def encode(value):
    if type(value) in (str,int,float,bool,type(None)):
        return value
    if type(value) in (date,datetime):
        return {"tag":type(value).__name__,"value":value.isoformat()}
    if type(value) in (list,tuple):
        return {"tag":type(value).__name__,"value":[encode(v) for v in value]}
    if type(value) is dict:
        if any(type(k) is not str for k in value):raise ValueError("Packet keys must be strings")
        return {"tag":"dict","value":{k:encode(v) for k,v in value.items()}}
    if is_dataclass(value) and _TYPES.get(type(value).__name__) is type(value):
        return {"tag":type(value).__name__,"value":{f.name:encode(getattr(value,f.name)) for f in fields(value) if f.init}}
    raise ValueError("Unregistered packet value")


def decode(value, depth=0):
    if depth>40:raise ValueError("Packet too deep")
    if type(value) in (str,int,float,bool,type(None)):return value
    if type(value) is not dict or set(value)!={"tag","value"}:raise ValueError("Invalid packet envelope")
    tag,raw=value["tag"],value["value"]
    if tag in ("date","datetime"):
        return (date if tag=="date" else datetime).fromisoformat(raw)
    if tag in ("list","tuple") and type(raw) is list:
        values=[decode(v,depth+1) for v in raw]
        return tuple(values) if tag=="tuple" else values
    if tag=="dict" and type(raw) is dict:return {k:decode(v,depth+1) for k,v in raw.items()}
    if tag in _TYPES and type(raw) is dict:
        cls=_TYPES[tag]
        expected={f.name for f in fields(cls) if f.init}
        legacy=expected-{"expected_plan","expected_bundle","case_group","selection_reason"}
        if set(raw)!=expected and not (cls is ReplayCase and set(raw)==legacy):
            raise ValueError("Packet fields mismatch")
        values={k:decode(v,depth+1) for k,v in raw.items()}
        if cls is IndicatorSpec:
            params=dict(values["_parameters"])
            if len(params)!=len(values["_parameters"]):raise ValueError("Duplicate indicator parameters")
            return IndicatorSpec(values["kind"],**params)
        return cls(**values)
    raise ValueError("Unknown packet type")


def freeze_case(case, path):
    body=(json.dumps(encode(case),ensure_ascii=False,allow_nan=False,sort_keys=True)+"\n").encode("utf-8")
    if len(body)>MAX_PACKET_BYTES:raise ValueError("Packet too large")
    with Path(path).open("xb") as stream:stream.write(body)
    return sha256(body).hexdigest()


def unique(pairs):
    value={}
    for k,v in pairs:
        if k in value:raise ValueError("Duplicate packet keys")
        value[k]=v
    return value


def load_manifest(path):
    path=Path(path)
    if path.stat().st_size>100000:raise ValueError("Manifest too large")
    raw=path.read_bytes()
    manifest=json.loads(raw.decode("utf-8"),object_pairs_hook=unique)
    if (type(manifest) is not dict or set(manifest)!={"version","cases"}
            or manifest["version"] not in (VERSION,LEGACY_VERSION)
            or type(manifest["cases"]) is not list or len(manifest["cases"])!=30):
        raise ValueError("Freeze exactly thirty cases")
    cases=[];ids=set()
    for row in manifest["cases"]:
        if set(row)!={"file","sha256","first"} or row["first"] not in ("on","off"):
            raise ValueError("Invalid pair manifest")
        packet=(path.parent/row["file"]).resolve()
        if not packet.is_relative_to(path.parent.resolve()) or packet.stat().st_size>MAX_PACKET_BYTES:
            raise ValueError("Packet must stay inside manifest directory")
        body=packet.read_bytes()
        if sha256(body).hexdigest()!=row["sha256"]:raise ValueError("Frozen packet changed")
        case=decode(json.loads(body.decode("utf-8"),object_pairs_hook=unique,
                   parse_constant=lambda _:(_ for _ in ()).throw(ValueError("Nonfinite packet"))))
        if (type(case) is not ReplayCase or not case.id or case.id in ids
                or type(case.checked) is not CheckedIntent or case.checked.task!="stock_research"
                or case.checked.operation!="new" or case.checked.outcome!="research"
                or case.now.tzinfo is None or type(case.important_items) is not tuple
                or any(type(b) is not ResearchDataBatch for b in case.batches)):
            raise ValueError("Invalid or incomplete frozen case")
        ids.add(case.id);cases.append((case,row["first"]))
    if sum(first=="on" for _,first in cases)!=15:raise ValueError("Freeze fifteen on-first pairs")
    return cases,dict(version=manifest["version"],manifest_sha256=sha256(raw).hexdigest(),pairs=30,turns=60,
        max_calls=dict(jev=300,deepseek=270,total=570),budget=dict(CNY="15",USD="1"),
        mode="plan_only",source_requests=0,intent_requests=0,
        notice="Separate live plan and price/usage checks required; no billing hard cap")


class PairBudget:
    """Count actual attempts, including existing product retries and failed calls."""
    def __init__(self, max_requests=570, *, attempt_sink=None):
        if type(max_requests) is not int or not 1 <= max_requests <= 570:
            raise ValueError("Use a global request limit between one and 570")
        self.lock=Lock();self.counts=dict(jev=0,deepseek=0)
        self.max_requests=max_requests
        self.attempt_sink=attempt_sink
        self.stop_reason=None
    def attempt(self,provider):
        with self.lock:
            if (self.stop_reason or sum(self.counts.values())>=self.max_requests
                    or self.counts[provider]>=(300 if provider=="jev" else 270)):
                self.stop_reason=self.stop_reason or "pair_call_budget"
                raise ModelCallError("pair_budget_exhausted","Paired evaluation allowance exhausted")
            self.counts[provider]+=1
            if self.attempt_sink is not None:
                try:self.attempt_sink(provider,dict(self.counts))
                except BaseException:
                    self.stop_reason="pair_audit_failed"
                    raise


@dataclass(frozen=True)
class VerifiedPrice:
    """An independently verified requested/returned model pair, per million tokens."""
    requested_model: str
    model: str
    input_rate: Decimal
    output_rate: Decimal

    def __post_init__(self):
        if (not self.requested_model or not self.model or any(
                type(v) is not Decimal or not v.is_finite() or v < 0
                for v in (self.input_rate, self.output_rate))):
            raise ValueError("Use verified model identities and finite prices")


class VerifiedCostGuard:
    """Local planning/observed stop thresholds, never a provider billing guarantee."""
    def __init__(self, prices, *, jev_alias_estimate=False):
        if set(prices) != {"jev", "deepseek"} or any(type(v) is not VerifiedPrice for v in prices.values()):
            raise ValueError("Resolve both providers' real model prices before execution")
        self.prices = dict(prices)
        if type(jev_alias_estimate) is not bool:
            raise ValueError("Use an explicit estimation policy")
        self.jev_alias_estimate=jev_alias_estimate
        self.alias_mismatches=0
        self.lock=Lock()
        self.entries={}
        self.serial=0
        self.spent = dict(jev=Decimal(0), deepseek=Decimal(0))
        self.limits = dict(jev=Decimal("1"), deepseek=Decimal("15"))

    def preflight(self, enabled, budget):
        # Allowance is for the whole next arm, not just the next title.
        with budget.lock:
            if (sum(budget.counts.values())>=budget.max_requests or budget.counts["deepseek"]>=270
                    or enabled and budget.counts["jev"]>=300):
                budget.stop_reason=budget.stop_reason or "pair_call_budget"
        for provider, tokens in (("jev", 40000 if enabled else 0), ("deepseek", 24000)):
            price = self.prices[provider]
            reserve = Decimal(tokens)*max(price.input_rate, price.output_rate)/1000000
            if self.spent[provider]+reserve >= self.limits[provider]:
                budget.stop_reason = budget.stop_reason or "pair_money_budget"

    def reconcile(self, arm, budget, *, ledger_key=None):
        with self.lock:
            if ledger_key is None:
                self.serial+=1
                ledger_key=("standalone",self.serial)
            return self._reconcile(arm,budget,ledger_key)

    def _reconcile(self, arm, budget, ledger_key):
        calls = [("triage",c.get("id",i),c) for i,c in enumerate((arm.get("triage") or {}).get("calls", []))]
        calls += [("answer",i,c) for i,c in enumerate(arm["answer_calls"])]
        known = True
        for kind, index, call in calls:
            key=(ledger_key,kind,index)
            provider = call.get("provider")
            price = self.prices.get(provider)
            usage = token_usage(call)
            estimated=provider=="jev" and self.jev_alias_estimate
            reserved=call.get("reserved_tokens")
            no_response=(call.get("status")=="timeout_estimated" and call.get("response_received") is False
                         and call.get("usage") is None and type(reserved) is int
                         and 0 < reserved <= (40000 if provider=="jev" else 24000))
            if (price is None or call.get("requested_model") != price.requested_model
                    or not no_response and (usage is None or not call.get("model"))
                    or call.get("model") is not None and not estimated and call["model"] != price.model):
                known = False
                budget.stop_reason = budget.stop_reason or "price_or_usage_unknown"
                continue
            prior=self.entries.get(key,{"amount":Decimal(0),"observed":Decimal(0),"alias":False})
            mismatch=estimated and call.get("model") is not None and call["model"]!=price.model
            if mismatch and not prior["alias"]:self.alias_mismatches+=1
            observed=(Decimal(usage.prompt_tokens)*price.input_rate
                + Decimal(usage.completion_tokens)*price.output_rate)/1000000 if usage else Decimal(0)
            amount=Decimal(reserved)*max(price.input_rate,price.output_rate)/1000000 if no_response else observed
            # Late observations can top up an estimate, but never release its reserve
            # or charge the same attempted request twice.
            amount=max(prior["amount"],amount)
            self.spent[provider] += amount-prior["amount"]
            self.entries[key]=dict(provider=provider,amount=amount,observed=max(prior["observed"],observed),
                                   alias=prior["alias"] or mismatch)
            if self.spent[provider] >= self.limits[provider]:
                budget.stop_reason = budget.stop_reason or "pair_money_budget"
        observed_ds=sum((e["observed"] for e in self.entries.values() if e["provider"]=="deepseek"),Decimal(0))
        estimated_cost={p:sum((e["amount"]-e["observed"] for e in self.entries.values() if e["provider"]==p),Decimal(0))
                        for p in self.prices}
        return dict(known=known, observed_CNY=str(observed_ds), charged_CNY=str(self.spent["deepseek"]),
                    charged_USD=str(self.spent["jev"]), timeout_estimated_CNY=str(estimated_cost["deepseek"]),
                    timeout_estimated_USD=str(estimated_cost["jev"]),
                    observed_USD=None if self.jev_alias_estimate else str(sum(
                        (e["observed"] for e in self.entries.values() if e["provider"]=="jev"),Decimal(0))),
                    jev_estimated_USD=str(self.spent["jev"]) if self.jev_alias_estimate else None,
                    jev_cost_status="estimated_unverified" if self.jev_alias_estimate else "verified_identity",
                    jev_alias_mismatches=self.alias_mismatches)


class FrozenSources:
    def __init__(self,case):self.case=case;self.retrieved_at=case.now
    def collect(self,request,kinds):
        found=[b for b in self.case.batches if b.request==request
               and tuple(s.kind for s in b.source_results)==tuple(kinds)]
        if len(found)!=1:raise ValueError("Frozen company request mismatch")
        return found[0]
    def daily(self,request):
        packets=[p if type(p) is DailyPacket else DailyPacket(p[0],p[1],self.case.now) for p in self.case.daily]
        found=[p for p in packets if p.request==request]
        if len(found)!=1:raise ValueError("Frozen daily request mismatch")
        class Source:
            source_name=request.source
            instrument_id=request.instrument_id
            start_date=request.start_date
            end_date=request.as_of_date
            adjustment="unadjusted"
        owner=self
        class Recorded(Source):
            def read_daily_bars(self):
                owner.retrieved_at=found[0].retrieved_at
                return list(found[0].bars)
        return Recorded()


def run_pairs(cases, *, model_factory, sorter_factory, cost_guard, budget=None, clock=monotonic,
              after_turn=None, progress=None, triage_sink=None):
    """Explicit trusted drivers only; the CLI never implicitly configures real models.

    cost_guard requires the separately approved prices. Tests use synthetic prices.
    """
    if (len(cases)!=30 or type(cost_guard) is not VerifiedCostGuard
            or any(type(c) is not ReplayCase or first not in ("on", "off") for c, first in cases)
            or len({c.id for c, _ in cases}) != 30 or sum(first == "on" for _, first in cases) != 15):
        raise ValueError("Use thirty frozen pairs and a verified cost guard")
    budget=budget or PairBudget();rows=[]
    def snapshot():
        return dict(version=VERSION,status="partial" if budget.stop_reason else "running",rows=rows,
                    attempts=dict(budget.counts),stop_reason=budget.stop_reason,enablement=False)
    for case,first in cases:
        pair=dict(id=case.id,question=case.question,important_items=case.important_items,arms={})
        rows.append(pair)
        for enabled in ((True,False) if first=="on" else (False,True)):
            cost_guard.preflight(enabled, budget)
            if budget.stop_reason:break
            model=model_factory();sources=FrozenSources(case);contexts=[];responses=[];triage=[]
            class Metered:
                def complete(self,messages,*,tools):
                    budget.attempt("deepseek")
                    call_started=clock()
                    phase="repair" if "仅重写一次" in messages[-1].content else "draft"
                    for message in messages:
                        if message.content.startswith("UNTRUSTED_RESEARCH_CONTEXT\n"):
                            contexts.append(json.loads(message.content.partition("\n")[2]))
                    reservation=24000  # Whole-turn conservative token allowance, recorded before sending.
                    requested=cost_guard.prices["deepseek"].requested_model
                    try:
                        response=model.complete(messages,tools=tools)
                    except Exception as error:
                        no_response=isinstance(error,ModelCallError) and error.code in ("timeout","connection_error")
                        responses.append(dict(provider="deepseek",requested_model=requested,model=None,usage=None,phase=phase,
                                              reserved_tokens=reservation,response_received=not no_response,
                                              status="timeout_estimated" if no_response else "usage_unknown",
                                              elapsed_ms=round((clock()-call_started)*1000)))
                        raise
                    responses.append(dict(provider=response.provider,requested_model=response.requested_model,
                        model=response.model,usage=asdict(response.usage) if response.usage else None,phase=phase,
                        reserved_tokens=reservation,response_received=True,status="ok",
                        elapsed_ms=round((clock()-call_started)*1000)))
                    return response
            settlement={"ready":False,"lock":Lock()}
            ledger_key=(case.id,"on" if enabled else "off")
            def receive(summary, *, case_id=case.id, side="on" if enabled else "off", saved=triage,
                        state=settlement,key=ledger_key):
                with state["lock"]:
                    saved.append(summary)
                    if state["ready"]:
                        cost_guard.reconcile(dict(triage=summary,answer_calls=[]),budget,ledger_key=key)
                if triage_sink is not None:triage_sink(case_id,side,summary)
            sorter=sorter_factory(receive,budget.attempt) if enabled else None
            if sorter is not None and type(sorter) is not NewsTriageSorter:raise ValueError("Use trusted sorter")
            session=ResearchSession();control=company_turn_control()
            started=clock()
            def execute(checked_model,working):
                context=build_intent_context(session=working,directory=case.directory,now=case.now,source=case.source)
                return execute_checked_intent(case.checked,case.question,context=context,model=checked_model,session=working,
                    control=control,data_service=sources,source_factory=sources.daily,clock=lambda:case.now,
                    headline_sorter=sorter)
            from trading_companion.multi_analysis import bind_retrieval_clock
            from trading_companion.product_research import bind_research_request_ids
            identifiers=[p.request.request_id if type(p) is DailyPacket else p[0].request_id for p in case.daily]
            with bind_retrieval_clock(lambda:sources.retrieved_at), bind_research_request_ids(identifiers):
                result=_controlled(execute,model=Metered(),session=session,control=control,default_calls=6,
                                   input_mode="natural",share_nested=True)
            pair["arms"]["on" if enabled else "off"]=dict(elapsed_ms=round((clock()-started)*1000),
                status=result.status,code=result.code,answer=result.product_answer.to_dict() if result.product_answer else None,
                bundle=result.product_bundle.to_dict() if result.product_bundle else None,
                contexts=contexts,answer_calls=responses,triage=triage[-1] if triage else None,control=control.to_dict())
            arm=pair["arms"]["on" if enabled else "off"]
            if (case.expected_bundle is not None and canonical(arm["bundle"])!=case.expected_bundle
                    or case.expected_plan is not None and canonical((arm["bundle"] or {}).get("plan"))!=case.expected_plan):
                budget.stop_reason=budget.stop_reason or "frozen_execution_failed"
            # Keep the existing answer retry/repair policy inside this arm; unknown
            # costs stop the following arm, never silently change product execution.
            if any(token_usage(call) is None and call.get("status")!="timeout_estimated" for call in responses):
                budget.stop_reason=budget.stop_reason or "answer_usage_unknown"
            with settlement["lock"]:
                arm["triage"]=triage[-1] if triage else None
                arm["costs"]=cost_guard.reconcile(arm,budget,ledger_key=ledger_key)
                settlement["ready"]=True
            if after_turn is not None:after_turn(arm,budget)
            if result.product_bundle is None or result.product_answer is None or not contexts:
                budget.stop_reason=budget.stop_reason or "frozen_execution_failed"
            if progress is not None:progress(snapshot())
        if budget.stop_reason:break
    return dict(version=VERSION,status="partial" if budget.stop_reason else "awaiting_blind_review",rows=rows,
                attempts=dict(budget.counts),stop_reason=budget.stop_reason,enablement=False)


def paired_summary(report, reviews):
    """Human review is required; absence of errors is never inferred from missing rows."""
    changed=improved=0;deltas=[];errors=0;token_deltas=[];stage_metrics=[]
    for row in report["rows"]:
        arms=row["arms"]
        if set(arms)!={"on","off"}:continue
        left,right=arms["off"],arms["on"]
        errors+=int(left["bundle"] is None or left["bundle"]!=right["bundle"])
        context_changed=bool(left["contexts"] and right["contexts"] and
                             left["contexts"][0]["evidence"]!=right["contexts"][0]["evidence"])
        changed+=context_changed
        deltas.append(right["elapsed_ms"]-left["elapsed_ms"])
        def totals(arm):
            calls=(arm.get("triage") or {}).get("calls",[])
            result={}
            for provider in ("jev","deepseek"):
                usages=[c.get("usage") for c in calls if c["provider"]==provider]
                if provider=="deepseek":usages += [c.get("usage") for c in arm["answer_calls"]]
                result[provider]=sum(u["total_tokens"] for u in usages) if all(u and u.get("total_tokens") is not None for u in usages) else None
            return result
        a,b=totals(left),totals(right)
        token_deltas.append({k:b[k]-a[k] if a[k] is not None and b[k] is not None else None for k in a})
        stages={}
        for name,arm in arms.items():
            triage=arm.get("triage") or {}
            calls=triage.get("calls",[])
            groups=dict(jev=[c for c in calls if c["provider"]=="jev"],
                        deepseek_triage=[c for c in calls if c["provider"]=="deepseek"],
                        draft=[c for c in arm["answer_calls"] if c["phase"]=="draft"],
                        repair=[c for c in arm["answer_calls"] if c["phase"]=="repair"])
            stages[name]=dict(turn_ms=arm["elapsed_ms"],triage_ms=triage.get("elapsed_ms",0),
                tokens={k:sum(token_usage(c).total_tokens for c in group)
                        if all(token_usage(c) is not None for c in group) else None for k,group in groups.items()},
                request_ms={k:sum(c["elapsed_ms"] for c in group)
                            if all(c.get("elapsed_ms") is not None for c in group) else None for k,group in groups.items()})
        stage_metrics.append(dict(id=row["id"],arms=stages))
        review=reviews.get(row["id"],{})
        if set(review)!={"baseline_omissions","new_omissions","cross_company_errors","citation_errors","important_promoted"}:
            errors+=1;continue
        values=[v for k,v in review.items() if k!="baseline_omissions"]
        if type(review["baseline_omissions"]) is not list or any(type(v) is not str for v in review["baseline_omissions"]):
            errors+=1;continue
        if any(type(v) is not int or v<0 for v in values):errors+=1;continue
        errors+=sum(review[k] for k in ("new_omissions","cross_company_errors","citation_errors"))
        improved+=bool(context_changed and row["important_items"] and review["important_promoted"]>0)
    p95=sorted(deltas)[ceil(len(deltas)*.95)-1] if deltas else None
    passed=(report["status"]=="awaiting_blind_review" and len(deltas)==30
            and len({row["id"] for row in report["rows"]})==30 and not errors
            and changed>=15 and improved>=10 and p95 is not None and p95<=4000)
    return dict(passed=passed,changed_pairs=changed,important_promoted_pairs=improved,
                errors_or_missing_reviews=errors,elapsed_delta_p95_ms=p95,
                elapsed_delta_median_ms=median(deltas) if deltas else None,
                elapsed_delta_max_ms=max(deltas) if deltas else None,
                elapsed_deltas_ms=deltas,token_deltas=token_deltas,stage_metrics=stage_metrics,enablement=False)
