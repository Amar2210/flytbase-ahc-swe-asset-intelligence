"""Small Gemma REST adapters. No API key or raw provider error is persisted."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
import json
import math
import random
import socket
import sqlite3
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import uuid

from copilot.rate_limits import RateLimitError, TokenBudget


class ModelError(RuntimeError):
    """An actionable, secret-free provider error safe to show in the UI."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 retryable: bool = False, attempts: int = 1):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.attempts = attempts


_TRANSIENT_HTTP = {408, 500, 502, 503, 504}
_SAFE_PROVIDER_STATUSES = {
    "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED",
    "INVALID_ARGUMENT", "UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND",
    "FAILED_PRECONDITION", "UNKNOWN", "CANCELLED",
}


def _provider_status(error: HTTPError) -> str | None:
    """Keep an allowlisted error code, never the raw body/message or request URL."""
    try:
        details = json.loads(error.read(65_536).decode("utf-8"))
        status = details.get("error", {}).get("status")
        return status if status in _SAFE_PROVIDER_STATUSES else None
    except (ValueError, OSError, AttributeError, TypeError):
        return None
    finally:
        error.close()


def _phase(payload: dict) -> str:
    if any("functionResponse" in part for content in payload.get("contents", [])
           for part in content.get("parts", []) if isinstance(part, dict)):
        return " while reading retrieved tool results"
    if any(m.get("role") == "tool" for m in payload.get("messages", [])):
        return " while reading retrieved tool results"
    return ""


@dataclass
class ModelReply:
    content: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)


