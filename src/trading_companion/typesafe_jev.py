"""Reusable bounded Choice protocol; no credential reads at import time."""

from dataclasses import dataclass, field
import json
from math import isfinite
import os
import re
from time import monotonic

from trading_companion.model import ModelConfigurationError

JEV_URL = "https://api.typesafe.ai/v1/systemone"
MAX_BYTES = 65536
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ClassifierError(Exception):
    def __init__(self, code, *, metadata=None):
        self.code, self.metadata = code, metadata or {}
        super().__init__(code)


def require(condition):
    if not condition:
        raise ClassifierError("invalid_response")


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _depth(value, level=0):
    require(level <= 12)
    if type(value) in (dict, list):
        require(len(value) <= 128)
        for child in (value.values() if type(value) is dict else value):
            _depth(child, level + 1)


def decode(raw):
    try:
        require(type(raw) is bytes and len(raw) <= MAX_BYTES and not raw.startswith(b"\xef\xbb\xbf"))
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                            parse_constant=lambda value: require(False))
        _depth(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ClassifierError("invalid_response") from None


def model_name(value):
    return value if type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value) else None


def count(value):
    return type(value) is int and 0 <= value <= 10**12


def probability(value):
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= 1


@dataclass(frozen=True)
class JevConfig:
    api_key: str = field(repr=False)
    model: str = "jev-latest"
    timeout_seconds: float = 15

    def __post_init__(self):
        if (type(self.api_key) is not str or not self.api_key or not self.api_key.isascii()
                or not self.api_key.isprintable() or any(c.isspace() for c in self.api_key)
                or model_name(self.model) is None or type(self.timeout_seconds) not in (int, float)
                or not isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60):
            raise ModelConfigurationError("Invalid local classifier configuration")

    @classmethod
    def from_env(cls, *, timeout_seconds=15):
        return cls(os.environ.get("TYPESAFE_API_KEY", ""), timeout_seconds=timeout_seconds)


def bounded_post(endpoint, key, body, *, timeout_seconds, post=None, clock=monotonic):
    """Fixed endpoints, one attempt, bounded decoded bytes; not an absolute wall-time cap."""
    from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL
    if endpoint not in (JEV_URL, DEEPSEEK_CHAT_URL):
        raise ModelConfigurationError("Unsupported classifier endpoint")
    import requests
    started = clock()
    response = None
    try:
        response = (requests.post if post is None else post)(
            endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body, timeout=(min(5, timeout_seconds), timeout_seconds), stream=True, allow_redirects=False)
        status = response.status_code
        if status != 200:
            code = {401: "auth_failed", 403: "auth_failed", 429: "rate_limited", 529: "overloaded"}.get(status)
            raise ClassifierError(code or ("redirect_refused" if 300 <= status < 400 else "http_error"))
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=4096):
            if clock() - started >= timeout_seconds:
                raise ClassifierError("timeout")
            if type(chunk) is not bytes:
                raise ClassifierError("invalid_response")
            size += len(chunk)
            if size > MAX_BYTES:
                raise ClassifierError("response_too_large")
            chunks.append(chunk)
        if clock() - started >= timeout_seconds:
            raise ClassifierError("timeout")
        return decode(b"".join(chunks))
    except requests.exceptions.Timeout:
        raise ClassifierError("timeout") from None
    except requests.exceptions.ConnectionError as error:
        from urllib3.exceptions import ReadTimeoutError
        # requests wraps urllib3's streaming read timeout in ConnectionError.
        code = "timeout" if error.args and isinstance(error.args[0], ReadTimeoutError) else "connection_error"
        raise ClassifierError(code) from None
    except requests.exceptions.RequestException:
        raise ClassifierError("connection_error") from None
    finally:
        if response is not None:
            response.close()


def jev_metadata(payload):
    result = {"model": None, "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}}
    if type(payload) is dict:
        result["model"] = model_name(payload.get("model"))
        usage = payload.get("usage")
        if type(usage) is dict:
            for src, dst in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                if count(usage.get(src)):
                    result["usage"][dst] = usage[src]
            if all(result["usage"][name] is not None for name in ("prompt_tokens", "completion_tokens")):
                result["usage"]["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return result


def parse_choices(payload, questions):
    metadata = jev_metadata(payload)
    try:
        require(type(payload) is dict and metadata["model"] is not None)
        require(type(payload.get("usage")) is dict
                and all(count(payload["usage"].get(key)) for key in ("input_tokens", "output_tokens")))
        answers = payload.get("answers")
        require(type(answers) is dict and answers.keys() == questions.keys())
        result = {}
        for name, question in questions.items():
            answer, options = answers[name], question["criteria"]
            require(type(answer) is dict and answer.get("type") == "choice")
            probabilities, choice, confidence = (answer.get(key) for key in ("probabilities", "choice", "confidence"))
            require(type(probabilities) is dict and probabilities.keys() == options.keys()
                    and all(probability(value) for value in probabilities.values()))
            require(abs(sum(probabilities.values()) - 1) <= 0.020000000001)
            require(type(choice) is str and choice in options and probability(confidence))
            require(probabilities[choice] == max(probabilities.values()))
            result[name] = {"choice": choice, "confidence": confidence, "probabilities": dict(probabilities)}
        return {**metadata, "answers": result}
    except ClassifierError:
        raise ClassifierError("invalid_response", metadata=metadata) from None


class JevClassifier:
    def __init__(self, config, *, post=None, clock=monotonic):
        if type(config) is not JevConfig:
            raise ModelConfigurationError("Use a trusted Jev configuration")
        self.config, self.post, self.clock = config, post, clock

    def classify(self, state, questions):
        if (type(questions) is not dict or not 1 <= len(questions) <= 8
                or any(type(name) is not str or not NAME.fullmatch(name) or type(q) is not dict
                       or q.keys() - {"type", "criteria", "instructions"} or q.get("type") != "choice"
                       or type(q.get("criteria")) is not dict or not 2 <= len(q["criteria"]) <= 32
                       or any(type(k) is not str or not NAME.fullmatch(k) or type(v) is not str
                              or not 0 < len(v) <= 2000 for k, v in q["criteria"].items()) for name, q in questions.items())):
            raise ModelConfigurationError("Invalid local Choice questions")
        body = {"model": self.config.model, "state": state, "questions": questions}
        try:
            raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            require(len(raw) <= MAX_BYTES)
            _depth(body)
        except (ValueError, TypeError, RecursionError, ClassifierError):
            raise ModelConfigurationError("Classifier request exceeds local bounds") from None
        payload = bounded_post(JEV_URL, self.config.api_key, body, timeout_seconds=self.config.timeout_seconds,
                               post=self.post, clock=self.clock)
        return parse_choices(payload, questions)
