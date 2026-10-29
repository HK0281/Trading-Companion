"""One-task adapters, with no labels or corpus metadata in transmitted state."""

from dataclasses import asdict
from hashlib import sha256
import json

from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL, DeepSeekConfig, parse_chat_response
from trading_companion.model import ModelCallError
from trading_companion.news_triage_models import deepseek_metadata, _FakeResponse
from trading_companion.typesafe_jev import ClassifierError, JevClassifier, bounded_post, decode, require, count
from trading_companion.intent_router_task import LABELS, INSTRUCTION, questions, valid_context

TEMPLATE = "intent-router-deepseek-json-v1"


def validate_state(state):
    require(type(state) is dict and state.keys() == {"question", "context"} and type(state["question"]) is str
            and 0 < len(state["question"]) <= 2000)
    valid_context(state["context"], ablated="latest_valid_research_kind" not in state["context"])


class JevIntent:
    def __init__(self, config, *, post=None):
        self.delegate = JevClassifier(config, post=post)

    def classify(self, state):
        validate_state(state)
        result = self.delegate.classify(state, questions())
        answer = result.pop("answers")["intent"]
        return {**result, "prediction": answer["choice"], "confidence": answer["confidence"], "probabilities": answer["probabilities"]}


class DeepSeekIntent:
    def __init__(self, config, *, post=None):
        if type(config) is not DeepSeekConfig:
            raise ValueError("Use a trusted configuration")
        self.config, self.post = config, post

    def classify(self, state):
        validate_state(state)
        body = {"model": self.config.model, "temperature": 0, "thinking": {"type": "disabled"}, "max_tokens": 128,
                "stream": False, "response_format": {"type": "json_object"}, "messages": [
                    {"role": "system", "content": INSTRUCTION + "仅返回严格JSON对象，恰好含intent标签字符串。" + json.dumps(questions(), ensure_ascii=False)},
                    {"role": "user", "content": json.dumps(state, ensure_ascii=False)}]}
        payload = bounded_post(DEEPSEEK_CHAT_URL, self.config.api_key, body, timeout_seconds=self.config.timeout_seconds, post=self.post)
        metadata = deepseek_metadata(payload)
        try:
            require(metadata["model"] is not None)
            response = parse_chat_response(payload, self.config.model)
            value = decode(response.text.encode())
            require(type(value) is dict and value.keys() == {"intent"} and type(value["intent"]) is str and value["intent"] in LABELS)
            usage = asdict(response.usage) if response.usage else metadata["usage"]
            require(all(v is None or count(v) for v in usage.values()))
            if all(v is not None for v in usage.values()):
                require(usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])
            return {"model": metadata["model"], "usage": usage, "prediction": value["intent"], "confidence": None, "probabilities": None}
        except (ModelCallError, ClassifierError):
            raise ClassifierError("invalid_response", metadata=metadata) from None


def fixture_post(provider):
    def post(url, **kwargs):
        body = kwargs["json"]
        state = body["state"] if provider == "jev" else json.loads(body["messages"][-1]["content"])
        validate_state(state)
        seed = sha256((provider + json.dumps(state, ensure_ascii=False, sort_keys=True)).encode()).digest()
        chosen, conf = LABELS[seed[0] % len(LABELS)], (.55, .75, .95, .99)[seed[1] % 4]
        if provider == "jev":
            return _FakeResponse({"model": body["model"], "answers": {"intent": {"type": "choice", "choice": chosen,
                "confidence": conf, "probabilities": {k: conf if k == chosen else (1-conf)/(len(LABELS)-1) for k in LABELS}}},
                "usage": {"input_tokens": 150, "output_tokens": 10}})
        return _FakeResponse({"model": body["model"], "choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({"intent": chosen})}}],
            "usage": {"prompt_tokens": 180, "completion_tokens": 10, "total_tokens": 190}})
    return post
