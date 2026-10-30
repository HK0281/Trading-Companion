"""Application-owned subjects and whole-reply clarification parsing; no cached prices."""

from dataclasses import dataclass
from datetime import datetime
import re
import unicodedata

from trading_companion.security_directory import Security


@dataclass(frozen=True)
class ResearchSubject:
    securities: tuple[Security, ...]
    kind: str
    source: str
    development: bool
    sequence: int
    published_at: datetime
    scope: object | None = None


def new_question(question):
    return type(question) is str and re.match(r"\s*新问题\s*[:：]", question) is not None


def independent_task(question, directory=None):
    """A complete new task can escape pending; a bare candidate cannot."""
    if type(question) is not str:
        return False
    if new_question(question):
        return True
    from trading_companion.indicator_knowledge import parse_knowledge_question
    from trading_companion.open_knowledge import is_general_knowledge_question
    from trading_companion.screening_intent import is_screening_request
    if (parse_knowledge_question(question) is not None
            or is_general_knowledge_question(question, directory=directory)
            or is_screening_request(question) and not re.search(r"筛选条件|把.*改|再加", question)):
        return True
    if directory is None:
        return False
    clauses = re.split(r"[,，;；]", question, maxsplit=1)
    if len(clauses) == 2 and directory.lookup(clauses[0].strip()).status == "resolved":
        if re.match(r"\s*(?:另外|同时|并且|但|且|改|换|把|将|添加|增加|删除|不要|不用)", clauses[1]):
            return False
    from trading_companion.research_instruments import research_spans
    spans = research_spans(question, directory)
    remaining = question
    for start, end in reversed(spans):
        remaining = remaining[:start] + " " + remaining[end:]
    remaining = remaining.strip(" ，,。.!！?？、:：;；")
    # A selection plus a condition update belongs to the original task.
    if not spans or not remaining or re.match(r"^(?:我指的是|我说的是|就选|选择|选|是)$", remaining):
        return False
    if re.match(r"^(?:改看|改成|改为|换成|添加|增加|加上|删除|移除|把|将)", remaining) and not re.search(r"比较|对比", remaining):
        return False
    from trading_companion.product_plan import wants_product_research, is_explicit_technical_request
    return wants_product_research(question) or is_explicit_technical_request(question)


def continuation_question(question):
    """General contextual research, excluding explicit restarts and unrelated statements."""
    if type(question) is not str or new_question(question):
        return False
    from trading_companion.research_decision import wants_decision
    from trading_companion.product_scope import is_natural_technical_request, is_product_indicator_edit
    return wants_decision(question) or is_natural_technical_request(question) or is_product_indicator_edit(question) or bool(re.search(
        r"为什么|为何|依据|接下来|后续|走势|判断|结论|分析|风险|新闻|公告|财报|基本面|技术面|"
        r"这(?:只|家|两只|三只)|它(?:们)?|^那|继续|刷新|最新|现在.*(?:做|看)|还有.*(?:吗|么)", question))


def replaces_pending(question, directory=None):
    """Knowledge is an interlude; a new research/screening task replaces pending."""
    from trading_companion.indicator_knowledge import parse_knowledge_question
    from trading_companion.open_knowledge import is_general_knowledge_question
    return (not new_question(question) and independent_task(question, directory)
            and parse_knowledge_question(question) is None
            and not is_general_knowledge_question(question, directory=directory))


def plural_subject(question):
    return bool(re.search(r"两只|三只|分别|各自|它们|这几只|都.*(?:买|卖|看)|比较", question))


def latest_subject(session):
    subject = session.latest_research
    if subject is not None:
        return subject
    # Compatibility for trusted callers constructing a single pre-existing result.
    # Never choose between two caches without a publication record.
    saved, technical = session.product_saved, session.confirmed
    if saved is not None and technical is None:
        return ResearchSubject(saved.plan.securities, "technical" if saved.plan.kinds == ("technical",) else "company", saved.plan.source,
                               saved.plan.source == "csv", 0, saved.saved_at, saved.plan)
    if technical is not None and saved is None:
        return ResearchSubject((technical.security,), "technical", technical.request.source,
                               technical.development_source is not None, 0, technical.confirmed_at)
    return None


def checked_subject(session, *, directory, reference_date, development, now):
    subject = latest_subject(session)
    if subject is None:
        return (), "missing_research_subject"
    if session.latest_research is not None and not session._subject_current:
        return (), "no_current_research_subject"
    if subject.published_at > now:
        return (), "research_subject_time_invalid"
    directory.assert_usable(reference_date, development=development)
    securities = []
    for old in subject.securities:
        match = directory.lookup(old.instrument_id)
        if match.status != "resolved" or match.candidates[0].name != old.name:
            return (), "research_subject_identity_changed"
        securities.append(match.candidates[0])
    return tuple(securities), None


def selection_reply(question, candidates, directory):
    """Return a checked selection and all remaining text; never discard a suffix."""
    text = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！").strip()
    parts = re.split(r"\s*[,;]\s*", text, maxsplit=1)
    head, suffix = parts[0], parts[1] if len(parts) == 2 else ""
    head = re.sub(r"^(?:我指的是|我说的是|就选|选择|选|是)\s*", "", head)
    ordinal = re.fullmatch(r"第?([1-9一二两三四五六七八九])(?:个|只|支)?", head)
    if ordinal:
        value = ordinal[1]
        index = int(value) if value.isdigit() else "一二三四五六七八九".find(value.replace("两", "二")) + 1
        if 0 < index <= len(candidates):
            old = candidates[index-1]
            match = directory.lookup(old.instrument_id)
        else:
            return None, suffix
    else:
        match = directory.lookup(head)
    return (match.candidates[0] if match.status == "resolved" else None), suffix


def selection_conditions(conditions, suffix, *, reference_date, instrument_id):
    """Apply one supported whole condition update after selecting a candidate."""
    from trading_companion.research_update import parse_condition_update, update_conditions
    from trading_companion.agent_input import InputIssue
    text = re.sub(r"^改看\s*((?:近|最近|过去).+)$", r"改成\1", suffix)
    update = parse_condition_update(text)
    if update.kind == "instrument":
        raise InputIssue("needs_clarification", "condition_update_not_supported",
                         "候选选择后不能再指定另一只股票；请明确一个研究对象，日期或指标条件可以附在后面。")
    return update_conditions(conditions, update, reference_date=reference_date,
                             instrument_id=instrument_id)
