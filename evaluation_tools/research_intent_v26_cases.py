"""V2.6 case admission and context construction only; no target model or scoring.

The public entry points read development and the 414 exposed legacy cases.
Author-only holdout material is deliberately absent from the default paths.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from hashlib import sha256
from math import ceil
from pathlib import Path
import re
import unicodedata

from evaluation_tools import research_intent_v25_cases as previous
from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation_cases import GOLD_FIELDS, ROOT, require, valid_row
from trading_companion.typesafe_jev import decode

VERSION = 'research-intent-v2.6-cases-v1'
DEV = ROOT / 'data/evaluation/research-intent-v2.6-dev.jsonl'
SETUPS = DEV.with_name('research-intent-v2.6-setups.json')
AMENDMENTS = DEV.with_name('research-intent-v2.6-amendments.json')
FREEZE = DEV.with_name('research-intent-v2.6-freeze.json')
COVERAGE = DEV.with_name('research-intent-v2.6-rule-coverage.json')
DEV_COUNT = 64
NOT_APPLICABLE = frozenset(('V2222', 'V2223'))
LEGACY_GAP = 'V2002'
PARENT_FILES = (
    'data/evaluation/research-intent-v2.jsonl',
    'data/evaluation/research-intent-v2-dev-amendments.json',
    'data/evaluation/research-intent-v2.5-amendments.json',
    'data/evaluation/research-intent-v2.5.1-amendments.json',
    'data/evaluation/research-intent-v2.5-dev.jsonl',
    'data/evaluation/research-intent-v2.5-holdout.jsonl',
)
PUBLIC_FILES = (
    'data/evaluation/research-intent-v2.6-dev.jsonl',
    'data/evaluation/research-intent-v2.6-dev.manifest.json',
    'data/evaluation/research-intent-v2.6-setups.json',
    'data/evaluation/research-intent-v2.6-amendments.json',
    'data/evaluation/research-intent-v2.6-rule-coverage.json',
    'docs/research-intent-v2.6-guard-principles.md',
    'docs/research-intent-v2.6-plan.md',
    'evaluation_tools/research_intent_v26_cases.py',
    'evaluation_tools/preflight_research_intents_v26.py',
    'tests/test_research_intent_v26_cases.py',
    'tests/test_research_intent_v25_holdout.py',
)
NEGATION_MARKERS = ('不', '别', '无需', '免', '去掉', '删', '移除', '取消', '停止', '放弃',
                    '减少', '少看', '少用', '没', '排除', '禁用')
EXCLUSIONS = frozenset(('profile', 'financial', 'news', 'announcements', 'technical', 'risk', 'plan',
    'indicator:sma', 'indicator:ema', 'indicator:macd', 'indicator:rsi', 'indicator:atr', 'indicator:boll',
    'indicator:volume', 'indicator:adx', 'indicator:obv', 'indicator:vwma'))
RULES = {
    'A.negative_after': '否定或移除标记可以在目标之后，不限制相邻及词序',
    'A.removal': '删除或取消研究内容，保留模型提出的排除项',
    'A.plan_false': '放弃价位不要求肯定价位依据，不恢复权限',
    'A.strict_scope': '严格范围的原文出处有效即可，不二次匹配限定句式',
    'A.negated_limit': '取消唯一范围限制不等于排除该研究领域',
    'A.clarify': '保留澄清，不因找到对象就自动恢复研究',
    'B.plan_positive': '价位授权必须来自本句肯定依据或有效同任务待办',
    'B.direction_no_plan': '方向评级本身不授权参考价位',
    'B.purchase_plan': '购买价值请求与明确价位否决分别核对',
    'B.veto': '价位否决撤销授权，不生成可执行价位',
    'B.plan_conflict': '肯定价位请求与否决冲突时澄清',
    'B.empty_strict_conflict': '严格范围中所有领域被排除时澄清',
    'B.plan_dependency': 'ATR或风险等计划依赖被排除时不得生成计划',
    'B.identity': '只从可信目录和程序状态绑定身份',
    'B.identity_ambiguous': '股票名称歧义时不猜身份',
    'B.identity_unknown': '未知股票名不得制造代码',
    'B.index_ambiguous': '指数指称不明确时澄清身份',
    'B.multi_subject': '多个明确对象均保留，不遗漏或追加对象',
    'B.missing_subject': '缺对象时先澄清对象',
    'B.source_mismatch': '数据来源不匹配的旧对象不可使用',
    'B.mode_mismatch': '演示与真实模式不匹配的旧对象不可使用',
    'B.memory_off': '关闭记忆不能沿用对象',
    'B.no_state': '无程序记录不从普通历史恢复身份',
    'B.consumed': '最近身份已消耗，不回退到旧缓存',
    'B.future_state': '未来创建的状态不得授权对象',
    'B.date_capability': '历史窗口、非法日期和能力上限仍核验',
    'B.future_actual': '未来实际事实不可用，不改写成条件情景',
    'B.horizon_invalid': '非法量化期限保留原文并澄清',
    'B.indicator_supported': '支持指标及其参数按原文保留',
    'B.indicator_unsupported': '未支持指标保留原文并澄清，不新增能力',
    'B.profile': '明确公司或业务情况为概况研究',
    'B.general_situation': '泛称情况不凭空指定领域',
    'B.screening': '筛选任务保留自身条件，不绑定为单只股票',
    'C.identity_only': '最近研究只提供对象身份，不提供任务授权',
    'C.no_old_horizon': '普通最近研究与旧报告不补未来期限',
    'C.no_old_plan': '普通旧报告不恢复目标或价位授权',
    'C.history_not_authority': '聊天历史不能提供新目标、期限或价位',
    'C.history_not_horizon': '历史样本窗口不充当未来期限',
    'C.valid_pending': '唯一有效同任务待办只续接登记字段',
    'C.new_subject': '换对象或新任务不借旧对象授权',
    'C.independent_new': '完整独立新任务可绕开无效或冲突待办',
    'C.operation': '模型区分new/update/refresh，程序不凭关键词改写',
    'C.explain_no_authority': '解释旧结论不产生新研究和价位权限',
    'C.pending_conflict': '多待办不猜选，模型理解后澄清冲突',
    'C.pending_expired': '待办在900秒边界失效，不延长或恢复授权',
    'C.pending_identity': '待办目录身份改变后不得恢复授权',
    'C.pending_mode': '待办模式改变后不得恢复授权',
    'D.command.hit': '取消清空固定整句命令零调用',
    'D.command.suffix': '取消清空有附句时完整交模型',
    'D.command.priority': '固定命令优先于待办',
    'D.history.hit': '固定历史命令零调用，仍校验保存结果',
    'D.history.suffix': '历史命令有附句时完整交模型',
    'D.history.priority': '历史命令优先于待办冲突',
    'D.guard.hit': '既有执行或注入搜索命中时本地拒绝',
    'D.guard.miss': '未命中执行注入规则不借研究词提前截走',
    'D.guard.whole_reject': '执行注入命中时整轮拒绝，不执行前半句研究',
    'D.horizon.hit': '有效待办的窄整句期限零调用',
    'D.horizon.suffix': '期限带附句时完整交模型',
    'D.horizon.invalid': '失效或冲突待办的期限输入交模型',
    'D.recall.hit': '固定整句方向评级回顾零调用',
    'D.recall.suffix': '回顾与新研究混合不截断',
    'D.knowledge.hit': '完整指标定义语法零调用',
    'D.knowledge.macd': 'MACD子项和最多10项的既有约束保留',
    'D.knowledge.suffix': '指标定义带研究附句时完整交模型',
    'D.explain.semantic': '宽泛保存结论解释交模型',
    'D.general_knowledge.semantic': '非指标定义的一般知识交模型',
    'D.unsupported_index.semantic': '已知不支持指数交模型理解后拒绝',
    'E.explicit': '本句明确对象使用question和none指代结构',
    'E.none': '完全无对象时source=none且reference=none',
    'E.latest': '最近范围恰好一个合法对象才可latest绑定',
    'E.latest_ambiguous': 'latest范围多对象时不得偷偷取第一只',
    'E.ordinal_latest': '模型给出最近研究的合法整数序号',
    'E.ordinal_shortlist': '模型给出保存名单中的合法序号',
    'E.ordinal_outside': '序号超出指定范围时澄清，不回退',
    'E.all_latest': '全部指代只取最近研究范围的全部对象',
    'E.all_shortlist': '全部指代只取保存名单范围的全部对象',
    'E.pending': '待办指代只查指定待办范围，不借最近研究',
    'E.scope_separation': '名单与最近研究的身份池不能合并',
    'E.unavailable_scope': '指定范围不可用时澄清，不换范围猜选',
    'F.product_target': '具体不支持产品作为研究对象时拒绝并保留原文',
    'F.product_code': '产品原文可包含代码或括号',
    'F.no_index_binding': '产品拒绝后不绑定它跟踪的指数',
    'F.product_background': '产品只作背景不覆盖股票研究',
    'F.product_concept': '产品概念解释不自动拒绝',
    'F.unsupported_index': '未支持指数能力边界保留',
}
# These are already exposed old questions, never new sealed identifiers.
LEGACY_WITNESSES = {
    'A.negated_limit': ['V25D073'], 'B.indicator_unsupported': ['V25D074'],
    'D.history.priority': ['V25D057'],
    'B.identity_unknown': ['V25D070'], 'B.index_ambiguous': ['V25D035'],
    'B.memory_off': ['V25D078'], 'B.general_situation': ['V25D063'],
    'E.ordinal_outside': ['V25H019'], 'F.unsupported_index': ['V25H057'],
    'D.unsupported_index.semantic': ['V25H057'],
    'C.pending_identity': ['V25D069'], 'C.pending_mode': ['V25D077'],
}


def public_sha256(path):
    return previous.public_sha256(path)


def valid_reference(value):
    require(type(value) is dict and value.keys() == {'kind', 'index', 'scope'}, 'Invalid reference structure')
    kind, index, scope = value['kind'], value['index'], value['scope']
    require(type(kind) is str and kind in ('none', 'latest', 'ordinal', 'all'), 'Invalid reference kind')
    if kind == 'none':
        require(index is None and scope is None, 'none reference must have null index/scope')
    else:
        require(type(scope) is str and scope in ('latest_research', 'shortlist', 'pending'), 'Invalid reference scope')
        require(type(index) is int and index >= 1 if kind == 'ordinal' else index is None,
                'Invalid reference index (bool is not an integer)')


def valid_new_row(row):
    metadata = {'variant_group', 'coverage', 'setup_id', 'reference_expectation', 'intent_evidence', 'rule_tags'}
    require(type(row) is dict and metadata <= row.keys(), 'Missing v2.6 metadata')
    legacy = deepcopy(row)
    for key in metadata:
        legacy.pop(key)
    require(type(row['id']) is str and re.fullmatch(r'V26[DH][0-9]{3}', row['id']) is not None
            and row['id'][3] == ('D' if row['split'] == 'dev' else 'H'), 'Invalid v2.6 ID/split')
    legacy['id'] = 'V2000'
    valid_row(legacy)
    require(row['fault'] is None and row['source']['kind'] == 'synthetic' and not row['probe']['steps'],
            'Language cases cannot inject target responses')
    for key in ('variant_group', 'setup_id'):
        require(type(row[key]) is str and re.fullmatch(r'[a-zA-Z0-9_-]{3,100}', row[key]) is not None)
    require(type(row['coverage']) is dict and row['coverage'].keys() <= set(previous.AXES)
            and all(value in previous.LABELS for value in row['coverage'].values()), 'Invalid coverage axis')
    require(type(row['rule_tags']) is list and row['rule_tags'] and len(set(row['rule_tags'])) == len(row['rule_tags'])
            and all(label in RULES for label in row['rule_tags']), 'Unknown or duplicate rule tag')
    reference = row['reference_expectation']
    require(type(reference) is dict and reference.keys() == {'reference', 'selection', 'note'}
            and type(reference['note']) is str and reference['note'], 'Invalid reference expectation')
    valid_reference(reference['reference'])
    require((row['gold']['subject_source'] == 'previous') == (reference['reference']['kind'] != 'none'),
            'reference/source mismatch')
    selection = reference['selection']
    require(selection is None or type(selection) is str and selection and selection in row['question'], 'Ungrounded selection')
    require(reference['reference']['kind'] != 'none' or selection is None, 'none reference has selection')
    evidence = row['intent_evidence']
    require(type(evidence) is dict and evidence.keys() == {'objective_phrase', 'plan_phrase', 'scope_phrase',
            'research_kind_phrases', 'clarification_phrase'}, 'Invalid intent evidence')
    for key in ('objective_phrase', 'plan_phrase', 'scope_phrase', 'clarification_phrase'):
        phrase = evidence[key]
        require(phrase is None or type(phrase) is str and phrase and phrase in row['question'], 'Ungrounded intent phrase')
    kinds = evidence['research_kind_phrases']
    require(type(kinds) is dict and set(kinds) == set(row['gold']['research_kinds'])
            and all(type(p) is str and p and p in row['question'] for p in kinds.values()), 'Missing domain evidence')
    require(not row['gold']['strict_scope'] or evidence['scope_phrase'] is not None, 'Missing strict scope evidence')
    require(len(row['gold']['exclusions']) == len(row['evidence']['exclusions'])
            and set(row['gold']['exclusions']) <= EXCLUSIONS
            and all(any(marker in phrase for marker in NEGATION_MARKERS) for phrase in row['evidence']['exclusions']),
            'Invalid exclusion evidence or direction')
    require(not row['gold']['horizon'] or row['evidence']['horizon'], 'Missing horizon evidence')
    require(not row['gold']['date_expression'] or row['evidence']['dates'], 'Missing date evidence')
    require(row['gold']['indicator_mentions'] == row['evidence']['indicators'], 'Indicator evidence mismatch')
    require(row['gold']['subject_source'] != 'question' or row['evidence']['mentions'], 'Missing subject evidence')


def load_cases(path=DEV, *, expected_sha256=None):
    raw = Path(path).read_bytes().replace(b'\r\n', b'\n')
    require(0 < len(raw) <= 2_000_000, 'Invalid public corpus size')
    if expected_sha256 is not None:
        require(sha256(raw).hexdigest() == expected_sha256, 'Public corpus fingerprint mismatch')
    require(len(raw.splitlines()) == DEV_COUNT, 'Public development count mismatch')
    rows = [decode(line) for line in raw.splitlines()]
    for row in rows:
        valid_new_row(row)
        require(row['split'] == 'dev', 'Public reader only accepts dev')
    require(len({r['id'] for r in rows}) == DEV_COUNT, 'Duplicate development ID')
    require(len({r['family_id'] for r in rows}) == 48, 'Development must cover all 48 families')
    return rows


def legacy_rows():
    """Return raw and already approved gold; never read any private packet."""
    from evaluation_tools.research_intent_v25_evaluation import apply_annotation_completion
    original, _ = previous.load_corpus()
    _, _, amended = previous.load_regression_amendments()
    effective, _ = apply_annotation_completion(original, {r['id']: r['gold'] for r in amended})
    dev = previous.load_cases()
    path = ROOT / 'data/evaluation/research-intent-v2.5-holdout.jsonl'
    raw = path.read_bytes().replace(b'\r\n', b'\n')
    require(len(raw.splitlines()) == 96, 'Exposed legacy inventory drift')
    exposed = [decode(line) for line in raw.splitlines()]
    for row in exposed:
        previous.valid_new_row(row)
        require(row['split'] == 'holdout', 'Exposed legacy split drift')
    rows = [*original, *dev, *exposed]
    require(len(rows) == 414 and len({r['id'] for r in rows}) == 414, 'Expected 414 public legacy rows')
    effective.update({r['id']: deepcopy(r['gold']) for r in [*dev, *exposed]})
    return rows, [dict(deepcopy(r), gold=deepcopy(effective[r['id']])) for r in rows]


def parent_hashes():
    return {name: public_sha256(ROOT/name) for name in PARENT_FILES}


def load_amendments(path=AMENDMENTS, *, require_approved=False):
    original, baseline = legacy_rows()
    value = read_json(path)
    require(type(value) is dict and value.keys() == {'version', 'parents_sha256_lf', 'changes'}
            and value['version'] == VERSION and value['parents_sha256_lf'] == parent_hashes()
            and type(value['changes']) is list, 'Invalid amendment parents/version')
    by_id, raw = {r['id']: deepcopy(r) for r in baseline}, {r['id']: r for r in original}
    seen = set()
    for item in value['changes']:
        require(type(item) is dict and item.keys() == {'id', 'parent_row_sha256', 'baseline_gold_sha256', 'field',
                'before', 'after', 'reason', 'rule', 'status', 'reviewer', 'approval_message'}, 'Invalid amendment item')
        identifier, field = item['id'], item['field']
        require(identifier in raw and field in GOLD_FIELDS and (identifier, field) not in seen, 'Duplicate/unknown amendment')
        parent = next(r for r in baseline if r['id'] == identifier)
        require(item['parent_row_sha256'] == digest(raw[identifier]) and item['baseline_gold_sha256'] == digest(parent['gold'])
                and item['before'] == parent['gold'][field] and type(item['before']) is type(parent['gold'][field])
                and item['before'] != item['after'] and item['rule'] in ('C', 'D', 'E', 'F')
                and type(item['reason']) is str and item['reason'].strip(), 'Amendment fingerprint/before mismatch')
        require(item['status'] in ('pending', 'approved'), 'Invalid amendment status')
        approved = item['status'] == 'approved'
        require(item['reviewer'] == ('claude' if approved else None)
                and (type(item['approval_message']) is int and item['approval_message'] > 432 if approved
                     else item['approval_message'] is None), 'Invalid amendment approval')
        checked = deepcopy(raw[identifier])
        checked = {k: v for k, v in checked.items() if k in {'id','family_id','split','question','context','gold','evidence','reason','tags','source','probe','fault'}}
        checked.update(id='V2000', gold=dict(deepcopy(parent['gold']), **{field: deepcopy(item['after'])}))
        valid_row(checked)
        if approved:
            by_id[identifier]['gold'][field] = deepcopy(item['after'])
        elif require_approved:
            raise ValueError('Claude approval required for pending amendment: '+identifier+'/'+field)
        seen.add((identifier, field))
    # Pending values are auditable proposals and never silently become the effective gold.
    return original, value, [by_id[r['id']] for r in baseline]


def load_setups(path=SETUPS):
    value = read_json(path)
    require(type(value) is dict and value.keys() == {'version', 'parent_sha256_lf', 'review', 'entries'}
            and value['version'] == VERSION
            and value['parent_sha256_lf'] == public_sha256(previous.SETUPS), 'Invalid setup provenance')
    parent = previous.load_setups()
    require(value['entries'] == parent['entries'], 'V2.6 currently reuses the approved neutral setups verbatim')
    review = value['review']
    require(review['status'] in ('pending', 'approved')
            and review['reviewer'] == ('claude' if review['status'] == 'approved' else None)
            and (type(review['approval_message']) is int and review['approval_message'] > 432
                 if review['status'] == 'approved' else review['approval_message'] is None), 'Invalid setup review')
    return value


def state_input(row):
    return deepcopy({key: row[key] for key in ('question', 'context', 'setup_id')})


def build_context(state, setups, prefixes):
    return previous.build_context(state, setups, prefixes)


def local_syntax(question):
    """Audit the published finite grammar, not a candidate implementation/prediction."""
    from trading_companion.product_plan import _EXECUTION, _INJECTION
    from trading_companion.indicator_knowledge import parse_knowledge_question
    from trading_companion.research_objective import parse_outlook_expression
    from trading_companion.agent_input import InputIssue
    text = unicodedata.normalize('NFKC', question).strip().rstrip('。.!！').strip()
    if _EXECUTION.search(question) or _INJECTION.search(question) or re.search(r'(?:提交|执行|发出|撤销)[^。\n]{0,80}(?:委托|订单)', question):
        return 'guard'
    if text in ('取消', '取消分析', '取消研究', '算了', '新对话', '重新开始', '清空会话'):
        return 'command'
    if text in ('查看上次结果', '查看上一次结果', '上次研究结果'):
        return 'history'
    short = text[:-1] if text.endswith(('?', '？')) else text
    if re.fullmatch(r'你(?:之前|上次|上一轮|刚才|此前)(?:说的|给出的)(?:方向|评级)是什么', short):
        return 'recall'
    if parse_knowledge_question(question) is not None:
        return 'knowledge'
    if re.fullmatch(r'短线|中线|长线|短期|中期|长期|未来[0-9零〇一二两三四五六七八九十百千万]+(?:天|周|个月|年|交易日|个交易日)', short):
        try:
            parsed = parse_outlook_expression(short).horizon
        except (InputIssue, ValueError):
            parsed = None
        if parsed is not None:
            return 'horizon'
    return None


def expected_local(row, context):
    syntax = local_syntax(row['question'])
    if syntax == 'horizon':
        pending = context.model_context()['pending']
        return bool(context.conversation_memory and pending and pending['valid']
                    and row['context']['pending'] in ('company_horizon','index_horizon'))
    if syntax == 'recall':
        return bool(context.conversation_memory and context.history)
    return syntax is not None


def preflight(rows, setups=None):
    from scripts.verify import clean_environment, offline_network
    from unittest.mock import patch
    from trading_companion import research_intent_v2 as intent
    core = previous.core
    old, _ = previous.load_corpus()
    prefixes = core.prefix_library(old)
    setups = setups or load_setups()
    records = []
    with clean_environment(), offline_network(), \
            patch.object(intent, 'understand', side_effect=AssertionError('No target understanding')), \
            patch.object(intent, 'local_preflight', side_effect=AssertionError('No target interpretation')), \
            patch.object(core, 'predict', side_effect=AssertionError('No target prediction')), \
            patch.object(core, 'understand', side_effect=AssertionError('No target understanding')), \
            patch.object(core, 'local_preflight', side_effect=AssertionError('No target interpretation')), \
            patch.object(core, 'metrics', side_effect=AssertionError('No scoring')), \
            patch.object(core, '_live_model', side_effect=AssertionError('No live model')), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch('trading_companion.local_env.load_evaluation_key_environment', side_effect=AssertionError('No credentials')):
        for row in rows:
            if row['id'] in NOT_APPLICABLE:
                records.append(dict(id=row['id'], status='not_applicable'))
                continue
            try:
                if 'setup_id' in row:
                    state = state_input(row)
                    state['question'] = '仅检查前置状态，不执行目标轮'
                    context, calls = build_context(state, setups, prefixes)
                else:
                    context, calls = core.program_context(dict(question='仅检查前置状态，不执行目标轮',
                        context=deepcopy(row['context'])), prefixes)
            except (ValueError, KeyError, IndexError):
                records.append(dict(id=row['id'], status='unbuildable'))
                continue
            record = dict(id=row['id'], status='buildable', setup_calls=calls,
                context_sha256=digest(context.model_context()), descriptor_sha256=digest(row['context']),
                setup=core.setup_metadata(context), expected_zero_intent_calls=expected_local(row, context))
            if row['id'].startswith('V26'):
                record['local_contract_matches'] = record['expected_zero_intent_calls'] == row['gold']['zero_intent_calls']
            records.append(record)
    failed = [r['id'] for r in records if r['status'] == 'unbuildable']
    local_mismatch = [r['id'] for r in records if r.get('local_contract_matches') is False]
    expected_gap = [LEGACY_GAP] if any(r['id'] == LEGACY_GAP for r in rows) else []
    return dict(count=len(rows), applicable=len(rows)-sum(r['status']=='not_applicable' for r in records),
        buildable=sum(r['status']=='buildable' for r in records),
        not_applicable=[r['id'] for r in records if r['status']=='not_applicable'],
        unbuildable=failed, expected_legacy_gap=expected_gap, local_contract_mismatch=local_mismatch,
        passed=failed == expected_gap and not local_mismatch, target_calls=0, prediction_rows=0, score_rows=0, rows=records)


def population_summary(rows):
    semantic = sum(not r['gold']['zero_intent_calls'] for r in rows)
    return dict(count=len(rows), families=dict(sorted(Counter(r['family_id'] for r in rows).items())),
        semantic_calls=semantic, local_zero_calls=len(rows)-semantic)


def thresholds(summary, dev_count=DEV_COUNT):
    return dict(all=dict(n=96, minimum_correct=92),
        implementation_unseen=dict(n=96, minimum_correct=92, minimum_fraction=.95),
        semantic=dict(n=summary['semantic_calls'], minimum_correct=ceil(.95*summary['semantic_calls'])),
        public_dev=dict(n=dev_count, minimum_correct=ceil(.95*dev_count)),
        public_regression=dict(n=412, minimum_correct=392, full_reference_n=414),
        families={f:dict(n=n, minimum_correct=ceil(.8*n)) for f,n in summary['families'].items() if n>=4},
        small_family_rule='n<4仅报告，仍计入总分', common_fields_not_worse=True,
        measurable_red_line_violations=0, failed_unmeasured_invalid_in_denominator=True,
        output_views=['raw_original','normalized_original','normalized_approved'])


def question_shape(row):
    # Keep the finite grammar (短期 versus 未来N天), while removing numeric swaps.
    shape_row = deepcopy(row)
    for phrase in row['evidence']['horizon']:
        replacement = re.sub(r'[0-9零〇一二两三四五六七八九十百千万]+', 'N', phrase)
        shape_row['question'] = shape_row['question'].replace(phrase, replacement)
    shape_row['evidence']['horizon'] = []
    return re.sub(r'[0-9]+', 'n', previous.question_shape(shape_row))


def validate_partition(dev, holdout, legacy):
    require(len(dev)==DEV_COUNT and len(holdout)==96, 'Expected 64/96 cases')
    expected = read_json(ROOT/'data/evaluation/research-intent-v2.5-freeze.json')['holdout']['summary']['families']
    require(dict(Counter(r['family_id'] for r in holdout)) == expected, 'Holdout family allocation drift')
    require({r['family_id'] for r in dev} == set(expected), 'Missing public family')
    groups, shapes = defaultdict(set), defaultdict(set)
    inputs = set()
    old_inputs = {digest(dict(question=r['question'],context=r['context'])) for r in legacy}
    for row in [*dev,*holdout]:
        valid_new_row(row)
        groups[row['variant_group']].add(row['split'])
        shapes[question_shape(row)].add(row['split'])
        key = digest(dict(question=row['question'],context=row['context']))
        require(key not in inputs and key not in old_inputs, 'Duplicate old/new input')
        inputs.add(key)
    require(all(len(s)==1 for s in groups.values()), 'Variant group leaks across sides')
    require(all(len(s)==1 for s in shapes.values()), 'Name/number/punctuation shape leaks across sides')
    return dict(variant_groups=len(groups), normalized_shapes=len(shapes), duplicate_inputs=0,
        cross_split_group_leaks=0, cross_split_shape_leaks=0,
        semantic_independence='机械检查不能证明语义独立，仍须Claude逐题审定；固定本地语法不能创造额外措辞。')


def load_manifest(rows, path=None):
    value = read_json(path or DEV.with_suffix('.manifest.json'))
    require(value['version']==VERSION and value['author']=='codex' and type(value['author_session_id']) is str
            and value['corpus_sha256_lf']==public_sha256(DEV) and value['summary']==population_summary(rows)
            and set(value['reviews'])=={r['id'] for r in rows}, 'Invalid development manifest')
    for row in rows:
        review=value['reviews'][row['id']]
        require(review['row_sha256']==digest(row) and review['status'] in ('pending','approved')
                and review['reviewer']==('claude' if review['status']=='approved' else None)
                and (type(review['approval_message']) is int and review['approval_message']>432
                     if review['status']=='approved' else review['approval_message'] is None), 'Invalid individual review')
    approved = sum(item['status'] == 'approved' for item in value['reviews'].values())
    require(value['review'] == dict(status='approved' if approved == len(rows) else 'partially_approved' if approved else 'pending',
            reviewed_count=approved, pending_count=len(rows)-approved), 'Development review summary mismatch')
    return value


def load_coverage(dev, path=COVERAGE):
    value=read_json(path)
    legacy,_=legacy_rows()
    public={r['id']:r for r in [*legacy,*dev]}
    require(value['version']==VERSION and value['uncovered_rules']==0
            and value['rule_count']==len(value['rules']), 'Invalid coverage inventory')
    required_public=set().union(*(set(r['rule_tags']) for r in dev))
    require(required_public <= value['rules'].keys(), 'Missing public rule coverage')
    for label,item in value['rules'].items():
        require(label in RULES and item['description']==RULES[label] and item['public_examples'], 'Missing public witness')
        for example in item['public_examples']:
            identifier=example['id']
            require(identifier in public and example['row_sha256']==digest(public[identifier])
                    and (label in public[identifier].get('rule_tags',[]) or identifier in LEGACY_WITNESSES.get(label,[])),
                    'Invalid public rule witness: '+label)
    require(value['deferred_checks']['G']['scored_in_language_accuracy'] is False
            and value['deferred_checks']['G']['status']=='not_implemented_not_measured', 'G must remain separately unmeasured')
    return value


def validate_freeze(path=FREEZE, *, require_review=False):
    value=read_json(path)
    require(value['version']==VERSION and value['status'] in ('awaiting_claude_review','frozen'), 'Invalid freeze status')
    require(set(value['public_files_sha256_lf'])==set(PUBLIC_FILES), 'Public inventory drift')
    for name,fingerprint in value['public_files_sha256_lf'].items():
        require(public_sha256(ROOT/name)==fingerprint, 'Public file drift: '+name)
    rows=load_cases(expected_sha256=value['dev_sha256_lf'])
    manifest=load_manifest(rows)
    _, amendments, _ = load_amendments(require_approved=require_review)
    setups = load_setups()
    load_coverage(rows)
    summary=value['holdout']['summary']
    old_summary=read_json(ROOT/'data/evaluation/research-intent-v2.5-freeze.json')['holdout']['summary']
    require(summary['count']==96 and summary['families']==old_summary['families']
            and type(summary['semantic_calls']) is int and 0<summary['semantic_calls']<=96
            and type(summary['local_zero_calls']) is int
            and summary['semantic_calls']+summary['local_zero_calls']==96, 'Frozen population drift')
    require(type(value['holdout']['sha256']) is str and re.fullmatch(r'[0-9a-f]{64}',value['holdout']['sha256']) is not None
            and {r['family_id'] for r in rows} == set(summary['families']), 'Frozen identity/inventory drift')
    require(value['thresholds']==thresholds(summary) and value['candidate']['eligible_for_switch'] is False
            and value['candidate']['live_enabled'] is False and value['paid_run']['status']=='requires_separate_approval',
            'Frozen threshold/authorization drift')
    approved = sum(item['status'] == 'approved' for item in manifest['reviews'].values())
    review = value['review']
    require(review['reviewed_dev_count'] == approved and review['pending_dev_count'] == DEV_COUNT-approved
            and type(review['reviewed_holdout_count']) is int and 0 <= review['reviewed_holdout_count'] <= 96
            and review['pending_holdout_count'] == 96-review['reviewed_holdout_count'], 'Frozen review counts drift')
    for split,n,buildable in (('dev',64,64),('holdout',96,96),('regression',414,411)):
        preflight_record = value['context_preflight'][split]
        require(preflight_record['count'] == n and preflight_record['buildable'] == buildable
                and preflight_record['passed'] is True and preflight_record['target_calls'] == 0
                and preflight_record['prediction_rows'] == 0 and preflight_record['score_rows'] == 0
                and not preflight_record['local_contract_mismatch']
                and preflight_record['preflight_tool_sha256_lf'] == public_sha256(Path(__file__)),
                'Frozen context preflight drift: '+split)
        require(preflight_record['unbuildable'] == ([LEGACY_GAP] if split == 'regression' else [])
                and preflight_record['not_applicable'] == (sorted(NOT_APPLICABLE) if split == 'regression' else [])
                and preflight_record['applicable'] == (412 if split == 'regression' else n), 'Frozen denominator drift')
    require(value['context_preflight']['dev']['authoring_input_sha256'] == digest(rows), 'Development preflight input drift')
    pending = [item for item in amendments['changes'] if item['status'] == 'pending']
    require(value['regression']['total'] == 414 and value['regression']['applicable'] == 412
            and value['regression']['not_applicable'] == sorted(NOT_APPLICABLE)
            and value['regression']['retained_setup_gap'] == [LEGACY_GAP]
            and value['regression']['pending_amended_cases'] == len({item['id'] for item in pending})
            and value['regression']['pending_amended_fields'] == len(pending), 'Regression inventory drift')
    partition = value['partition_checks']
    require(partition['cross_split_group_leaks'] == partition['cross_split_shape_leaks'] == partition['duplicate_inputs'] == 0,
            'Frozen partition leak')
    isolation = value['isolation']
    require(value['author_session_id'] == manifest['author_session_id'] == isolation['author_session_id']
            and isolation['implementation_session_must_be_new'] is True
            and isolation['implementation_must_not_read_author_logs'] is True, 'Author session isolation drift')
    if require_review:
        require(value['status']=='frozen' and value['review']['status']=='approved'
                and value['review']['reviewer']=='claude' and type(value['review']['approval_message']) is int
                and value['review']['approval_message']>432
                and all(item['status']=='approved' for item in manifest['reviews'].values())
                and setups['review']['status'] == 'approved'
                and value['review']['reviewed_holdout_count']==96 and value['isolation']['materials_moved_outside_repository'] is True,
                'Claude individual review and isolation required')
    return value
