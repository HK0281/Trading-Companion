from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.model import ModelConfigurationError
from trading_companion.typesafe_jev import (
    ClassifierError, JEV_URL, JevClassifier, JevConfig, MAX_BYTES, bounded_post, decode, parse_choices,
)

QUESTIONS = {"decision": {"type": "choice", "criteria": {"yes": "Applies", "no": "Does not apply"}}}


def payload():
    return {"model": "jev-served-2026", "answers": {"decision": {"type": "choice", "choice": "yes", "confidence": .9,
            "probabilities": {"yes": .8, "no": .2}}}, "usage": {"input_tokens": 120, "output_tokens": 12}}


def http_response(value=None, *, status=200, raw=None):
    response = Mock(status_code=status)
    response.iter_content.return_value = [raw if raw is not None else json.dumps(payload() if value is None else value).encode()]
    return response


class JevTests(unittest.TestCase):
    def test_actual_protocol_and_privacy_projection(self):
        value = payload()
        value["untrusted"] = "never-log-this"
        value["answers"]["decision"]["unexpected"] = "never-log-this"
        response, post = http_response(value), Mock()
        post.return_value = response
        classifier = JevClassifier(JevConfig("test-credential"), post=post)
        result = classifier.classify({"text": "test"}, QUESTIONS)
        self.assertEqual(result["model"], "jev-served-2026")
        self.assertNotEqual(result["answers"]["decision"]["confidence"], .8)
        self.assertEqual(result["usage"]["total_tokens"], 132)
        self.assertNotIn("never-log-this", repr(result))
        self.assertNotIn("test-credential", repr(classifier.config))
        args = post.call_args
        self.assertEqual(args.args, (JEV_URL,))
        self.assertEqual(args.kwargs["json"], {"model": "jev-latest", "state": {"text": "test"}, "questions": QUESTIONS})
        self.assertFalse(args.kwargs["allow_redirects"])
        self.assertTrue(args.kwargs["stream"])
        self.assertEqual(args.kwargs["timeout"], (5, 15))
        response.close.assert_called_once()

    def test_probability_validation_and_metadata_survives_invalid_answer(self):
        mutations = [
            lambda p: p.pop("model"), lambda p: p.pop("usage"),
            lambda p: p["usage"].update(input_tokens=True), lambda p: p["usage"].update(output_tokens=-1),
            lambda p: p["answers"].update(extra=p["answers"]["decision"]),
            lambda p: p["answers"]["decision"].pop("confidence"),
            lambda p: p["answers"]["decision"].update(type="score"),
            lambda p: p["answers"]["decision"].update(choice="no"),
            lambda p: p["answers"]["decision"].update(choice=[]),
            lambda p: p["answers"]["decision"].update(confidence=float("nan")),
            lambda p: p["answers"]["decision"].update(confidence=True),
            lambda p: p["answers"]["decision"].update(probabilities={"yes": .8}),
            lambda p: p["answers"]["decision"].update(probabilities={"yes": .8, "no": .2, "other": 0}),
            lambda p: p["answers"]["decision"].update(probabilities={"yes": 1.01, "no": -.01}),
            lambda p: p["answers"]["decision"].update(probabilities={"yes": .7, "no": .27}),
            lambda p: p["answers"]["decision"].update(probabilities={"yes": float("inf"), "no": 0}),
        ]
        for mutate in mutations:
            value = payload()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ClassifierError) as context:
                parse_choices(value, QUESTIONS)
            self.assertEqual(context.exception.code, "invalid_response")
            self.assertNotIn("answers", context.exception.metadata)
        value = payload()
        value["answers"]["decision"].update(choice="bad")
        with self.assertRaises(ClassifierError) as context:
            parse_choices(value, QUESTIONS)
        self.assertEqual(context.exception.metadata["usage"]["prompt_tokens"], 120)

    def test_ties_and_sum_tolerance_are_not_renormalized(self):
        for choice in ("yes", "no"):
            value = payload()
            value["answers"]["decision"].update(choice=choice, probabilities={"yes": .5, "no": .5})
            self.assertEqual(parse_choices(value, QUESTIONS)["answers"]["decision"]["choice"], choice)
        value = payload()
        value["answers"]["decision"]["probabilities"] = {"yes": .8, "no": .18}
        result = parse_choices(value, QUESTIONS)
        self.assertEqual(result["answers"]["decision"]["probabilities"]["no"], .18)

    def test_http_failures_timeout_size_and_no_retries(self):
        for status, code in ((401,"auth_failed"),(403,"auth_failed"),(429,"rate_limited"),(529,"overloaded"),
                             (302,"redirect_refused"),(400,"http_error"),(422,"http_error"),(500,"http_error")):
            response, post = http_response(status=status), Mock()
            post.return_value = response
            with self.subTest(status=status), self.assertRaises(ClassifierError) as context:
                bounded_post(JEV_URL, "test", {}, timeout_seconds=1, post=post)
            self.assertEqual(context.exception.code, code)
            post.assert_called_once()
            response.close.assert_called_once()
            response.iter_content.assert_not_called()
        for error, code in ((requests.Timeout("secret"), "timeout"), (requests.ConnectionError("secret"), "connection_error")):
            with self.assertRaises(ClassifierError) as context:
                bounded_post(JEV_URL, "test", {}, timeout_seconds=1, post=Mock(side_effect=error))
            self.assertEqual(str(context.exception), code)
        response = http_response(raw=b"x"*(MAX_BYTES+1))
        with self.assertRaises(ClassifierError) as context:
            bounded_post(JEV_URL, "test", {}, timeout_seconds=1, post=Mock(return_value=response))
        self.assertEqual(context.exception.code, "response_too_large")
        with self.assertRaises(ClassifierError) as context:
            bounded_post(JEV_URL, "test", {}, timeout_seconds=1, post=Mock(return_value=http_response()), clock=Mock(side_effect=[0,2]))
        self.assertEqual(context.exception.code, "timeout")

    def test_json_and_local_request_bounds(self):
        for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b'\xef\xbb\xbf{}',b'['*20+b'0'+b']'*20,b'[] trailing',b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(ClassifierError):
                decode(raw)
        with self.assertRaises(ModelConfigurationError):
            bounded_post("https://example.invalid", "test", {}, timeout_seconds=1)
        for key in ("", "bad\nkey", "密钥"):
            with self.assertRaises(ModelConfigurationError):
                JevConfig(key)
        post = Mock()
        with self.assertRaises(ModelConfigurationError):
            JevClassifier(JevConfig("test"), post=post).classify({"x": "x"*MAX_BYTES}, QUESTIONS)
        post.assert_not_called()

    def test_streaming_read_timeout_is_timeout_and_response_is_closed(self):
        from urllib3.exceptions import ReadTimeoutError
        for error, code in (
            (requests.ConnectionError(ReadTimeoutError(None, None, "artificial timeout")), "timeout"),
            (requests.ConnectionError("artificial disconnect"), "connection_error"),
        ):
            response = http_response()
            response.iter_content.side_effect = error
            with self.subTest(code=code), self.assertRaises(ClassifierError) as caught:
                bounded_post(JEV_URL, "test", {}, timeout_seconds=1, post=Mock(return_value=response))
            self.assertEqual(caught.exception.code, code)
            response.close.assert_called_once()
