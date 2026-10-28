"""Default: all 40 artificial journeys, offline. No real mode is implemented in phase 5a."""

import argparse
import json
from pathlib import Path
import sys

# The executable lives in scripts/, while the shared offline guards live in scripts.verify.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_companion.workflow_evaluation import evaluate_workflows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--mode", choices=("offline",), default="offline")
    parser.add_argument("--case", action="append", dest="selected_cases")
    parser.add_argument("--repeat", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--collection-workers", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--model-call-limit", type=int)
    parser.add_argument("--trace-dir", help="Retain trace-v2 records in a new run subdirectory")
    result = evaluate_workflows(**vars(parser.parse_args(argv)))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["status"] == "passed" else 2 if result["status"] == "invalid_request" else 1


if __name__ == "__main__":
    raise SystemExit(main())
