"""Relevant older document parents survive only within their own title capacity."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from trading_companion.product_answer import synthesize_product_answer
from trading_companion.product_narrative_answer import _context
from trading_companion.product_research import model_research_context
from trading_companion.research_documents import DocumentCandidate, ResearchDocument
from trading_companion.tool_model import validate_tool_messages
from tests.test_document_answer import UNREAD_TEXT, document_bundle
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import NOW


def older_parent_bundle(*, ascii_titles=False):
    bundle = document_bundle(count=3, title_characters=480, body_companies=(2,))
    if ascii_titles:
        bundle = replace(bundle, companies=tuple(replace(company, evidence=tuple(
            replace(item, display='News title ' + 'x' * 470) if item.kind == 'news' else item
            for item in company.evidence)) for company in bundle.companies))
    parent = [item for item in bundle.companies[2].evidence if item.kind == 'news'][-1]
    document = ResearchDocument(DocumentCandidate(parent.id, parent.instrument_id,
        bundle.companies[2].name, parent.kind, parent.source, parent.display, parent.url, parent.date),
        (bundle.supplemental_evidence[0].display, ('Background only. ' * 60).strip(), UNREAD_TEXT),
        NOW.isoformat(), 'sina-artibody-v3')
    body = bundle.supplemental_evidence[0]
    metadata = {**body.document, 'document_id': document.id, 'parent_title_id': parent.id,
                'sha256': document.content_sha256}
    body = replace(body, id=document.id + ':1', document=metadata, dependencies=(parent.id,),
                   url=parent.url)
    item = {**bundle.documents['items'][0], 'parent_title_id': parent.id, 'title': parent.display,
            'url': parent.url, 'document_id': document.id, 'sha256': document.content_sha256,
            'extracted_characters': len(document.text)}
    return replace(bundle, supplemental_evidence=(body,),
                   documents={**bundle.documents, 'items': [item]}), parent


class DocumentContextTests(unittest.TestCase):
    def test_budget_includes_final_omitted_count_digit_width(self):
        bundle, _ = older_parent_bundle()
        baseline = replace(bundle, supplemental_evidence=(), documents=None)
        for limit in (672, 986, 14500):
            for candidate in (baseline, bundle):
                with self.subTest(limit=limit, documents=bool(candidate.supplemental_evidence)):
                    context = model_research_context(candidate, limit=limit)
                    self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), limit)
                    self.assertEqual(context['omitted_from_model'],
                                     len(candidate.evidence) - len(context['evidence']))

    def test_older_parent_replaces_only_its_own_company_domain_title(self):
        bundle, parent = older_parent_bundle()
        base = replace(bundle, supplemental_evidence=(), documents=None)
        original_ids = {item.id: f'E{i}' for i, item in enumerate(base.evidence, 1)}
        baseline = model_research_context(base)
        before = {item['id']: item for item in baseline['evidence']}
        self.assertNotIn(original_ids[parent.id], before, 'Dense fixture must omit the fifth title')

        result = model_research_context(bundle)
        after = {item['id']: item for item in result['evidence']}
        offered = [item for item in after.values() if item['content_kind'] == 'body_excerpt']
        self.assertEqual(len(offered), 1)
        self.assertIn(original_ids[parent.id], after)
        self.assertEqual(offered[0]['document']['parent_title_id'], parent.id)
        self.assertEqual(offered[0]['id'], f'E{len(base.evidence) + 1}')
        for identifier, item in before.items():
            if (item['instrument_id'], item['kind'], item['content_kind']) != (
                    parent.instrument_id, parent.kind, 'title'):
                self.assertEqual(after[identifier], item)
        removed = set(before) - set(after)
        self.assertTrue(removed)
        self.assertTrue(all((before[i]['instrument_id'], before[i]['kind'], before[i]['content_kind'])
                           == (parent.instrument_id, parent.kind, 'title') for i in removed))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 14500)
        self.assertLessEqual(len(json.dumps(offered, ensure_ascii=False)), 3000)
        self.assertEqual(result['omitted_from_model'], len(bundle.evidence) - len(after))

    def test_older_actual_parent_and_body_reach_final_model_with_stable_references(self):
        bundle, parent = older_parent_bundle(ascii_titles=True)
        model = NarrativeModel()
        # Use a smaller source-context allowance so the complete protocol still
        # has room for its fixed answer/repair instructions and an excerpt.
        with patch('trading_companion.product_narrative_answer.model_research_context',
                   side_effect=lambda value: model_research_context(value, limit=10000)):
            synthesize_product_answer('Explain the supplied news.', bundle, model=model, repair_invalid=False)
        self.assertEqual(len(model.calls), 1)
        validate_tool_messages(model.calls[0], tools=[])
        context = json.loads(model.calls[0][2].content.split('\n', 1)[1])
        records = {item['id']: item for item in context['evidence']}
        parent_alias = f'E{next(i for i, item in enumerate(bundle.evidence, 1) if item.id == parent.id)}'
        offered = [item for item in records.values() if item['content_kind'] == 'body_excerpt']
        self.assertTrue(offered)
        self.assertIn(parent_alias, records)
        self.assertTrue(all(item['document']['parent_title_id'] == parent.id for item in offered))
        self.assertNotIn(UNREAD_TEXT, json.dumps(context, ensure_ascii=False))

    def test_unaffordable_parent_does_not_change_baseline_or_take_other_domain_capacity(self):
        bundle, parent = older_parent_bundle()
        # Only one title of this issuer/domain fits, leaving no capacity for the
        # much larger actual parent plus its excerpt even after a slot exchange.
        company = bundle.companies[2]
        evidence = tuple(replace(item, display='Short title') if item.kind == 'news' and item.id != parent.id
                         else item for item in company.evidence)
        bundle = replace(bundle, companies=(*bundle.companies[:2], replace(company, evidence=evidence)))
        base = replace(bundle, supplemental_evidence=(), documents=None)
        for limit in (500, 1000, 1600):
            with self.subTest(limit=limit):
                before = model_research_context(base, limit=limit)
                result = model_research_context(bundle, limit=limit)
                self.assertEqual(result['evidence'], before['evidence'])
                self.assertFalse(any(item['content_kind'] == 'body_excerpt' for item in result['evidence']))
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), limit)

    def test_forged_cross_company_parent_is_not_supplied(self):
        bundle, parent = older_parent_bundle()
        foreign = bundle.companies[0].evidence[2]
        body = bundle.supplemental_evidence[0]
        body = replace(body, document={**body.document, 'parent_title_id': foreign.id})
        bundle = replace(bundle, supplemental_evidence=(body,))
        self.assertFalse(any(item['content_kind'] == 'body_excerpt' for item in _context(bundle)['evidence']))


if __name__ == '__main__':
    unittest.main()
