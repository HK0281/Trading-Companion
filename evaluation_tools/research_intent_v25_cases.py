"""V2.5 authoring admission and context-only preflight; no target prediction API.

Holdout text lives outside tracked files and is read only via an explicit path
and byte-level SHA256. Existing v2 corpus/annotation readers remain unchanged.
"""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from math import ceil
from pathlib import Path
import re
import unicodedata

from trading_companion.intent_router_task import digest, read_json
from trading_companion.research_intent_evaluation_cases import ROOT, GOLD_FIELDS, require, valid_context, valid_row, load_corpus
from trading_companion.research_intent_v2_annotations import load_amendments, revised_rows
from trading_companion import research_intent_v2_evaluation as core
from trading_companion.typesafe_jev import decode

VERSION = 'research-intent-v2.5-cases-v1'
DEV = ROOT / 'data/evaluation/research-intent-v2.5-dev.jsonl'
SETUPS = ROOT / 'data/evaluation/research-intent-v2.5-setups.json'
AMENDMENTS = ROOT / 'data/evaluation/research-intent-v2.5-amendments.json'
LOCK = ROOT / 'data/evaluation/research-intent-v2.5-freeze.json'
RULE_COVERAGE = ROOT / 'data/evaluation/research-intent-v2.5-rule-coverage.json'
PUBLIC_FILES = (
    'data/evaluation/research-intent-v2.5-dev.jsonl',
    'data/evaluation/research-intent-v2.5-dev.manifest.json',
    'data/evaluation/research-intent-v2.5-setups.json',
    'data/evaluation/research-intent-v2.5-amendments.json',
    'data/evaluation/research-intent-v2.5-rule-coverage.json',
    'docs/research-intent-v2.5-plan.md', 'docs/research-intent-v2-contract.md',
    'evaluation_tools/__init__.py', 'evaluation_tools/research_intent_v25_cases.py',
    'evaluation_tools/preflight_research_intents_v25.py',
    'tests/test_research_intent_v25_cases.py',
    'data/evaluation/research-intent-v2.jsonl', 'data/evaluation/research-intent-v2.manifest.json',
    'data/evaluation/research-intent-v2-dev-amendments.json',
    'src/trading_companion/research_intent_evaluation_cases.py',
    'src/trading_companion/research_intent_v2_evaluation.py',
    'src/trading_companion/research_intent_evaluation_fixtures.py',
    'src/trading_companion/workflow_evaluation_fixtures.py',
)
COUNTS = {'dev': 78, 'holdout': 96}
DEV_ADDITIONS = {'D09': 1, 'D16': 2, 'D19': 1, 'D23': 1, 'D25': 1, 'H02': 1,
                 'H05': 1, 'H07': 1, 'H14': 1, 'H15': 4}
SPECIAL_COUNTS = {'H01': (2, 4), 'H02': (2, 4), 'H03': (4, 8), 'H05': (2, 4),
                  'H06': (4, 6), 'H10': (3, 6), 'H14': (2, 4), 'H15': (4, 6), 'H16': (2, 4),
                  'D01': (1, 3), 'D08': (1, 3), 'D09': (1, 3), 'D12': (1, 2),
                  'D16': (1, 2), 'D19': (1, 2), 'D20': (1, 2), 'D31': (1, 2)}
AXES = ('negation', 'history_command', 'technical_domain', 'ambiguous_identity', 'product_scope',
        'pending_validity', 'pending_conflict', 'same_subject', 'future_fact')
LABELS = ('positive', 'negative', 'boundary')
SETUP_KINDS = ('snapshot', 'company_saved', 'company_horizon', 'company_horizon_source',
               'company_horizon_mode', 'company_horizon_identity', 'conflict', 'index_horizon', 'index_saved')