class GemmaClient:
    def __init__(self, api_key: str, provider: str = "google",
                 model: str = "gemma-4-31b-it", timeout: int = 90,
                 max_output_tokens: int = 4096, max_input_tokens: int = 12000,
                 max_http_attempts: int = 3, rate_limit_db_path: str | None = None,
                 google_tpm_limit: int = 16000, google_tpm_headroom: int = 1500,
                 google_quota_group: str = "default-project"):
        self.api_key = api_key.strip()
        self.provider = provider
        self.model = model.strip().removeprefix("models/")
        self.timeout = max(5, min(int(timeout), 180))
        self.max_output_tokens = max_output_tokens
        self.max_input_tokens = max(256, int(max_input_tokens))
        self.max_http_attempts = max(1, min(3, int(max_http_attempts)))
        # Keep numeric usage only. Prompts, keys and provider error bodies are
        # deliberately excluded because the UI persists these records in chats.
        self.request_metrics: list[dict[str, Any]] = []
        self.metrics_callback: Callable[[list[dict[str, Any]]], None] | None = None
        self._active_metric: dict[str, Any] | None = None
        self.rate_limiter = None
        self._active_reservation = None
        if not self.api_key:
            setting = "OPENROUTER_API_KEY" if provider == "openrouter" else "GOOGLE_API_KEY (or GEMINI_API_KEY)"
            raise ModelError(f"Set {setting} in .env to enable Gemma chat.")
        if provider == "google":
            if not self.model.startswith("gemma-") or "/" in self.model:
                raise ModelError("Choose a hosted Gemma model ID, for example gemma-4-31b-it.")
        elif provider == "openrouter":
            if "/gemma-" not in self.model or not self.model.endswith(":free"):
                raise ModelError("Use a free Gemma model ID such as google/gemma-4-31b-it:free.")
        else:
            raise ModelError("Choose Google AI Studio or OpenRouter.")
        if provider == "google" and rate_limit_db_path is not None:
            try:
                self.rate_limiter = TokenBudget(rate_limit_db_path, provider=provider,
                    model=self.model, quota_group=google_quota_group,
                    limit=google_tpm_limit, headroom=google_tpm_headroom)
            except RateLimitError as exc:
                raise ModelError(str(exc), attempts=0) from None
            except (sqlite3.Error, OSError):
                raise ModelError("The shared TPM ledger is unavailable. Check that the local database is writable.", attempts=0) from None

    def _reserve_attempt(self) -> None:
        if self.rate_limiter is None:
            return
        metric = self._active_metric
        previous_wait = float(metric.get("rate_limit_wait_seconds", 0)) if metric else 0.0
        def progress(values):
            if metric is not None:
                metric.update(values)
                metric["rate_limit_wait_seconds"] = round(previous_wait + values["rate_limit_wait_seconds"], 2)
                metric["status"] = "queued" if values["rate_limit_status"] == "queued" else "pending"
                self._notify_metrics()
        try:
            self._active_reservation = self.rate_limiter.reserve(
                metric.get("input_tokens") if metric else None, progress)
        except RateLimitError as exc:
            if metric is not None:
                metric["failure_kind"] = "local_tpm_budget"
                metric["rate_limit_status"] = "blocked"
            raise ModelError(str(exc), attempts=metric.get("inference_http_attempts", 0) if metric else 0) from None
        except (sqlite3.Error, OSError):
            if metric is not None:
                metric["failure_kind"] = "local_tpm_ledger"
            raise ModelError("The shared TPM ledger is unavailable. No generation request was sent for this attempt. Retry when the local database is writable.",
                             attempts=metric.get("inference_http_attempts", 0) if metric else 0) from None

    @staticmethod
    def _retry_after(error: HTTPError) -> float:
        """Accept only a bounded number/date header; never expose its contents."""
        value = error.headers.get("Retry-After", "") if error.headers else ""
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError, AttributeError):
                return 60.0
        return max(1.0, min(seconds, 3600.0)) if math.isfinite(seconds) else 60.0

    def _cooldown(self, seconds: float) -> None:
        if self.rate_limiter is None:
            return
        try:
            self.rate_limiter.cooldown(seconds)
        except (sqlite3.Error, OSError):
            if self._active_metric is not None:
                self._active_metric["rate_limit_issue"] = "The local cooldown could not be saved."

    def _post_json(self, url: str, headers: dict, payload: dict) -> dict:
        request = Request(url, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json", **headers}, method="POST")
        provider = "Google AI Studio" if self.provider == "google" else "OpenRouter"
        phase = _phase(payload)
        deadline = time.monotonic() + self.timeout
        max_attempts = self.max_http_attempts
        for attempt in range(1, max_attempts + 1):
            if self.rate_limiter is not None:
                before_wait = time.monotonic()
                self._reserve_attempt()
                # Queue time does not consume the network generation budget.
                deadline += max(0.0, time.monotonic() - before_wait)
            if self._active_metric is not None:
                self._active_metric["inference_http_attempts"] = attempt
            remaining = max(0.1, deadline - time.monotonic())
            status_code, provider_status = None, None
            try:
                with urlopen(request, timeout=min(self.timeout, remaining)) as response:
                    result = json.loads(response.read(8_000_000).decode("utf-8"))
                    if not isinstance(result, dict):
                        raise ModelError("The provider returned an unreadable response. Retry the request.")
                    if self._active_metric is not None:
                        self._active_metric.pop("failure_kind", None)
                    return result
            except HTTPError as exc:
                status_code = int(exc.code)
                if status_code == 429 and self.rate_limiter is not None:
                    self._cooldown(self._retry_after(exc))
                    if self._active_metric is not None:
                        self._active_metric["rate_limit_status"] = "provider_cooldown"
                provider_status = _provider_status(exc)
                transient = status_code in _TRANSIENT_HTTP
                if self._active_metric is not None:
                    self._active_metric["failure_kind"] = f"HTTP {status_code}" + (f" {provider_status}" if provider_status else "")
            except (socket.timeout, TimeoutError):
                transient = True
                if self._active_metric is not None:
                    self._active_metric["failure_kind"] = "timeout"
            except (URLError, ConnectionError, IncompleteRead):
                transient = True
                if self._active_metric is not None:
                    self._active_metric["failure_kind"] = "network"
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ModelError("The provider returned an unreadable response. Retry the request.", attempts=attempt) from None
            # Retry only inference HTTP requests. Tool execution is outside this
            # adapter, so a retried synthesis request cannot repeat a crew write.
            # A 429 can mean a depleted daily free quota: leave that to the user.
            delay = (0.8 * 2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
            remaining = deadline - time.monotonic()
            if transient and attempt < max_attempts and remaining > delay + 0.1:
                time.sleep(delay)
                continue
            suffix = f" after {attempt} attempt{'s' if attempt != 1 else ''}"
            if status_code is not None:
                status = f" ({provider_status})" if provider_status else ""
                prefix = f"{provider} returned HTTP {status_code}{status}{phase}{suffix}. "
                guidance = {
                    400: "Check the model ID and its tool-calling support.",
                    401: "The API key was not accepted. Check the provider key in .env.",
                    403: "This API key does not have access to the selected model or project.",
                    404: "The selected model is unavailable. Check its configured ID in .env.",
                    429: "A rate limit was reached. This can be an account limit or upstream provider capacity; it does not establish that your daily free quota is exhausted. Retry later or choose another available free Gemma endpoint. This request was not automatically retried.",
                }.get(status_code, "The model service is temporarily unavailable. Retry shortly; if this persists, try the other configured free Gemma model." if transient else "Check the provider status and model configuration before retrying.")
                raise ModelError(prefix + guidance, status_code=status_code,
                                 retryable=transient, attempts=attempt) from None
            timed_out = self._active_metric is not None and self._active_metric.get("failure_kind") == "timeout"
            guidance = (f"The {provider} request timed out{phase}{suffix} within its {self.timeout}-second generation budget. "
                        "This may be provider latency or a network timeout. Try a smaller request or retry later."
                        if timed_out else f"Could not reach {provider}{phase}{suffix}. Check your network and retry.")
            raise ModelError(guidance,
                             retryable=True, attempts=attempt) from None

    @staticmethod
    def _token_count(value: Any) -> int | None:
        """Do not turn absent/invalid usage into an invented zero or estimate."""
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _notify_metrics(self) -> None:
        if callable(self.metrics_callback):
            try:
                self.metrics_callback(deepcopy(self.request_metrics))
            except Exception:
                # A progress display must never break a model response or a
                # grounded local fallback. The persisted metrics remain usable.
                pass

    def _count_google_tokens(self, payload: dict) -> tuple[int | None, str | None]:
        """Count the full generation input, once, independently of inference.

        The contents-only API omits system instructions and tool declarations.
        Google's generateContentRequest form accepts the exact generation body
        plus its model resource name, so tool replies, selected SOP passages and
        private history are all measured with the same tokenizer. This endpoint
        does not generate an answer. When shared pacing is enabled, an unavailable
        count prevents generation because the input cannot be reserved safely.
        """
        body = {"generateContentRequest": {"model": "models/" + self.model, **payload}}
        request = Request(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + quote(self.model, safe="") + ":countTokens",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        try:
            # A single bounded metadata request: no inference retry loop here.
            with urlopen(request, timeout=min(self.timeout, 10)) as response:
                result = json.loads(response.read(1_000_000).decode("utf-8"))
            count = self._token_count(result.get("totalTokens")) if isinstance(result, dict) else None
            return (count, None) if count is not None else (None, "The tokenizer returned no valid count.")
        except HTTPError as exc:
            code = int(exc.code)
            if code == 429 and self.rate_limiter is not None:
                self._cooldown(self._retry_after(exc))
            _provider_status(exc)  # Consume/close safely; never save raw details.
            return None, f"Token counting unavailable (HTTP {code})."
        except (URLError, socket.timeout, TimeoutError, ConnectionError, IncompleteRead, OSError):
            return None, "Token counting unavailable (network error)."
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "The tokenizer returned an unreadable count."

    def _record_google_usage(self, response: dict) -> None:
        metric = self._active_metric
        usage = response.get("usageMetadata", {})
        if metric is None or not isinstance(usage, dict):
            return
        prompt = self._token_count(usage.get("promptTokenCount"))
        if prompt is not None:
            metric.update(input_tokens=prompt, input_token_source="usageMetadata", measured=True)
            if self.rate_limiter is not None and self._active_reservation is not None:
                try:
                    self.rate_limiter.reconcile(self._active_reservation, prompt)
                except (sqlite3.Error, OSError):
                    metric["rate_limit_issue"] = "The local reservation could not be updated from returned usage."
                if prompt > self._active_reservation.tokens:
                    metric["rate_limit_reserved_tokens"] = prompt
                    metric["rate_limit_window_reserved_tokens"] = metric.get("rate_limit_window_reserved_tokens", 0) + prompt - self._active_reservation.tokens
        for field, target in (
            ("candidatesTokenCount", "output_tokens"),
            ("totalTokenCount", "total_tokens"),
            ("thoughtsTokenCount", "thought_tokens"),
            ("cachedContentTokenCount", "cached_input_tokens"),
            ("toolUsePromptTokenCount", "tool_use_prompt_tokens"),
        ):
            count = self._token_count(usage.get(field))
            if count is not None:
                metric[target] = count

    def _record_openrouter_usage(self, response: dict) -> None:
        metric = self._active_metric
        usage = response.get("usage", {})
        if metric is None or not isinstance(usage, dict):
            return
        prompt = self._token_count(usage.get("prompt_tokens"))
        if prompt is not None:
            metric.update(input_tokens=prompt, input_token_source="usageMetadata", measured=True)
        for field, target in (("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
            count = self._token_count(usage.get(field))
            if count is not None:
                metric[target] = count

    @staticmethod
    def _google_contents(messages: list[dict]) -> tuple[str, list[dict]]:
        instructions, contents = [], []
        for message in messages:
            role = message["role"]
            if role == "system":
                instructions.append(str(message.get("content", "")))
                continue
            if role == "tool":
                try:
                    result = json.loads(message.get("content", "{}"))
                except json.JSONDecodeError:
                    result = {"text": message.get("content", "")}
                function_response = {"name": message["name"], "response": {"result": result}}
                if message.get("provider_id"):
                    function_response["id"] = message["provider_id"]
                part = {"functionResponse": function_response}
                if contents and contents[-1]["role"] == "user" and message.get("_consecutive_tool"):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue
            if role == "assistant" and message.get("google_parts"):
                contents.append({"role": "model", "parts": message["google_parts"]})
                continue
            parts: list[dict] = []
            if message.get("content"):
                parts.append({"text": str(message["content"])})
            if role == "assistant":
                for call in message.get("tool_calls", []):
                    function = call["function"]
                    args = function.get("arguments", "{}")
                    parts.append({"functionCall": {"name": function["name"],
                                                   "args": json.loads(args) if isinstance(args, str) else args}})
            if parts:
                contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
        return "\n\n".join(instructions), contents

    def complete(self, messages: list[dict], definitions: list[dict]) -> ModelReply:
        metric = {"provider": self.provider, "model": self.model,
                  "input_tokens": None, "input_token_source": "unavailable",
                  "measured": False, "output_tokens": None, "total_tokens": None,
                  "inference_http_attempts": 0, "status": "pending",
                  "input_token_budget": self.max_input_tokens}
        self.request_metrics.append(metric)
        self._active_metric = metric
        self._active_reservation = None
        try:
            reply = self._google(messages, definitions) if self.provider == "google" else self._openrouter(messages, definitions)
            metric["status"] = "completed"
            return reply
        except Exception:
            metric["status"] = "failed"
            raise
        finally:
            self._active_metric = None
            self._notify_metrics()

    def _google(self, messages: list[dict], definitions: list[dict]) -> ModelReply:
        instructions, contents = self._google_contents(messages)
        payload = {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": self.max_output_tokens},
        }
        if definitions:
            # FunctionDeclaration.parameters is Google's restricted Schema object.
            # Our tools use full JSON Schema, including nullable/union values.
            payload["tools"] = [{"functionDeclarations": [
                {"name": definition["name"], "description": definition["description"],
                 "parametersJsonSchema": definition["parameters"]}
                for definition in definitions
            ]}]
        count, count_error = self._count_google_tokens(payload)
        metric = self._active_metric
        if metric is not None:
            if count is not None:
                metric.update(input_tokens=count, input_token_source="countTokens", measured=True)
            if count_error:
                metric["count_error"] = count_error
            self._notify_metrics()
        if count is not None and count > self.max_input_tokens:
            if metric is not None:
                metric["failure_kind"] = "input_budget"
            raise ModelError(
                f"This request contains {count:,} input tokens, above the configured "
                f"{self.max_input_tokens:,}-token input budget. No generation request was sent. "
                "Narrow the question or retrieve smaller evidence batches. "
                "This is the app's request-size limit, not a measured provider TPM rejection.",
                attempts=0,
            )
        response = self._post_json(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + quote(self.model, safe="") + ":generateContent",
            {"x-goog-api-key": self.api_key}, payload,
        )
        self._record_google_usage(response)
        candidates = response.get("candidates", [])
        if not candidates:
            raise ModelError("The model returned no answer. Try a shorter question or check the provider's model availability.")
        parts = candidates[0].get("content", {}).get("parts", [])
        calls, text = [], []
        for part in parts:
            if "text" in part and not part.get("thought"):
                text.append(part["text"])
            if "functionCall" in part:
                call = part["functionCall"]
                args = call.get("args", {})
                if not isinstance(args, dict):
                    raise ModelError("The model returned invalid tool arguments. Please retry.")
                calls.append({"id": call.get("id") or "call_" + uuid.uuid4().hex,
                              "provider_id": call.get("id"), "name": call["name"], "arguments": args})
        return ModelReply("\n".join(text), calls,
                          {"role": "assistant", "content": "\n".join(text), "google_parts": parts})

    def _openrouter(self, messages: list[dict], definitions: list[dict]) -> ModelReply:
        clean_messages = []
        for message in messages:
            clean = {key: message[key] for key in ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning_details")
                     if key in message}
            clean_messages.append(clean)
        payload = {"model": self.model, "messages": clean_messages,
                   "temperature": 0.1, "max_tokens": self.max_output_tokens}
        if definitions:
            payload["tools"] = [{"type": "function", "function": definition} for definition in definitions]
        self._notify_metrics()
        response = self._post_json("https://openrouter.ai/api/v1/chat/completions",
                                   {"Authorization": "Bearer " + self.api_key,
                                    "X-OpenRouter-Title": "Asset Intelligence Copilot"}, payload)
        self._record_openrouter_usage(response)
        choices = response.get("choices", [])
        if not choices or choices[0].get("error"):
            raise ModelError("The selected provider returned no usable answer. Retry or choose another free Gemma endpoint.")
        message = choices[0].get("message", {})
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        calls = []
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            try:
                raw_args = function.get("arguments", "{}")
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(arguments, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                raise ModelError("The model returned invalid tool arguments. Please retry.") from None
            calls.append({"id": call.get("id") or "call_" + uuid.uuid4().hex,
                          "name": function["name"], "arguments": arguments})
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                       for c in calls]
        if message.get("reasoning_details"):
            assistant["reasoning_details"] = message["reasoning_details"]
        return ModelReply(content, calls, assistant)
