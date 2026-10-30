"""Output boundaries and structured recovery, without real providers or market data."""
import ast
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.answer_diagnostics import AnswerDiagnostics, bind_diagnostics
from trading_companion.deepseek_model import DeepSeekConfig, DeepSeekTextModel
from trading_companion.deepseek_tool_model import DeepSeekToolModel, _parse_response
from trading_companion.model import ChatMessage, ModelConfigurationError
from trading_companion.narrative_answer_parser import parse_narrative
from trading_companion.product_answer import synthesize_product_answer
from trading_companion.tool_model import ToolMessage, FakeToolModel
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, bind_turn
from trading_companion.turn_trace import safe_answer_validation
from tests.test_deepseek_tool_model import provider_payload
from tests.test_product_decision_answer import decision_bundle
from tests.test_product_narrative_answer import NarrativeModel


class InterruptedModel(NarrativeModel):
    def __init__(self, interrupted):
        super().__init__()
        self.interrupted = interrupted

    def complete(self, messages, *, tools):
        response = super().complete(messages, tools=tools)
        if len(self.calls) in self.interrupted:
            # Even syntactically valid JSON cannot be accepted with finish_reason=length.
            return replace(response, status='incomplete', finish_reason='length')
        return response


class AnswerOutputTests(unittest.TestCase):
    def run_answer(self, model):
        return synthesize_product_answer('分析走势', decision_bundle(), model=model, repair_invalid=True)

    def test_length_is_distinct_from_other_interruptions_and_survives_fake_model(self):
        for reason in ('length', 'aborted', 'insufficient_system_resource'):
            response = _parse_response(provider_payload(finish=reason), 'fixture', frozenset(), frozenset())
            self.assertEqual((response.status, response.finish_reason), ('incomplete', reason))
            self.assertEqual(FakeToolModel([response]).complete([ToolMessage('user', 'JSON')], tools=()).finish_reason, reason)
            self.assertEqual(response.to_dict()['finish_reason'], reason)

    def test_truncated_first_draft_is_not_parsed_and_gets_one_concise_repair(self):
        model = InterruptedModel({1})
        from trading_companion.product_narrative_answer import parse_narrative as real_parse
        with patch('trading_companion.product_narrative_answer.parse_narrative', wraps=real_parse) as parse:
            run = self.run_answer(model)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual((run.answer.status, len(model.calls)), ('accepted', 2))
        self.assertIn('上一稿超长被截断', model.calls[1][-1].content)
        self.assertIn('numbers 只标注关键价位', model.calls[1][-1].content)
        self.assertLessEqual(len(model.calls[1][-1].content),700)
        q = run.answer.quality
        self.assertEqual((q['output_truncations'], q['initial_output_truncations'], q['repair_output_truncations']), (1,1,0))
        self.assertEqual(q['structure_failures'], 0)

    def test_other_interruptions_with_no_text_are_not_misreported_as_length_or_bad_context(self):
        class OtherInterrupt(NarrativeModel):
            def complete(self, messages, *, tools):
                response = super().complete(messages,tools=tools)
                return replace(response,status='incomplete',text=None,finish_reason='aborted')
        model = OtherInterrupt()
        run = self.run_answer(model)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(run.answer.quality.get('output_truncations',0),0)
        self.assertNotIn('answer_context_unusable',[r['code'] for r in run.answer.quality['reasons']])

    def test_two_length_outputs_fall_back_without_third_call_or_prefix_acceptance(self):
        delegate = InterruptedModel({1,2})
        control = TurnControl(TurnLimits(model_calls=2))
        control.start()
        with bind_turn(control), patch('trading_companion.product_narrative_answer.parse_narrative',
                                       side_effect=AssertionError('Truncated output must not be parsed')):
            run = self.run_answer(ControlledToolModel(delegate, control))
        self.assertEqual((run.answer.status, len(delegate.calls)), ('fallback',2))
        self.assertTrue(run.answer.narratives[0]['current'])
        self.assertTrue(run.answer.trade_plans)
        self.assertFalse(any(d['status']=='available' for d in run.answer.decisions))
        q = run.answer.quality
        self.assertEqual((q['output_truncations'],q['initial_output_truncations'],q['repair_output_truncations']), (2,1,1))
        self.assertEqual(q['structure_failures'],0)
        self.assertIn('answer_output_truncated', [i['code'] for i in q['reasons']])
        self.assertEqual(safe_answer_validation(q)['repair_output_truncations'],1)
        self.assertEqual(safe_answer_validation(q)['reasons'][0]['code'],'answer_output_truncated')

    def test_repair_truncation_preserves_valid_first_draft_content(self):
        class Model(InterruptedModel):
            def __init__(self):
                super().__init__({2})
                self.change = lambda p,n,c: p['narratives'][0]['current'].update(text='收盘999元。')
        model = Model()
        run = self.run_answer(model)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(run.answer.decisions[0]['status'],'available')
        self.assertIn('历史波动提供观察范围', run.answer.text)
        self.assertNotIn('999',run.answer.text)

    def test_diagnostics_record_length_and_code_without_reclassifying_it_as_json_error(self):
        with TemporaryDirectory(dir='.agentbus/tmp') as root:
            diagnostics = AnswerDiagnostics(root)
            with bind_diagnostics(diagnostics):
                self.run_answer(InterruptedModel({1,2}))
            records = [json.loads(s) for s in (diagnostics.directory/'records.jsonl').read_text(encoding='utf-8').splitlines()]
        drafts = [r for r in records if r['kind']=='model_draft']
        self.assertEqual([r['finish_reason'] for r in drafts],['length','length'])
        self.assertEqual([r['code'] for r in drafts],['answer_output_truncated']*2)

    def test_recovered_punctuation_still_checks_numbers(self):
        def change(payload, n, context):
            payload['narratives'][0]['current'] = dict(text='收盘999元。')
            return json.dumps(payload,ensure_ascii=False)[:-1]
        run = self.run_answer(NarrativeModel(change))
        self.assertNotIn('999',run.answer.text)
        self.assertGreater(run.answer.quality['deleted_sentences'],0)
        self.assertEqual(run.answer.quality['punctuation_structure_recoveries'],2)


