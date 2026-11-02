"""不是新成绩：只读 v2.6.1 公开原始响应，离线检查当前候选；不调用模型。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation_tools import replay_research_intents_v261 as previous


SOURCE_CONTRACT = 'research-intent-v2.6.1'
SOURCES = {split: previous.ROOT/'.agentbus/evaluation-runs'/SOURCE_CONTRACT/'public'/split/'report.json'
           for split in ('dev', 'regression')}


def migrate_response(value):
    return previous.migrate_response(value, source_contract=SOURCE_CONTRACT)


def replay_report(split, archived):
    return previous.replay_report(split, archived, source_contract=SOURCE_CONTRACT)


def main(argv=None):
    return previous.run_cli(argv, source_contract=SOURCE_CONTRACT, sources=SOURCES, prefix='v262-offline-replay-')


if __name__ == '__main__':
    raise SystemExit(main())
