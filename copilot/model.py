"""OpenRouter chat completions. No API key or raw provider error is persisted."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from http.client import IncompleteRead
import json
import math
import random
import socket
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


DEFAULT_MODEL = "google/gemma-4-31b-it:free"
COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


class ModelError(RuntimeError):
    """An actionable, secret-free provider error safe to show in the UI."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 retryable: bool = False, attempts: int = 1):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.attempts = attempts


_TRANSIENT_HTTP = {408, 500, 502, 503, 504}


def _close_error(error: HTTPError) -> None:
    """Discard raw provider details, which may contain URLs or credentials."""
    try:
        error.read(65_536)
    except (OSError, AttributeError):
        pass
    finally:
        error.close()


def _phase(payload: dict) -> str:
    if any(message.get("role") == "tool" for message in payload.get("messages", [])):
        return " while reading retrieved tool results"
    return ""


@dataclass
class ModelReply:
    content: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)


class OpenRouterClient:
    provider = "openrouter"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: int = 90,
                 max_output_tokens: int = 4096, max_input_tokens: int = 12000,
                 max_http_attempts: int = 3):
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = max(5, min(int(timeout), 180))
        self.max_output_tokens = max(1, int(max_output_tokens))
        self.max_input_tokens = max(256, int(max_input_tokens))
        self.max_http_attempts = max(1, min(3, int(max_http_attempts)))
        # Persist metadata only, never prompts, keys or raw provider errors.
        self.request_metrics: list[dict[str, Any]] = []
        self.metrics_callback: Callable[[list[dict[str, Any]]], None] | None = None
        self._active_metric: dict[str, Any] | None = None
        if not self.api_key:
            raise ModelError("Set OPENROUTER_API_KEY in .env to enable chat.")
        if "/gemma-" not in self.model or not self.model.endswith(":free"):
            raise ModelError("Use a free Gemma model ID such as google/gemma-4-31b-it:free.")

    def _post_json(self, payload: dict) -> dict:
        request = Request(COMPLETIONS_URL, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json",
                                   "Authorization": "Bearer " + self.api_key,
                                   "X-OpenRouter-Title": "Asset Intelligence Copilot"}, method="POST")
        phase = _phase(payload)
        deadline = time.monotonic() + self.timeout
        for attempt in range(1, self.max_http_attempts + 1):
            if self._active_metric is not None:
                self._active_metric["inference_http_attempts"] = attempt
            remaining = max(0.1, deadline - time.monotonic())
            status_code = None
            try:
                with urlopen(request, timeout=min(self.timeout, remaining)) as response:
                    result = json.loads(response.read(8_000_000).decode("utf-8"))
                    if not isinstance(result, dict):
                        raise ModelError("The provider returned an unreadable response. Retry the request.", attempts=attempt)
                    if self._active_metric is not None:
                        self._active_metric.pop("failure_kind", None)
                    return result
            except HTTPError as exc:
                status_code = int(exc.code)
                _close_error(exc)
                transient = status_code in _TRANSIENT_HTTP
                if self._active_metric is not None:
                    self._active_metric["failure_kind"] = f"HTTP {status_code}"
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
            # Retry only inference, never tool writes. A real HTTP 429 returns
            # promptly; no local queue or persisted cooldown is introduced.
            delay = (0.8 * 2 ** (attempt - 1)) + random.uniform(0.0, 0.25)
            remaining = deadline - time.monotonic()
            if transient and attempt < self.max_http_attempts and remaining > delay + 0.1:
                time.sleep(delay)
                continue
            suffix = f" after {attempt} attempt{'s' if attempt != 1 else ''}"
            if status_code is not None:
                guidance = {
                    400: "Check the model ID, request size and its tool-calling support.",
                    401: "The API key was not accepted. Check OPENROUTER_API_KEY in .env.",
                    403: "This API key does not have access to the selected model.",
                    404: "The selected model is unavailable. Check COPILOT_MODEL in .env.",
                    429: "A rate limit was reached. This can be an OpenRouter account limit or upstream capacity; it does not establish that your daily free quota is exhausted. Retry later. This request was not automatically retried.",
                }.get(status_code, "The model service is temporarily unavailable. Retry shortly; if this persists, check OpenRouter availability." if transient else "Check OpenRouter status and the model configuration before retrying.")
                raise ModelError(f"OpenRouter returned HTTP {status_code}{phase}{suffix}. " + guidance,
                                 status_code=status_code, retryable=transient, attempts=attempt) from None
            timed_out = self._active_metric is not None and self._active_metric.get("failure_kind") == "timeout"
            guidance = (f"The OpenRouter request timed out{phase}{suffix} within its {self.timeout}-second generation budget. "
                        "This may be provider latency or a network timeout. Try a smaller request or retry later."
                        if timed_out else f"Could not reach OpenRouter{phase}{suffix}. Check your network and retry.")
            raise ModelError(guidance, retryable=True, attempts=attempt) from None

    @staticmethod
    def _token_count(value: Any) -> int | None:
        """Do not turn absent/invalid usage into an invented zero or estimate."""
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _notify_metrics(self) -> None:
        if callable(self.metrics_callback):
            try:
                self.metrics_callback(deepcopy(self.request_metrics))
            except Exception:
                # Telemetry callbacks must not interrupt an answer or fallback.
                pass

    def _record_usage(self, response: dict) -> None:
        metric = self._active_metric
        usage = response.get("usage", {})
        if metric is None or not isinstance(usage, dict):
            return
        prompt = self._token_count(usage.get("prompt_tokens"))
        if prompt is not None:
            metric.update(input_tokens=prompt, input_token_source="provider_usage", measured=True)
        for field, target in (("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
            count = self._token_count(usage.get(field))
            if count is not None:
                metric[target] = count

    def complete(self, messages: list[dict], definitions: list[dict]) -> ModelReply:
        metric = {"provider": self.provider, "model": self.model,
                  "input_tokens": None, "input_token_source": "unavailable",
                  "measured": False, "output_tokens": None, "total_tokens": None,
                  "inference_http_attempts": 0, "status": "pending",
                  "input_token_budget": self.max_input_tokens}
        self.request_metrics.append(metric)
        self._active_metric = metric
        try:
            clean_messages = [{key: message[key] for key in
                               ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning_details")
                               if key in message} for message in messages]
            payload = {"model": self.model, "messages": clean_messages,
                       "temperature": 0.1, "max_tokens": self.max_output_tokens}
            if definitions:
                payload["tools"] = [{"type": "function", "function": definition} for definition in definitions]
            # OpenRouter returns actual token usage after generation. This is a
            # rough UTF-8 request-size guard (4 bytes/token), NOT a tokenizer or
            # an exact model-context limit. Include instructions/history/tools.
            input_bytes = len(json.dumps({key: payload[key] for key in ("messages", "tools") if key in payload},
                                        ensure_ascii=False).encode("utf-8"))
            estimate = math.ceil(input_bytes / 4)
            metric.update(input_size_bytes=input_bytes, input_token_estimate=estimate)
            self._notify_metrics()
            if estimate > self.max_input_tokens:
                metric["failure_kind"] = "input_budget"
                raise ModelError(
                    f"The request-size estimate is {estimate:,} tokens, above the configured "
                    f"{self.max_input_tokens:,}-token input budget. No generation request was sent. "
                    "Narrow the question or retrieve smaller evidence batches. This local byte-based estimate "
                    "is not a provider token count or TPM measurement.", attempts=0)
            response = self._post_json(payload)
            self._record_usage(response)
            reply = self._parse_reply(response)
            metric["status"] = "completed"
            return reply
        except Exception:
            metric["status"] = "failed"
            raise
        finally:
            self._active_metric = None
            self._notify_metrics()

    @staticmethod
    def _parse_reply(response: dict) -> ModelReply:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or choices[0].get("error"):
            raise ModelError("OpenRouter returned no usable answer. Retry or check the selected model's availability.")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ModelError("The provider returned an unreadable answer. Please retry.")
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
        if not isinstance(content, str):
            raise ModelError("The provider returned an unreadable answer. Please retry.")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ModelError("The model returned invalid tool arguments. Please retry.")
        calls = []
        for call in raw_calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
                raise ModelError("The model returned an invalid tool name. Please retry.")
            try:
                raw_args = function.get("arguments", "{}")
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(arguments, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                raise ModelError("The model returned invalid tool arguments. Please retry.") from None
            identifier = call.get("id")
            calls.append({"id": identifier if isinstance(identifier, str) and identifier else "call_" + uuid.uuid4().hex,
                          "name": function["name"], "arguments": arguments})
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [{"id": call["id"], "type": "function",
                                        "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
                                       for call in calls]
        if message.get("reasoning_details"):
            assistant["reasoning_details"] = message["reasoning_details"]
        return ModelReply(content, calls, assistant)
