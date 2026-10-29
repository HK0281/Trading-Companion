"""Headline adapters share task definitions, never product transport or gold labels."""

from dataclasses import asdict
from hashlib import sha256
import json

from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL, DeepSeekConfig, parse_chat_response
from trading_companion.model import ModelCallError
from trading_companion.news_triage import INSTRUCTIONS, OPTIONS, questions
from trading_companion.typesafe_jev import (
    ClassifierError, JevClassifier, bounded_post, count, decode, model_name, require,
)

DEEPSEEK_TEMPLATE = "news-triage-deepseek-json-v1"


def deepseek_metadata(payload):
    result = {"model": None, "usage": dict.fromkeys(("prompt_tokens", "completion_tokens", "total_tokens"))}
    if type(payload) is dict:
        result["model"] = model_name(payload.get("model"))
        usage = payload.get("usage")
        if type(usage) is dict:
            for key in result["usage"]:
                if count(usage.get(key)):
                    result["usage"][key] = usage[key]
    return result


class DeepSeekTriage:
    def __init__(self, config, *, post=None):
        if type(config) is not DeepSeekConfig:
            raise ValueError("Use a trusted DeepSeek configuration")
        self.config, self.post = config, post

    def classify(self, state):
        body = {"model": self.config.model, "temperature": 0, "thinking": {"type": "disabled"},
                "max_tokens": self.config.max_output_tokens, "stream": False,
                "response_format": {"type": "json_object"}, "messages": [
                    {"role": "system", "content": INSTRUCTIONS + "仅输出严格JSON对象，恰好包含relevance/event_type/tone三个标签字符串。"
                     + json.dumps(questions(), ensure_ascii=False)},
                    {"role": "user", "content": json.dumps(state, ensure_ascii=False)}]}
        payload = bounded_post(DEEPSEEK_CHAT_URL, self.config.api_key, body,
                               timeout_seconds=self.config.timeout_seconds, post=self.post)
        metadata = deepseek_metadata(payload)
        try:
            require(metadata["model"] is not None)
            response = parse_chat_response(payload, self.config.model)
            labels = decode(response.text.encode("utf-8"))
            require(type(labels) is dict and labels.keys() == OPTIONS.keys()
                    and all(type(labels[k]) is str and labels[k] in choices for k, choices in OPTIONS.items()))
            usage = asdict(response.usage) if response.usage is not None else metadata["usage"]
            require(all(v is None or count(v) for v in usage.values()))
            if all(v is not None for v in usage.values()):
                require(usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])
            return {"model": metadata["model"], "usage": usage,
                    "answers": {k: {"choice": v, "confidence": None, "probabilities": None} for k, v in labels.items()}}
        except (ModelCallError, ClassifierError):
            raise ClassifierError("invalid_response", metadata=metadata) from None


class JevTriage:
    def __init__(self, config, *, post=None):
        self.delegate = JevClassifier(config, post=post)

    def classify(self, state):
        return self.delegate.classify(state, questions())


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def iter_content(self, chunk_size):
        yield self.raw

    def close(self):
        pass


def fixture_post(provider):
    """Independent deterministic protocol responses; input contains no annotation fields."""
    def post(url, **kwargs):
        body = kwargs["json"]
        state = body["state"] if provider == "jev" else json.loads(body["messages"][-1]["content"])
        require(state.keys() == {"title", "company_name", "company_code"})
        seed = sha256((provider + json.dumps(state, ensure_ascii=False, sort_keys=True)).encode("utf-8")).digest()
        labels = {key: choices[seed[i] % len(choices)] for i, (key, choices) in enumerate(OPTIONS.items())}
        if provider == "jev":
            answers = {}
            for i, (key, choices) in enumerate(OPTIONS.items()):
                conf = (0.55, 0.75, 0.95)[seed[i + 3] % 3]
                answers[key] = {"type": "choice", "choice": labels[key], "confidence": conf,
                                "probabilities": {c: conf if c == labels[key] else (1-conf)/(len(choices)-1) for c in choices}}
            return _FakeResponse({"model": body["model"], "answers": answers,
                                  "usage": {"input_tokens": 120, "output_tokens": 12}})
        return _FakeResponse({"model": body["model"], "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps(labels)}}],
            "usage": {"prompt_tokens": 140, "completion_tokens": 25, "total_tokens": 165}})
    return post
