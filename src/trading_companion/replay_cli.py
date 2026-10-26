"""Read-only local archive listing, export and historical exposure replay."""

import argparse
from datetime import datetime
import json
from pathlib import Path

from trading_companion.historical_replay import ReplayConfig, replay_archived_snapshot
from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect saved research; never request market data or models")
    parser.add_argument("--db", required=True, help="Existing research snapshot SQLite file")
    parser.add_argument("--known-at", help="Optional timezone-aware local observation cutoff")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    inspect = actions.add_parser("show")
    inspect.add_argument("snapshot_id")
    replay = actions.add_parser("replay")
    replay.add_argument("snapshot_id")
    replay.add_argument("--stock", required=True)
    replay.add_argument("--window", type=int, default=20)
    replay.add_argument("--cost-bps", type=float, default=5)
    args = parser.parse_args(argv)
    if not Path(args.db).is_file():
        parser.error("Use an existing snapshot archive")
    try:
        known_at = datetime.fromisoformat(args.known_at) if args.known_at else None
        with ResearchSnapshotStore(Path(args.db)) as store:
            if args.action == "list":
                output = {"snapshots": store.list(known_at=known_at)}
            else:
                output = store.load(args.snapshot_id, known_at=known_at)
                if args.action == "replay":
                    output = replay_archived_snapshot(output, instrument_id=args.stock,
                        config=ReplayConfig(args.window, args.cost_bps)).to_dict()
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (SnapshotStoreError, ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"status": "unavailable", "code": "archive_replay_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
