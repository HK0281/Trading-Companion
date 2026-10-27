"""Shortlist continuation using the existing research executors and one shared turn."""

from dataclasses import replace

from trading_companion.agent import AgentTurnResult, _controlled, _has_research, _run_prepared
from trading_companion.product_agent import _execute as execute_product
from trading_companion.product_plan import is_explicit_technical_request
from trading_companion.research_intent import prepare_research_question
from trading_companion.screening_context import select_screening
from trading_companion.turn_control import checkpoint


def _execute(question, *, model, session, directory, quote_source, source, development_source,
             now, reference_date, data_service, market_background_source, clock, workflow):
    selection = select_screening(question, saved=session.screening_saved, directory=directory,
        quote_source=quote_source, source="csv" if development_source is not None else source,
        development=development_source is not None, now=clock())
    if not selection.ready:
        if selection.invalidate:
            session.clear_screening()
        return AgentTurnResult(status="needs_clarification", code=selection.code, message=selection.message,
                               stage="clarification", input_mode="natural", user_question=question)
    # Selecting a new subject consumes old research even if interpretation or collection fails.
    session.begin_research(preserve_screening=True)
    effective = selection.effective_question
    if len(selection.securities) == 1 and is_explicit_technical_request(effective):
        prepared = prepare_research_question(effective, model=model, directory=directory,
            source="csv" if development_source is not None else source, reference_date=reference_date,
            development=development_source is not None)
        checkpoint()
        if prepared.status == "ready" and prepared.security != selection.securities[0]:
            return AgentTurnResult(status="needs_clarification", code="screening_selection_plan_mismatch",
                stage="clarification", input_mode="natural", message="研究计划与选择的名单对象不一致，未查询研究数据。",
                model_responses=(prepared.model_response,) if prepared.model_response is not None else (),
                user_question=question)
        prepared = replace(prepared, user_question=question)
        outcome = _run_prepared(prepared, model=model, development_source=development_source,
                                question=question, workflow=workflow)
        checkpoint()
        if (_has_research(outcome) and outcome.safe_answer is not None
                and outcome.safe_answer.status in ("complete", "limited")):
            session.remember_confirmed(prepared, now=now, development_source=development_source)
            session.remember_result(prepared, outcome.research_result, now=now, development_source=development_source)
        return replace(outcome, user_question=question, session_event="screening_research")
    if data_service is None:
        return AgentTurnResult(status="needs_clarification", code="screening_research_unavailable",
            stage="clarification", input_mode="natural", message="已识别名单对象，但当前服务未配置公司研究能力。",
            user_question=question)
    outcome = execute_product(effective, model=model, session=session, directory=directory, now=now,
        reference_date=reference_date, source=source, development_source=development_source,
        data_service=data_service, source_factory=None, market_background_source=market_background_source,
        background_clock=clock, selected_securities=selection.securities, original_question=question)
    return replace(outcome, user_question=question)


def run_screening_followup_turn(question, *, model, session, directory, quote_source, source, now,
                                reference_date=None, development_source=None, data_service=None,
                                market_background_source=None, clock=None, control=None, workflow=True):
    from datetime import datetime, timedelta, timezone
    from trading_companion.turn_control import company_turn_control, workflow_turn_control

    clock = clock or (lambda: datetime.now(timezone.utc))
    reference = reference_date or now.astimezone(timezone(timedelta(hours=8))).date()
    return _controlled(lambda checked, working: _execute(question, model=checked, session=working,
        directory=directory, quote_source=quote_source, source=source, development_source=development_source,
        now=now, reference_date=reference, data_service=data_service,
        market_background_source=market_background_source, clock=clock, workflow=workflow),
        model=model, session=session, control=control or (company_turn_control() if data_service is not None
                                                         else workflow_turn_control()),
        default_calls=6, input_mode="natural")
