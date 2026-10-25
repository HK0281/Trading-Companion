"""Request-bound workflow prose with independently rechecked snapshot evidence."""

from dataclasses import replace
import json
import re
import unicodedata

from trading_companion.multi_contracts import MultiIndicatorRequest, MultiIndicatorResult
from trading_companion.relation_explanation import (
    EXPLANATION_VERSION, ExplanationValidation, validate_indicator_explanation,
)
from trading_companion.research_report import build_research_report, indicator_label
from trading_companion.research_response import ResponseValidation, validate_research_response
from trading_companion.research_tool import ToolExecution


WORKFLOW_RESPONSE_VERSION = "research-workflow-response-v1"
WORKFLOW_RESPONSE_INSTRUCTION = (
    'Final ONLY JSON keys: version,request_id,instrument_id,trade_date,claims,paragraphs. '
    'version="research-workflow-response-v1"; IDs/date from primary (dates[0]). '
    'claims:1-8 distinct available rows as '
    '{"snapshot":"primary","evidence_id":"fN","relation":"<field>","value":"<value>"}, '
    'copy alias/field/value; use supplement for supplemental claims. paragraphs:exactly 3 objects '
    '{"role":"answer","text":"<Chinese>","evidence_ids":["primary:f7"]}, '
    'roles answer/outlook/watch_for in order; cite 1-8 matching claims (e.g. supplement:f7). '
    'Each text 1-600 chars: direct view/conflicts, conditional outlook, confirm/invalidate; '
    'aim 200-350 total. No headings/tables/raw IDs/digits except exact indicator labels; '
    'no invented news/status/targets. Position is not crossing, volatility is not direction, '
    'signals are not votes. CSV/synthetic is development data, not a real stock outlook. '
)


