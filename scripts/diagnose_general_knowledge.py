"""Read-only plan by default; Claude runs the approved, bounded live diagnostic."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation_tools.research_intent_integration_acceptance import TransportObserver, new_run_directory
from trading_companion.open_knowledge import KNOWLEDGE_VALIDATION_VERSION, answer_general_knowledge
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn
from trading_companion.turn_trace import (
    JsonlTraceSink, trace_control, trace_message, trace_outcome, trace_phase, trace_run_id,
)


APPROVAL_MESSAGE_ID = 541
MAX_CALLS = 14
CASES = (
    ('macd_base', 'MACD怎么计算', 3),
    ('macd_formula', 'MACD的计算公式是什么', 3),
    ('macd_parameters', 'MACD的12、26、9参数是什么意思', 3),
    ('macd_lines', 'MACD的DIF、DEA和柱状图如何计算', 3),
    ('etf_control', 'ETF是什么', 2),
)
_STOPS = frozenset(('turn_cancelled', 'turn_deadline_exceeded', 'model_call_budget_exhausted',
                    'analysis_budget_exhausted', 'token_budget_exhausted'))


def build_plan(max_calls=None):
    if max_calls is not None and (type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS):
        raise ValueError('Call limit must be between 1 and 14')
    return dict(version='general-knowledge-diagnostic-plan-v1', approval_message_id=APPROVAL_MESSAGE_ID,
                planned_attempts=MAX_CALLS, max_calls=max_calls, retry=False, rewrite=False,
                cases=[dict(case_id=case_id, question=question, repeats=repeats)
                       for case_id, question, repeats in CASES])


def load_live_model():
    # The default plan never reaches this configuration-reading branch.
    from trading_companion.model_registry import configured_models
    return configured_models(default_provider='deepseek')['deepseek']()


def _not_run():
    return dict(version=KNOWLEDGE_VALIDATION_VERSION, state='not_run', reason=None)


def summarize(report, observer):
    rows = report['rows']
    by_case = Counter((r['diagnostics']['reason'], r['case_id']) for r in rows
                      if r['diagnostics']['state'] == 'rejected')
    return dict(planned_attempts=len(rows), model_calls_attempted=sum(r['model_calls_attempted'] for r in rows),
                responses_received=sum(r['responses_received'] for r in rows),
                provider_attempts=len(observer.events),
                wire_started=sum(e['wire_started'] for e in observer.events),
                status_counts=dict(Counter(r['status'] for r in rows)),
                reason_by_case=[dict(reason=reason, case_id=case_id, count=count)
                                for (reason, case_id), count in sorted(by_case.items())],
                case_counts=[dict(case_id=case_id, planned=repeats,
                    accepted=sum(r['status'] == 'accepted' for r in rows if r['case_id'] == case_id),
                    failed=sum(r['status'] in ('failed', 'stopped') for r in rows if r['case_id'] == case_id),
                    not_measured=sum(r['status'] == 'not_measured' for r in rows if r['case_id'] == case_id))
                    for case_id, _, repeats in CASES])


def run_live(*, max_calls, output, model_factory=None):
    build_plan(max_calls)  # Validate before writes, configuration reads or model construction.
    if max_calls is None:
        raise ValueError('Live diagnostics require an explicit call limit')
    folder = new_run_directory(output)
    observer = TransportObserver(max_calls)
    report = dict(version='general-knowledge-diagnostic-report-v1', approval_message_id=APPROVAL_MESSAGE_ID,
                  max_calls=max_calls, status='running', reason=None, rows=[
                      dict(case_id=case_id, repeat=repeat, status='not_measured', reason='not_run', code=None,
                           diagnostics=_not_run(), model_calls_attempted=0, responses_received=0,
                           elapsed_ms=None, usage=None, usage_complete=False, run_id=None)
                      for case_id, _, repeats in CASES for repeat in range(1, repeats + 1)])

    def save():
        report['summary'] = summarize(report, observer)
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    try:
        save()  # Preserve the complete denominator even if startup or the first request fails.
        model = (load_live_model if model_factory is None else model_factory)()
        sink = JsonlTraceSink(folder / 'trace')
        with observer.installed():
            for row in report['rows']:
                if observer.exhausted or report['summary']['model_calls_attempted'] >= max_calls:
                    row['reason'] = 'global_call_limit'
                    continue
                question = next(question for case_id, question, _ in CASES if case_id == row['case_id'])
                control = TurnControl(TurnLimits(model_calls=1, analyses=0))
                control.start()
                with trace_message(sink, mode='normal', provider='deepseek', history=False), bind_turn(control):
                    trace_phase('execution')
                    trace_control(control)
                    row['run_id'] = trace_run_id()
                    try:
                        run = answer_general_knowledge(question, model=ControlledToolModel(
                            model, control, retry_transient_errors=False))
                        # Deliberately do not serialize run, answer, model_response or question.
                        row.update(status='accepted' if run.answer.status == 'accepted' else 'failed',
                                   reason=None, code=run.answer.code, diagnostics=run.answer.diagnostics)
                    except TurnStopped as stop:
                        row.update(status='stopped', reason=None, code=stop.code if stop.code in _STOPS else 'other')
                    except KeyboardInterrupt:
                        row['reason'] = 'interrupted'
                        control.cancel()
                        raise
                    except Exception:
                        row.update(status='failed', reason='diagnostic_error', code='diagnostic_error')
                    finally:
                        control.finish()
                        meter = control.to_dict()
                        row.update(model_calls_attempted=meter['model_calls_attempted'],
                                   responses_received=meter['responses_received'], elapsed_ms=meter['elapsed_ms'],
                                   usage=meter['reported_usage'], usage_complete=meter['usage_complete_for_attempts'])
                        trace_outcome(SimpleNamespace(status='completed' if row['status'] == 'accepted' else 'failed',
                                                      code=row['code'], workflow_trace=None))
                        save()
        report.update(status='observations_recorded' if all(r['status'] != 'not_measured' for r in report['rows'])
                      else 'incomplete', reason=None)
    except KeyboardInterrupt:
        report.update(status='incomplete', reason='interrupted')
        for row in report['rows']:
            if row['reason'] == 'not_run':
                row['reason'] = 'interrupted'
    except Exception:
        report.update(status='incomplete', reason='diagnostic_unavailable')
    finally:
        save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--max-calls', type=int, help='Explicit global ceiling, 1 to 14; failures also count')
    parser.add_argument('--output', type=Path, help='New directory strictly beneath .agentbus/tmp')
    args = parser.parse_args(argv)
    if args.live and (args.max_calls is None or args.output is None):
        parser.error('--live requires --max-calls and --output')
    try:
        plan = build_plan(args.max_calls)
        if not args.live:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        report = run_live(max_calls=args.max_calls, output=args.output)
        print(json.dumps(dict(status=report['status'], reason=report['reason'], summary=report['summary']),
                         ensure_ascii=False, indent=2))
        return 0 if report['status'] == 'observations_recorded' else 1
    except Exception:
        # Provider/configuration exceptions can contain credentials. Never print their text.
        print('知识诊断未能完成：请检查调用上限、配置及新的隔离输出目录。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
