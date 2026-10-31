"""Selected-purpose prose checks, kept separate from the backward-compatible trend blocks."""
import re
from trading_companion.answer_validation import check_claim
from trading_companion.index_scenario import selected_plan, plan_evidence

INSTRUCTION = '''If scenario_candidates are supplied, the application computes all candidate plans. Your direction selects exactly one purpose: 偏多=conditional_entry, 震荡=wait, 偏空=holding_exit, 暂不判断=no plan. Never infer direction from ATR or choose direction merely to expose a plan.
Keep the ordinary blocks fact-only as before. Optionally add scenario_blocks (0-2 objects with title, text, evidence_ids, numbers in the same format). These separate blocks may ONLY quote the fields and exact displayed values of the candidate selected by your direction. Use the supplied S evidence IDs and numbers.field; every planned point must have an explicit field label. Stops, targets, ratio and reference close are distinct uses even when values coincide. Do not turn a candidate, observed high/low or fact into another purpose. Copy plan horizon/method, never calculate a future date. Use conditional wording and say 指数不可直接交易，点位为情景参考. 偏空 has reference_close and exit_below ONLY; no entry, target or short selling. 震荡 has observation bounds ONLY. An unavailable plan or 暂不判断 has no numerical plan prose. In all user-facing titles and prose use Chinese purpose descriptions 条件性入场/持有退出观察/等待观察; never output the internal names conditional_entry/holding_exit/wait. Do not include leverage, positions, fund prices, execution or guarantees.'''


def validate_scenario_blocks(value, result, direction):
    from trading_companion.index_answer import IndexAnswerQuality, _scope_allowed, _price_relations_allowed, _title
    from trading_companion.index_research import answer_context
    plan = selected_plan(result, direction)
    records = plan_evidence(plan)
    context = answer_context(result)
    # Facts are not plan sources; unknown/unused-purpose refs must fail even if the number matches.
    context.update(evidence=records, _index_scenario=True)
    generated = retained = rejected = partial = 0
    output = []
    if type(value) is not list or len(value) > 2:
        return (), IndexAnswerQuality(rejected_paragraphs=1)
    for block in value:
        original = block.get('text', '') if type(block) is dict else ''
        count = len(re.findall(r'[^。！？!?\n]+[。！？!?]?', original)) if type(original) is str else 0
        title = _title(block.get('title')) if type(block) is dict else None
        if (not records or title not in ('情景', '关键点位', '观察重点', '局限')
                or type(original) is not str or not 0 < len(original) <= 600):
            generated += count
            rejected += 1
            continue
        def guard(sentence):
            # Remove only known field names before the original index scope check;
            # all other trading recommendations and unsupported assertions still fail.
            allowed = {'conditional_entry': r'入场参考|止损参考|跌破观察位|止损|止盈\s*[12]|目标\s*[12]|实际盈亏比\s*[12]',
                'holding_exit': r'收盘参考|退出观察位|跌破止损参考',
                'wait': r'下方观察位|上方观察位'}[plan['purpose']]
            if re.search(r'保证|必然|一定|预测目标|建议.{0,4}(?:买|卖)|加仓|减仓|持仓|基金.{0,8}\d', sentence):
                return False
            cleaned = re.sub(allowed, '指数参考', sentence)
            return _scope_allowed(cleaned, result.request, None) and _price_relations_allowed(sentence, answer_context(result))
        stats = {}
        text, ids, _, count, kept = check_claim(block, instrument=result.request.instrument_id,
            context=context, section='scenarios', index=generated, expected_plan=plan['plan_id'],
            sentence_guard=guard, stats=stats)
        generated += count
        retained += kept
        partial += stats.get('partially_retained_sentences', 0)
        if text:
            output.append((title, text, tuple(ids)))
        else:
            rejected += 1
    return tuple(output), IndexAnswerQuality(generated, retained, generated-retained, len(output), rejected, partial)