def file_sha256(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def public_sha256(path):
    """Git public files have LF-canonical hashes; sealed files retain exact bytes."""
    return sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def valid_new_row(row):
    require(type(row) is dict and {'variant_group', 'coverage', 'setup_id'} <= row.keys(), 'Missing v2.5 metadata')
    legacy = deepcopy(row)
    for field in ('variant_group', 'coverage', 'setup_id'):
        legacy.pop(field)
    require(type(row.get('id')) is str and re.fullmatch(r'V25[DH][0-9]{3}', row['id']) is not None,
            'Invalid v2.5 case ID')
    require(row['id'][3] == ('D' if row['split'] == 'dev' else 'H'), 'Case ID/split mismatch')
    legacy['id'] = 'V2000'  # Reuse only the historical row schema, not its split/size contract.
    valid_row(legacy)
    require(row['fault'] is None and row['source']['kind'] == 'synthetic' and not row['probe']['steps'],
            'New language cases cannot inject response faults or target scripts')
    for key in ('variant_group', 'setup_id'):
        require(type(row[key]) is str and re.fullmatch(r'[a-zA-Z0-9_-]{3,100}', row[key]) is not None)
    require(type(row['coverage']) is dict and row['coverage'].keys() <= set(AXES)
            and all(value in LABELS for value in row['coverage'].values()), 'Invalid cross-cutting coverage')
    if row['context']['pending'] != 'none':
        require(all(row['context']['pending_binding'].values()), 'Session/turn binding is architecturally unmeasurable')


def load_cases(path=DEV, *, split='dev', expected_sha256=None):
    require(split in COUNTS, 'Invalid v2.5 split')
    require(split != 'holdout' or expected_sha256 is not None, 'Explicit holdout SHA256 is required')
    path = Path(path)
    require(split != 'holdout' or path.resolve() != DEV.resolve(), 'Explicit holdout file is required')
    require(0 < path.stat().st_size <= 2_000_000, 'Invalid v2.5 corpus size')
    raw = path.read_bytes()
    if expected_sha256 is not None:
        require(type(expected_sha256) is str and re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is not None
                and sha256(raw.replace(b'\r\n', b'\n') if split == 'dev' else raw).hexdigest() == expected_sha256,
                'V2.5 corpus fingerprint mismatch')
    require(len(raw.splitlines()) == COUNTS[split], 'V2.5 split count mismatch')
    rows = [decode(line) for line in raw.splitlines()]
    for row in rows:
        valid_new_row(row)
        require(row['split'] == split, 'Mixed v2.5 splits')
    require(len({r['id'] for r in rows}) == len(rows), 'Duplicate v2.5 ID')
    return rows


def load_dev_manifest(rows, path=None):
    value = read_json(path or DEV.with_suffix('.manifest.json'))
    require(type(value) is dict and value.keys() == {'version', 'author', 'author_session_id',
            'corpus_sha256', 'summary', 'review', 'reviews'} and value['version'] == VERSION
            and value['author'] == 'codex' and type(value['author_session_id']) is str
            and value['corpus_sha256'] == public_sha256(DEV) and value['summary'] == population_summary(rows),
            'Invalid public development manifest')
    require(type(value['reviews']) is dict and value['reviews'].keys() == {r['id'] for r in rows}, 'Missing individual review')
    for row in rows:
        review = value['reviews'][row['id']]
        require(type(review) is dict and review.keys() == {'row_sha256', 'status', 'reviewer', 'note', 'approval_message'}
                and review['row_sha256'] == digest(row) and review['status'] in ('pending', 'approved')
                and review['reviewer'] == ('claude' if review['status'] == 'approved' else None)
                and (type(review['approval_message']) is int and review['approval_message'] >= 392
                     if review['status'] == 'approved' else review['approval_message'] is None)
                and type(review['note']) is str and bool(review['note']), 'Invalid individual development review')
    approved = sum(r['status'] == 'approved' for r in value['reviews'].values())
    summary = value['review']
    require(summary['status'] == ('approved' if approved == len(rows) else 'partially_approved' if approved else 'pending')
            and summary['reviewed_count'] == approved and summary['pending_count'] == len(rows)-approved,
            'Development review summary mismatch')
    return value


def load_regression_amendments(path=AMENDMENTS):
    """Layer multi-field #390 changes over #366, without loosening the old reader."""
    rows, manifest = load_corpus()
    previous = load_amendments(rows, manifest['corpus_sha256'])
    effective = revised_rows(rows, previous)
    value = read_json(path)
    require(type(value) is dict and value.keys() == {'version', 'parent_corpus_sha256',
            'previous_amendments_sha256', 'changes'} and value['version'] == VERSION
            and value['parent_corpus_sha256'] == manifest['corpus_sha256']
            and value['previous_amendments_sha256'] == public_sha256(ROOT / 'data/evaluation/research-intent-v2-dev-amendments.json')
            and type(value['changes']) is list, 'Invalid v2.5 amendment parent/version')
    original = {r['id']: r for r in rows}
    by_id = {r['id']: deepcopy(r) for r in effective}
    seen = set()
    for change in value['changes']:
        require(type(change) is dict and change.keys() == {'id', 'parent_row_sha256', 'field', 'before',
                'after', 'reason', 'reviewer', 'status', 'approval_message'}, 'Invalid amendment schema')
        identifier, field = change['id'], change['field']
        require(identifier in original and field in GOLD_FIELDS and (identifier, field) not in seen
                and change['parent_row_sha256'] == digest(original[identifier])
                and change['reviewer'] == 'claude' and change['status'] == 'approved'
                and change['approval_message'] == 390 and type(change['reason']) is str and bool(change['reason'])
                and by_id[identifier]['gold'][field] == change['before'] and change['before'] != change['after'],
                'Unapproved, duplicate or mismatched v2.5 amendment')
        by_id[identifier]['gold'][field] = deepcopy(change['after'])
        valid_row(by_id[identifier])
        seen.add((identifier, field))
    require(len(seen) == 39 and len({i for i, _ in seen}) == 24, 'Approved #390 amendment inventory mismatch')
    return effective, value, [by_id[r['id']] for r in rows]


def load_setups(path=SETUPS):
    value = read_json(path)
    require(type(value) is dict and value.keys() == {'version', 'review', 'entries'}
            and value['version'] == VERSION and type(value['entries']) is dict, 'Invalid v2.5 setups')
    for key, entry in value['entries'].items():
        require(type(key) is str and type(entry) is dict and entry.keys() == {'kind', 'history', 'note'}
                and entry['kind'] in SETUP_KINDS and type(entry['history']) is list
                and all(type(s) is str and s for s in entry['history'])
                and type(entry['note']) is str and entry['note'], 'Invalid neutral setup')
    return value


def state_input(row):
    """Construction input contains no gold, evidence, ID, split or variant labels."""
    return deepcopy({k: row[k] for k in ('question', 'context', 'setup_id')})


def _index_context(value, entry):
    from trading_companion.index_agent import run_index_research_turn
    from trading_companion.index_demo import index_demo_response
    from trading_companion.index_source import synthetic_index_bars
    from trading_companion.research_intent_v2 import IntentContext
    from trading_companion.research_session import ResearchSession
    from trading_companion.security_directory import SecurityDirectory
    from trading_companion.workflow_evaluation_fixtures import SECURITIES, NOW, REFERENCE_DATE

    class FixedIndexModel:
        calls = 0

        def complete(self, messages, *, tools):
            self.calls += 1
            response = index_demo_response(messages, tools)
            if response is None:
                raise ValueError('Unexpected fixed index setup call')
            return response

    class FixedIndexSource:
        def __init__(self, request):
            self.request = request

        def read_daily_bars(self):
            return synthetic_index_bars(self.request)

    pending_seed = entry['kind'] == 'index_horizon'
    require(value['source'] == 'tencent' and not value['development'] and value['company_enabled']
            and value['workflow_enabled'] and value['conversation_memory'] and not value['shortlist']
            and value['pending'] == ('index_horizon' if pending_seed else 'none')
            and not value['has_company'] and not value['has_technical']
            and value['has_index'] == (not pending_seed), 'Index setup descriptor mismatch')
    session, model = ResearchSession(), FixedIndexModel()
    directory = SecurityDirectory(SECURITIES, 'v25-neutral-index-setup', NOW, False)
    for question in entry['history']:
        result = run_index_research_turn(question, model=model, directory=directory, session=session,
            now=NOW, reference_date=REFERENCE_DATE, source_factory=FixedIndexSource)
        require(result.status in ('needs_clarification', 'completed', 'limited'), 'Index seed did not build')
    pending = session.index_pending
    if entry['kind'] == 'index_horizon':
        require(pending is not None and pending.stage == 'horizon' and session.latest_research is None,
                'Index seed did not create a native horizon pending')
        require(value['latest'] is None and all(value['pending_binding'].values()), 'Index pending descriptor mismatch')
        session.index_pending = replace(pending, created_at=NOW-timedelta(seconds=value['pending_age_seconds']))
    else:
        require(session.index_saved is not None and session.latest_research is not None, 'Index seed did not publish')
        latest = session.latest_research
        actual = dict(kind=latest.kind, subjects=[latest.scope.instrument_id], age_seconds=0,
                      source=latest.source, development=latest.development, current=session._subject_current,
                      horizon=latest.scope.horizon)
        require(value['latest'] == actual, 'Index publication descriptor mismatch')
    return IntentContext(session, directory, NOW, REFERENCE_DATE, value['source'], value['development'],
        tuple(value['history']), None, value['company_enabled'], value['workflow_enabled'],
        value['conversation_memory']), model.calls


def build_context(state, setups, prefixes):
    require(type(state) is dict and state.keys() == {'question', 'context', 'setup_id'}, 'Construction input schema')
    require(state['setup_id'] in setups['entries'], 'Unknown neutral setup')
    entry, value = setups['entries'][state['setup_id']], state['context']
    valid_context(value)
    require(value['history'] == entry['history'], 'Neutral setup/history mismatch')
    kind = entry['kind']
    if kind.startswith('index_'):
        return _index_context(value, entry)
    seed = deepcopy(value)
    if kind.startswith('company_horizon') or kind == 'conflict':
        require(value['pending'] == ('conflict' if kind == 'conflict' else 'company_horizon'), 'Pending kind mismatch')
        require(value['source'] == ('akshare_eastmoney' if kind == 'company_horizon_source' else 'tencent')
                and value['development'] == (kind == 'company_horizon_mode'), 'Pending mutation descriptor mismatch')
        seed.update(source='tencent', development=False)
    # The target question is never executed, interpreted, or used to select a seed.
    context, calls = core.program_context(dict(question='仅构建程序状态，不执行目标轮', context=seed), prefixes)
    if kind == 'company_saved':
        require(context.session.product_saved is not None, 'Saved setup did not build a native cache')
    context = replace(context, source=value['source'], development=value['development'])
    if kind == 'company_horizon_source':
        require(value['source'] != 'tencent', 'Source fault must switch the current source')
    elif kind == 'company_horizon_mode':
        require(value['development'] is True, 'Mode fault must switch the current mode')
    elif kind == 'company_horizon_identity':
        from trading_companion.security_directory import SecurityDirectory, Security
        securities = tuple(Security(s.instrument_id, s.name + '（目录已更新）', s.aliases)
                           if s.instrument_id == '000001.SZ' else s for s in context.directory.securities)
        context = replace(context, directory=SecurityDirectory(securities, 'v25-directory-switch', context.now, False))
    return context, calls


def question_shape(row):
    text = unicodedata.normalize('NFKC', row['question'])
    # Detect name/date/parameter swaps; state combinations still need human group review.
    spans = [(part, key) for key in ('mentions', 'horizon', 'dates', 'indicators') for part in row['evidence'][key]]
    for part, key in sorted(spans, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(unicodedata.normalize('NFKC', part), '{' + key + '}')
    return ''.join(c.lower() for c in text if c.isalnum() or c in '{}')


def validate_partition(dev, holdout, legacy):
    require(len(dev) == COUNTS['dev'] and len(holdout) == COUNTS['holdout'], 'Expected 78/96 new cases')
    require(len({r['id'] for r in [*dev, *holdout]}) == sum(COUNTS.values()), 'Duplicate v2.5 ID')
    families = {r['family_id'] for r in legacy}
    groups, shapes, inputs = defaultdict(set), defaultdict(set), set()
    old_inputs = {digest(dict(question=r['question'], context=r['context'])) for r in legacy}
    for row in [*dev, *holdout]:
        valid_new_row(row)
        require(row['family_id'] in families, 'Unknown ability family')
        groups[row['variant_group']].add(row['split'])
        shapes[question_shape(row)].add(row['split'])
        key = digest(dict(question=row['question'], context=row['context']))
        require(key not in inputs and key not in old_inputs, 'Duplicate old/new evaluation input')
        inputs.add(key)
    require(all(len(s) == 1 for s in groups.values()), 'Variant group leaks across splits')
    require(all(len(s) == 1 for s in shapes.values()), 'Subject/date/indicator-swap question leaks across splits')
    for split, rows, index in (('dev', dev, 0), ('holdout', holdout, 1)):
        counts = Counter(r['family_id'] for r in rows)
        require(set(counts) == families and all(counts[f] == SPECIAL_COUNTS.get(f[:3], (1, 1))[index]
                                              + (DEV_ADDITIONS.get(f[:3], 0) if split == 'dev' else 0)
                                              for f in families), 'Family allocation mismatch: ' + split)
        for axis in AXES:
            require({r['coverage'][axis] for r in rows if axis in r['coverage']} == set(LABELS),
                    'Missing positive/negative/boundary coverage: ' + split + '/' + axis)
    return dict(variant_groups=len(groups), normalized_shapes=len(shapes), cross_split_group_leaks=0,
                cross_split_shape_leaks=0, duplicate_inputs=0,
                note='机械检查只拦相同组和替换式重复；跨题语义近似仍须Claude逐题复核。')


def population_summary(rows):
    semantic = sum(not r['gold']['zero_intent_calls'] for r in rows)
    return dict(count=len(rows), families=dict(sorted(Counter(r['family_id'] for r in rows).items())),
        tasks=dict(sorted(Counter(r['gold']['task'] for r in rows).items())),
        outcomes=dict(sorted(Counter(r['gold']['outcome'] for r in rows).items())),
        semantic_calls=semantic, local_zero_calls=len(rows)-semantic,
        coverage={axis: dict(sorted(Counter(r['coverage'][axis] for r in rows if axis in r['coverage']).items()))
                  for axis in AXES})


RULE_DESCRIPTIONS = {
    'benchmark_default': '大盘默认上证指数', 'index_explicit_technical': '指数明确技术领域保留',
    'direction_without_prices': '方向评级与价位授权分开', 'purchase_value_prices': '购买价值默认包含参考价位',
    'veto_preserves_direction': '计划否决保留未来方向', 'plan_conflict': '肯定价位与否决计划冲突',
    'empty_strict_domain_conflict': '严格领域全被否定须澄清', 'strict_time_only_false': '仅限时间不设严格领域',
    'strict_negated_only_false': '被否定的唯一范围不设严格范围', 'daily_weekly_not_indicator': '日周K线是领域而非指标',
    'profile_explicit_situation': '公司或业务情况明确为概况', 'situation_unspecified_domain': '泛称某公司情况不限定领域',
    'ambiguous_stock_identity': '歧义股票名保留任务并澄清身份', 'unknown_stock_identity': '未知股票名保留任务并澄清身份',
    'uncertain_index_identity': '不明确指数身份须澄清', 'unsupported_product': '具体不支持产品研究拒绝',
    'product_concept': '产品概念解释可以回答', 'product_background': '产品只作股票背景不误拒绝',
    'known_index_local_reject': '已知不支持指数本地拒绝', 'explicit_comparison': '多个明确对象可比较',
    'same_single_subject': '最近唯一同一对象可指代', 'ordinal_selection': '序号在当前对象或名单中核验',
    'ambiguous_previous_subject': '最近多对象单数指代不得猜选', 'consumed_subject': '已消耗最近身份不从旧缓存回流',
    'future_publication': '未来发布时间不得授权绑定', 'source_mismatch': '来源改变阻断旧身份',
    'mode_mismatch': '模式改变阻断旧身份', 'memory_off_no_binding': '关闭记忆不沿用对象',
    'old_horizon_not_inherited': '旧研究期限不能静默继承', 'knowledge_interlude': '知识插曲不改变最近身份',
    'history_saved_stock': '历史命令引用合法已存股票结果', 'history_saved_index': '历史命令引用合法已存指数结果',
    'history_without_result': '无保存结果的历史命令仍本地处理', 'history_before_conflict': '历史命令优先于待办冲突',
    'command_before_pending': '取消或清空命令优先于待办', 'explain_local': '已有整句解释语法本地识别',
    'explain_semantic': '其他解释使用一次语义理解', 'screening_update': '修改既有筛选条件',
    'screening_refresh': '按旧条件刷新筛选', 'stock_horizon_local_valid': '有效公司期限待办零调用续接',
    'stock_horizon_local_invalid': '失效公司期限待办零调用澄清并清除授权',
    'index_horizon_local_valid': '有效指数期限待办零调用续接',
    'index_horizon_local_invalid': '失效或到期边界的指数期限待办本地澄清',
    'pending_conflict_local': '普通冲突待办本地澄清', 'conflict_independent_new': '完整新任务绕过待办冲突',
    'expired_independent_new': '完整新任务绕过过期待办', 'pending_directory_changed': '目录变化使待办身份失效',
    'pending_mode_changed': '模式变化使待办失效', 'pending_source_changed': '来源变化使待办失效',
    'pending_clock_rollback': '时钟回拨使待办失效', 'pending_suffix_semantic': '待办回复附句必须理解完整条件',
    'future_actual_unavailable': '未来实际事实不得伪装成条件情景', 'date_unexecutable': '非法或未完成历史窗口保留原文并澄清',
    'history_future_separate': '历史窗口与未来期限分开', 'relative_this_window': '这N天周月为相对历史窗口',
    'horizon_interval': '未来期限区间按统一中点口径', 'horizon_named_band': '短中长档映射代表期限',
    'horizon_invalid': '非法量化未来期限不得当省略', 'missing_horizon_for_prices': '已有对象要价位先确认期限',
    'missing_subject_before_horizon': '缺对象优先澄清对象', 'unsupported_indicator': '不支持指标保留原文并澄清',
    'parameterized_indicator': '明确指标参数完整保留',
}


def rule_labels(row):
    """Authoring audit taxonomy, not predictions: field rules plus public semantic branches."""
    from trading_companion.indicator_knowledge import topic_key
    from trading_companion.market_scope import _UNSUPPORTED
    from trading_companion.workflow_evaluation_fixtures import SECURITIES
    from trading_companion.security_directory import SecurityDirectory
    gold, context, question = row['gold'], row['context'], row['question']
    labels = {f'{key}:{gold[key]}' for key in ('task', 'operation', 'objective_kind', 'subject_source', 'outcome')}
    for key in ('wants_plan', 'strict_scope', 'zero_intent_calls'):
        labels.add(f'{key}:{str(gold[key]).lower()}')
    labels.add('binding:' + ('multiple' if len(gold['bound_subjects']) > 1 else 'single' if gold['bound_subjects'] else 'none'))
    for key in ('horizon', 'date_expression', 'indicator_mentions', 'exclusions', 'research_kinds'):
        labels.add(f'{key}:' + ('present' if gold[key] else 'absent'))
    labels.update('research_kind:' + kind for kind in gold['research_kinds'])
    labels.update('exclusion:' + kind for kind in gold['exclusions'])
    indicators = [(mention, topic_key(mention)) for mention in gold['indicator_mentions']]
    labels.update('indicator:' + kind for _, kind in indicators if kind is not None)
    latest, pending = context['latest'], context['pending']
    domains = {'profile', 'financial', 'technical', 'news', 'announcements', 'risk'}
    product = bool(re.search(r'ETF|基金|期货|期权', question, re.I))
    directory = SecurityDirectory(SECURITIES, 'v25-rule-coverage-only', core.NOW, False)
    mentions = [directory.lookup(m) for m in row['evidence']['mentions']]
    checks = {
        'benchmark_default': gold['task'] == 'index_research' and '大盘' in question,
        'index_explicit_technical': gold['task'] == 'index_research' and 'technical' in gold['research_kinds'],
        'direction_without_prices': gold['objective_kind'] == 'decision' and not gold['wants_plan'],
        'purchase_value_prices': gold['wants_plan'] and bool(re.search(r'值|划算|贵不贵', question)),
        'veto_preserves_direction': gold['objective_kind'] == 'decision' and 'plan' in gold['exclusions'],
        'plan_conflict': gold['outcome'] == 'clarify_conflict' and 'plan' in gold['exclusions'],
        'empty_strict_domain_conflict': gold['strict_scope'] and gold['outcome'] == 'clarify_conflict'
            and not gold['research_kinds'] and bool(set(gold['exclusions']) & domains),
        'strict_time_only_false': not gold['strict_scope'] and bool(re.search(r'(?:只|仅)(?:看|用|分析|研究)', question))
            and (gold['date_expression'] is not None or '历史' in question) and not gold['research_kinds'],
        'strict_negated_only_false': not gold['strict_scope'] and bool(re.search(r'(?:不要|不能|别|不必)(?:再)?(?:只|仅)(?:看|用|分析|研究)', question)),
        'daily_weekly_not_indicator': 'technical' in gold['research_kinds'] and bool(re.search(r'日线|周线|K线', question, re.I)),
        'profile_explicit_situation': 'profile' in gold['research_kinds'] and bool(re.search(r'公司情况|业务情况|主营业务', question)),
        'situation_unspecified_domain': gold['task'] == 'stock_research' and not gold['research_kinds'] and '情况' in question,
        'ambiguous_stock_identity': gold['task'] == 'stock_research' and gold['outcome'] == 'clarify_identity'
            and any(m.status == 'ambiguous' for m in mentions),
        'unknown_stock_identity': gold['task'] == 'stock_research' and gold['outcome'] == 'clarify_identity'
            and bool(mentions) and all(m.status not in ('resolved', 'ambiguous') for m in mentions),
        'uncertain_index_identity': gold['task'] == 'index_research' and gold['outcome'] == 'clarify_identity',
        'unsupported_product': gold['task'] == 'unsupported' and product and not gold['zero_intent_calls'],
        'product_concept': gold['task'] == 'knowledge' and product,
        'product_background': gold['task'] == 'stock_research' and product,
        'known_index_local_reject': gold['task'] == 'unsupported' and gold['zero_intent_calls'] and bool(_UNSUPPORTED.search(question)),
        'explicit_comparison': gold['subject_source'] == 'question' and len(gold['bound_subjects']) > 1,
        'same_single_subject': gold['subject_source'] == 'previous' and len(gold['bound_subjects']) == 1
            and bool(re.search(r'同一只|同一家', question)),
        'ordinal_selection': gold['subject_source'] == 'previous' and bool(re.search(r'第[0-9一二三四五六七八九十]+(?:只|家|个)', question)),
        'ambiguous_previous_subject': gold['outcome'] == 'clarify_subject' and latest is not None and len(latest['subjects']) > 1,
        'consumed_subject': latest is not None and not latest['current'],
        'future_publication': latest is not None and latest['age_seconds'] < 0,
        'source_mismatch': latest is not None and latest['source'] != context['source'],
        'mode_mismatch': latest is not None and latest['development'] != context['development'],
        'memory_off_no_binding': not context['conversation_memory'] and not gold['bound_subjects'] and gold['subject_source'] == 'previous',
        'old_horizon_not_inherited': latest is not None and latest['age_seconds'] >= 900
            and gold['wants_plan'] and gold['horizon'] is None,
        'knowledge_interlude': latest is not None and any(re.search(r'是什么|解释|定义', q) for q in context['history']),
        'history_saved_stock': gold['task'] == 'history' and gold['subject_source'] == 'previous' and latest is not None and latest['kind'] != 'index',
        'history_saved_index': gold['task'] == 'history' and gold['subject_source'] == 'previous' and latest is not None and latest['kind'] == 'index',
        'history_without_result': gold['task'] == 'history' and not gold['bound_subjects'],
        'history_before_conflict': gold['task'] == 'history' and pending == 'conflict',
        'command_before_pending': gold['task'] == 'command' and pending != 'none',
        'explain_local': gold['operation'] == 'explain' and gold['zero_intent_calls'],
        'explain_semantic': gold['operation'] == 'explain' and not gold['zero_intent_calls'],
        'screening_update': gold['task'] == 'screening' and gold['operation'] == 'update',
        'screening_refresh': gold['task'] == 'screening' and gold['operation'] == 'refresh',
        'stock_horizon_local_valid': pending == 'company_horizon' and gold['zero_intent_calls'] and gold['outcome'] == 'research',
        'stock_horizon_local_invalid': pending == 'company_horizon' and gold['zero_intent_calls'] and gold['outcome'] == 'clarify_conditions',
        'index_horizon_local_valid': pending == 'index_horizon' and gold['zero_intent_calls'] and gold['outcome'] == 'research',
        'index_horizon_local_invalid': pending == 'index_horizon' and gold['zero_intent_calls'] and gold['outcome'] == 'clarify_conditions',
        'pending_conflict_local': pending == 'conflict' and gold['zero_intent_calls'] and gold['outcome'] == 'clarify_conflict',
        'conflict_independent_new': pending == 'conflict' and gold['subject_source'] == 'question' and gold['outcome'] == 'research',
        'expired_independent_new': pending != 'none' and context['pending_age_seconds'] >= 900
            and gold['subject_source'] == 'question' and gold['outcome'] == 'research',
        'pending_directory_changed': 'identity' in row.get('setup_id', '') and pending == 'company_horizon' and gold['outcome'] == 'clarify_conditions',
        'pending_mode_changed': pending != 'none' and context['development'] and gold['outcome'] == 'clarify_conditions',
        'pending_source_changed': pending != 'none' and context['source'] != 'tencent' and gold['outcome'] == 'clarify_conditions',
        'pending_clock_rollback': pending != 'none' and context['pending_age_seconds'] < 0,
        'pending_suffix_semantic': pending != 'none' and gold['operation'] == 'update' and not gold['zero_intent_calls'],
        'future_actual_unavailable': gold['outcome'] == 'clarify_conditions' and bool(re.search(r'实际|已实现', question)),
        'date_unexecutable': gold['date_expression'] is not None and gold['outcome'] == 'clarify_conditions',
        'history_future_separate': gold['date_expression'] is not None and gold['horizon'] is not None,
        'relative_this_window': bool(gold['date_expression'] and gold['date_expression'].startswith('这')),
        'horizon_interval': any(re.search(r'到|至|~|～|-', value) for value in row['evidence']['horizon']),
        'horizon_named_band': any(re.search(r'[短中长][线期]', value) for value in row['evidence']['horizon']),
        'horizon_invalid': gold['objective_kind'] == 'decision' and gold['horizon'] is None and gold['outcome'] == 'clarify_conditions',
        'missing_horizon_for_prices': gold['wants_plan'] and gold['outcome'] == 'clarify_horizon',
        'missing_subject_before_horizon': gold['wants_plan'] and gold['outcome'] == 'clarify_subject',
        'unsupported_indicator': any(kind is None for _, kind in indicators),
        'parameterized_indicator': any(re.search(r'\d', mention) for mention, _ in indicators),
    }
    labels.update(name for name, present in checks.items() if present)
    return labels


def rule_description(label):
    if label in RULE_DESCRIPTIONS:
        return RULE_DESCRIPTIONS[label]
    field, value = label.split(':', 1)
    names = dict(task='任务', operation='操作', objective_kind='目标', subject_source='对象来源', outcome='预计动作',
        wants_plan='价位授权', strict_scope='严格领域或指标范围', zero_intent_calls='零理解调用', binding='对象绑定数量',
        horizon='未来期限', date_expression='历史日期原文', indicator_mentions='正向指标', exclusions='排除项',
        research_kinds='明确研究领域', research_kind='明确领域', exclusion='明确排除', indicator='明确支持指标')
    return names[field] + '：' + value


def build_rule_coverage(dev, holdout, regression):
    """Author-only call: returns public examples and a separately sealed per-case map."""
    required = set().union(*(rule_labels(row) for row in holdout))
    # Structurally unmeasurable legacy descriptions cannot stand in as public witnesses.
    public = [*dev, *(r for r in regression if r['id'] not in ('V2002', 'V2222', 'V2223'))]
    labelled = [(r['id'], rule_labels(r)) for r in public]
    rules = {label: dict(description=rule_description(label),
                        public_examples=[identifier for identifier, labels in labelled if label in labels][:3])
             for label in sorted(required)}
    missing = [label for label, item in rules.items() if not item['public_examples']]
    require(not missing, 'Missing public rule witnesses: ' + ', '.join(missing))
    report = dict(version=VERSION, rule_count=len(rules), holdout_count=96, uncovered_rules=0, rules=rules)
    private = {r['id']: dict(row_sha256=digest(r), labels=sorted(rule_labels(r))) for r in holdout}
    return report, private


def load_rule_coverage(dev, path=RULE_COVERAGE):
    value = read_json(path)
    require(type(value) is dict and value.keys() == {'version', 'rule_count', 'holdout_count', 'uncovered_rules', 'rules'}
            and value['version'] == VERSION and value['rule_count'] == len(value['rules'])
            and value['holdout_count'] == 96 and value['uncovered_rules'] == 0, 'Invalid public rule coverage')
    _, _, regression = load_regression_amendments()
    public = {r['id']: r for r in [*dev, *regression] if r['id'] not in ('V2002', 'V2222', 'V2223')}
    for label, item in value['rules'].items():
        require(type(item) is dict and item.keys() == {'description', 'public_examples'}
                and item['description'] == rule_description(label)
                and type(item['public_examples']) is list and 1 <= len(item['public_examples']) <= 3
                and len(set(item['public_examples'])) == len(item['public_examples'])
                and all(identifier in public and label in rule_labels(public[identifier]) for identifier in item['public_examples']),
                'Missing or mismatched public rule witness: ' + label)
    return value


def thresholds(summary):
    return dict(all=dict(n=96, minimum_correct=92), implementation_unseen=dict(n=96, minimum_fraction=.95),
        semantic=dict(n=summary['semantic_calls'], minimum_correct=ceil(.95*summary['semantic_calls'])),
        families={family: dict(n=n, minimum_correct=ceil(.8*n)) for family, n in summary['families'].items() if n >= 4},
        small_family_rule='题数少于4的族只计入总分，逐族仍报告',
        common_fields_not_worse=True, measurable_red_line_violations=0,
        failed_unmeasured_invalid_in_denominator=True, maximum_target_attempts=96,
        output_views=['raw_original', 'normalized_original', 'normalized_approved'])


def preflight(rows, setups, prefixes):
    from scripts.verify import clean_environment, offline_network
    from unittest.mock import patch
    records = []
    with clean_environment(), offline_network(), \
            patch('trading_companion.local_env.load_model_environment', side_effect=AssertionError('No credentials')), \
            patch.object(core, 'predict', side_effect=AssertionError('No target prediction')), \
            patch.object(core, 'understand', side_effect=AssertionError('No target understanding')), \
            patch.object(core, 'local_preflight', side_effect=AssertionError('No target interpretation')), \
            patch.object(core, 'metrics', side_effect=AssertionError('No target scoring')):
        for row in rows:
            state = state_input(row)
            state['question'] = '仅检查前置状态，不执行目标轮'
            context, calls = build_context(state, setups, prefixes)
            records.append(dict(id=row['id'], context_sha256=digest(context.model_context()),
                descriptor_sha256=digest(state['context']), construction=setups['entries'][row['setup_id']]['kind'],
                setup_calls=calls, setup=core.setup_metadata(context)))
    return dict(count=len(rows), buildable=len(records), target_calls=0, prediction_rows=0,
                score_rows=0, passed=len(rows) == len(records), rows=records)


def validate_freeze(lock_path=LOCK, *, require_review=False):
    lock = read_json(lock_path)
    require(lock['version'] == VERSION and lock['status'] in ('awaiting_claude_review', 'frozen'), 'Invalid freeze status')
    require(set(lock['public_files_sha256']) == set(PUBLIC_FILES), 'Frozen public file inventory drift')
    for name, fingerprint in lock['public_files_sha256'].items():
        require(public_sha256(ROOT / name) == fingerprint, 'Frozen public file drift: ' + name)
    dev = load_cases(expected_sha256=lock['dev_sha256'])
    manifest = load_dev_manifest(dev)
    approved_dev = sum(item['status'] == 'approved' for item in manifest['reviews'].values())
    require(lock['review']['reviewed_dev_count'] == approved_dev
            and lock['review']['pending_dev_count'] == len(dev)-approved_dev,
            'Frozen development review counts mismatch')
    coverage = load_rule_coverage(dev)
    require(lock['rule_coverage'] == dict(rule_count=coverage['rule_count'], uncovered_rules=0,
            public_examples_only=True), 'Frozen rule coverage drift')
    summary = lock['holdout']['summary']
    original, _ = load_corpus()
    families = {r['family_id'] for r in original}
    require(summary['count'] == 96 and set(summary['families']) == families
            and all(summary['families'][f] == SPECIAL_COUNTS.get(f[:3], (1, 1))[1] for f in families)
            and type(summary['semantic_calls']) is int and 1 <= summary['semantic_calls'] <= 96
            and summary['local_zero_calls'] + summary['semantic_calls'] == 96
            and sum(summary['tasks'].values()) == sum(summary['outcomes'].values()) == 96,
            'Frozen holdout population drift')
    require(lock['thresholds'] == thresholds(lock['holdout']['summary']), 'Frozen threshold drift')
    if require_review:
        review = lock['review']
        pending = sum(item['status'] == 'pending' for item in manifest['reviews'].values())
        require(not pending, f'Claude review required: {pending} pending development cases')
        require(lock['status'] == 'frozen' and review['reviewer'] == 'claude'
                and review['status'] == 'approved' and type(review['approval_message']) is int
                and review['approval_message'] > 390 and review['reviewed_holdout_count'] == 96
                and manifest['review']['status'] == 'approved'
                and all(r['status'] == 'approved' for r in manifest['reviews'].values())
                and load_setups()['review']['status'] == 'approved',
                'Claude individual review and sealing are required')
    return lock
