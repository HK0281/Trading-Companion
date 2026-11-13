"""Build a real v1 database using the committed 9aa98af implementation, never user files."""

from contextlib import contextmanager
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import ModuleType
from uuid import uuid4


@lru_cache(maxsize=1)
def _legacy_sources():
    root = Path(__file__).resolve().parent / "fixtures/personal-memory-v1-9aa98af"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["source_commit"] != "9aa98af9228d736cd417e2b27f7906aefe7542d9"
            or manifest["hash_normalization"] != "LF"):
        raise AssertionError("Unexpected legacy fixture provenance")
    fingerprints = {
        "personal_memory": "2349a1730b808caf19a8d991b1dcaf423f6bf0de213d6d30614aa6c0d043f8e9",
        "personal_memory_store": "077436f906391de9ca0e58e378b18070299a1d673a5b5db0c8551a399e821985",
    }
    sources = {}
    for name, fingerprint in fingerprints.items():
        filename = name + ".py.txt"
        # Fingerprint repository text, preserving behavior across Windows CRLF checkouts.
        data = (root / filename).read_bytes().replace(b"\r\n", b"\n")
        if (sha256(data).hexdigest() != fingerprint or manifest["files"][filename] != {
                "source_path": f"src/trading_companion/{name}.py", "sha256": fingerprint}):
            raise AssertionError("Legacy fixture source changed")
        sources[name] = data.decode("utf-8")
    return sources


@contextmanager
def legacy_v1_store(path, *, clock):
    """Only remap the old modules' import namespace; execute their original behavior."""
    package = "_watchlist_v1_" + uuid4().hex
    names = [package, package + ".personal_memory", package + ".personal_memory_store"]
    base = ModuleType(package)
    base.__path__ = []
    sys.modules[package] = base
    store = None
    try:
        for name, source in _legacy_sources().items():
            module = ModuleType(package + "." + name)
            module.__file__ = f"9aa98af:src/trading_companion/{name}.py"
            module.__package__ = package
            sys.modules[module.__name__] = module
            if name == "personal_memory_store":
                original = "from trading_companion.personal_memory import ("
                if source.count(original) != 1:
                    raise AssertionError("Unexpected legacy module import boundary")
                source = source.replace(original, f"from {package}.personal_memory import (", 1)
            exec(compile(source, module.__file__, "exec"), module.__dict__)
        store = sys.modules[names[-1]].SqlitePersonalMemoryStore(path, clock=clock)
        yield store
    finally:
        if store is not None:
            store.close()
        for name in reversed(names):
            sys.modules.pop(name, None)