class PunctuationRecoveryTests(unittest.TestCase):
    def test_missing_inner_closer_and_trailing_commas_preserve_all_values(self):
        for text, expected in (
            ('{"narratives":[{"stance":{"text":"原值10.01元"}],"decisions":[]}',
             dict(narratives=[dict(stance=dict(text='原值10.01元'))],decisions=[])),
            ('{"text":"括号}]、逗号,、转义\\\"不改", "values":[1,2,],}',
             dict(text='括号}]、逗号,、转义"不改',values=[1,2])),
            ('{"nested":{"values":[1,2',dict(nested=dict(values=[1,2]))),
        ):
            with self.subTest(text=text):
                parsed = parse_narrative(text)
                self.assertTrue(parsed.punctuation_repaired)
                self.assertFalse(parsed.partial)
                self.assertEqual(parsed.payload, expected)

    def test_recovery_cannot_supply_values_or_hide_ambiguous_or_unsafe_content(self):
        for text in ('{"a":[[[[1', '{"a":}', '{"a":"unfinished', '{"a":NaN,}',
                     '{"a":1,"a":2', '{"a":1,}{}', '保证收益。{"a":1,}', '{"a":[1,,]}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_narrative(text)


class StructuredProviderRequestTests(unittest.TestCase):
    def test_every_production_no_tool_caller_requires_json_and_plain_text_is_separate(self):
        # This inventory must change explicitly when a new no-tool caller is added.
        expected = {'open_knowledge': 'GENERAL_KNOWLEDGE_INSTRUCTION', 'product_answer': 'PRODUCT_ANSWER_INSTRUCTION',
                    'product_decision_answer': 'INSTRUCTION', 'product_narrative_answer': 'INSTRUCTION'}
        root = Path('src/trading_companion')
        callers = set()
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
                if (isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='complete'
                    and any(k.arg=='tools' and isinstance(k.value,ast.Tuple) and not k.value.elts for k in node.keywords)):
                    callers.add(path.stem)
        self.assertEqual(callers,set(expected))
        import importlib
        for module, constant in expected.items():
            instruction = getattr(importlib.import_module('trading_companion.'+module),constant)
            self.assertIn('json',instruction.lower(),module)
        config = DeepSeekConfig('fixture','synthetic-key',max_output_tokens=4096)
        with patch('trading_companion.deepseek_tool_model._post_chat',return_value=provider_payload(content='{}')) as post:
            DeepSeekToolModel(config).complete([ToolMessage('user','JSON')],tools=())
        self.assertEqual(post.call_args.args[1]['response_format'],dict(type='json_object'))
        self.assertEqual(post.call_args.args[1]['max_tokens'],4096)
        with patch('trading_companion.deepseek_model._post_chat',return_value=provider_payload(content='Plain text')) as post:
            DeepSeekTextModel(config).complete([ChatMessage('user','Explain plainly')])
        self.assertNotIn('response_format',post.call_args.args[1])

    def test_tool_requests_keep_small_output_and_no_json_mode(self):
        from trading_companion.workflow_tools import WORKFLOW_TOOL_DEFINITIONS
        config = DeepSeekConfig('fixture','synthetic-key',max_output_tokens=4096)
        with patch('trading_companion.deepseek_tool_model._post_chat',return_value=provider_payload()) as post:
            DeepSeekToolModel(config).complete([ToolMessage('user','Research')],tools=WORKFLOW_TOOL_DEFINITIONS)
        self.assertEqual(post.call_args.args[1]['max_tokens'],1500)
        self.assertNotIn('response_format',post.call_args.args[1])
        self.assertEqual(DeepSeekConfig('fixture','synthetic-key',max_output_tokens=8192).max_output_tokens,8192)
        with self.assertRaises(ModelConfigurationError):
            DeepSeekConfig('fixture','synthetic-key',max_output_tokens=8193)
