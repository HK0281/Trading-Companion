"""Product-v2 adapters share definitions; DeepSeek can explicitly abstain per question."""

from dataclasses import asdict
import json

from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL, DeepSeekConfig, parse_chat_response
from trading_companion.model import ModelCallError
from trading_companion.news_triage_models import deepseek_metadata
from trading_companion.news_triage_product import INSTRUCTIONS, OPTIONS, UNRESOLVED, questions
from trading_companion.typesafe_jev import ClassifierError, JevClassifier, bounded_post, count, decode, require

DEEPSEEK_TEMPLATE = "news-triage-product-deepseek-json-v1"


class ProductJev:
    def __init__(self, config, *, post=None):
        self.delegate = JevClassifier(config, post=post)

    def classify(self, state):
        result = self.delegate.classify(state, questions())
        if (result["answers"]["relevance"]["choice"] == "unrelated"
                and result["answers"]["tone"]["choice"] != "neutral"):
            raise ClassifierError("invalid_response", metadata={k: result[k] for k in ("model", "usage")})
        return result


class ProductDeepSeek:
    def __init__(self, config, *, post=None):
        if type(config) is not DeepSeekConfig:
            raise ValueError("Use a trusted DeepSeek configuration")
        self.config, self.post = config, post

    def classify(self, state):
        body = {"model": self.config.model, "temperature": 0, "thinking": {"type": "disabled"},
                "max_tokens": self.config.max_output_tokens, "stream": False,
                "response_format": {"type": "json_object"}, "messages": [
                    {"role": "system", "content": INSTRUCTIONS +
                     "仅输出严格JSON对象，恰含relevance/tone/importance三个字符串；各为该题标签或unresolved。"
                     "不要输出置信度或概率。" + json.dumps(questions(), ensure_ascii=False)},
                    {"role": "user", "content": json.dumps(state, ensure_ascii=False)}]}
        payload = bounded_post(DEEPSEEK_CHAT_URL, self.config.api_key, body,
                               timeout_seconds=self.config.timeout_seconds, post=self.post)
        metadata = deepseek_metadata(payload)
        try:
            require(metadata["model"] is not None)
            response = parse_chat_response(payload, self.config.model)
            labels = decode(response.text.encode("utf-8"))
            require(type(labels) is dict and labels.keys() == OPTIONS.keys()
                    and all(type(labels[q]) is str and labels[q] in (*OPTIONS[q], UNRESOLVED) for q in OPTIONS))
            require(labels["relevance"] != "unrelated" or labels["tone"] in ("neutral", UNRESOLVED))
            usage = asdict(response.usage) if response.usage is not None else metadata["usage"]
            require(all(v is None or count(v) for v in usage.values()))
            if all(v is not None for v in usage.values()):
                require(usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])
            return {"model": metadata["model"], "usage": usage,
                    "answers": {q: {"choice": value, "confidence": None, "probabilities": None}
                                for q, value in labels.items()}}
        except (ModelCallError, ClassifierError):
            raise ClassifierError("invalid_response", metadata=metadata) from None
