"""Inspect an explicitly selected local evidence archive without model or network access."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_companion.snapshot_store import ResearchSnapshotStore, SnapshotStoreError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--id", help="Load one saved content identifier instead of listing metadata")
    parser.add_argument("--known-at", help="Aware ISO timestamp; only locally observed, retrieved data is eligible")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if not path.is_file():
        print(json.dumps({"status": "unavailable", "code": "snapshot_archive_not_found"}))
        return 2
    try:
        known_at = datetime.fromisoformat(args.known_at) if args.known_at else None
        with ResearchSnapshotStore(path) as store:
            result = store.load(args.id, known_at=known_at) if args.id else store.list(known_at=known_at)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (SnapshotStoreError, ValueError):
        print(json.dumps({"status": "unavailable", "code": "snapshot_query_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
