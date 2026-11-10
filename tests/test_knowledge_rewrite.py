"""Regression cases from the 2026-11-09 diagnostic; all models are offline."""

from dataclasses import replace
import json
from unittest.mock import Mock, patch

from tests.test_open_knowledge import GuardedTests, PRIVATE, QUESTION, RecordingModel, response
from trading_companion import open_knowledge as knowledge
from trading_companion.agent import run_general_knowledge_turn
from trading_companion.model import ModelCallError, ModelRequestError, TokenUsage
from trading_companion.tool_model import ToolModelResponse
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn


class SequenceModel(RecordingModel):
    def __init__(self, values):
        super().__init__()
        self.values = iter(values)
        self.callback = lambda: setattr(self, 'result', next(self.values))


class KnowledgeRelaxationTests(GuardedTests):
    def answer(self, paragraphs, *, citations=None):
        refs = [{'paragraph': 0, 'sources': ['K1']}] if citations is None else citations
        return knowledge.answer_general_knowledge(QUESTION,
            model=RecordingModel(response(paragraphs, citations=refs))).answer

    def test_hypothesis_must_precede_every_current_number_claim_in_the_same_clause(self):
        for prefix in ('假设', '假如', '例如', '比如', '举例', '设', '若', '假设：'):
            with self.subTest(prefix=prefix):
                answer = self.answer((prefix + '当前股价为20元，只作计算示例。',))
                self.assertEqual(answer.status, 'accepted')
        for text in ('今日股价为20元。', '当前价格是20元，假设每股收益为2元。',
                     '假设PE为10；当前股价为20元。', '例如PE为10，今日股价为20元。',
                     '假设当前股价为20元。今日股价为21元。', '当前PE是10；若当前PB是2。'):
            with self.subTest(text=text):
                self.assertEqual(self.answer((text,)).diagnostics['reason'], 'knowledge_current_number_claim')

    def test_titles_and_formula_windows_pass_but_reference_markers_still_fail(self):
        for text in ('【计算步骤】DIF=EMA[12]-EMA[26]。', '【风险提醒】金叉不等于未来必涨。',
                     'ema[12]、EMA[N]、x[1]是参数写法。'):
            with self.subTest(text=text):
                self.assertEqual(self.answer((text,)).status, 'accepted')
        for marker in ('【K1】', '【1】', '【来源】', '【资料1】', '【参考文献1】', '[K1]', '[1]', '参数 [12]'):
            with self.subTest(marker=marker):
                self.assertEqual(self.answer((marker,)).diagnostics['reason'], 'knowledge_bracket_reference')

    def test_overflow_merges_in_order_and_remaps_only_original_citations(self):
        paragraphs = [f'步骤{i}。' for i in range(9)]
        answer = self.answer(paragraphs, citations=[{'paragraph': i, 'sources': ['K1']} for i in (0, 5, 6, 8)])
        self.assertEqual(answer.paragraphs, (*paragraphs[:5], ' '.join(paragraphs[5:])))
        self.assertEqual([c.paragraph_indices for c in answer.citations], [(0, 5)])
        self.assertEqual(answer.diagnostics['adjustments'], ['knowledge_paragraphs_merged'])

    def test_overflow_keeps_whole_prefix_and_records_merge_and_truncation(self):
        paragraphs = ['前段'] * 5 + ['甲' * 798, '乙', '丙', '丁']
        answer = self.answer(paragraphs, citations=[{'paragraph': 6, 'sources': ['K1']}])
        self.assertEqual(answer.status, 'accepted')
        self.assertEqual(answer.paragraphs[-1], '甲' * 798 + ' 乙')
        self.assertEqual(len(answer.paragraphs[-1]), 800)
        self.assertEqual(answer.citations[0].paragraph_indices, (5,))
        self.assertEqual(answer.diagnostics['adjustments'], list(knowledge.KNOWLEDGE_ADJUSTMENTS))
        truncated = self.answer(['前段'] * 5 + ['甲' * 800, '乙', '短'])
        self.assertEqual(truncated.paragraphs[-1], '甲' * 800)
        self.assertEqual(truncated.diagnostics['adjustments'], ['knowledge_paragraphs_truncated'])

    def test_discarded_citation_does_not_fake_coverage_and_invalid_tail_still_fails(self):
        paragraphs = ['前段'] * 5 + ['甲' * 800, '尾段']
        refs = [{'paragraph': 6, 'sources': ['K1']}]
        answer = self.answer(paragraphs, citations=refs)
        self.assertEqual(answer.diagnostics['reason'], 'knowledge_citation_missing')
        self.assertEqual(answer.diagnostics['adjustments'], ['knowledge_paragraphs_truncated'])
        for refs, reason in (
            ([{'paragraph': 7, 'sources': ['K1']}], 'citation_index_invalid'),
            ([{'paragraph': 6, 'sources': ['K9']}], 'citation_source_unknown'),
            ([{'paragraph': 6, 'sources': ['K1']}] * 2, 'citation_index_duplicate'),
            ([{'paragraph': i, 'sources': ['K1']} for i in range(8)], 'citation_count_invalid'),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self.answer(paragraphs, citations=refs).diagnostics['reason'], 'knowledge_' + reason)
        for tail, reason in (('今日股价为20元。', 'current_number_claim'), ('保证收益。', 'guaranteed_return'),
                             ('我已联网。', 'claimed_access'), ('example.com', 'link_forbidden'),
                             ('<b>标题</b>', 'html_forbidden')):
            with self.subTest(tail=tail):
                self.assertEqual(self.answer([*paragraphs[:-1], tail]).diagnostics['reason'], 'knowledge_' + reason)

    def test_each_original_overflow_paragraph_can_cite_before_remapping(self):
        paragraphs = [f'步骤{i}。' for i in range(9)]
        answer = self.answer(paragraphs, citations=[{'paragraph': i, 'sources': ['K1']} for i in range(9)])
        self.assertEqual(answer.status, 'accepted')
        self.assertEqual(answer.citations[0].paragraph_indices, tuple(range(6)))
        self.assertEqual(answer.paragraphs[-1], ' '.join(paragraphs[5:]))


