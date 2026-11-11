"""Opt-in online ordering; no labels, evidence deletion, credentials at import, or retries."""

from concurrent.futures import FIRST_COMPLETED, wait
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
from time import monotonic

from trading_companion.deepseek_model import ANSWER_OUTPUT_TOKENS
from trading_companion.news_triage_product import identity_precheck, input_state
from trading_companion.news_triage_product_ensemble import combine, component
from trading_companion.news_triage_product_models import ProductJev, ProductDeepSeek
from trading_companion.news_triage_shadow import title_snapshot
from trading_companion.news_triage_sorting_control import (
    DEEPSEEK_TOKEN_LIMIT, JEV_TOKEN_LIMIT, SortingRound, invoke, submit_classifier,
)
from trading_companion.turn_control import TurnStopped, current_control
from trading_companion.typesafe_jev import ClassifierError

VERSION = "news-triage-sorting-v1"
_ORDER = ContextVar("headline_sort_order", default=None)


def estimated_tokens(value):
    """Conservative planning heuristic, not a tokenizer or hard billing cap."""
    text = value if type(value) is str else json.dumps(value, ensure_ascii=False)
    return (len(text.encode("utf-8"))+2)//3


class _BodyReady(BaseException):
    pass


def request_body(function):
    """Inspect exactly the frozen adapter's request without sending it."""
    body = None
    def preview(endpoint, **kwargs):
        nonlocal body
        body = kwargs["json"]
        raise _BodyReady()
    try:
        function(preview)
    except _BodyReady:
        return body
    raise ValueError("Classifier did not construct its fixed request")


@dataclass(frozen=True)
class SortOrder:
    company: str
    promoted: frozenset
    round: object


@contextmanager
def bind_sort_order(order):
    token = _ORDER.set(order)
    try:
        yield
    finally:
        _ORDER.reset(token)


def apply_context_order(selected, records):
    order = _ORDER.get()
    if order is None:
        return selected
    result = list(selected)
    for kind in ("news", "announcements"):
        belongs = lambda r: r["instrument_id"] == order.company and r["kind"] == kind
        positions = [i for i, r in enumerate(selected) if belongs(r)]
        original = [selected[i] for i in positions]
        if not original:
            continue
        rank = lambda r: r["id"] not in order.promoted
        candidate = sorted([r for r in records if belongs(r)], key=rank)[:len(original)]
        if len(json.dumps(candidate, ensure_ascii=False)) > len(json.dumps(original, ensure_ascii=False)):
            candidate = sorted(original, key=rank)
            order.round.capacity_fallbacks += 1
        for index, record in zip(positions, candidate):
            result[index] = record
    return result


def compatible_context(original, candidate, order):
    """After raw-value enrichment and repair trimming, protect every other slot."""
    before, after = original["evidence"], candidate["evidence"]
    if len(before) != len(after):
        return False
    for left, right in zip(before, after):
        target = left["instrument_id"] == order.company and left["kind"] in ("news", "announcements")
        if not target and left != right:
            return False
        if target and (right["instrument_id"], right["kind"]) != (left["instrument_id"], left["kind"]):
            return False
    return len({r["id"] for r in after}) == len(after)


