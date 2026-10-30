"""Compare claim validation on saved drafts, without model calls or new data.

Example: --dataset tech records.jsonl report.json --baseline-ref b5d77eb --output DIR
Artifacts contain private draft text; use an ignored local output directory.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = r'(?:sma|ema|rsi|macd|boll|atr|adx|obv|volume|vwma)\([^()]*\)'
DAILY = re.compile(r'[^|]+\|(?P<instrument>[^|]+)\|(?P<date>\d{4}-\d{2}-\d{2})\|(?P<spec>'+SPEC+r')\|(?P<field>[^|]+)$')
WEEKLY = re.compile(r'\|weekly:(?P<spec>'+SPEC+r'):(?P<field>[^:]+)$')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def context_matches(public, context):
    for row in context['evidence']:
        if not re.fullmatch(r'E[1-9]\d*', row['id']):
            return False
        index = int(row['id'][1:])-1
        if index >= len(public):
            return False
        if any(row[key] != public[index].get(key) for key in
               ('instrument_id', 'kind', 'label', 'date', 'raw_value', 'unit', 'temporal_scope') if key in row):
            return False
    return True


def sidecar(public, context):
    """Use structured public identities, proving every supplied E-index first.

    Public report facts prove executed parameter identities, not raw-bar provenance.
    Only local RSI relations may borrow a nonsupplied public fact. Other readings
    still require a row in the original model context.
    """
    if not context_matches(public, context):
        raise ValueError('report_evidence_does_not_match_model_context')
    supplied = {public[int(r['id'][1:])-1]['id']: r for r in context['evidence']}
    output = {}
    for security in context['securities']:
        instrument = security['instrument_id']
        facts = [e for e in public if e['instrument_id'] == instrument and e.get('kind') == 'technical']
        confirmed = sorted({m['spec'] for e in facts if (m := DAILY.fullmatch(e['id']))
                            and m['instrument'] == instrument and m['date'] == e.get('date')})
        latest = {period: max((e.get('date') or '' for e in facts if
                   (DAILY.fullmatch(e['id']) if period == 'daily' else WEEKLY.search(e['id']))), default='')
                  for period in ('daily', 'weekly')}
        values, local = [], []
        for fact in facts:
            match = DAILY.fullmatch(fact['id'])
            period = 'daily'
            if not match:
                match, period = WEEKLY.search(fact['id']), 'weekly'
                if match and fact.get('temporal_scope') != 'completed_week_derived':
                    continue
            if match and match['spec'] in confirmed and type(fact.get('raw_value')) in (int, float):
                value = dict(indicator_id=match['spec'], timeframe=period, field=match['field'],
                             value=str(fact['raw_value']), date=fact['date'], ref=fact['id'])
                if (match['spec'].startswith('rsi(') and match['field'] == 'value'
                        and fact.get('unit') == 'index_points' and fact['date'] == latest[period]
                        and context['start_date'] <= fact['date'] <= context['daily_cutoff']):
                    local.append(dict(value, instrument_id=instrument, source='local_computed_rsi'))
                if fact['id'] in supplied:
                    values.append(dict(value, ref=supplied[fact['id']]['id']))
            row = supplied.get(fact['id'])
            mean = re.fullmatch(r'[^|]+\|price_context:ma(20|60)', fact['id'])
            alignment = re.fullmatch(r'日周方向核对 · ('+SPEC+r')', fact.get('label', ''))
            if row and mean and row.get('unit') == 'CNY/share' and row.get('temporal_scope') == 'daily_price_context':
                values.append(dict(indicator_id=f'sma(window={mean[1]})', timeframe='daily', field='value',
                    value=str(row['raw_value']), date=row['date'], ref=row['id'], source='price_context'))
            if (row and alignment and alignment[1] in confirmed and fact['id'].endswith('|timeframes:alignment')
                    and row.get('temporal_scope') == 'cross_timeframe_derived'
                    and row.get('raw_value') in ('aligned_above', 'aligned_below')):
                values.append(dict(indicator_id=alignment[1], timeframe='cross_timeframe', field='alignment',
                    value=row['raw_value'], date=row['date'], ref=row['id']))
        output[instrument] = dict(confirmed=confirmed, values=values, local_rsi=local)
    return output


def claims(node, path, section):
    """Yield each structured text and title once; numbers stay attached to text."""
    if isinstance(node, list):
        for index, value in enumerate(node):
            yield from claims(value, f'{path}[{index}]', section)
    elif isinstance(node, dict):
        if isinstance(node.get('text'), str):
            yield path+'.text', {key: node[key] for key in ('text', 'numbers', 'evidence_ids') if key in node}, section
        if isinstance(node.get('title'), str):
            yield path+'.title', dict(text=node['title']), section
        for key, value in node.items():
            if key not in ('text', 'title', 'numbers', 'evidence_ids') and isinstance(value, (dict, list)):
                yield from claims(value, path+'.'+key, section if section == 'decision' else key)


def make_jobs(label, records, report):
    candidates = defaultdict(list)
    for case in report['cases']:
        turns = case.get('turns', []) or [*case.get('setup', []), dict(case, phase='target')]
        for turn in turns:
            final = turn.get('company_research') or {}
            if final.get('evidence'):
                candidates[(case['id'], turn.get('phase', 'setup'))].append(final['evidence'])
    current, occurrence, jobs, gaps = {}, Counter(), [], []
    for event_index, event in enumerate(records):
        key = event.get('case_id'), event.get('phase'), event.get('turn')
        if event['kind'] == 'model_context':
            occurrence[key] += 1
            context = deepcopy(event['context'])
            public = next((p for p in candidates[key[:2]] if context_matches(p, context)), None)
            if public is not None:
                context['_technical_validation'] = sidecar(public, context)
            current[key] = context, occurrence[key], public is not None
        if event['kind'] != 'model_draft':
            continue
        identity = dict(dataset=label, case_id=key[0], phase=key[1], turn=key[2],
                        context_occurrence=occurrence[key], draft=event.get('draft'), event_index=event_index)
        if key not in current:
            gaps.append(dict(identity, reason='replay_context_unavailable')); continue
        context, count, known = current[key]
        if not known:
            gaps.append(dict(identity, reason='replay_context_unavailable')); continue
        try:
            draft = json.loads(event.get('text', ''))
        except (ValueError, TypeError):
            gaps.append(dict(identity, reason='draft_not_json')); continue
        if not isinstance(draft, dict):
            gaps.append(dict(identity, reason='draft_not_object')); continue
        for group_key in ('narratives', 'decisions'):
            for index, group in enumerate(draft.get(group_key, [])):
                if not isinstance(group, dict):
                    continue
                for path, item, section in claims(group, f'{group_key}[{index}]', 'decision' if group_key == 'decisions' else 'summary'):
                    jobs.append(dict(identity, path=path, item=item, instrument=group.get('instrument_id'),
                                     section=section, context=context))
    return jobs, gaps


def worker(source, jobs_path, output):
    sys.path.insert(0, str(Path(source).resolve()/'src'))
    from trading_companion.answer_validation import check_claim
    rows = []
    for job in json.loads(Path(jobs_path).read_text(encoding='utf-8')):
        stats = {}
        result = check_claim(job['item'], instrument=job['instrument'], context=job['context'],
                             section=job['section'], index=0, path=job['path'], stats=stats)
        rows.append(dict(text=result[0], evidence_ids=result[1], codes=[i.code for i in result[2]],
                         generated=result[3], retained=result[4], stats=stats,
                         rejected_sentences=[dict(index=i.index, code=i.code, text=i.text) for i in result[2]]))
    write_json(output, rows)


def category(old, new):
    if old['text'] and not new['text']:
        return 'new_regression'
    if old['text']:
        return 'previously_accepted'
    if not new['text']:
        return 'still_rejected'
    return 'partially_recovered' if new['stats'].get('partially_retained_sentences') or new['retained'] < new['generated'] else 'fully_recovered'


def run(args):
    output = args.output.resolve()
    if not output.is_relative_to((ROOT/'.agentbus/tmp').resolve()):
        raise ValueError('Private replay output must stay in .agentbus/tmp')
    if output.exists():
        raise ValueError('Output must be a new directory')
    output.mkdir(parents=True)
    revision = subprocess.check_output(['git', 'rev-parse', args.baseline_ref], cwd=ROOT, text=True).strip()
    archive, baseline = output/'baseline.tar', output/'baseline'
    subprocess.run(['git', 'archive', '--output', str(archive), revision, 'src'], cwd=ROOT, check=True)
    baseline.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(baseline, filter='data')
    jobs, gaps, sources = [], [], []
    for label, records_path, report_path in args.dataset:
        records = [json.loads(line) for line in Path(records_path).read_text(encoding='utf-8').splitlines()]
        report = json.loads(Path(report_path).read_text(encoding='utf-8'))
        added, missing = make_jobs(label, records, report)
        jobs.extend(added); gaps.extend(missing)
        sources.append(dict(dataset=label, diagnostics_sha256=digest(records_path), report_sha256=digest(report_path),
                            diagnostics=str(records_path), report=str(report_path)))
    write_json(output/'jobs.json', jobs)
    for name, source in (('old', baseline), ('new', ROOT)):
        subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), '--worker',
                        str(source), str(output/'jobs.json'), str(output/(name+'.json'))], check=True)
    old, new = [json.loads((output/(name+'.json')).read_text(encoding='utf-8')) for name in ('old', 'new')]
    counts, by_draft, rows = defaultdict(Counter), defaultdict(list), []
    for job, before, after in zip(jobs, old, new, strict=True):
        status = category(before, after)
        counts[job['dataset']][status] += 1
        counts[job['dataset']]['deleted_clauses'] += after['stats'].get('deleted_clauses', 0)
        bucket = f"{job['dataset']}/{'first_draft' if job['draft'] == 1 else 'repair_draft'}"
        counts[bucket][status] += 1
        for key in ('generated_clauses', 'retained_clauses', 'deleted_clauses', 'partially_retained_sentences'):
            counts[bucket][key] += after['stats'].get(key, 0)
        counts[bucket]['generated_sentences'] += after['generated']
        counts[bucket]['deleted_sentences'] += after['generated'] - after['retained']
        identity = tuple(job[k] for k in ('dataset', 'case_id', 'phase', 'turn', 'context_occurrence', 'draft'))
        by_draft[identity].append((before, after))
        rows.append(dict(**{k:v for k,v in job.items() if k != 'context'}, status=status, old=before, new=after))
    drafts = []
    for identity, items in by_draft.items():
        old_count = sum(bool(a['text']) for a,b in items)
        new_count = sum(bool(b['text']) for a,b in items)
        drafts.append(dict(identity=list(identity), fields=len(items), old_accepted_fields=old_count, new_accepted_fields=new_count,
                           old_all_text_accepted=old_count == len(items), new_all_text_accepted=new_count == len(items),
                           old_generated_sentences=sum(a['generated'] for a,b in items),
                           new_generated_sentences=sum(b['generated'] for a,b in items),
                           old_retained_sentences=sum(a['retained'] for a,b in items),
                           new_retained_sentences=sum(b['retained'] for a,b in items),
                           new_deleted_clauses=sum(b['stats'].get('deleted_clauses',0) for a,b in items)))
    write_json(output/'items.json', rows)
    write_json(output/'drafts.json', drafts)
    write_json(output/'gaps.json', gaps)
    names = ('answer_validation.py', 'answer_numeric_bindings.py', 'answer_technical_validation.py', 'answer_clauses.py', 'answer_claim_recovery.py')
    summary = dict(baseline_revision=revision, sources=sources, counts=dict(counts), drafts=len(drafts), fields=len(rows),
                   gaps=Counter(g['reason'] for g in gaps), source_sha256={name:digest(ROOT/'src/trading_companion'/name) for name in names},
                   script_sha256=digest(Path(__file__)),
                   scope='同一历史草稿逐文本字段的核对；标题单列，不重复聚合正文。不是完整评级重跑或真实删句率。',
                   limitations=['报告结构化事实用于恢复指标身份；缺少原始行情快照，不能独立重算来源。',
                                '普通数字只用原送模证据；仅RSI关系可用同轮公开事实。',
                                '未回放完整价位选择、结构合并和一次修正流程；缺失上下文不计成功。'])
    write_json(output/'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', nargs=3, action='append', metavar=('LABEL', 'DIAGNOSTICS', 'REPORT'))
    parser.add_argument('--baseline-ref', default='b5d77eb')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', nargs=3, metavar=('SOURCE_ROOT', 'JOBS', 'OUTPUT'))
    args = parser.parse_args()
    if args.worker:
        worker(*args.worker)
    elif args.dataset and args.output:
        run(args)
    else:
        parser.error('--dataset and --output are required')
