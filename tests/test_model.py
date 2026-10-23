"""OpenRouter protocol tests use fake responses, never a real API key."""

import io
import json
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from copilot.model import COMPLETIONS_URL, DEFAULT_MODEL, OpenRouterClient, ModelError


def provider_error(code, message="Provider detail must stay private", headers=None):
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    return HTTPError("https://provider/?key=private-key-never-print", code,
                     "raw provider detail", headers or {}, io.BytesIO(body))


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def answer(text="Answer from retrieved records", **extra):
    return response({"choices": [{"message": {"role": "assistant", "content": text}}], **extra})


class ModelTests(unittest.TestCase):
    def test_only_openrouter_request_default_model_and_native_tools(self):
        client = OpenRouterClient("test-secret")
        definition = {"name": "get_well", "description": "Read well", "parameters": {
            "type": "object", "properties": {"asset_id": {"type": "string"}, "extra": {"type": ["string", "null"]}}}}
        reply = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "abc", "function": {"name": "get_well", "arguments": '{"asset_id":"WELL-0001"}'}}]}}]}
        with patch("copilot.model.urlopen", return_value=response(reply)) as post:
            result = client.complete([{"role": "user", "content": "Read this well"}], [definition])
        self.assertEqual(post.call_count, 1)
        request = post.call_args.args[0]
        self.assertEqual(request.full_url, COMPLETIONS_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(payload["tools"][0]["function"], definition)
        self.assertEqual(result.calls[0]["arguments"], {"asset_id": "WELL-0001"})
        self.assertEqual(result.assistant_message["tool_calls"][0]["id"], "abc")
        self.assertNotIn("test-secret", json.dumps(payload))
        self.assertFalse(hasattr(client, "rate_limiter"))

    def test_parallel_tool_ids_nullable_schema_and_reasoning_survive_followup(self):
        client = OpenRouterClient("test-secret")
        details = [{"type": "reasoning.encrypted", "data": "opaque"}]
        first = {"choices": [{"message": {"content": "", "reasoning": "Do not display hidden reasoning",
                    "reasoning_details": details, "tool_calls": [
                        {"id": "read-a", "function": {"name": "get_well", "arguments": '{"asset_id":"WELL-0001"}'}},
                        {"id": "read-b", "function": {"name": "get_document", "arguments": {"path": "fleet/sops/notice.md"}}}]}}]}
        messages = [{"role": "system", "content": "Policy"}, {"role": "user", "content": "Read evidence"}]
        with patch("copilot.model.urlopen", side_effect=[response(first), answer("Grounded")]) as post:
            reply = client.complete(messages, [])
            self.assertEqual(reply.content, "")
            messages.append(reply.assistant_message)
            messages.extend([{"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                              "content": '{"result":"evidence"}', "internal_private_flag": "not a provider field"}
                             for call in reply.calls])
            client.complete(messages, [])
        payload = json.loads(post.call_args.args[0].data)
        self.assertEqual(payload["messages"][2]["reasoning_details"], details)
        self.assertEqual([item["tool_call_id"] for item in payload["messages"][-2:]], ["read-a", "read-b"])
        self.assertEqual([item["name"] for item in payload["messages"][-2:]], ["get_well", "get_document"])
        self.assertNotIn("internal_private_flag", json.dumps(payload))
        self.assertNotIn("Do not display hidden reasoning", json.dumps(payload))

    def test_invalid_provider_tools_are_rejected_safely(self):
        invalid = [
            {"function": {"name": "get_well", "arguments": "[1,2]"}},
            {"function": {"name": "get_well", "arguments": "not JSON"}},
            {"function": {"name": "get_well", "arguments": "null"}},
            {"function": {"name": "get_well", "arguments": True}},
            {"function": {"arguments": "{}"}}, {"function": {"name": "", "arguments": "{}"}},
            {"function": None}, "not a call object",
        ]
        for call in invalid:
            with self.subTest(call=call), patch("copilot.model.urlopen", return_value=response({
                    "choices": [{"message": {"tool_calls": [call]}}]})):
                with self.assertRaisesRegex(ModelError, "invalid tool"):
                    OpenRouterClient("secret").complete([{"role": "user", "content": "Read"}], [])

    def test_unconfigured_or_paid_endpoint_is_not_used(self):
        with self.assertRaisesRegex(ModelError, r"OPENROUTER_API_KEY.*\.env"):
            OpenRouterClient("")
        with self.assertRaises(ModelError):
            OpenRouterClient("secret", "google/gemma-4-31b-it")
        with self.assertRaises(TypeError):
            OpenRouterClient("secret", provider="google")

    def test_transient_failures_retry_with_backoff_and_identical_payload(self):
        with patch("copilot.model.urlopen", side_effect=[provider_error(500), provider_error(502), answer()]) as post, \
                patch("copilot.model.time.sleep") as sleep, patch("copilot.model.random.uniform", return_value=0):
            result = OpenRouterClient("private-key-never-print").complete([{"role": "user", "content": "Read status"}], [])
        self.assertEqual(result.content, "Answer from retrieved records")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.8, 1.6])
        payloads = [call.args[0].data for call in post.call_args_list]
        self.assertTrue(all(payload == payloads[0] for payload in payloads))
        self.assertNotIn(b"private-key-never-print", payloads[0])

    def test_exhausted_tool_followup_error_is_safe_and_keeps_ids(self):
        messages = [{"role": "user", "content": "Overview"},
                    {"role": "assistant", "tool_calls": [{"id": "query-1", "type": "function", "function": {
                        "name": "query_records", "arguments": '{"sql":"SELECT count(*) FROM wells"}'}}]},
                    {"role": "tool", "name": "query_records", "tool_call_id": "query-1", "content": '{"rows":[{"wells":200}]}'}]
        with patch("copilot.model.urlopen", side_effect=[provider_error(503) for _ in range(3)]) as post, \
                patch("copilot.model.time.sleep"):
            with self.assertRaises(ModelError) as raised:
                OpenRouterClient("private-key-never-print").complete(messages, [])
        error = raised.exception
        self.assertEqual((error.status_code, error.attempts, error.retryable), (503, 3, True))
        self.assertIn("reading retrieved tool results", str(error))
        self.assertNotIn("private", str(error))
        self.assertNotIn("Provider detail", str(error))
        self.assertEqual(json.loads(post.call_args.args[0].data)["messages"][-1]["tool_call_id"], "query-1")

    def test_single_attempt_diagnostic_does_not_retry(self):
        client = OpenRouterClient("test-secret", max_http_attempts=1)
        with patch("copilot.model.urlopen", side_effect=provider_error(503)) as post, \
                patch("copilot.model.time.sleep") as sleep:
            with self.assertRaises(ModelError) as raised:
                client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertEqual(client.request_metrics[0]["inference_http_attempts"], 1)
        sleep.assert_not_called()

    def test_http_429_and_invalid_requests_never_queue_or_retry(self):
        for code in (429, 400, 401, 403, 404):
            client = OpenRouterClient("private-key-never-print")
            with self.subTest(code=code), patch("copilot.model.urlopen", side_effect=provider_error(
                    code, "private-key-never-print", {"Retry-After": "999999"})) as post, \
                    patch("copilot.model.time.sleep") as sleep:
                with self.assertRaises(ModelError) as raised:
                    client.complete([{"role": "user", "content": "Read"}], [])
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual((raised.exception.status_code, raised.exception.retryable), (code, False))
                self.assertNotIn("private", str(raised.exception))
                self.assertFalse(any(key.startswith("rate_limit_") for key in client.request_metrics[0]))
                if code == 429:
                    self.assertIn("not automatically retried", str(raised.exception))

    def test_network_and_gateway_timeouts_recover(self):
        for errors in ([socket.timeout("private URL"), URLError("private network detail")], [provider_error(504)]):
            with self.subTest(errors=len(errors)), patch("copilot.model.urlopen", side_effect=[*errors, answer("Recovered")]) as post, \
                    patch("copilot.model.time.sleep"):
                result = OpenRouterClient("secret").complete([{"role": "user", "content": "Read"}], [])
            self.assertEqual(result.content, "Recovered")
            self.assertEqual(post.call_count, len(errors) + 1)

    def test_retry_budget_is_bounded_by_original_timeout(self):
        with patch("copilot.model.time.monotonic", side_effect=[0, 0, 10]), \
                patch("copilot.model.urlopen", side_effect=provider_error(500)) as post, \
                patch("copilot.model.time.sleep") as sleep:
            with self.assertRaises(ModelError) as raised:
                OpenRouterClient("secret", timeout=5).complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(raised.exception.attempts, 1)
        sleep.assert_not_called()
        self.assertEqual(OpenRouterClient("secret", timeout=999, max_http_attempts=99).timeout, 180)
        self.assertEqual(OpenRouterClient("secret", max_http_attempts=99).max_http_attempts, 3)

    def test_generation_timeout_is_distinguished_from_network_rejection(self):
        client = OpenRouterClient("secret", timeout=5)
        with patch("copilot.model.urlopen", side_effect=socket.timeout()), \
                patch("copilot.model.time.monotonic", side_effect=[0, 0, 10]):
            with self.assertRaisesRegex(ModelError, "timed out.*5-second"):
                client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(client.request_metrics[0]["failure_kind"], "timeout")

    def test_unreadable_responses_are_safe(self):
        for data in (b"not JSON", b"[]", b'\xff'):
            with self.subTest(data=data), patch("copilot.model.urlopen", return_value=io.BytesIO(data)) as post:
                with self.assertRaisesRegex(ModelError, "unreadable response"):
                    OpenRouterClient("secret").complete([{"role": "user", "content": "Read"}], [])
                self.assertEqual(post.call_count, 1)
        for value in ({}, {"choices": [None]}, {"choices": [{"message": None}]},
                      {"choices": [{"message": {"content": {"private": "details"}}}]}):
            with self.subTest(value=value), patch("copilot.model.urlopen", return_value=response(value)):
                with self.assertRaises(ModelError) as raised:
                    OpenRouterClient("secret").complete([{"role": "user", "content": "Read"}], [])
                self.assertNotIn("private", str(raised.exception))


