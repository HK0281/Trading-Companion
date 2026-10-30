"""Independent, finite scripts and sufficiently long artificial technical histories."""
from datetime import datetime, timedelta, timezone
import json

from trading_companion.daily_bar import DailyBar
from trading_companion.decision_answer_evaluation import DecisionFixedSources
from trading_companion.decision_demo import scripted_narrative
from trading_companion.product_plan import PRODUCT_INTENT_TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolModelResponse, validate_tool_messages
from trading_companion.workflow_evaluation_fixtures import FixedDateTime

NOW = datetime(2026, 10, 29, 4, tzinfo=timezone.utc)
REFERENCE_DATE = NOW.date()
DATES = {'T01': '最近三个月', 'T02': '近三个月', 'T03': '最近六个月', 'T04': '最近',
         'T05': '2026-07-01至2026-09-30', 'T06': '最近三个月', 'T07': '最近三个月',
         'T08': '最近三个月', 'T09': '最近三个月', 'T10': '最近三个月'}


class TechnicalDateTime(FixedDateTime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class TechnicalDailySource:
    adjustment, is_synthetic = 'unadjusted', True

    def __init__(self, request, owner):
        self.request, self.owner = request, owner
        self.source_name, self.instrument_id = request.source, request.instrument_id

    def read_daily_bars(self):
        self.owner.daily_reads += 1
        days, cursor = [], self.request.as_of_date
        while len(days) < 400:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        # Not exchange-calendar observations: deliberately artificial weekdays.
        return [DailyBar(self.instrument_id, day, 10 + i / 100, 11 + i / 100,
                         9 + i / 100, 10.2 + i / 100, 100000 + i * 100, 1000000 + i * 1000)
                for i, day in enumerate(reversed(days))]


class TechnicalFixedSources(DecisionFixedSources):
    def __init__(self, cases, workers):
        from trading_companion.security_directory import SecurityDirectory
        super().__init__(cases, workers)
        self.directory = SecurityDirectory(self.directory.securities, 'technical_evaluation_synthetic', NOW, False)
        self.http.directory = self.directory
        self.company._clock = lambda: NOW

    def __enter__(self):
        from unittest.mock import patch
        super().__enter__()
        for module in ('multi_analysis', 'analysis', 'research_tool'):
            self.stack.enter_context(patch('trading_companion.' + module + '.datetime', TechnicalDateTime))
        return self

    def daily(self, request):
        return TechnicalDailySource(request, self)


class TechnicalContractModel:
    """The script specifies phases separately from the corpus's grading expectations."""
    def __init__(self):
        self.remaining, self.failed = [], False

    def start(self, case, turn_index):
        self.case, self.turn_index = case, turn_index
        self.failed = False
        self.remaining = (['answer'] if case['id'] in ('T08', 'T10') and turn_index else ['intent', 'answer'])

    def complete(self, messages, *, tools):
        validate_tool_messages(messages, tools=tools)
        if not self.remaining:
            self.failed = True
            raise AssertionError('Unexpected extra technical evaluation call')
        phase = self.remaining.pop(0)
        if phase == 'intent' and tools and tools[0].name == PRODUCT_INTENT_TOOL_NAME:
            return ToolModelResponse('tool_calls', tool_calls=(ToolCall('technical_eval_intent', PRODUCT_INTENT_TOOL_NAME,
                dict(intent='research', instrument_mentions=[self.case['security_name']] if self.turn_index == 0 else [],
                     research_kinds=['technical'], date_expression=DATES[self.case['id']] if not self.turn_index else None,
                     outlook_expression=None, wants_risk=False, clarification_reason=None)),))
        if phase == 'answer' and not tools and 'product-research-answer-v3' in messages[0].content:
            context = json.loads(messages[2].content.split('\n', 1)[1])
            return ToolModelResponse('complete', json.dumps(scripted_narrative(context), ensure_ascii=False))
        self.failed = True
        raise AssertionError('Unexpected technical evaluation phase')
