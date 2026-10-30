"""Sequential batch execution preserves budgets, complete pairing and prior evidence."""
import json
import unittest
from unittest.mock import patch

from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.research_tool import TOOL_NAME, ToolExecutionError
from trading_companion.tool_model import MAX_TOTAL_CHARACTERS, ToolCall, ToolMessage, ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn
from trading_companion.workflow_tools import KNOWLEDGE_TOOL_NAME, SNAPSHOT_TOOL_NAME
from tests.test_workflow_tools import bars, call, fixture_executor, history, request


def proposal(*calls):
    return ToolModelResponse('tool_calls', tool_calls=calls)


class WorkflowBatchTests(unittest.TestCase):
    def setUp(self):
        self.confirmed = request(indicators=(IndicatorSpec('sma',window=3),))
        self.executor = fixture_executor(self.confirmed)
        self.messages = history()

    def primary(self):
        response = proposal(call(confirmed=self.confirmed))
        with patch('trading_companion.data_source.CsvDailyBarSource.read_daily_bars', return_value=bars()) as read:
            results, stop = self.executor.execute_batch(response,messages=self.messages)
        self.assertEqual(read.call_count,1)
        self.assertIsNone(stop)
        self.messages.extend((response.as_message(),*results))
        return results[0]

    def test_same_batch_primary_then_snapshot_uses_one_fetch_and_original_ids(self):
        response = proposal(call(confirmed=self.confirmed),
            ToolCall('snapshot',SNAPSHOT_TOOL_NAME,dict(section='overview')))
        with patch('trading_companion.data_source.CsvDailyBarSource.read_daily_bars',return_value=bars()) as read:
            results, stop = self.executor.execute_batch(response,messages=self.messages)
        read.assert_called_once()
        self.assertIsNone(stop)
        self.assertEqual([r.call_id for r in results],['primary_1','snapshot'])
        self.assertEqual(results[1].result['request_id'],self.confirmed.request_id)
        validate_tool_messages([*self.messages,response.as_message(),*results],tools=self.executor.definitions)
        self.assertEqual(self.executor.tool_calls_used,2)

    def test_invalid_scope_and_arguments_reject_only_their_own_calls(self):
        wrong = {**call(confirmed=self.confirmed).arguments,'instrument_id':'600000.SH'}
        response = proposal(ToolCall('bad_scope',TOOL_NAME,wrong),
            ToolCall('bad_args',KNOWLEDGE_TOOL_NAME,{}),call(confirmed=self.confirmed))
        with patch('trading_companion.data_source.CsvDailyBarSource.read_daily_bars',return_value=bars()) as read:
            results,stop = self.executor.execute_batch(response,messages=self.messages)
        read.assert_called_once()
        self.assertIsNone(stop)
        self.assertEqual([r.result.get('code') for r in results[:2]],['proposal_request_mismatch','invalid_arguments'])
        self.assertEqual(results[2].result['version'],'research-view-v1')
        self.assertEqual(self.executor.tool_calls_used,3)

    def test_each_rejected_attempt_counts_and_over_budget_calls_are_not_executed(self):
        calls = [ToolCall('attempt_'+str(i),KNOWLEDGE_TOOL_NAME,{}) for i in range(8)]
        response = proposal(*calls)
        with patch.object(self.executor,'_dispatch',wraps=self.executor._dispatch) as dispatch:
            results,stop = self.executor.execute_batch(response,messages=self.messages)
        self.assertIsNone(stop)
        self.assertEqual(dispatch.call_count,6)
        self.assertEqual(self.executor.tool_calls_used,6)
        self.assertEqual([r.result['status'] for r in results],['rejected']*6+['not_executed']*2)
        self.assertEqual([r.result['code'] for r in results[-2:]],['tool_call_budget_exhausted']*2)
        self.assertEqual([s['call_id'] for s in self.executor.steps],[c.call_id for c in calls])
        validate_tool_messages([*self.messages,response.as_message(),*results],tools=self.executor.definitions)

    def test_model_last_opportunity_pairs_batch_without_executing_it(self):
        response = proposal(call(confirmed=self.confirmed),ToolCall('snapshot',SNAPSHOT_TOOL_NAME,dict(section='overview')))
        with patch.object(self.executor,'_dispatch',side_effect=AssertionError('No execution at model limit')):
            results,stop = self.executor.execute_batch(response,messages=self.messages,allow_execution=False)
        self.assertEqual(stop,'workflow_iteration_limit')
        self.assertEqual(self.executor.tool_calls_used,0)
        self.assertEqual([r.result['status'] for r in results],['not_executed']*2)
        validate_tool_messages([*self.messages,response.as_message(),*results],tools=self.executor.definitions)

    def test_total_result_overflow_retains_previous_results_and_skips_remaining_calls(self):
        primary = self.primary()
        original = primary.to_dict()
        calls = [ToolCall('large_'+str(i),KNOWLEDGE_TOOL_NAME,dict(topics=['sma'])) for i in range(3)]
        response = proposal(*calls)
        baseline = [*self.messages,response.as_message(),*[ToolMessage('tool',call_id=c.call_id,name=c.name,result={}) for c in calls]]
        size = sum(len(json.dumps(d.to_dict(),ensure_ascii=True)) for d in self.executor.definitions)
        size += sum(len(json.dumps(m.to_dict(),ensure_ascii=True)) for m in baseline)
        padding = min(9000, int((MAX_TOTAL_CHARACTERS-size)*.65))
        self.assertGreater(padding,1000)

        def large(call):
            output = ToolMessage('tool',call_id=call.call_id,name=call.name,result=dict(status='knowledge_result',text='x'*padding))
            return output,'executed','indicator_knowledge_returned',None

        with patch.object(self.executor,'_dispatch',side_effect=large) as dispatch:
            results,stop = self.executor.execute_batch(response,messages=self.messages)
        self.assertEqual(stop,'workflow_context_limit')
        self.assertEqual(dispatch.call_count,2)
        self.assertEqual(results[0].result['text'],'x'*padding)
        self.assertEqual(results[1].result['code'],'tool_result_too_large')
        self.assertEqual(results[2].result,dict(status='not_executed',code='workflow_context_limit',synthesis_allowed=False))
        self.assertEqual(self.messages[-1].to_dict(),original)  # Entire primary evidence, unchanged.
        self.assertEqual(self.executor.primary_execution.messages[0].to_dict(),original)
        validate_tool_messages([*self.messages,response.as_message(),*results],tools=self.executor.definitions)

    def test_protocol_error_rejects_entire_batch_before_primary_fetch(self):
        response = proposal(call(confirmed=self.confirmed),ToolCall('unknown','place_trade',{}))
        with patch.object(self.executor,'_dispatch',side_effect=AssertionError('No unauthorized batch')):
            with self.assertRaises(ToolExecutionError):
                self.executor.execute_batch(response,messages=self.messages)
        self.assertEqual(self.executor.tool_calls_used,0)

    def test_cancellation_between_calls_stops_following_execution_and_records_it(self):
        control = TurnControl(TurnLimits(model_calls=6,profile='research_workflow'))
        control.start()
        response = proposal(ToolCall('first',KNOWLEDGE_TOOL_NAME,dict(topics=['sma'])),
                            ToolCall('second',KNOWLEDGE_TOOL_NAME,dict(topics=['rsi'])))
        def after():
            control.cancel()
        with bind_turn(control), self.assertRaises(TurnStopped):
            self.executor.execute_batch(response,messages=self.messages,stop_after=after)
        self.assertEqual(self.executor.tool_calls_used,1)
        self.assertEqual(self.executor.steps[0]['status'],'executed')
        self.assertEqual(self.executor.steps[1]['code'],'turn_cancelled')
        self.assertEqual(self.executor.steps[1]['status'],'not_executed')