class NewsTriageSorter:
    def __init__(self, *, configure=None, post=None, clock=monotonic, sink=None, attempt_guard=None):
        if configure is None:
            from trading_companion.model_registry import configured_news_sorting
            configure = configured_news_sorting
        self.configure, self.post, self.clock = configure, post, clock
        self.sink, self.attempt_guard = sink, attempt_guard

    def sort(self, bundle, *, directory, now, question):
        control = current_control()
        if (control is None or bundle.plan.explicit_historical
                or not set(bundle.plan.kinds) & {"news", "announcements"}):
            return None
        from trading_companion.turn_trace import news_triage_sink
        trace_sink = news_triage_sink()
        def publish(summary):
            if trace_sink is not None:
                trace_sink(summary)
            if self.sink is not None:
                self.sink(summary)
        round = SortingRound(control, clock=self.clock, sink=publish, attempt_guard=self.attempt_guard)
        try:
            snapshot = title_snapshot(bundle, captured_at=now)
            company = next((c for c in (snapshot or {}).get("companies", []) if c["titles"]), None)
            if company is None:
                round.finish("no_titles")
                return None
            queues = {k: [t for t in company["titles"] if t["kind"] == k] for k in ("news", "announcements")}
            rows = []
            for index in range(5):
                for kind in queues:
                    if index < len(queues[kind]):
                        title = queues[kind][index]
                        row = dict(title, id=title["evidence_id"], company_name=company["security_name"],
                                   capture_identity_checked=True)
                        row["identity"] = identity_precheck(row, directory)
                        rows.append(row)
            round.candidates = len(rows)
            # Pure constructors only. Both keys must exist before either provider is called.
            jev_config, ds_config = self.configure()
            from trading_companion.product_narrative_answer import prepare_answer_context, _messages
            baseline = prepare_answer_context(question, bundle)
            messages = _messages(question, baseline)
            answer_reserve = 2*(sum(estimated_tokens(m.content) for m in messages)+ANSWER_OUTPUT_TOKENS) + 700
            ds_config = replace(ds_config, timeout_seconds=3, max_output_tokens=2048)
            jev_config = replace(jev_config, timeout_seconds=2)
            chosen, estimate = [], 0
            jev_estimate = 0
            for row in rows:
                trial = chosen+[row]
                items = [{"id": r["id"], **input_state(r)} for r in trial]
                body = request_body(lambda post: ProductDeepSeek(ds_config, post=post).classify_questions_batch(items))
                proposed = estimated_tokens(body)+ds_config.max_output_tokens
                jbody = request_body(lambda post: ProductJev(jev_config, post=post).classify(input_state(row)))
                jtokens = estimated_tokens(jbody)+256
                if (proposed >= DEEPSEEK_TOKEN_LIMIT or jev_estimate+jtokens >= JEV_TOKEN_LIMIT
                        or not control.optional_model_available(proposed, answer_reserve)):
                    break
                chosen, estimate, jev_estimate = trial, proposed, jev_estimate+jtokens
            if not chosen:
                round.finish("triage_budget")
                return None
            round.covered = len(chosen)
            # Rolling concurrency, never a per-turn executor whose shutdown waits for late work.
            deadline = min(round.deadline, self.clock()+2)
            pending, first, next_index = {}, {}, 0
            def submit(index):
                row = chosen[index]
                future = submit_classifier(lambda: invoke(round, "jev",
                    lambda post: ProductJev(jev_config, post=post).classify(input_state(row)),
                    deadline=deadline, post=self.post))
                if future is None:
                    raise ClassifierError("triage_workers_busy")
                pending[future] = index
            while next_index < min(3, len(chosen)):
                submit(next_index)
                next_index += 1
            while pending:
                round.check(deadline)
                done, _ = wait(pending, timeout=min(.05, max(0, deadline-self.clock())), return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    first[index] = future.result()
                    if next_index < len(chosen):
                        submit(next_index)
                        next_index += 1
            round.check(deadline)
            items = [{"id": row["id"], **input_state(row)} for row in chosen]
            second = invoke(round, "deepseek", lambda post: ProductDeepSeek(ds_config, post=post).classify_questions_batch(items),
                deadline=min(round.deadline, self.clock()+3), estimate=estimate,
                answer_reserve=answer_reserve, post=self.post)
            if (second["extra_returned_items"] or second["unknown_returned_items"]
                    or any(item["status"] != "ok" for item in second["items"])):
                raise ClassifierError("triage_protocol")
            round.check(round.deadline)
            promoted = set()
            eids = {e.id: f"E{i}" for i, e in enumerate(bundle.evidence, 1)}
            for i, (row, ds_item) in enumerate(zip(chosen, second["items"])):
                left = component(dict(first[i], status="ok", reason="ok", call_ids=[i+1]), "jev")
                right = component(dict(second, status="ok", reason="ok", call_ids=[len(chosen)+1]), "deepseek", ds_item)
                result = combine(left, right)
                if (result["answers"]["importance"]["choice"] == "high"
                        and result["answers"]["relevance"]["choice"] in ("primary", "mentioned")):
                    promoted.add(eids[row["id"]])
            round.promoted = len(promoted)
            round.finish("classified")
            return SortOrder(company["instrument_id"], frozenset(promoted), round)
        except TurnStopped:
            round.finish("turn_stopped")
            raise
        except Exception as error:
            # ClassifierError codes are fixed local categories; never log raw exceptions.
            round.finish(error.code if isinstance(error, ClassifierError) else "triage_unavailable")
            control.checkpoint()
            return None
