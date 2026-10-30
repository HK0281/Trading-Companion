"""Mechanical technical-answer checks, independent of the model and the product scope parser."""
import re
import unicodedata

from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.research_decision import RATINGS
from trading_companion.workflow_evaluation_metrics import ratio

FAILURE_CODES = ('answer_stance_mismatch', 'answer_numeric_unmatched', 'answer_binding_invalid')
UNAVAILABLE_PLANS = {
    'plan_excluded': '本次研究未启用技术面或参考计划。',
    'risk_atr_not_requested': '本次未计算所需的波动指标。',
    'risk_atr_unavailable': '最新波动指标不可用或历史窗口不足。',
    'risk_nonpositive_reference': '未取得有效的正价格或历史波动。',
    'risk_evidence_mismatch': '行情未通过来源和日期核对。',
    'plan_price_grid_unavailable': '按价格网格舍入后无法形成有效计划。',
    'decision_context_unusable': '未取得完整且通过核对的计划资料。',
}
_MENTION = re.compile(r'(?<![a-z])(?P<kind>VWMA|VOLUME|MACD|BOLL|SMA|EMA|RSI|ATR|ADX|OBV|MA)'
                      r'(?![a-z_])\s*(?:\(\s*(?:window\s*=\s*)?)?(?P<window>\d+)?', re.I)
_ALIASES = re.compile(r'(?P<window>\d+)\s*(?:日|天|周)\s*(?:简单)?均线|'
                      r'(?P<boll>布林带)|(?P<rsi>相对强弱指标)|(?P<volume>成交量指标|量比)', re.I)
_SPEC = re.compile(r'(sma|ema|rsi|atr|boll|macd|volume|adx|obv|vwma)\([^)]*\)', re.I)
_PLAN_PRICE = re.compile(r'(?:止损|止盈|目标价?|退出价)[^。！？!?\n]{0,24}'
                         r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)\s*(?:元|块)|'
                         r'(?:止损|止盈|目标价)(?:价位?|位|价格)?\s*(?:(?:可|设为|建议|参考|是|为|在|放在|约|到|定为|[:：])\s*)*'
                         r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)|'
                         r'\d+(?:\.\d+)?\s*元[^。！？!?\n]{0,12}(?:止损|止盈|目标价)', re.I)


def prose_blocks(value):
    """Inspect displayed analysis, not source URLs, IDs or diagnostic messages."""
    if type(value) is dict:
        for key, item in value.items():
            if key in ('text', 'title', 'lower_label', 'upper_label') and type(item) is str:
                yield item
            elif type(item) in (dict, list, tuple):
                yield from prose_blocks(item)
    elif type(value) in (list, tuple):
        for item in value:
            yield from prose_blocks(item)


def _negative_mention(text, match):
    before = text[max(0, match.start() - 12):match.start()]
    after = text[match.end():match.end() + 12]
    return bool(re.search(r'(?:未请求|未计算|未使用|没有请求|不使用|不看|不提供|无需计算)\s*$', before)
                or re.match(r'\s*(?:未请求|未计算|不可用|未提供)', after))


def plan_price_claim(text):
    """Require an affirmative numerical plan claim, including structured level text."""
    text = unicodedata.normalize('NFKC', text)
    for sentence in re.split(r'[。！？!?\n；;]', text):
        for price in _PLAN_PRICE.finditer(sentence):
            labels = list(re.finditer(r'止损|止盈|目标价?|退出价', sentence[price.start():price.end()]))
            for label in labels:
                start, end = price.start() + label.start(), price.start() + label.end()
                before = sentence[max(0, start - 16):start]
                after = sentence[end:end + 12]
                if (re.search(r'(?:不提供|未提供|未计算|没有|无法提供|不能提供|不设定|不设置|不设)\s*$', before)
                        or re.match(r'\s*(?:不可用|未提供|无法给出|不能给出)', after)):
                    continue
                return True
    return False


