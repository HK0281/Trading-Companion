"""Product-v2 adapters share definitions; DeepSeek can explicitly abstain per question."""

from dataclasses import asdict
from collections import Counter
import json

from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL, DeepSeekConfig, parse_chat_response
from trading_companion.model import ModelCallError
from trading_companion.news_triage_models import deepseek_metadata
from trading_companion.news_triage_product import INSTRUCTIONS, OPTIONS, UNRESOLVED, questions
from trading_companion.typesafe_jev import ClassifierError, JevClassifier, bounded_post, count, decode, require

DEEPSEEK_TEMPLATE = "news-triage-product-deepseek-json-v1"
BATCH_TEMPLATE = "news-triage-product-importance-batch-json-v1"
QUESTION_BATCH_TEMPLATE = "news-triage-product-three-question-batch-json-v1"


class ProductJev:
    def __init__(self, config, *, post=None):
        self.delegate = JevClassifier(config, post=post)

    def classify(self, state, *, selected_questions=None):
        selected = questions() if selected_questions is None else {q: questions()[q] for q in selected_questions}
        result = self.delegate.classify(state, selected)
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

    def classify_importance_batch(self, items):
        """One request, positional IDs, and independent failures without response repair."""
        return self._classify_batch(items, all_questions=False)

    def classify_questions_batch(self, items):
        return self._classify_batch(items, all_questions=True)

    def _classify_batch(self, items, *, all_questions):
        if (not 1 <= len(items) <= 10 or len({r["id"] for r in items}) != len(items)
                or len({r["instrument_id"] for r in items}) != 1):
            raise ValueError("Use one company and at most ten distinct titles")
        selected = questions() if all_questions else {"importance": questions()["importance"]}
        if all_questions:
            schema = {"id": "原ID", **{q: "标签" for q in selected}}
            instruction = ("逐条独立判断全部三题。严格输出JSON对象"
                + json.dumps({"items": [schema]}, ensure_ascii=False) + "。"
                "按输入顺序逐条返回，不能遗漏、重复、添加或重排。每题标签必须为该题选项或unresolved；不要输出其他字段或概率。")
        else:
            # Preserve the existing split arm's exact prompt and template identity.
            instruction = ('仅判断importance。严格输出JSON对象{"items":[{"id":"原ID","importance":"标签"}]}。'
                "按输入顺序逐条返回，不能遗漏、重复、添加或重排。标签可为unknown或unresolved；不要输出其他题或概率。")
        body = {"model": self.config.model, "temperature": 0, "thinking": {"type": "disabled"},
                "max_tokens": self.config.max_output_tokens, "stream": False,
                "response_format": {"type": "json_object"}, "messages": [
                    {"role": "system", "content": INSTRUCTIONS + instruction + json.dumps(selected, ensure_ascii=False)},
                    {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)}]}
        payload = bounded_post(DEEPSEEK_CHAT_URL, self.config.api_key, body,
                               timeout_seconds=self.config.timeout_seconds, post=self.post)
        metadata = deepseek_metadata(payload)
        try:
            require(metadata["model"] is not None)
            response = parse_chat_response(payload, self.config.model)
            labels = decode(response.text.encode("utf-8"))
            require(type(labels) is dict and set(labels) == {"items"} and type(labels["items"]) is list)
            usage = asdict(response.usage) if response.usage is not None else metadata["usage"]
            require(all(v is None or count(v) for v in usage.values()))
            if all(v is not None for v in usage.values()):
                require(usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"])
            validate = validate_question_batch_items if all_questions else validate_batch_items
            return {"model": metadata["model"], "usage": usage, **validate(items, labels["items"])}
        except (ModelCallError, ClassifierError):
            raise ClassifierError("invalid_response", metadata=metadata) from None


def validate_batch_items(requested, returned):
    counts = Counter(r.get("id") for r in returned if type(r) is dict and type(r.get("id")) is str)
    expected = {r["id"] for r in requested}
    output = []
    for index, item in enumerate(requested):
        value = returned[index] if index < len(returned) else None
        reason, choice = "ok", UNRESOLVED
        if value is None:
            reason = "missing_item"
        elif type(value) is not dict or set(value) != {"id", "importance"}:
            reason = "invalid_item"
        elif value["id"] != item["id"]:
            reason = "position_or_id_mismatch"
        elif counts[item["id"]] != 1:
            reason = "duplicate_id"
        elif type(value["importance"]) is not str or value["importance"] not in (*OPTIONS["importance"], UNRESOLVED):
            reason = "invalid_label"
        else:
            choice = value["importance"]
            if choice == UNRESOLVED:
                reason = "model_abstention"
        output.append({"id": item["id"], "status": "ok" if reason == "ok" else "unresolved", "reason": reason,
                       "answer": {"choice": choice, "confidence": None, "probabilities": None}})
    return {"items": output, "extra_returned_items": max(0, len(returned)-len(requested)),
            "unknown_returned_items": sum(type(r) is not dict or type(r.get("id")) is not str or r["id"] not in expected
                                          for r in returned)}


def validate_question_batch_items(requested, returned):
    """Do not repair positions; bad labels invalidate only their own question."""
    counts = Counter(r.get("id") for r in returned if type(r) is dict and type(r.get("id")) is str)
    expected, output = {r["id"] for r in requested}, []
    for index, item in enumerate(requested):
        value = returned[index] if index < len(returned) else None
        reason = "ok"
        if value is None:
            reason = "missing_item"
        elif type(value) is not dict or set(value) != {"id", *OPTIONS}:
            reason = "invalid_item"
        elif value["id"] != item["id"]:
            reason = "position_or_id_mismatch"
        elif counts[item["id"]] != 1:
            reason = "duplicate_id"
        answers, states = {}, {}
        for q in OPTIONS:
            choice, why = UNRESOLVED, reason
            if why == "ok":
                if type(value[q]) is not str or value[q] not in (*OPTIONS[q], UNRESOLVED):
                    why = "invalid_label"
                else:
                    choice = value[q]
                    if choice == UNRESOLVED:
                        why = "model_abstention"
            if (q == "tone" and reason == "ok" and value["relevance"] == "unrelated"
                    and choice not in ("neutral", UNRESOLVED)):
                choice, why = UNRESOLVED, "unrelated_directional_tone"
            answers[q] = {"choice": choice, "confidence": None, "probabilities": None}
            states[q] = {"status": "ok" if why == "ok" else "unresolved", "reason": why}
        complete = all(s["status"] == "ok" for s in states.values())
        output.append({"id": item["id"], "status": "ok" if complete else "unresolved",
                       "reason": "ok" if complete else "partial_questions", "answers": answers,
                       "question_status": states})
    return {"items": output, "extra_returned_items": max(0, len(returned)-len(requested)),
            "unknown_returned_items": sum(type(r) is not dict or type(r.get("id")) is not str or r["id"] not in expected
                                          for r in returned)}