class KnowledgeRewriteTests(GuardedTests):
    def run_model(self, values, *, calls=2):
        control = TurnControl(TurnLimits(model_calls=calls, analyses=0))
        control.start()
        delegate = SequenceModel(values)
        with bind_turn(control):
            run = knowledge.answer_general_knowledge(QUESTION, model=ControlledToolModel(
                delegate, control, retry_transient_errors=False), repair_invalid=True)
        return run, delegate, control

    def test_rewrite_feedback_contains_only_fixed_reason_and_uses_unchanged_original_messages(self):
        bad = ToolModelResponse('complete', PRIVATE)
        good = replace(response(), usage=TokenUsage(10, 20, 30))
        run, model, control = self.run_model([bad, good])
        self.assertEqual(run.answer.status, 'accepted')
        self.assertEqual(run.answer.diagnostics['acceptance'], 'after_rewrite')
        self.assertEqual(run.answer.diagnostics['first_reason'], 'knowledge_json_invalid')
        self.assertEqual(run.answer.diagnostics['rewrite_attempts'], 1)
        self.assertEqual(run.model_responses, (bad, good))
        self.assertEqual(run.model_response, good)
        self.assertEqual(run.model_calls_attempted, 2)
        first, second = model.calls
        self.assertEqual(second[0][:-1], first[0][:-1])
        self.assertTrue(second[0][-1].content.startswith(first[0][-1].content))
        self.assertEqual(first[0][0].content, knowledge.GENERAL_KNOWLEDGE_INSTRUCTION)
        self.assertEqual((first[1], second[1]), ((), ()))
        self.assertIn('knowledge_json_invalid', second[0][-1].content)
        self.assertNotIn(PRIVATE, json.dumps([m.content for m in second[0]]))
        self.assertEqual(control.to_dict()['responses_received'], 2)

    def test_second_draft_checks_every_redline_and_never_calls_a_third_time(self):
        for text, reason in (('今日股价为20元。', 'current_number_claim'), ('保证收益。', 'guaranteed_return'),
                             ('我已联网。', 'claimed_access'), ('example.com', 'link_forbidden'),
                             ('<b>标题</b>', 'html_forbidden')):
            with self.subTest(reason=reason):
                run, model, _ = self.run_model([response((text,)), response((text,)), response()], calls=3)
                self.assertEqual(run.answer.diagnostics['reason'], 'knowledge_' + reason)
                self.assertEqual(run.answer.diagnostics['rewrite_attempts'], 1)
                self.assertEqual(len(model.calls), 2)
                self.assertNotIn(text, run.answer.text)
        run, _, _ = self.run_model([ToolModelResponse('complete', PRIVATE), response(('今日股价为20元。',))])
        self.assertEqual(run.answer.diagnostics['first_reason'], 'knowledge_json_invalid')
        self.assertEqual(run.answer.diagnostics['reason'], 'knowledge_current_number_claim')

    def test_redline_first_draft_can_be_replaced_by_a_fully_checked_second_draft(self):
        for text in ('今日股价为20元。', '保证收益。', '我已联网。', 'example.com', '<b>标题</b>'):
            with self.subTest(text=text):
                run, model, _ = self.run_model([response((text,)), response()])
                self.assertEqual(run.answer.status, 'accepted')
                self.assertEqual(run.answer.diagnostics['acceptance'], 'after_rewrite')
                self.assertEqual(len(model.calls), 2)
                self.assertNotIn(text, run.answer.text)

    def test_budget_one_preserves_original_failure_and_no_control_cannot_rewrite(self):
        run, model, control = self.run_model([ToolModelResponse('complete', PRIVATE), response()], calls=1)
        self.assertEqual(run.answer.diagnostics['reason'], 'knowledge_json_invalid')
        self.assertEqual(run.answer.diagnostics['rewrite_attempts'], 0)
        self.assertEqual(len(model.calls), 1)
        self.assertIsNone(control.to_dict()['stop_code'])
        delegate = RecordingModel(ToolModelResponse('complete', PRIVATE))
        run = knowledge.answer_general_knowledge(QUESTION, model=delegate, repair_invalid=True)
        self.assertEqual(len(delegate.calls), 1)
        self.assertEqual(run.answer.diagnostics['rewrite_attempts'], 0)

    def test_rewrite_refusal_or_provider_failure_preserves_original_validation_reason(self):
        for value in (ToolModelResponse('refused', PRIVATE), ToolModelResponse('incomplete', PRIVATE)):
            with self.subTest(status=value.status):
                run, _, _ = self.run_model([ToolModelResponse('complete', PRIVATE), value])
                self.assertEqual(run.answer.diagnostics['reason'], 'knowledge_json_invalid')
                self.assertEqual(len(run.model_responses), 2)
        for code in ('timeout', 'api_session_call_limit'):
            delegate = RecordingModel(ToolModelResponse('complete', PRIVATE))
            delegate.callback = lambda: setattr(delegate, 'error', ModelCallError(code, PRIVATE)) if len(delegate.calls) == 2 else None
            control = TurnControl(TurnLimits(model_calls=2))
            control.start()
            with bind_turn(control):
                run = knowledge.answer_general_knowledge(QUESTION, model=ControlledToolModel(
                    delegate, control, retry_transient_errors=False), repair_invalid=True)
            self.assertEqual(run.answer.diagnostics['reason'], 'knowledge_json_invalid')
            self.assertEqual(len(run.model_responses), 1)
            self.assertEqual(run.model_calls_attempted, 2)

    def test_protocol_capacity_failure_keeps_original_failure_without_rewrite(self):
        with patch.object(knowledge, 'validate_tool_messages', side_effect=[None, ModelRequestError(PRIVATE)]):
            run, model, _ = self.run_model([ToolModelResponse('complete', PRIVATE), response()])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(run.answer.diagnostics['rewrite_attempts'], 0)
        self.assertEqual(run.answer.diagnostics['reason'], 'knowledge_json_invalid')

    def test_cancellation_during_rewrite_is_not_swallowed(self):
        control = TurnControl(TurnLimits(model_calls=2))
        control.start()
        delegate = RecordingModel(ToolModelResponse('complete', PRIVATE))
        delegate.callback = lambda: control.cancel() if len(delegate.calls) == 2 else None
        with bind_turn(control), self.assertRaises(TurnStopped) as raised:
            knowledge.answer_general_knowledge(QUESTION, model=ControlledToolModel(delegate, control), repair_invalid=True)
        self.assertEqual(raised.exception.code, 'turn_cancelled')
        self.assertEqual(control.to_dict()['responses_received'], 2)

    def test_agent_retains_both_responses_and_sums_both_usage_records(self):
        first = replace(response(('【K1】',)), usage=TokenUsage(10, 20, 30))
        second = replace(response(), usage=TokenUsage(11, 21, 32))
        model = SequenceModel([first, second])
        turn = run_general_knowledge_turn(QUESTION, model=model, control=TurnControl(TurnLimits(model_calls=2)))
        self.assertEqual(turn.code, 'general_knowledge_answered')
        self.assertEqual(turn.model_responses, (first, second))
        self.assertEqual(turn.model_calls_attempted, 2)
        self.assertEqual(turn.to_dict()['runtime']['reported_usage'],
                         dict(prompt_tokens=21, completion_tokens=41, total_tokens=62))

    def test_rewrite_shares_deadline_and_observed_token_threshold(self):
        for stop in ('turn_deadline_exceeded', 'token_budget_exhausted'):
            with self.subTest(stop=stop):
                clock = Mock(return_value=0.0)
                control = TurnControl(TurnLimits(model_calls=2, timeout_seconds=1, observed_tokens=50), clock=clock)
                control.start()
                delegate = SequenceModel([replace(response(('【K1】',)), usage=TokenUsage(10, 10, 20)),
                                          replace(response(), usage=TokenUsage(20, 20, 40))])
                advance = delegate.callback
                def callback():
                    advance()
                    if stop == 'turn_deadline_exceeded' and len(delegate.calls) == 2:
                        clock.return_value = 2.0
                delegate.callback = callback
                with bind_turn(control), self.assertRaises(TurnStopped) as raised:
                    knowledge.answer_general_knowledge(QUESTION, model=ControlledToolModel(delegate, control),
                                                       repair_invalid=True)
                self.assertEqual(raised.exception.code, stop)
                self.assertEqual(control.to_dict()['responses_received'], 2)

    def test_default_standalone_turn_keeps_its_single_call_limit(self):
        model = SequenceModel([ToolModelResponse('complete', PRIVATE), response()])
        turn = run_general_knowledge_turn(QUESTION, model=model)
        self.assertEqual(turn.code, 'general_knowledge_output_invalid')
        self.assertEqual(len(model.calls), 1)
