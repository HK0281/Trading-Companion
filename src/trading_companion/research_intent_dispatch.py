"""Unwired phase-5A orchestration: one control, one pure intent, one legacy fallback."""

from time import monotonic
from contextvars import ContextVar

from trading_companion.agent import _controlled
from trading_companion.model import ModelConfigurationError
from trading_companion.research_intent_context import IntentContextError, IntentRequest
from trading_companion.research_intent_v2 import local_preflight, understand_active
from trading_companion.request_routing import _command_text, _COMMANDS, _HISTORY
from trading_companion.turn_control import TurnControl
from trading_companion.turn_trace import trace_intent_stage


_DISPATCHING = ContextVar("research_intent_dispatching", default=False)


def run_intent_dispatch_turn(question, *, model, intent_model, session, control,
                             request_factory, legacy, execute, history=False, clock=monotonic):
    """Trusted callbacks only; the service rejects enabling this unfinished path."""
    if (type(control) is not TurnControl or type(history) is not bool
            or not all(callable(f) for f in (request_factory, legacy, execute, clock))):
        raise ModelConfigurationError("Use trusted intent dispatch dependencies")
    if _DISPATCHING.get():
        raise ModelConfigurationError("Intent dispatch cannot re-enter itself")

    def measured(stage, function):
        before, start, code = control.to_dict(), clock(), "ok"
        request_characters = None
        try:
            result = function()
            if stage == "context" and type(result) is IntentRequest:
                request_characters = result.serialized_characters
            return result
        except IntentContextError as error:
            code = error.code
            request_characters = error.serialized_characters
            raise
        except BaseException as error:
            code = getattr(error, "code", "intent_model_failure")
            raise
        finally:
            trace_intent_stage(stage, code, before, control.to_dict(),
                               max(0, round((clock() - start) * 1000)), request_characters)

    def action(checked_model, working):
        def old_path():
            return measured("legacy", lambda: legacy(question, model=model,
                session=working, control=control))

        def fallback():
            control.checkpoint()
            if control.to_dict()["model_calls_attempted"] >= control.limits.model_calls:
                control.reserve_model()
            return old_path()

        # These exact control operations have no semantic-context dependency.
        if history or (type(question) is str and _command_text(question) in (*_COMMANDS, *_HISTORY)):
            return old_path()
        try:
            request = measured("context", lambda: request_factory(question, working))
            if type(request) is not IntentRequest:
                raise ModelConfigurationError("Use the program-built intent request")
        except IntentContextError:
            return fallback()
        local = local_preflight(question, request.context)
        if local is not None:
            return measured("local", lambda: execute(local, model=model, session=working, control=control))

        before, start = control.to_dict(), clock()
        run = None
        try:
            run = understand_active(question, model=intent_model, context=request.context, control=control)
        finally:
            after = control.to_dict()
            code = (after["stop_code"] or (run.code if run is not None else "intent_model_failure"))
            trace_intent_stage("understanding", code, before, after,
                               max(0, round((clock() - start) * 1000)))
        control.checkpoint()
        if run.status != "ok":
            # Enter the existing exhaustion path without reserving a phantom call.
            return fallback()
        return measured("execution", lambda: execute(run.intent, model=model, session=working, control=control))

    token = _DISPATCHING.set(True)
    try:
        return _controlled(action, model=model, control=control, default_calls=6,
            input_mode="natural", session=session, share_nested=True)
    finally:
        _DISPATCHING.reset(token)
