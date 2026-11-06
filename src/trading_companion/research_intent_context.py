"""Build the frozen candidate's context from bounded, read-only program state."""

from copy import copy
from dataclasses import dataclass
from datetime import timedelta, timezone
import json

from trading_companion.conversation_memory import TurnDigest
from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.model import ModelConfigurationError, ModelRequestError
from trading_companion.research_intent_v2 import IntentContext, TOOL, request_messages
from trading_companion.research_session import ResearchSession
from trading_companion.tool_model import MAX_TOTAL_CHARACTERS, MAX_USER_CHARACTERS, validate_tool_messages


class IntentContextError(Exception):
    def __init__(self, code, *, serialized_characters=None):
        if code not in ("intent_context_invalid", "intent_context_unavailable", "context_request_too_large"):
            raise ModelConfigurationError("Use an application-owned intent context failure")
        self.code = code
        self.serialized_characters = serialized_characters
        super().__init__(code)


@dataclass(frozen=True)
class IntentRequest:
    context: IntentContext
    messages: tuple
    serialized_characters: int


def build_intent_context(*, session, directory, now, reference_date=None, source="tencent",
                         development=False, digests=(), quote_source=None, company_enabled=True,
                         workflow_enabled=True, conversation_memory=True):
    """Caller owns the turn lock; no cache invalidation can reach its session."""
    if (type(session) is not ResearchSession or type(digests) not in (list, tuple)
            or any(type(d) is not TurnDigest for d in digests)):
        raise IntentContextError("intent_context_invalid")
    snapshot = copy(session)
    snapshot._turn_parent = None
    try:
        reference = reference_date if reference_date is not None else now.astimezone(
            timezone(timedelta(hours=8))).date()
        history = tuple(d.question for d in digests[-6:]) if conversation_memory else ()
        return IntentContext(snapshot, directory, now, reference, source, development,
            history, quote_source, company_enabled, workflow_enabled, conversation_memory)
    except (AttributeError, TypeError, ValueError, ModelConfigurationError):
        raise IntentContextError("intent_context_invalid") from None


def prepare_intent_request(question, *, development_source=None, **configuration):
    if (type(question) is not str or not 0 < len(question.strip()) <= MAX_USER_CHARACTERS
            or any(not c.isprintable() and c not in "\n\r\t" for c in question)):
        raise IntentContextError("intent_context_invalid")
    context = build_intent_context(**configuration)
    try:
        from trading_companion.decision_horizon import project_delivery
        context = project_delivery(context, question, development_source=development_source)
        messages = request_messages(question, context)
        total = sum(len(json.dumps(t.to_dict(), ensure_ascii=True)) for t in (TOOL,))
        total += sum(len(json.dumps(m.to_dict(), ensure_ascii=True)) for m in messages)
        if total > MAX_TOTAL_CHARACTERS:
            raise IntentContextError("context_request_too_large", serialized_characters=total)
        validate_tool_messages(messages, tools=(TOOL,))
    except ModelRequestError:
        raise IntentContextError("intent_context_invalid") from None
    except (DataValidationError, InvalidRequestError):
        raise IntentContextError("intent_context_unavailable") from None
    return IntentRequest(context, messages, total)
