"""Bounded, duplicate-free UTF-8 artifacts; exclusive writes never replace a run."""

from hashlib import sha256
import json
from pathlib import Path


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate artifact key")
        result[key] = value
    return result


def read_json(path, *, limit=16*1024*1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError("Artifact too large")
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite artifact")))
    return value, sha256(raw).hexdigest()


def write_json(path, value):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)+"\n").encode("utf-8")
    with Path(path).open("xb") as output:
        output.write(raw)
    return sha256(raw).hexdigest()


def canonical(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()
