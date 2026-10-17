"""Provider protocol tests use local fake responses, never a real API key."""

import io
import json
import socket
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

from copilot.model import GemmaClient, ModelError
from copilot.rate_limits import TokenBudget


def provider_error(code, status="INTERNAL", message="Provider detail must stay private"):
    body = json.dumps({"error": {"code": code, "status": status, "message": message}}).encode()
    return HTTPError("https://provider/?key=private-key-never-print", code,
                     "raw provider detail", {}, io.BytesIO(body))


def google_answer(text="Answer from retrieved records"):
    return io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]}).encode())


class CapturingClient(GemmaClient):
    def _post_json(self, url, headers, payload):
        self.sent = (url, headers, payload)
        return self.response


class ModelTests(unittest.TestCase):
    def setUp(self):
        # Protocol/retry tests stub the optional metadata boundary separately.
        # Their urlopen sequences therefore represent inference requests only.
        count = patch.object(GemmaClient, "_count_google_tokens", return_value=(None, "Disabled in offline protocol test."))
        count.start()
        self.addCleanup(count.stop)

    def test_openrouter_native_tool_arguments_and_free_model(self):
        client = CapturingClient("test-secret", "openrouter", "google/gemma-4-31b-it:free")
        client.response = {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "abc", "function": {"name": "get_well", "arguments": '{"asset_id":"WELL-0001"}'}}]}}]}
        definition = {"name": "get_well", "description": "Read well", "parameters": {"type": "object"}}
        result = client.complete([{"role": "user", "content": "Read this well"}], [definition])
        self.assertEqual(result.calls[0]["arguments"], {"asset_id": "WELL-0001"})
        self.assertEqual(result.assistant_message["tool_calls"][0]["id"], "abc")
        self.assertEqual(client.sent[2]["tools"][0]["function"], definition)
        self.assertNotIn("test-secret", json.dumps(client.sent[2]))

    def test_google_hidden_thought_is_not_an_answer_and_signature_is_preserved(self):
        client = CapturingClient("test-secret")
        parts = [{"thought": True, "text": "hidden reasoning"},
                 {"functionCall": {"id": "g1", "name": "get_well", "args": {"asset_id": "WELL-0001"}}, "thoughtSignature": "opaque"}]
        client.response = {"candidates": [{"content": {"parts": parts}}]}
        reply = client.complete([{"role": "user", "content": "Status?"}], [])
        self.assertEqual(reply.content, "")
        _, contents = client._google_contents([reply.assistant_message])
        self.assertEqual(contents[0]["parts"], parts)
        self.assertEqual(reply.calls[0]["provider_id"], "g1")

    def test_provider_errors_do_not_expose_secrets_or_raw_body(self):
        client = GemmaClient("private-key-never-print")
        error = HTTPError("https://provider/?key=private-key-never-print", 429, "private error detail", {}, None)
        with patch("copilot.model.urlopen", side_effect=error):
            with self.assertRaises(ModelError) as raised:
                client.complete([{"role": "user", "content": "Status?"}], [])
        self.assertIn("rate limit", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_invalid_provider_tool_arguments_are_not_executed(self):
        client = CapturingClient("secret", "openrouter", "google/gemma-4-31b-it:free")
        client.response = {"choices": [{"message": {"tool_calls": [
            {"id": "bad", "function": {"name": "get_well", "arguments": "[1,2]"}}]}}]}
        with self.assertRaises(ModelError):
            client.complete([{"role": "user", "content": "Read"}], [])

    def test_unconfigured_or_paid_endpoint_is_not_used(self):
        with self.assertRaisesRegex(ModelError, r"GOOGLE_API_KEY.*\.env"):
            GemmaClient("")
        with self.assertRaisesRegex(ModelError, r"OPENROUTER_API_KEY.*\.env"):
            GemmaClient("", "openrouter", "google/gemma-4-31b-it:free")
        with self.assertRaises(ModelError):
            GemmaClient("secret", "openrouter", "google/gemma-4-31b-it")

    def test_transient_failures_retry_inference_with_backoff_and_identical_payload(self):
        client = GemmaClient("private-key-never-print")
        messages = [{"role": "user", "content": "Read well status"}]
        with patch("copilot.model.urlopen", side_effect=[provider_error(500), provider_error(502), google_answer()]) as post, \
                patch("copilot.model.time.sleep") as sleep, patch("copilot.model.random.uniform", return_value=0):
            result = client.complete(messages, [])
        self.assertEqual(result.content, "Answer from retrieved records")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.8, 1.6])
        payloads = [call.args[0].data for call in post.call_args_list]
        self.assertTrue(all(payload == payloads[0] for payload in payloads))
        self.assertNotIn(b"private-key-never-print", payloads[0])

    def test_exhausted_tool_followup_error_exposes_safe_status_and_preserves_protocol(self):
        client = GemmaClient("private-key-never-print")
        messages = [{"role": "user", "content": "Field overview"},
                    {"role": "assistant", "google_parts": [{"functionCall": {"id": "query-1", "name": "query_records", "args": {"sql": "SELECT count(*) FROM wells"}}}]},
                    {"role": "tool", "name": "query_records", "provider_id": "query-1", "content": '{"result":{"rows":[{"wells":200}]}}'}]
        with patch("copilot.model.urlopen", side_effect=[provider_error(503, "UNAVAILABLE") for _ in range(3)]) as post, \
                patch("copilot.model.time.sleep"):
            with self.assertRaises(ModelError) as raised:
                client.complete(messages, [])
        error = raised.exception
        self.assertEqual(error.status_code, 503)
        self.assertEqual(error.attempts, 3)
        self.assertTrue(error.retryable)
        self.assertIn("HTTP 503 (UNAVAILABLE)", str(error))
        self.assertIn("reading retrieved tool results", str(error))
        self.assertNotIn("private", str(error))
        self.assertNotIn("Provider detail", str(error))
        payload = json.loads(post.call_args.args[0].data)
        tool_response = payload["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(tool_response["id"], "query-1")
        self.assertEqual(tool_response["name"], "query_records")

    def test_single_attempt_diagnostic_does_not_retry_transient_error(self):
        client = GemmaClient("test-secret", "openrouter", "google/gemma-4-31b-it:free", max_http_attempts=1)
        with patch("copilot.model.urlopen", side_effect=provider_error(503, "UNAVAILABLE")) as post, \
                patch("copilot.model.time.sleep") as sleep:
            with self.assertRaises(ModelError) as raised:
                client.complete([{"role": "user", "content": "Read recall evidence"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(raised.exception.attempts, 1)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(client.request_metrics[0]["inference_http_attempts"], 1)
        sleep.assert_not_called()

    def test_quota_and_invalid_requests_are_not_automatically_retried(self):
        for code, status in [(429, "RESOURCE_EXHAUSTED"), (400, "INVALID_ARGUMENT"), (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED")]:
            with self.subTest(code=code), patch("copilot.model.urlopen", side_effect=provider_error(code, status)) as post, \
                    patch("copilot.model.time.sleep") as sleep:
                with self.assertRaises(ModelError) as raised:
                    GemmaClient("secret").complete([{"role": "user", "content": "Read"}], [])
                self.assertEqual(post.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(raised.exception.status_code, code)
                self.assertFalse(raised.exception.retryable)

    def test_network_and_gateway_timeouts_recover_without_reexecuting_tools(self):
        with patch("copilot.model.urlopen", side_effect=[socket.timeout("private URL"), URLError("private network detail"), google_answer("Recovered")]) as post, \
                patch("copilot.model.time.sleep"):
            result = GemmaClient("secret").complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(result.content, "Recovered")
        self.assertEqual(post.call_count, 3)
        with patch("copilot.model.urlopen", side_effect=[provider_error(504, "DEADLINE_EXCEEDED"), google_answer("Gateway recovered")]) as post, \
                patch("copilot.model.time.sleep"):
            result = GemmaClient("secret").complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(result.content, "Gateway recovered")
        self.assertEqual(post.call_count, 2)

    def test_retry_budget_is_bounded_by_original_request_timeout(self):
        with patch("copilot.model.time.monotonic", side_effect=[0, 0, 10]), \
                patch("copilot.model.urlopen", side_effect=provider_error(500)) as post, \
                patch("copilot.model.time.sleep") as sleep:
            with self.assertRaises(ModelError) as raised:
                GemmaClient("secret", timeout=5).complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(raised.exception.attempts, 1)
        sleep.assert_not_called()

    def test_untrusted_provider_status_and_malformed_json_are_not_exposed(self):
        with patch("copilot.model.urlopen", side_effect=provider_error(400, "private-key-never-print", "secret body")):
            with self.assertRaises(ModelError) as raised:
                GemmaClient("private-key-never-print").complete([{"role": "user", "content": "Read"}], [])
        self.assertNotIn("private-key", str(raised.exception))
        self.assertNotIn("secret body", str(raised.exception))
        for response in (b"not JSON", b"[]"):
            with self.subTest(response=response), patch("copilot.model.urlopen", return_value=io.BytesIO(response)) as post:
                with self.assertRaisesRegex(ModelError, "unreadable response"):
                    GemmaClient("secret").complete([{"role": "user", "content": "Read"}], [])
                self.assertEqual(post.call_count, 1)


class TokenTelemetryTests(unittest.TestCase):
    @staticmethod
    def response(value):
        return io.BytesIO(json.dumps(value).encode())

    def test_oversized_measured_input_stops_before_generation(self):
        client = GemmaClient("secret", max_input_tokens=12000)
        with patch("copilot.model.urlopen", return_value=self.response({"totalTokens": 18862})) as post:
            with self.assertRaisesRegex(ModelError, "18,862.*12,000") as raised:
                client.complete([{"role": "user", "content": "Read source evidence"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].full_url.endswith(":countTokens"))
        self.assertEqual(raised.exception.attempts, 0)
        metric = client.request_metrics[0]
        self.assertEqual(metric["inference_http_attempts"], 0)
        self.assertEqual(metric["failure_kind"], "input_budget")
        self.assertEqual(metric["input_tokens"], 18862)
        self.assertEqual(metric["status"], "failed")

    def test_generation_timeout_is_distinguished_from_network_rejection(self):
        client = GemmaClient("secret", timeout=5)
        with patch.object(client, "_count_google_tokens", return_value=(100, None)), \
                patch("copilot.model.urlopen", side_effect=socket.timeout()), \
                patch("copilot.model.time.monotonic", side_effect=[0, 0, 10]):
            with self.assertRaisesRegex(ModelError, "timed out.*5-second"):
                client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(client.request_metrics[0]["failure_kind"], "timeout")

    def test_full_request_count_includes_system_history_tools_and_sop_results(self):
        client = GemmaClient("private-key-never-print")
        snapshots = []
        client.metrics_callback = snapshots.append
        messages = [
            {"role": "system", "content": "System policy plus complete SQLite schema"},
            {"role": "user", "content": "Earlier private user discussion"},
            {"role": "assistant", "content": "Earlier private answer"},
            {"role": "user", "content": "What does the SOP say about this well?"},
            {"role": "assistant", "google_parts": [{"functionCall": {
                "id": "doc-1", "name": "get_document", "args": {"path": "spill_sop.md"}},
                "thoughtSignature": "opaque-signature"}]},
            {"role": "tool", "name": "get_document", "provider_id": "doc-1",
             "content": json.dumps({"result": {"text": "Qualified spill correction SOP passage", "rows": [{"asset_id": "WELL-0007"}]}})},
        ]
        definition = {"name": "get_document", "description": "Read the matching complete SOP",
                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}
        answer = {"candidates": [{"content": {"parts": [{"text": "Grounded answer"}]}}],
                  "usageMetadata": {"promptTokenCount": 108, "candidatesTokenCount": 22,
                                    "totalTokenCount": 130, "cachedContentTokenCount": 5,
                                    "thoughtsTokenCount": 0, "toolUsePromptTokenCount": 0}}
        with patch("copilot.model.urlopen", side_effect=[self.response({"totalTokens": 101}), self.response(answer)]) as post:
            reply = client.complete(messages, [definition])
        self.assertEqual(reply.content, "Grounded answer")
        self.assertEqual(post.call_count, 2)
        count_request, generation_request = [call.args[0] for call in post.call_args_list]
        self.assertTrue(count_request.full_url.endswith("/models/gemma-4-31b-it:countTokens"))
        self.assertTrue(generation_request.full_url.endswith(":generateContent"))
        counted = json.loads(count_request.data)
        generated = json.loads(generation_request.data)
        self.assertEqual(set(counted), {"generateContentRequest"})
        full = counted["generateContentRequest"]
        self.assertEqual(full.pop("model"), "models/gemma-4-31b-it")
        self.assertEqual(full, generated)
        self.assertEqual(full["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"], definition["parameters"])
        self.assertIn("complete SQLite schema", full["systemInstruction"]["parts"][0]["text"])
        self.assertIn("Earlier private user discussion", json.dumps(full["contents"]))
        self.assertIn("Qualified spill correction SOP passage", json.dumps(full["contents"]))
        self.assertEqual(post.call_args_list[0].kwargs["timeout"], 10)
        self.assertEqual(snapshots[0][0]["input_tokens"], 101)
        self.assertEqual(snapshots[0][0]["input_token_source"], "countTokens")
        self.assertEqual(snapshots[0][0]["status"], "pending")
        metric = client.request_metrics[0]
        self.assertEqual(metric["input_tokens"], 108)
        self.assertEqual(metric["input_token_source"], "usageMetadata")
        self.assertEqual(metric["output_tokens"], 22)
        self.assertEqual(metric["total_tokens"], 130)
        self.assertEqual(metric["cached_input_tokens"], 5)
        self.assertTrue(metric["measured"])
        self.assertEqual(metric["inference_http_attempts"], 1)
        self.assertEqual(metric["status"], "completed")
        self.assertEqual(snapshots[-1], client.request_metrics)
        self.assertNotIn("private-key-never-print", json.dumps(client.request_metrics))
        self.assertNotIn("Qualified spill correction", json.dumps(client.request_metrics))

    def test_failed_count_never_blocks_or_retries_generation(self):
        for error in [provider_error(429, "RESOURCE_EXHAUSTED"), provider_error(404, "NOT_FOUND"), socket.timeout("private URL")]:
            with self.subTest(error=type(error).__name__):
                client = GemmaClient("secret", timeout=5)
                with patch("copilot.model.urlopen", side_effect=[error, google_answer("Answer still generated")]) as post, \
                        patch("copilot.model.time.sleep") as sleep:
                    reply = client.complete([{"role": "user", "content": "Status"}], [])
                self.assertEqual(reply.content, "Answer still generated")
                self.assertEqual(post.call_count, 2)
                self.assertEqual(post.call_args_list[0].kwargs["timeout"], 5)
                sleep.assert_not_called()
                metric = client.request_metrics[0]
                self.assertIsNone(metric["input_tokens"])
                self.assertFalse(metric["measured"])
                self.assertEqual(metric["input_token_source"], "unavailable")
                self.assertIn("unavailable", metric["count_error"])
                self.assertEqual(metric["inference_http_attempts"], 1)
                self.assertEqual(metric["status"], "completed")
                self.assertNotIn("private", json.dumps(metric))

    def test_count_survives_failed_inference_and_records_real_retry_attempts(self):
        client = GemmaClient("private-key-never-print")
        with patch("copilot.model.urlopen", side_effect=[self.response({"totalTokens": 456}),
                   provider_error(503), provider_error(503), provider_error(503)]) as post, \
                patch("copilot.model.time.sleep"):
            with self.assertRaises(ModelError):
                client.complete([{"role": "user", "content": "Read"}], [])
        metric = client.request_metrics[0]
        self.assertEqual(post.call_count, 4)  # One tokenizer request; three inference attempts.
        self.assertEqual(metric["input_tokens"], 456)
        self.assertEqual(metric["input_token_source"], "countTokens")
        self.assertEqual(metric["inference_http_attempts"], 3)
        self.assertEqual(metric["status"], "failed")
        self.assertIsNone(metric["output_tokens"])
        self.assertNotIn("private", json.dumps(metric))

    def test_invalid_counts_are_unavailable_and_usage_metadata_can_recover_them(self):
        for invalid in [None, True, "123", -1, 1.5]:
            with self.subTest(invalid=invalid):
                client = GemmaClient("secret")
                answer = {"candidates": [{"content": {"parts": [{"text": "Answer"}]}}],
                          "usageMetadata": {"promptTokenCount": 222, "candidatesTokenCount": 3, "totalTokenCount": 225}}
                with patch("copilot.model.urlopen", side_effect=[self.response({"totalTokens": invalid}), self.response(answer)]):
                    client.complete([{"role": "user", "content": "Read"}], [])
                metric = client.request_metrics[0]
                self.assertEqual(metric["input_tokens"], 222)
                self.assertEqual(metric["input_token_source"], "usageMetadata")
                self.assertTrue(metric["measured"])

    def test_each_tool_round_has_its_own_count_and_callback_gets_detached_snapshots(self):
        client = GemmaClient("secret")
        snapshots = []
        def callback(metrics):
            snapshots.append(metrics)
            metrics[0]["input_tokens"] = 999999
        client.metrics_callback = callback
        responses = [self.response({"totalTokens": 100}), google_answer("First"),
                     self.response({"totalTokens": 250}), google_answer("Second")]
        with patch("copilot.model.urlopen", side_effect=responses) as post:
            client.complete([{"role": "user", "content": "Read"}], [])
            client.complete([{"role": "user", "content": "Read"},
                             {"role": "tool", "name": "query_records", "content": '{"rows":[{"total":200}]}'}], [])
        self.assertEqual(post.call_count, 4)
        self.assertEqual([metric["input_tokens"] for metric in client.request_metrics], [100, 250])
        self.assertEqual(len(snapshots[0]), 1)
        self.assertEqual(len(snapshots[-1]), 2)
        self.assertEqual([metric["status"] for metric in client.request_metrics], ["completed", "completed"])

    def test_broken_progress_callback_cannot_prevent_answer(self):
        client = GemmaClient("secret")
        client.metrics_callback = lambda _: (_ for _ in ()).throw(RuntimeError("UI unavailable"))
        with patch("copilot.model.urlopen", side_effect=[self.response({"totalTokens": 12}), google_answer("OK")]):
            self.assertEqual(client.complete([{"role": "user", "content": "Hi"}], []).content, "OK")
        self.assertEqual(client.request_metrics[0]["input_tokens"], 12)

    def test_openrouter_uses_returned_usage_without_google_tokenizer_request(self):
        client = GemmaClient("secret", "openrouter", "google/gemma-4-31b-it:free")
        response = {"choices": [{"message": {"role": "assistant", "content": "Answer"}}],
                    "usage": {"prompt_tokens": 301, "completion_tokens": 8, "total_tokens": 309}}
        with patch("copilot.model.urlopen", return_value=self.response(response)) as post:
            client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(client.request_metrics[0]["input_tokens"], 301)
        self.assertEqual(client.request_metrics[0]["input_token_source"], "usageMetadata")
        self.assertEqual(client.request_metrics[0]["output_tokens"], 8)


class SharedPacingTests(unittest.TestCase):
    """The adapter reserves every attempt; all time/network boundaries are fake."""

    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("paced-model-test-" + uuid4().hex)
        self.root.mkdir()
        self.db = self.root / "app.sqlite3"
        self.now = 1000.0
        self.sleeps = []

    def tearDown(self):
        if self.root.resolve().parent == Path(__file__).resolve().parent and self.root.name.startswith("paced-model-test-"):
            shutil.rmtree(self.root)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def client(self, **kwargs):
        client = GemmaClient("not-a-real-key", rate_limit_db_path=str(self.db), **kwargs)
        client.rate_limiter = TokenBudget(self.db, provider="google", model=client.model,
                                         clock=self.clock, sleep=self.sleep)
        return client

    def test_complete_rounds_wait_across_clients_without_consuming_generation_timeout(self):
        first, second = self.client(timeout=5), self.client(timeout=5)
        updates = []
        second.metrics_callback = updates.append
        with patch.object(first, "_count_google_tokens", return_value=(6000, None)), \
                patch.object(second, "_count_google_tokens", return_value=(11000, None)), \
                patch("copilot.model.time.monotonic", side_effect=self.clock), \
                patch("copilot.model.urlopen", side_effect=[google_answer(), google_answer()]) as post:
            first.complete([{"role": "user", "content": "First user question"}], [])
            second.complete([{"role": "user", "content": "Second user question"}], [])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["timeout"], 5)
        self.assertEqual(second.request_metrics[0]["rate_limit_wait_seconds"], 60)
        self.assertTrue(any(snapshot[0]["status"] == "queued" for snapshot in updates))
        self.assertEqual(second.request_metrics[0]["status"], "completed")

    def test_http_retry_reserves_again_and_keeps_failed_attempt_budget(self):
        client = self.client(timeout=5)
        with patch.object(client, "_count_google_tokens", return_value=(8000, None)), \
                patch("copilot.model.time.monotonic", side_effect=self.clock), \
                patch("copilot.model.time.sleep", side_effect=self.sleep), \
                patch("copilot.model.random.uniform", return_value=0), \
                patch("copilot.model.urlopen", side_effect=[provider_error(500), google_answer()]) as post:
            client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(client.request_metrics[0]["inference_http_attempts"], 2)
        self.assertGreater(client.request_metrics[0]["rate_limit_wait_seconds"], 59)
        self.assertGreater(post.call_args.kwargs["timeout"], 4)
        self.assertLessEqual(max(self.sleeps), 1)

    def test_provider_429_is_not_retried_and_sets_shared_retry_after_cooldown(self):
        client = self.client()
        error = provider_error(429, "RESOURCE_EXHAUSTED")
        error.headers["Retry-After"] = "120"
        with patch.object(client, "_count_google_tokens", return_value=(1000, None)), \
                patch("copilot.model.urlopen", side_effect=error) as post:
            with self.assertRaisesRegex(ModelError, "HTTP 429"):
                client.complete([{"role": "user", "content": "Read"}], [])
        self.assertEqual(post.call_count, 1)
        next_client = self.client()
        with patch.object(next_client, "_count_google_tokens", return_value=(1000, None)), \
                patch("copilot.model.urlopen", return_value=google_answer()):
            next_client.complete([{"role": "user", "content": "Next user"}], [])
        self.assertEqual(next_client.request_metrics[0]["rate_limit_wait_seconds"], 120)
        self.assertEqual(client.request_metrics[0]["rate_limit_status"], "provider_cooldown")

    def test_unknown_count_blocks_paced_generation_without_invented_zero(self):
        client = self.client()
        with patch.object(client, "_count_google_tokens", return_value=(None, "Unavailable")), \
                patch("copilot.model.urlopen") as post:
            with self.assertRaisesRegex(ModelError, "unknown input") as raised:
                client.complete([{"role": "user", "content": "Read"}], [])
        post.assert_not_called()
        self.assertEqual(raised.exception.attempts, 0)
        self.assertIsNone(client.request_metrics[0]["input_tokens"])
        self.assertEqual(client.request_metrics[0]["failure_kind"], "local_tpm_budget")

    def test_usage_above_reservation_is_reconciled_without_storing_prompt_or_key(self):
        client = self.client()
        response = {"candidates": [{"content": {"parts": [{"text": "Answer"}]}}],
                    "usageMetadata": {"promptTokenCount": 9000}}
        with patch.object(client, "_count_google_tokens", return_value=(1000, None)), \
                patch("copilot.model.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            client.complete([{"role": "user", "content": "Private content must stay out of ledger"}], [])
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute("SELECT * FROM model_token_reservations").fetchall()
        self.assertEqual(rows[0][-1], 9000)
        self.assertNotIn("Private content", str(rows))
        self.assertNotIn("not-a-real-key", str(rows))


if __name__ == "__main__":
    unittest.main()