def unexpected_indicators(text, allowed, *, prose=False):
    text = unicodedata.normalize('NFKC', text)
    kinds = {value.split('(', 1)[0] for value in allowed}
    unexpected = set()
    for match in _MENTION.finditer(text):
        if prose and _negative_mention(text, match):
            continue
        kind = match['kind'].lower()
        kind = 'sma' if kind == 'ma' else kind
        window = match['window']
        if kind not in kinds or window and not any(
                re.search(r'(?:window|fast)=' + window + r'(?:[,)]|$)', spec) for spec in allowed
                if spec.startswith(kind + '(')):
            unexpected.add(match.group().strip())
    for match in _ALIASES.finditer(text):
        if prose and _negative_mention(text, match):
            continue
        kind = 'sma' if match['window'] else next(k for k in ('boll', 'rsi', 'volume') if match[k])
        if kind not in kinds or match['window'] and f'sma(window={match["window"]})' not in allowed:
            unexpected.add(match.group())
    for match in _SPEC.finditer(text):
        if prose and _negative_mention(text, match):
            continue
        spec = match.group().lower()
        spec = re.sub(r'^(sma|ema|rsi|atr|volume|adx|vwma)\((\d+)\)$', r'\1(window=\2)', spec)
        if spec not in allowed:
            unexpected.add(match.group())
    return sorted(unexpected)


def quality_metrics(quality):
    quality = quality or {}
    counts = {key: quality.get(key) if type(quality.get(key)) is int and quality[key] >= 0 else None
              for key in ('generated_sentences', 'deleted_sentences', 'deduplicated_drivers', 'deduplicated_sentences',
                          'inline_binding_recoveries')}
    known = counts['generated_sentences'] is not None and counts['deleted_sentences'] is not None
    counts['sentence_deletion_rate'] = (ratio(counts['deleted_sentences'], counts['generated_sentences'])
                                       if known else None)
    drafts = quality.get('draft_issue_counts')
    known_issues = (quality.get('issue_count_version') == 'answer-issues-v1' and type(drafts) is list
                    and 1 <= len(drafts) <= 2 and all(type(d) is dict and all(type(v) is int and v >= 0
                                                                          for v in d.values()) for d in drafts))
    counts['draft_failure_counts'] = {
        phase: {code: (sum(d.get(code, 0) for d in selected) if known_issues else None) for code in FAILURE_CODES}
        for phase, selected in (('first_draft', (drafts or [])[:1]), ('repair_draft', (drafts or [])[1:]),
                                ('all_drafts', drafts or []))}
    counts['final_reason_counts'] = {code: sum(r.get('code') == code for r in quality.get('reasons', []))
                                    if type(quality.get('reasons')) is list else None for code in FAILURE_CODES}
    counts['issue_counts_known'] = known_issues
    counts['output_truncations'] = (quality.get('output_truncations')
        if quality.get('output_truncation_version') == 'answer-output-v1'
        and type(quality.get('output_truncations')) is int else None)
    from trading_companion.answer_validation_metrics import clause_metrics
    counts.update(clause_metrics([quality]))
    return counts


