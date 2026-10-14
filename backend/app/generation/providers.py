"""Model provider interface and an OpenAI-compatible HTTP implementation (Hugging Face router by default).

Secrets: the API key is only ever placed in the Authorization header. It is never logged, returned or put in an
exception message. Error messages shown to users never include provider response bodies.
"""
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger("app.generation")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # a reply is bounded by max_tokens; anything far larger is not a model reply


class ProviderError(Exception):
    """A model-provider failure. `message` is safe to show to users."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ImageInput:
    media_type: str
    data: bytes
    filename: str


class GenerationProvider(Protocol):
    def complete(self, *, model: str, system: str, user_text: str, images: list[ImageInput]) -> str: ...


def _tls_context():
    """Trust the operating system's certificate store (corporate networks re-sign TLS with their own root CA, which
    Python's bundled certifi list does not contain). Falls back to the default bundle if truststore is not installed."""
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        max_output_tokens: int,
        client: httpx.Client | None = None,
    ):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._max_tokens = max_output_tokens
        self._deadline_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=10), verify=_tls_context())

    def complete(self, *, model: str, system: str, user_text: str, images: list[ImageInput]) -> str:
        if not self._key.strip():
            raise ProviderError("ai_not_configured", "No AI provider token is configured. Set AI_API_KEY (or HF_TOKEN) in .env.")
        content: list[dict] = [{"type": "text", "text": user_text}]
        for img in images:  # the image bytes themselves go to the model, not just a name or size
            b64 = base64.b64encode(img.data).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{img.media_type};base64,{b64}"}})
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "max_tokens": self._max_tokens,
            "temperature": 0.2,
        }
        # Streamed so the reply size and the total wall-clock time are bounded (httpx timeouts apply per read, not overall).
        try:
            with self._client.stream("POST", self._url, json=body, headers={"Authorization": f"Bearer {self._key}"}) as resp:
                status = resp.status_code
                payload = self._read_limited(resp) if status < 400 else b""
        except httpx.TimeoutException as exc:
            raise ProviderError("ai_timeout", "The AI provider took too long to respond. Try again.", True) from exc
        except httpx.HTTPError as exc:
            logger.warning("AI provider connection error: %s", type(exc).__name__)
            raise ProviderError("ai_unavailable", "The AI provider could not be reached. Try again shortly.", True) from exc

        if status in (401, 403):
            raise ProviderError("ai_auth_failed", "The AI provider rejected the configured token. Check AI_API_KEY / HF_TOKEN.")
        if status == 429:
            raise ProviderError("ai_rate_limited", "The AI provider rate limit was reached. Wait a moment and retry.", True)
        if status >= 500:
            raise ProviderError("ai_unavailable", "The AI provider reported a server error. Try again shortly.", True)
        if status == 413:
            raise ProviderError(
                "ai_request_too_large",
                "The request was too large for the AI provider (the current project code plus the new screenshots exceed its "
                "size or per-minute token limit). Use fewer or smaller screenshots, or a shorter prompt, and try again.",
            )
        if status >= 400:
            logger.warning("AI provider rejected the request: HTTP %s (model=%s)", status, model)
            raise ProviderError(
                "ai_request_rejected",
                f"The AI provider rejected the request (HTTP {status}). Check the configured model ID and that it accepts "
                "images when screenshots are used.",
            )
        try:
            choice = json.loads(payload)["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("ai_bad_response", "The AI provider returned an unexpected response.", True) from exc
        if choice.get("finish_reason") == "length":  # a cut-off reply would yield silently truncated source files
            raise ProviderError(
                "ai_output_truncated",
                "The model reply hit the output limit and was cut off. Try a simpler request or raise AI_MAX_OUTPUT_TOKENS.",
            )
        if isinstance(text, list):  # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("ai_empty_response", "The AI provider returned an empty reply.", True)
        return text

    def _read_limited(self, resp: httpx.Response) -> bytes:
        deadline = time.monotonic() + self._deadline_seconds
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ProviderError("ai_bad_response", "The AI provider returned an unexpectedly large response.", True)
            if time.monotonic() > deadline:
                raise ProviderError("ai_timeout", "The AI provider took too long to respond. Try again.", True)
            chunks.append(chunk)
        return b"".join(chunks)
