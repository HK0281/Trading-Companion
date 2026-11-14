"""Replay the retired v2.6 freeze's recorded public-file fingerprints in offline tests.

The v2.6 freeze last validated on the real tree at tag `research-intent-v2.6-freeze-valid`.
Later clean-ups moved or deleted some of its public files, so tests that exercise the freeze
logic substitute the recorded fingerprints instead of hashing the current tree.
"""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from evaluation_tools import research_intent_v26_cases as cases


@contextmanager
def recorded_v26_public_files():
    recorded = cases.read_json(cases.FREEZE)['public_files_sha256_lf']
    current = cases.public_sha256
    root = cases.ROOT.resolve()

    def fingerprint(path):
        name = Path(path).resolve().relative_to(root).as_posix()
        return recorded[name] if name in recorded else current(path)

    with patch.object(cases, 'public_sha256', side_effect=fingerprint):
        yield recorded