class TokenTelemetryTests(unittest.TestCase):
    def test_request_size_guard_is_labelled_as_estimate_and_never_calls_provider(self):
        for where in ("system", "tool", "definition"):
            messages = [{"role": "user", "content": "Read"}]
            definitions = []
            if where == "definition":
                definitions = [{"name": "read", "description": "x" * 2000, "parameters": {"type": "object"}}]
            else:
                messages.insert(0, {"role": where, "content": "é" * 1000})
            client = OpenRouterClient("secret", max_input_tokens=256)
            with self.subTest(where=where), patch("copilot.model.urlopen") as post:
                with self.assertRaisesRegex(ModelError, "request-size estimate.*256") as raised:
                    client.complete(messages, definitions)
                post.assert_not_called()
                self.assertEqual(raised.exception.attempts, 0)
                metric = client.request_metrics[0]
                self.assertEqual(metric["failure_kind"], "input_budget")
                self.assertEqual(metric["inference_http_attempts"], 0)
                self.assertIsNone(metric["input_tokens"])
                self.assertFalse(metric["measured"])
                self.assertGreater(metric["input_token_estimate"], 256)

    def test_returned_usage_is_measured_but_estimate_never_masquerades_as_count(self):
        client = OpenRouterClient("secret", max_output_tokens=1234)
        snapshots = []
        client.metrics_callback = snapshots.append
        with patch("copilot.model.urlopen", return_value=answer(usage={"prompt_tokens": 301, "completion_tokens": 8, "total_tokens": 309})) as post:
            client.complete([{"role": "system", "content": "Policy and schema"}, {"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(json.loads(post.call_args.args[0].data)["max_tokens"], 1234)
        self.assertIsNone(snapshots[0][0]["input_tokens"])
        self.assertFalse(snapshots[0][0]["measured"])
        metric = client.request_metrics[0]
        self.assertEqual((metric["input_tokens"], metric["output_tokens"], metric["total_tokens"]), (301, 8, 309))
        self.assertEqual(metric["input_token_source"], "provider_usage")
        self.assertTrue(metric["measured"])
        self.assertNotIn("secret", json.dumps(client.request_metrics))
        self.assertNotIn("Policy and schema", json.dumps(client.request_metrics))

    def test_missing_invalid_usage_stays_unavailable_not_zero(self):
        for invalid in (None, True, "123", -1, 1.5):
            client = OpenRouterClient("secret")
            with self.subTest(invalid=invalid), patch("copilot.model.urlopen", return_value=answer(
                    usage={"prompt_tokens": invalid, "completion_tokens": invalid, "total_tokens": invalid})):
                client.complete([{"role": "user", "content": "Read"}], [])
                metric = client.request_metrics[0]
                self.assertIsNone(metric["input_tokens"])
                self.assertIsNone(metric["output_tokens"])
                self.assertFalse(metric["measured"])
        with patch("copilot.model.urlopen", return_value=answer(usage={"prompt_tokens": 0})):
            client = OpenRouterClient("secret")
            client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(client.request_metrics[0]["input_tokens"], 0)
        self.assertTrue(client.request_metrics[0]["measured"])

    def test_failed_inference_keeps_unknown_usage_and_real_attempt_count(self):
        client = OpenRouterClient("private-key-never-print")
        with patch("copilot.model.urlopen", side_effect=[provider_error(503) for _ in range(3)]) as post, \
                patch("copilot.model.time.sleep"):
            with self.assertRaises(ModelError):
                client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 3)
        self.assertIsNone(client.request_metrics[0]["input_tokens"])
        self.assertEqual(client.request_metrics[0]["inference_http_attempts"], 3)
        self.assertEqual(client.request_metrics[0]["status"], "failed")
        self.assertNotIn("private", json.dumps(client.request_metrics))

    def test_each_round_has_detached_metrics_and_broken_callback_cannot_block(self):
        client = OpenRouterClient("secret")
        snapshots = []
        def callback(metrics):
            snapshots.append(metrics)
            metrics[0]["input_tokens"] = 999999
        client.metrics_callback = callback
        with patch("copilot.model.urlopen", side_effect=[answer("First", usage={"prompt_tokens": 100}),
                                                         answer("Second", usage={"prompt_tokens": 250})]):
            client.complete([{"role": "user", "content": "Read"}], [])
            client.complete([{"role": "user", "content": "Read again"}], [])
        self.assertEqual([metric["input_tokens"] for metric in client.request_metrics], [100, 250])
        self.assertEqual(len(snapshots[0]), 1)
        self.assertEqual(len(snapshots[-1]), 2)
        client.metrics_callback = lambda _: (_ for _ in ()).throw(RuntimeError("UI unavailable"))
        with patch("copilot.model.urlopen", return_value=answer("OK")):
            self.assertEqual(client.complete([{"role": "user", "content": "Hi"}], []).content, "OK")


if __name__ == "__main__":
    unittest.main()