class _Invalid(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise _Invalid(code)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "workflow_response_duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise _Invalid("workflow_response_invalid_json")


def _observations(observations, expected_request):
    """Validate the common frozen snapshot without importing the workflow dispatcher."""
    _require(type(expected_request) is MultiIndicatorRequest,
             "workflow_response_context_mismatch")
    _require(type(observations) in (tuple, list) and 1 <= len(observations) <= 2,
             "workflow_response_invalid_observations")
    indexed = {}
    for observation in observations:
        key = getattr(observation, "key", None)
        execution = getattr(observation, "execution", None)
        _require(type(key) is str and key in ("primary", "supplement") and key not in indexed
                 and type(execution) is ToolExecution and execution.status == "executed"
                 and type(execution.full_result) is MultiIndicatorResult,
                 "workflow_response_invalid_observations")
        indexed[key] = execution
    _require("primary" in indexed, "workflow_response_invalid_observations")
    primary = indexed["primary"].full_result
    _require(build_research_report(primary, expected_request=expected_request).status
             in ("complete", "limited") and primary.synthesis_allowed,
             "workflow_response_context_mismatch")
    for key, execution in indexed.items():
        if key == "primary":
            continue
        result = execution.full_result
        _require(type(result.request) is MultiIndicatorRequest,
                 "workflow_response_snapshot_mismatch")
        _require(result.request.request_id != primary.request.request_id
                 and result.request.instrument_id == primary.request.instrument_id
                 and result.request.start_date == primary.request.start_date
                 and result.request.as_of_date == primary.request.as_of_date
                 and result.request.source == primary.request.source
                 and result.source == primary.source
                 and result.price_adjustment == primary.price_adjustment
                 and result.data_authenticity == primary.data_authenticity
                 and result.retrieved_at == primary.retrieved_at
                 and result.bars == primary.bars
                 and result.data_status == primary.data_status
                 and build_research_report(result, expected_request=result.request).status
                 in ("complete", "limited"),
                 "workflow_response_snapshot_mismatch")
    return indexed


def validate_workflow_response(text, *, observations, expected_request):
    """Check all selected relation claims; this does not verify arbitrary prose semantics."""
    request_id = expected_request.request_id if type(expected_request) is MultiIndicatorRequest else None
    try:
        indexed = _observations(observations, expected_request)
        _require(type(text) is str and 0 < len(text) <= 8000,
                 "workflow_response_invalid_json")
        try:
            payload = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, RecursionError) as error:
            if isinstance(error, _Invalid):
                raise
            raise _Invalid("workflow_response_invalid_json") from error
        _require(type(payload) is dict, "workflow_response_invalid_shape")
        if payload.get("version") != WORKFLOW_RESPONSE_VERSION:
            # Legacy responses remain primary-only even when a supplement exists.
            return validate_research_response(text, execution=indexed["primary"],
                                              expected_request=expected_request)
        _require(payload.keys() == {"version", "request_id", "instrument_id", "trade_date",
                                    "claims", "paragraphs"}, "workflow_response_invalid_shape")
        primary = indexed["primary"].full_result
        _require(payload["request_id"] == expected_request.request_id
                 and payload["instrument_id"] == expected_request.instrument_id
                 and payload["trade_date"] == primary.latest_trade_date.isoformat(),
                 "workflow_response_context_mismatch")
        claims = payload["claims"]
        _require(type(claims) is list and 1 <= len(claims) <= 8,
                 "workflow_response_invalid_claims")
        groups, seen = {}, set()
        for claim in claims:
            _require(type(claim) is dict
                     and claim.keys() == {"snapshot", "evidence_id", "relation", "value"}
                     and all(type(value) is str and 0 < len(value) <= 100
                             for value in claim.values()), "workflow_response_invalid_claims")
            key, alias = claim["snapshot"], claim["evidence_id"]
            _require(key in indexed and re.fullmatch(r"f[0-9]+", alias) is not None
                     and (key, alias) not in seen, "workflow_response_invalid_claims")
            seen.add((key, alias))
            groups.setdefault(key, []).append({field: claim[field]
                                                for field in ("evidence_id", "relation", "value")})
        checked_claims = []
        for key, group in groups.items():
            execution = indexed[key]
            request = execution.full_result.request
            envelope = {"version": EXPLANATION_VERSION, "request_id": request.request_id,
                        "instrument_id": request.instrument_id,
                        "trade_date": execution.full_result.latest_trade_date.isoformat(), "claims": group}
            relations = validate_indicator_explanation(json.dumps(envelope, ensure_ascii=False),
                execution=execution, expected_request=request)
            if relations.status != "accepted":
                return ResponseValidation("rejected", relations.code,
                    ExplanationValidation("rejected", relations.code, request_id))
            checked_claims.extend(replace(claim, evidence_id=f"{key}:{claim.evidence_id}")
                                  for claim in relations.claims)
        allowed = {claim.evidence_id: claim.fact_id for claim in checked_claims}
        paragraphs = payload["paragraphs"]
        _require(type(paragraphs) is list and len(paragraphs) == 3,
                 "workflow_response_invalid_paragraphs")
        # A label is numeric-safe only if it belongs to a snapshot used by a checked claim.
        labels = {indicator_label(spec) for key in groups
                  for spec in indexed[key].full_result.request.indicators}
        checked_paragraphs = []
        for paragraph, role in zip(paragraphs, ("answer", "outlook", "watch_for")):
            _require(type(paragraph) is dict
                     and paragraph.keys() == {"role", "text", "evidence_ids"}
                     and paragraph["role"] == role and type(paragraph["text"]) is str
                     and 1 <= len(paragraph["text"].strip()) <= 600
                     and not any(unicodedata.category(character) == "Cc"
                                 for character in paragraph["text"]),
                     "workflow_response_invalid_paragraphs")
            citations = paragraph["evidence_ids"]
            _require(type(citations) is list and 1 <= len(citations) <= 8
                     and all(type(alias) is str and alias in allowed for alias in citations)
                     and len(set(citations)) == len(citations),
                     "workflow_response_invalid_citations")
            remaining = paragraph["text"]
            for label in sorted(labels, key=len, reverse=True):
                remaining = remaining.replace(label, "")
            _require(re.search(r"\d", remaining) is None,
                     "workflow_response_numeric_prose_not_supported")
            checked_paragraphs.append((role, paragraph["text"].strip(),
                                       tuple(allowed[alias] for alias in citations)))
        relations = ExplanationValidation("accepted", "indicator_relations_checked", request_id,
                                          tuple(checked_claims))
        return ResponseValidation("accepted", "workflow_response_evidence_checked", relations,
                                  tuple(checked_paragraphs))
    except (ValueError, TypeError, AttributeError, RecursionError) as error:
        code = str(error) if isinstance(error, _Invalid) else "workflow_response_invalid_shape"
        return ResponseValidation("rejected", code,
                                  ExplanationValidation("rejected", code, request_id))
