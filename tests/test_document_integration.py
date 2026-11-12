"""Document configuration and orchestration stay offline, optional and request-idempotent."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from trading_companion.api_server import main
from trading_companion.api_service import ResearchApiService, public_product_turn
from trading_companion.document_collection import DocumentCollector
from trading_companion.model import ModelConfigurationError
from trading_companion.news_triage_shadow import NewsTitleCapture
from trading_companion.news_triage_sorting import NewsTriageSorter
from trading_companion.product_agent import run_product_research_turn
from trading_companion.research_session import ResearchSession
from tests.test_document_answer import document_bundle
from tests.test_product_agent import ProductModelFixture
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import MemorySourceFactory, NOW, directory
from tests.test_news_triage_shadow import public_data
from tests.test_research_intent_adapter import IntentFixture
from tests.test_research_intent_v2 import draft
from tests.test_screening_followup_api import ScreeningJourneyFixture


class DocumentIntegrationTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch('socket.socket.connect', side_effect=AssertionError('No network')),
                      patch('socket.getaddrinfo', side_effect=AssertionError('No DNS')),
                      patch('trading_companion.local_env._read_local_values', side_effect=AssertionError('No .env')),
                      patch('trading_companion.product_research._build_source', MemorySourceFactory())):
            guard.start()
            self.addCleanup(guard.stop)

    def service(self, *, enabled=False, provider='deepseek', **options):
        model = options.pop('model', ProductModelFixture())
        intent = options.pop('intent', IntentFixture([draft(research_kinds=[
            dict(kind='news', phrase='新闻'), dict(kind='announcements', phrase='公告')])]))
        service = ResearchApiService(model_factory=lambda: model, provider=provider,
            directory_loader=lambda _: directory(), data_mode='tencent', wall_clock=lambda: NOW,
            reference_date=NOW.date(), product_data_service=public_data(), research_intent_v2_enabled=enabled,
            intent_model_factory=lambda: intent, **options)
        self.addCleanup(service.close)
        return service, service.create_session(), model

    @staticmethod
    def send(service, binding, question='分析平安银行的新闻和公告', identifier='one'):
        return service.message(binding['session_id'], binding['access_token'], identifier, question)['result']

    def test_policy_and_dependency_injection_reject_invalid_values_before_setup(self):
        for options in ({'research_documents': True}, {'research_documents': 'all'},
                        {'research_documents': None}, {'document_collector': object()}):
            with self.subTest(options=options), patch('trading_companion.api_service.ensure_data_mode_dependencies') as deps:
                with self.assertRaises(ModelConfigurationError):
                    ResearchApiService(model_factory=ProductModelFixture, directory_loader=lambda _: directory(), **options)
                deps.assert_not_called()

    def test_off_and_isolated_modes_cannot_use_even_an_injected_collector(self):
        from trading_companion.development_fixtures import load_development_fixtures
        collector = DocumentCollector()
        collector.enrich = Mock(side_effect=AssertionError('Disabled collector'))
        for mode in ('off', 'development', 'demo', 'synthetic'):
            with self.subTest(mode=mode):
                options = dict(model_factory=ProductModelFixture, directory_loader=lambda _: directory(),
                    data_mode='tencent', research_documents='events', document_collector=collector)
                if mode == 'off':
                    options['research_documents'] = 'off'
                elif mode == 'synthetic':
                    options.update(data_mode='synthetic', development_source=load_development_fixtures()[0])
                else:
                    options['trace_mode'] = mode
                service = ResearchApiService(**options)
                self.addCleanup(service.close)
                self.assertIsNone(service._document_collector)
        collector.enrich.assert_not_called()
        service, _, _ = self.service()
        self.assertIsNone(service._document_collector)
        live, _, _ = self.service(research_documents='events')
        self.assertIs(type(live._document_collector), DocumentCollector)

    def test_old_v2_and_openai_paths_forward_once_and_replay_never_reads_again(self):
        for enabled, provider in ((False, 'deepseek'), (True, 'deepseek'), (False, 'openai')):
            with self.subTest(enabled=enabled, provider=provider):
                collector = DocumentCollector()
                collector.enrich = Mock(side_effect=lambda bundle, **_: bundle)
                service, binding, _ = self.service(enabled=enabled, provider=provider,
                    research_documents='events', document_collector=collector)
                with patch.object(service, '_legacy_turn', wraps=service._legacy_turn) as legacy:
                    result = self.send(service, binding)
                    if enabled:
                        legacy.assert_not_called()
                self.assertEqual(result['stage'], 'company_research', result)
                collector.enrich.assert_called_once()
                self.assertEqual(collector.enrich.call_args.kwargs, dict(
                    question='分析平安银行的新闻和公告', now=NOW, sorting=False))
                repeated = self.send(service, binding)
                self.assertEqual(repeated, result)
                collector.enrich.assert_called_once()

    def test_capture_then_documents_then_sorter_then_answer(self):
        events = []
        collector, sorter = DocumentCollector(), NewsTriageSorter()
        collector.enrich = Mock(side_effect=lambda bundle, **_: events.append('documents') or bundle)
        sorter.sort = Mock(side_effect=lambda *_, **__: events.append('sorting'))
        model = ProductModelFixture(on_synthesis=lambda: events.append('answer'))
        with TemporaryDirectory() as temporary:
            capture = NewsTitleCapture(Path(temporary))
            capture.capture = Mock(side_effect=lambda *_, **__: events.append('capture'))
            service, binding, _ = self.service(enabled=True, model=model, research_documents='events',
                document_collector=collector, headline_capture=capture, news_triage_sorting=True, news_sorter=sorter)
            self.send(service, binding)
        self.assertEqual(events, ['capture', 'documents', 'sorting', 'answer'])
        self.assertIs(collector.enrich.call_args.kwargs['sorting'], True)

    def test_saved_explanation_and_saved_result_do_not_fetch_documents(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                collector = DocumentCollector()
                collector.enrich = Mock(side_effect=lambda bundle, **_: bundle)
                values = [draft(research_kinds=[dict(kind='news', phrase='新闻')]),
                          draft(operation='explain', subject=dict(source='previous', mentions=[], selection=None))]
                service, binding, _ = self.service(enabled=enabled, intent=IntentFixture(values),
                    research_documents='events', document_collector=collector)
                self.send(service, binding, '分析平安银行的新闻')
                self.send(service, binding, '为什么这样判断', 'explanation')
                self.assertEqual(collector.enrich.call_count, 1)
                service.history(binding['session_id'], binding['access_token'])
                self.assertEqual(collector.enrich.call_count, 1)

    def test_saved_bundle_marks_only_final_context_excerpts_without_discarding_candidates(self):
        question = '分析平安银行的新闻和公告'
        candidates = document_bundle(title_characters=180, excerpts_per_document=2, complete=False)
        candidates = replace(candidates, plan=replace(candidates.plan, user_question=question))
        baseline = replace(candidates, supplemental_evidence=(), documents=None)
        collector, session, model = DocumentCollector(), ResearchSession(), NarrativeModel()
        collector.enrich = Mock(return_value=candidates)
        with patch('trading_companion.product_agent.collect_product_research', return_value=baseline):
            result = run_product_research_turn(question, session=session, model=model, directory=directory(),
                now=NOW, source='tencent', data_service=public_data(), prepared=candidates.plan,
                document_collector=collector)

        self.assertEqual(result.product_answer.status, 'accepted')
        self.assertEqual(len(model.calls), 1)
        context = json.loads(model.calls[0][2].content.split('\n', 1)[1])
        supplied = [record for record in context['evidence'] if record['content_kind'] == 'body_excerpt']
        self.assertEqual(len(supplied), 1, 'The real final request budget must remove one candidate')
        self.assertIs(supplied[0]['document']['document_complete'], False)
        self.assertEqual(len(candidates.documents['items'][0]['excerpts']), 2)
        expected = result.product_answer.documents
        self.assertEqual(result.product_bundle.documents, expected)
        self.assertEqual(session.product_saved.bundle.documents, expected)
        self.assertEqual(result.product_answer.to_dict()['documents'], expected)
        self.assertEqual(public_product_turn(result)['company_research']['documents'], expected)
        self.assertIs(expected['items'][0]['complete'], False)
        self.assertEqual(expected['status'], 'partial')
        self.assertEqual([excerpt['sha256'] for excerpt in expected['items'][0]['excerpts']],
                         [record['document']['excerpt_sha256'] for record in supplied])
        self.assertEqual(result.product_bundle.supplemental_evidence, candidates.supplemental_evidence)
        self.assertEqual(len(result.product_bundle.to_dict()['supplemental_evidence']), 2)
        original_aliases = {record.id: f'E{index}' for index, record in enumerate(candidates.evidence, 1)}
        saved_aliases = {record.id: f'E{index}' for index, record in enumerate(session.product_saved.bundle.evidence, 1)}
        self.assertEqual(saved_aliases, original_aliases)

        history = run_product_research_turn('查看上次结果', session=session, model=model, directory=directory(),
            now=NOW, source='tencent', data_service=public_data(), document_collector=collector)
        self.assertEqual(history.code, 'product_saved_result')
        self.assertEqual(history.product_bundle.documents, history.product_answer.documents)
        self.assertEqual(history.product_bundle.supplemental_evidence, candidates.supplemental_evidence)
        collector.enrich.assert_called_once()
        self.assertEqual(len(model.calls), 1)

    def test_context_failure_keeps_candidates_but_marks_no_summary_excerpts_as_supplied(self):
        candidates = document_bundle(excerpts_per_document=2)
        baseline = replace(candidates, supplemental_evidence=(), documents=None)
        collector, session, model = DocumentCollector(), ResearchSession(), NarrativeModel()
        collector.enrich = Mock(return_value=candidates)
        with patch('trading_companion.product_agent.collect_product_research', return_value=baseline), \
                patch('trading_companion.product_narrative_answer._context', side_effect=ValueError('Fixture unavailable')):
            result = run_product_research_turn('分析平安银行的新闻和公告', session=session, model=model,
                directory=directory(), now=NOW, source='tencent', data_service=public_data(),
                prepared=candidates.plan, document_collector=collector)

        self.assertEqual(model.calls, [])
        self.assertIsNone(result.product_answer.documents)
        self.assertEqual(result.product_bundle.documents['items'][0]['excerpts'], [])
        self.assertEqual(session.product_saved.bundle.documents['items'][0]['excerpts'], [])
        self.assertEqual(result.product_bundle.supplemental_evidence, candidates.supplemental_evidence)
        self.assertEqual(len(result.product_bundle.supplemental_evidence), 2)
        self.assertEqual(len(candidates.documents['items'][0]['excerpts']), 2)

    def test_demo_and_synthetic_event_flags_fail_before_configuration_or_dependencies(self):
        for flags in (['--demo'], ['--data', 'synthetic']):
            with self.subTest(flags=flags), redirect_stderr(StringIO()), \
                    patch('trading_companion.api_server.load_model_environment') as environment, \
                    patch('trading_companion.api_server.ensure_data_mode_dependencies') as deps:
                with self.assertRaises(SystemExit) as caught:
                    main([*flags, '--research-documents', 'events'])
                self.assertEqual(caught.exception.code, 2)
                environment.assert_not_called()
                deps.assert_not_called()

    def test_normal_launcher_forwards_event_policy_without_loading_document_content(self):
        with redirect_stdout(StringIO()), patch('uvicorn.run'), \
                patch('trading_companion.api_server._prepare_startup_directory'), \
                patch('trading_companion.model_registry.configured_models', return_value={'deepseek': ProductModelFixture}), \
                patch('trading_companion.api_service.ResearchApiService', wraps=ResearchApiService) as factory:
            self.assertEqual(main(['--data', 'tencent', '--research-documents', 'events', '--no-research-intent-v2']), 0)
        self.assertEqual(factory.call_args.kwargs['research_documents'], 'events')


class DocumentScreeningIntegrationTests(ScreeningJourneyFixture, unittest.TestCase):
    def test_shortlist_followup_forwards_collector_and_original_question(self):
        collector = DocumentCollector()
        collector.enrich = Mock(side_effect=lambda bundle, **_: bundle)
        self.install(research_documents='events', document_collector=collector)
        self.screen()
        collector.enrich.assert_not_called()
        result = self.send('第5只只看新闻，不要技术指标')['result']
        self.assertEqual(result['stage'], 'company_research', result)
        collector.enrich.assert_called_once()
        self.assertEqual(collector.enrich.call_args.kwargs['question'], '第5只只看新闻，不要技术指标')
        self.assertFalse(collector.enrich.call_args.kwargs['sorting'])


if __name__ == '__main__':
    unittest.main()
