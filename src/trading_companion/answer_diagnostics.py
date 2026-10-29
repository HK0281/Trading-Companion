"""Explicit evaluation-only raw diagnostics; the default context stores nothing."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from uuid import uuid4

_ACTIVE = ContextVar('answer_diagnostics', default=None)
_LOCATION = ContextVar('answer_diagnostic_location', default={})
ROOT = Path(__file__).resolve().parents[2] / '.agentbus/tmp'


def check_diagnostics_root(path, *, file=False):
    if path is None:
        return None
    selected = Path(path).resolve()
    if not selected.is_relative_to(ROOT) or selected.exists() and not (selected.is_file() if file else selected.is_dir()):
        raise ValueError('Diagnostics must stay inside .agentbus/tmp')
    return selected


class AnswerDiagnostics:
    def __init__(self, root, *, max_records=4000, max_bytes=8_000_000):
        self.root = check_diagnostics_root(root)
        self.max_records, self.max_bytes = max_records, max_bytes
        self.records = self.bytes = self.dropped = 0
        self.failed = False
        self.directory = None

    def emit(self, kind, **fields):
        if self.failed:
            return
        record = dict(kind=kind) | _LOCATION.get() | fields
        try:
            raw = (json.dumps(record, ensure_ascii=False, allow_nan=False)+'\n').encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            self.failed = True
            return
        if self.records >= self.max_records or self.bytes+len(raw) > self.max_bytes:
            self.dropped += 1
            return
        try:
            if self.directory is None:
                parent = check_diagnostics_root(self.root)
                parent.mkdir(parents=True, exist_ok=True)
                self.directory = parent / ('diagnostics-'+uuid4().hex)
                self.directory.mkdir(exist_ok=False)
            check_diagnostics_root(self.directory)
            target = check_diagnostics_root(self.directory/'records.jsonl', file=True)
            with target.open('ab') as output:
                output.write(raw)
            self.records += 1
            self.bytes += len(raw)
        except (OSError, ValueError, RuntimeError):
            self.failed = True

    def summary(self):
        return dict(enabled=True, complete=not self.failed and not self.dropped,
                    records=self.records, bytes=self.bytes, dropped=self.dropped,
                    directory=str(self.directory) if self.directory else None)


@contextmanager
def bind_diagnostics(collector):
    token = _ACTIVE.set(collector)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


@contextmanager
def diagnostic_location(**fields):
    if _ACTIVE.get() is None:
        yield
        return
    token = _LOCATION.set({**_LOCATION.get(), **fields})
    try:
        yield
    finally:
        _LOCATION.reset(token)


def diagnostics_enabled():
    return _ACTIVE.get() is not None


def diagnostic_event(kind, **fields):
    collector = _ACTIVE.get()
    if collector is not None:
        collector.emit(kind, **fields)