def grade_turn(case, expected, result, observation=None, source_calls=None):
    company = result.get('company_research') or {}
    instrument = case['instrument_id']
    evidence = company.get('evidence', [])
    narratives = company.get('narratives', [])
    prose = list(prose_blocks(narratives + company.get('decisions', [])))
    prose += [text for text in (company.get('text'), result.get('answer')) if type(text) is str]
    prose += [text for text in company.get('paragraphs', []) if type(text) is str]
    model_narratives = [n for n in narratives if n.get('origin') == 'model' and n.get('instrument_id') == instrument]
    checks = dict(published=result.get('status') in ('completed', 'limited'),
        model_answer=company.get('version') == 'product-research-answer-v3' and company.get('origin') == 'model'
        and company.get('status') in ('accepted', 'limited') and (company.get('quality') or {}).get('state') in ('complete', 'partial')
        and bool(model_narratives) and any(text.strip() for text in prose_blocks(model_narratives)),
        identity=[s.get('instrument_id') for s in company.get('securities', [])] == [instrument]
        and all(e.get('instrument_id') == instrument for e in evidence)
        and (result.get('parameters') or {}).get('instrument_id') == instrument)
    formed = all(checks.values())
    violations, unknown = [], []
    if observation is None:
        unknown.append('collection_observation_missing')
    if source_calls is None:
        unknown.append('source_call_ledger_missing')
    actual = []
    try:
        actual = [IndicatorSpec(i['kind'], **{k: v for k, v in i.items() if k != 'kind'}).indicator_id
                  for i in result['parameters']['indicators']]
    except (KeyError, TypeError, ValueError):
        unknown.append('confirmed_indicators_missing')
    checks['required_indicators'] = set(expected['required_indicators']) <= set(actual)
    if expected['strict_indicators']:
        allowed = expected['strict_indicators']
        if set(actual) != set(allowed):
            violations.append(dict(code='scope_confirmed_indicators', actual=actual))
        if observation is not None:
            if (observation.get('indicator_ids') != actual
                    or any(ids != actual for ids in observation.get('technical_requests', []))
                    or not observation.get('technical_requests')):
                violations.append(dict(code='scope_actual_indicators'))
        for index, record in enumerate(evidence):
            extra = unexpected_indicators(' '.join(str(record.get(k, '')) for k in ('id', 'label', 'display', 'dependencies')), allowed)
            if extra:
                violations.append(dict(code='scope_evidence_indicator', evidence_index=index, indicators=extra))
        for index, text in enumerate(prose):
            extra = unexpected_indicators(text, allowed, prose=True)
            if extra:
                violations.append(dict(code='scope_prose_indicator', block=index, indicators=extra))
    domains = expected['allowed_domains']
    if domains:
        for call in source_calls or []:
            if call.get('kind') not in domains:
                violations.append(dict(code='scope_source_domain', kind=call.get('kind')))
        for record in evidence:
            if record.get('kind') not in domains and record.get('kind') != 'risk':
                violations.append(dict(code='scope_evidence_domain', kind=record.get('kind')))
        if observation is not None and not set(observation.get('kinds', [])) <= set(domains):
            violations.append(dict(code='scope_confirmed_domains'))
    if expected['expected_dates']:
        dates = expected['expected_dates']
        checks['historical_dates'] = [(result.get('parameters') or {}).get(k) for k in ('start_date', 'as_of_date')] == dates
    plans = company.get('trade_plans', [])
    available = [p for p in plans if p.get('status') == 'available' and p.get('instrument_id') == instrument
                 and p.get('atr_indicator_id') == 'atr(window=14)' and p.get('values')
                 and 'atr(window=14)' in actual
                 and any('atr(window=14)' in dep for dep in p.get('evidence_ids', []))
                 and any(e.get('id') == p.get('plan_id') and e.get('content_kind') == 'trade_plan'
                         and e.get('instrument_id') == instrument and e.get('dependencies') == p.get('evidence_ids')
                         for e in evidence)]
    if not available:
        for index, text in enumerate(prose):
            if plan_price_claim(text):
                violations.append(dict(code='scope_plan_price_without_atr', block=index))
        for narrative in narratives:
            for level in narrative.get('levels', []):
                declarations = [str(level.get('text', '')) + ' ' + str(level.get('lower', '')) + '元']
                declarations += [str(level.get(side + '_label', '')) + ' ' + str(level.get(side, '')) + '元'
                                 for side in ('lower', 'upper')]
                if any(plan_price_claim(text) for text in declarations):
                    violations.append(dict(code='scope_structured_plan_without_atr'))
    plan_explanation = None
    if expected['requires_decision']:
        decisions = company.get('decisions', [])
        decision = decisions[0] if len(decisions) == 1 else {}
        rating = RATINGS.get(decision.get('rating'))
        checks['rating_direction'] = bool(rating and decision.get('status') == 'available'
            and decision.get('code') == 'decision_ready' and decision.get('instrument_id') == instrument
            and decision.get('direction') in rating[1])
        selected = next((p for p in plans if rating and p.get('purpose') == rating[0]
                         and p.get('instrument_id') == instrument), {})
        plan_explanation = UNAVAILABLE_PLANS.get(selected.get('code'))
        checks['reference_plan'] = (selected in available and decision.get('plan_id') == selected.get('plan_id')) or bool(
            selected.get('status') == 'unavailable' and not selected.get('values') and plan_explanation
            and decision.get('plan_id') is None)
    return dict(formed=formed, checks=checks, passed=all(checks.values()) and not violations and not unknown,
                scope=dict(violations=violations, unknown=unknown, passed=not violations and not unknown),
                plan_unavailable_explanation=plan_explanation, **quality_metrics(company.get('quality')))
