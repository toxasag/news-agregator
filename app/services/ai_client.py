"""Async OpenRouter client shared by dedup, content generation and SEO stages.

The client is deliberately thin: one retrying POST helper plus three typed
wrappers (`embed`, `embed_batch`, `complete`) and the strict `ask_yes_no`
arbiter used by level 2 of the deduplicator.

Two behaviours matter to callers:

* **Graceful degradation.** Every request path raises
  :class:`NoCredentialsError` (a :class:`AIClientError`) when
  ``OPENROUTER_API_KEY`` is unset, so services can catch a single exception
  type and fall back to their zero-token behaviour instead of crashing the
  pipeline. Constructing the client never raises; only using it does.
* **Early validation.** :meth:`AIClient.embed` verifies the vector width
  against ``AI_EMBEDDING_DIMENSIONS`` before returning. A wrong-width vector
  would otherwise be rejected by pgvector at INSERT time, far away from the
  model that produced it.

The API key is never logged and never interpolated into an exception message.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Final, Self, cast

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.ai_provider_key import AIProviderKey
from app.models.ai_provider_model import AIProviderModel
from app.services.gemini_key_pool import (
    GEMINI_BASE_URL,
    _decrypt_key,
    get_next_healthy_key,
    mark_key_cooldown,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: Bounded retry budget: one initial attempt plus three retries.
DEFAULT_MAX_ATTEMPTS: Final[int] = 4
INITIAL_BACKOFF_SECONDS: Final[float] = 0.5
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 20.0
#: Upper bound honoured for a server-supplied ``Retry-After``. A hostile or
#: buggy header must not park a worker for hours.
MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0

EMBEDDINGS_PATH: Final[str] = "/embeddings"
CHAT_COMPLETIONS_PATH: Final[str] = "/chat/completions"
MODELS_PATH: Final[str] = "/models"

#: Affirmative arbiter answers, matched after lowercasing and de-punctuating.
YES_TOKENS: Final[frozenset[str]] = frozenset({"yes", "y", "true", "да"})
#: Negative arbiter answers, matched after lowercasing and de-punctuating.
NO_TOKENS: Final[frozenset[str]] = frozenset({"no", "n", "false", "нет"})

#: Collapses punctuation, quotes and whitespace so `"YES."`, `**yes**` and
#: `«да»` all reduce to a bare token.
_NON_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[\W_]+", re.UNICODE)
_YO_FOLD: Final[dict[int, str]] = {0x0451: "\u0435"}

#: In-memory TTL cache for models missing upstream or unsupported by gateways.
#: Maps base_url:model -> expire_monotonic to avoid burning pool keys.
_DEAD_MODELS_CACHE: Final[dict[str, float]] = {}

_QUOTA_SIGNATURES: Final[tuple[str, ...]] = (
    "quota",
    "arrearage",
    "insufficient",
    "balance",
    "额度",
    "欠费",
    "good standing",
    "out of credit",
)

_AUTH_SIGNATURES: Final[tuple[str, ...]] = (
    "invalid api key",
    "unauthorized",
    "token expired",
    "forbidden",
    "bad token",
)

_MISSING_MODEL_SIGNATURES: Final[tuple[str, ...]] = (
    "not found",
    "unknown model",
    "no channel",
    "model_not_found",
    "does not exist",
    "unsupported model",
    "invalid model",
)


class AIClientError(RuntimeError):
    """Base class for every failure surfaced by :class:`AIClient`."""


class NoCredentialsError(AIClientError):
    """Raised when OpenRouter is not configured.

    Callers catch this to degrade gracefully: the pipeline must stay fully
    usable at zero AI spend, per docs/04_FILTER_AND_DEDUP.md.
    """


class AITransportError(AIClientError):
    """Network-level failure (timeout, DNS, connection reset)."""


class AIStatusError(AIClientError):
    """Non-2xx HTTP response from OpenRouter."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class AIRateLimitError(AIStatusError):
    """HTTP 429: quota or rate limit exhausted after the retry budget."""


class AIResponseError(AIClientError):
    """A 2xx response whose payload is unusable (bad shape or vector width)."""


def _is_retryable_status(status_code: int) -> bool:
    """Whether a status code justifies another attempt.

    Only 429 and 5xx are retried: any other 4xx is a deterministic client
    error (bad model name, malformed payload, revoked key) that would fail
    identically on every attempt.
    """
    return status_code == httpx.codes.TOO_MANY_REQUESTS or 500 <= status_code < 600


def _retry_after_seconds(header_value: str | None) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP-date.

    Returns ``None`` when the header is absent or unparseable, in which case
    the caller falls back to exponential backoff.
    """
    if header_value is None:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        seconds = (deadline - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def parse_yes_no(answer: str) -> bool | None:
    """Parse a strict YES/NO answer, tolerating case, punctuation and Russian.

    Returns ``None`` when the answer cannot be classified, so the caller can
    apply its own safe default rather than guessing from a garbled reply.
    """
    folded = answer.lower().translate(_YO_FOLD)
    normalised = _NON_WORD_RE.sub(" ", folded).strip()
    if not normalised:
        return None
    if normalised in YES_TOKENS:
        return True
    if normalised in NO_TOKENS:
        return False
    first_word = normalised.split(" ", 1)[0]
    if first_word in YES_TOKENS:
        return True
    if first_word in NO_TOKENS:
        return False
    return None


def _error_message(payload: Any) -> str:
    """Extract a short, key-free description from an OpenRouter error body."""
    if isinstance(payload, Mapping):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message[:300]
    if isinstance(payload, str) and payload:
        return payload[:300]
    return "unspecified error"


def _build_gemini_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI-style chat payload to Google Gemini format."""
    contents: list[dict[str, Any]] = []
    system_text = ""
    for msg in list(payload.get("messages", [])):
        role = msg.get("role", "user")
        text = str(msg.get("content", ""))
        if role == "system":
            system_text += f"{text}\n"
        elif role in ("assistant", "model"):
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})

    if system_text and contents and contents[0].get("role") == "user":
        orig = contents[0]["parts"][0].get("text", "")
        contents[0]["parts"][0]["text"] = f"{system_text.strip()}\n\n{orig}"
    elif not contents and system_text:
        contents.append({"role": "user", "parts": [{"text": system_text.strip()}]})

    gen_cfg: dict[str, Any] = {}
    if "max_tokens" in payload:
        gen_cfg["maxOutputTokens"] = payload["max_tokens"]
    if "temperature" in payload:
        gen_cfg["temperature"] = payload["temperature"]
    resp_fmt = payload.get("response_format")
    if isinstance(resp_fmt, dict) and resp_fmt.get("type") == "json_object":
        gen_cfg["responseMimeType"] = "application/json"

    body: dict[str, Any] = {"contents": contents}
    if gen_cfg:
        body["generationConfig"] = gen_cfg
    return body


def _extract_gemini_text(gdata: Mapping[str, Any]) -> str:
    """Extract assistant textual content from Gemini generateContent response."""
    text_parts: list[str] = []
    candidates = gdata.get("candidates")
    if isinstance(candidates, list):
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            parts = cand.get("content", {}).get("parts", [])
            for part in parts:
                if isinstance(part, dict) and "text" in part and not part.get("thought"):
                    text_parts.append(str(part["text"]))
    return "".join(text_parts).strip()


def _is_gateway_model_dead(base_url: str, model: str) -> bool:
    clean_base = base_url.rstrip("/")
    if not clean_base.endswith("/v1") and "/v1/" not in clean_base:
        clean_base = f"{clean_base}/v1"
    dead_until = _DEAD_MODELS_CACHE.get(f"{clean_base}:{model}", 0.0)
    return dead_until > time.monotonic()


def _mark_gateway_model_dead(
    base_url: str, model: str, ttl_sec: float = 300.0
) -> None:
    clean_base = base_url.rstrip("/")
    if not clean_base.endswith("/v1") and "/v1/" not in clean_base:
        clean_base = f"{clean_base}/v1"
    _DEAD_MODELS_CACHE[f"{clean_base}:{model}"] = time.monotonic() + ttl_sec
    logger.warning("Gateway %s model %s dead for %ds", clean_base, model, int(ttl_sec))


async def _handle_provider_failure(
    session: Any,
    matched_key: Any,
    base_url: str,
    clean_model: str,
    *,
    p_label: str,
    status_code: int,
    err_text: str,
) -> None:
    """Analyze error response signatures and apply differentiated key cooldowns."""
    err_lower = err_text.lower()

    if status_code in (401, 403) or any(t in err_lower for t in _AUTH_SIGNATURES):
        await mark_key_cooldown(
            session,
            matched_key.id,
            f"Auth Error ({status_code}): {err_text[:180]}",
            seconds=86400,
            health="error",
        )
        raise AIStatusError(
            f"Provider {p_label} HTTP {status_code} Auth Error: {err_text[:180]}",
            status_code=status_code,
        )

    if status_code == 402 or any(t in err_lower for t in _QUOTA_SIGNATURES):
        await mark_key_cooldown(
            session,
            matched_key.id,
            f"Quota Exhausted: {err_text[:180]}",
            seconds=86400,
            health="error",
        )
        raise AIStatusError(
            f"Provider {p_label} Quota Exhausted: {err_text[:180]}",
            status_code=status_code,
        )

    if status_code in (400, 404, 503) and any(
        t in err_lower for t in _MISSING_MODEL_SIGNATURES
    ):
        _mark_gateway_model_dead(base_url, clean_model, 300.0)
        raise AIStatusError(
            f"Provider {p_label} model {clean_model} not supported: {err_text[:180]}",
            status_code=status_code,
        )

    cd_sec = 5 if status_code >= 500 else 10
    await mark_key_cooldown(
        session,
        matched_key.id,
        f"HTTP {status_code}: {err_text[:180]}",
        seconds=cd_sec,
        health="rate_limited",
    )
    raise AIStatusError(
        f"Provider {p_label} HTTP {status_code}: {err_text[:180]}",
        status_code=status_code,
    )


async def _stream_chat_fallback(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body_custom: dict[str, Any],
    *,
    clean_model: str,
    p_label: str,
    t0: float,
) -> tuple[dict[str, Any] | None, int, str]:
    """Fallback to Server-Sent Events stream for gateways that reject standard POST."""
    stream_body = dict(body_custom)
    stream_body["stream"] = True
    async with client.stream("POST", url, headers=headers, json=stream_body) as sres:
        if sres.status_code != 200:
            body_err = (await sres.aread()).decode("utf-8", errors="ignore")
            return None, sres.status_code, body_err
        lat_ms = int((time.monotonic() - t0) * 1000)
        chunks: list[str] = []
        async for line in sres.aiter_lines():
            clean_l = line.strip()
            if line.startswith("data: ") and clean_l != "data: [DONE]":
                with contextlib.suppress(Exception):
                    chunk_data = json.loads(line[6:].strip())
                    delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content") or ""
                    if content:
                        chunks.append(content)
        full_text = "".join(chunks)
        w_count = len(full_text.split())
        return (
            {
                "model": clean_model,
                "provider_name": p_label,
                "latency_ms": lat_ms,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": full_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": w_count,
                    "total_tokens": w_count,
                    "provider_name": p_label,
                    "latency_ms": lat_ms,
                    "model": clean_model,
                },
            },
            200,
            "",
        )


class AIClient:
    """Typed async wrapper around the OpenRouter HTTP API.

    The underlying :class:`httpx.AsyncClient` is created lazily on first use
    and reused for every subsequent call, so connection setup and TLS
    handshakes are paid once per process. Use :meth:`close` (or the async
    context manager) to release it.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_key = settings.openrouter_api_key if api_key is None else api_key
        self._api_key = raw_key.strip()
        self._base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        raw_timeout = settings.ai_request_timeout if timeout is None else timeout
        self._timeout = float(raw_timeout)
        self._max_attempts = max(1, max_attempts)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether an API key is available for this client instance."""
        return bool(self._api_key)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the pooled HTTP client, if one was ever created."""
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        if not self._api_key:
            raise NoCredentialsError(
                "OPENROUTER_API_KEY is not set: AI calls are unavailable and "
                "callers must degrade to their zero-token behaviour"
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(self._timeout),
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    transport=self._transport,
                )
            return self._client

    def _decode(self, response: httpx.Response, path: str) -> dict[str, Any]:
        """Decode a 2xx body, rejecting non-object and error payloads.

        Handles a server-sent-event stream as well as a plain JSON document,
        because some gateways stream unconditionally: this deployment's gateway
        answers ``text/event-stream`` even without ``stream: true``. Read as plain
        JSON, such a body yields only the first chunk — two characters, ``'{"'`` —
        which surfaced as "the model returned invalid JSON" while the model had
        answered perfectly.
        """
        if self._looks_like_sse(response):
            return self._decode_stream(response, path)
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise AIResponseError(
                f"{path} returned a non-JSON body "
                f"(status {response.status_code}, {len(response.content)} bytes)"
            ) from exc
        if not isinstance(payload, dict):
            raise AIResponseError(
                f"{path} returned {type(payload).__name__}, expected a JSON object"
            )
        error = payload.get("error")
        if error is not None:
            raise AIResponseError(f"{path} returned an error: {_error_message(error)}")
        return payload

    @staticmethod
    def _looks_like_sse(response: httpx.Response) -> bool:
        """Whether this body is a server-sent-event stream.

        The content type is checked first, then the payload prefix: a gateway that
        streams while advertising ``application/json`` is still a stream, and the
        prefix is unambiguous.
        """
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            return True
        return response.content[:6].lstrip().startswith(b"data:")

    def _decode_stream(
        self, response: httpx.Response, path: str
    ) -> dict[str, Any]:
        """Reassemble a streamed completion into the non-streaming shape.

        Concatenates the ``delta.content`` fragments and returns a payload with the
        same ``choices[0].message.content`` structure the rest of the client
        expects, so streaming stays invisible above this method.
        """
        content: list[str] = []
        finish_reason: str | None = None
        model = ""
        usage: dict[str, Any] | None = None

        for chunk in self._stream_chunks(response.text, path):
            error = chunk.get("error")
            if error is not None:
                raise AIResponseError(
                    f"{path} returned an error: {_error_message(error)}"
                )
            model = str(chunk.get("model") or model)
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            fragment, reason = _chunk_content(chunk)
            if fragment:
                content.append(fragment)
            finish_reason = reason or finish_reason

        if not content:
            raise AIResponseError(
                f"{path} streamed no content "
                f"({len(response.content)} bytes, finish_reason={finish_reason})"
            )

        payload: dict[str, Any] = {
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(content)},
                    "finish_reason": finish_reason,
                }
            ],
        }
        if usage is not None:
            payload["usage"] = usage
        return payload

    def _stream_chunks(self, body: str, path: str) -> list[dict[str, Any]]:
        """Parse the `data:` lines of an SSE body into chunk objects.

        A malformed chunk is skipped rather than fatal: the stream is a transport
        detail, and one bad line must not discard an otherwise complete answer.
        """
        chunks: list[dict[str, Any]] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except ValueError:
                logger.debug("%s: skipping an unparsable stream chunk", path)
                continue
            if isinstance(parsed, dict):
                chunks.append(parsed)
        return chunks

    def _status_error(self, response: httpx.Response, path: str) -> AIStatusError:
        """Build a typed error for a non-2xx response, without leaking the key."""
        detail = response.text[:300].replace("\n", " ").strip()
        body = detail or "no body"
        message = f"{path} failed with HTTP {response.status_code}: {body}"
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return AIRateLimitError(message, status_code=response.status_code)
        return AIStatusError(message, status_code=response.status_code)

    async def _get(self, path: str) -> dict[str, Any]:
        """GET ``path`` once.

        Deliberately not retried: the only caller is the model catalogue, which
        renders a settings page. Making an operator wait through a backoff ladder
        for an optional autocompletion list is the wrong trade.
        """
        client = await self._ensure_client()
        try:
            response = await client.get(path)
        except httpx.TimeoutException as exc:
            raise AITransportError(
                f"{path} timed out after {self._timeout:.0f}s ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:
            raise AITransportError(
                f"{path} transport failure: {type(exc).__name__}"
            ) from exc
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._status_error(response, path)
        return self._decode(response, path)

    async def _call_google_provider(
        self, session: Any, clean_model: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Dispatch to Google Gemini direct API with multi-key rotation."""
        max_attempts = 3
        last_exc: Exception | None = None
        for _ in range(max_attempts):
            key_id, raw_key = await get_next_healthy_key(session, "google")
            url = f"{GEMINI_BASE_URL}/models/{clean_model}:generateContent"
            headers = {"x-goog-api-key": raw_key, "Content-Type": "application/json"}
            body = _build_gemini_request(payload)

            t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=self._timeout) as h_client:
                res = await h_client.post(url, headers=headers, json=body)
                lat_ms = int((time.monotonic() - t0) * 1000)
                if res.status_code == 429:
                    await mark_key_cooldown(
                        session,
                        key_id,
                        "HTTP 429 Rate Limit",
                        seconds=15,
                        health="rate_limited",
                    )
                    last_exc = AIRateLimitError("Google 429", status_code=429)
                    continue

                if res.status_code != 200:
                    err_text = res.text
                    err_l = err_text.lower()
                    auth_errs = (
                        "api_key_invalid",
                        "api key not valid",
                        "consumer_suspended",
                        "permission denied",
                    )
                    if any(t in err_l for t in auth_errs):
                        await mark_key_cooldown(
                            session,
                            key_id,
                            f"Google Auth: {err_text[:180]}",
                            seconds=86400,
                            health="error",
                        )
                    elif any(t in err_l for t in ("quota", "resource_exhausted")):
                        await mark_key_cooldown(
                            session,
                            key_id,
                            f"Google Quota: {err_text[:180]}",
                            seconds=60,
                            health="rate_limited",
                        )
                    last_exc = AIStatusError(
                        f"Google HTTP {res.status_code}: {err_text[:180]}",
                        status_code=res.status_code,
                    )
                    continue

                gdata = res.json()
                res_text = _extract_gemini_text(gdata)
                usage_meta = gdata.get("usageMetadata", {})
                return {
                    "model": clean_model,
                    "provider_name": "Google Gemini",
                    "latency_ms": lat_ms,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": res_text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                        "total_tokens": usage_meta.get("totalTokenCount", 0),
                        "provider_name": "Google Gemini",
                        "latency_ms": lat_ms,
                        "model": clean_model,
                    },
                }
        if last_exc is not None:
            raise last_exc
        raise AIStatusError("Google Gemini attempts exhausted", status_code=500)

    async def _call_custom_provider(
        self,
        session: Any,
        matched_key: Any,
        clean_model: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Dispatch to an OpenAI-compatible custom gateway with streaming fallback."""
        raw_key = _decrypt_key(matched_key.api_key_encrypted)
        base_url = matched_key.base_url.rstrip("/")
        if not base_url.endswith("/v1") and "/v1/" not in base_url:
            base_url = f"{base_url}/v1"
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {raw_key}",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/event-stream",
        }
        body_custom = dict(payload)
        body_custom["model"] = clean_model
        p_label = matched_key.custom_name or matched_key.provider_name or "Custom"

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=self._timeout) as h_client:
            res = await h_client.post(url, headers=headers, json=body_custom)
            lat_ms = int((time.monotonic() - t0) * 1000)

            if res.status_code == 200:
                result_json = cast(dict[str, Any], res.json())
                result_json["provider_name"] = p_label
                result_json["latency_ms"] = lat_ms
                if "usage" in result_json and isinstance(result_json["usage"], dict):
                    result_json["usage"]["provider_name"] = p_label
                    result_json["usage"]["latency_ms"] = lat_ms
                    result_json["usage"]["model"] = clean_model
                else:
                    result_json["usage"] = {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "provider_name": p_label,
                        "latency_ms": lat_ms,
                        "model": clean_model,
                    }
                return result_json

            if res.status_code == 429:
                await mark_key_cooldown(
                    session,
                    matched_key.id,
                    "HTTP 429 Rate Limit",
                    seconds=15,
                    health="rate_limited",
                )
                raise AIRateLimitError("Provider 429 Rate Limit", status_code=429)

            if res.status_code in (500, 502, 503, 504):
                stream_res, s_code, s_err = await _stream_chat_fallback(
                    h_client,
                    url,
                    headers,
                    body_custom,
                    clean_model=clean_model,
                    p_label=p_label,
                    t0=t0,
                )
                if stream_res is not None:
                    return stream_res
                await _handle_provider_failure(
                    session,
                    matched_key,
                    base_url,
                    clean_model,
                    p_label=p_label,
                    status_code=s_code,
                    err_text=s_err,
                )

            await _handle_provider_failure(
                session,
                matched_key,
                base_url,
                clean_model,
                p_label=p_label,
                status_code=res.status_code,
                err_text=res.text,
            )
            raise AIStatusError(
                f"Provider {p_label} error {res.status_code}", status_code=res.status_code
            )

    async def _dispatch_custom_keys(
        self,
        provider_hint: str | None,
        clean_model: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Find matching custom provider keys and try them in priority order."""
        try:
            async with AsyncSessionLocal() as session:
                stmt = select(AIProviderKey).where(AIProviderKey.is_active.is_(True))
                if provider_hint:
                    stmt = stmt.where(
                        (AIProviderKey.custom_name == provider_hint)
                        | (AIProviderKey.provider_name == provider_hint)
                    )
                else:
                    stmt = (
                        stmt.join(
                            AIProviderModel,
                            AIProviderModel.provider_key_id == AIProviderKey.id,
                        )
                        .where(AIProviderModel.model_id == clean_model)
                        .order_by(
                            AIProviderModel.is_working.desc(),
                            (AIProviderKey.health == "healthy").desc(),
                            AIProviderKey.last_checked_at.desc().nullslast(),
                        )
                    )

                matched_keys = list((await session.execute(stmt)).scalars().all())
                last_exc: Exception | None = None
                for matched_key in matched_keys:
                    if not matched_key or not matched_key.base_url:
                        continue
                    if _is_gateway_model_dead(matched_key.base_url, clean_model):
                        continue

                    try:
                        return await self._call_custom_provider(
                            session, matched_key, clean_model, payload
                        )
                    except (AIRateLimitError, AIStatusError) as key_err:
                        last_exc = key_err
                        p_name = matched_key.custom_name or matched_key.provider_name
                        logger.warning(
                            "Provider %s failed for %s (%s); trying next key",
                            p_name,
                            clean_model,
                            key_err,
                        )
                        continue
                if last_exc is not None:
                    raise last_exc
        except (AIRateLimitError, AIStatusError):
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Direct provider dispatch (%s), fallback", exc)
            return None
        return None

    async def _dispatch_provider_call(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Attempt to dispatch a chat completion directly to a database provider pool."""
        model_str = str(payload.get("model") or "").strip()
        if not model_str:
            return None

        provider_hint: str | None = None
        clean_model = model_str
        if ":" in model_str and not model_str.startswith("http"):
            parts = model_str.split(":", 1)
            provider_hint = parts[0].strip()
            clean_model = parts[1].strip()

        if (
            provider_hint in ("google", "gemini")
            or clean_model.startswith(("gemini-", "gemma-"))
        ):
            async with AsyncSessionLocal() as session:
                return await self._call_google_provider(session, clean_model, payload)

        return await self._dispatch_custom_keys(provider_hint, clean_model, payload)

    async def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST ``payload`` to ``path``, retrying 429 and 5xx with backoff."""
        if path == CHAT_COMPLETIONS_PATH:
            try:
                direct = await self._dispatch_provider_call(payload)
                if direct is not None:
                    return direct
            except (AIRateLimitError, AIStatusError):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Direct provider dispatch error: %s", exc)

        return await self._post_with_retry(path, payload)

    async def _post_with_retry(
        self, path: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """POST to gateway with retry budget."""
        client = await self._ensure_client()
        backoff = INITIAL_BACKOFF_SECONDS
        last_error: AIClientError = AITransportError(f"{path} was never attempted")

        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            t0 = time.monotonic()
            try:
                response = await client.post(path, json=dict(payload))
                lat_ms = int((time.monotonic() - t0) * 1000)
            except httpx.TimeoutException as exc:
                last_error = AITransportError(
                    f"{path} timed out after {self._timeout:.0f}s ({type(exc).__name__})"
                )
            except httpx.HTTPError as exc:
                last_error = AITransportError(
                    f"{path} transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    decoded = self._decode(response, path)
                    decoded["provider_name"] = "OpenRouter"
                    decoded["latency_ms"] = lat_ms
                    if "usage" in decoded and isinstance(decoded["usage"], dict):
                        decoded["usage"].setdefault("provider_name", "OpenRouter")
                        decoded["usage"].setdefault("latency_ms", lat_ms)
                        req_model = str(payload.get("model") or "")
                        decoded["usage"].setdefault("model", req_model)
                    return decoded
                last_error = self._status_error(response, path)
                if not _is_retryable_status(response.status_code):
                    raise last_error
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))

            if attempt == self._max_attempts:
                break

            delay = (
                min(backoff, MAX_BACKOFF_SECONDS)
                if retry_after is None
                else retry_after
            )
            logger.warning(
                "OpenRouter %s attempt %d/%d failed (%s); retrying in %.2fs",
                path,
                attempt,
                self._max_attempts,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)
            backoff *= BACKOFF_MULTIPLIER

        raise last_error

    def _validate_vector(self, values: Any, *, position: int) -> list[float]:
        """Coerce one embedding to ``list[float]`` and enforce the schema width.

        The width check is not defensive noise: ``news_clusters.embedding`` is a
        ``vector(1536)`` column, so a model returning a different width would
        fail at INSERT with an opaque pgvector error long after the AI call.
        """
        if not isinstance(values, list):
            raise AIResponseError(
                f"embedding #{position} is {type(values).__name__}, expected a list"
            )
        expected = settings.ai_embedding_dimensions
        if len(values) != expected:
            raise AIResponseError(
                f"embedding model {settings.ai_model_embedding!r} returned "
                f"{len(values)} dimensions for input #{position}, but the schema "
                f"stores vector({expected}); AI_EMBEDDING_DIMENSIONS and the "
                "model must agree"
            )
        try:
            return [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise AIResponseError(
                f"embedding #{position} contains a non-numeric component"
            ) from exc

    def _parse_embeddings(
        self, payload: Mapping[str, Any], count: int
    ) -> list[list[float]]:
        """Extract ``count`` vectors from an embeddings response, in input order."""
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != count:
            found = len(data) if isinstance(data, list) else "no"
            raise AIResponseError(
                f"embeddings response carried {found} item(s) for {count} input(s)"
            )
        ordered: list[tuple[int, Any]] = []
        for position, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise AIResponseError(
                    f"embeddings item #{position} is {type(item).__name__}, "
                    "expected an object"
                )
            index = item.get("index")
            ordered.append((index if isinstance(index, int) else position, item))
        ordered.sort(key=lambda pair: pair[0])
        return [
            self._validate_vector(item.get("embedding"), position=position)
            for position, item in ordered
        ]

    async def list_models(self) -> list[str]:
        """Return the model identifiers the gateway advertises.

        Used by the settings page to offer autocompletion, so a mistyped model is
        caught before it becomes a failed generation hours later. Returns an empty
        list on any failure rather than raising: a missing catalogue must not make
        the settings page — the one place to fix a broken configuration —
        unreachable.
        """
        try:
            payload = await self._get(MODELS_PATH)
        except AIClientError:
            logger.warning("Could not fetch the model catalogue", exc_info=True)
            return []

        items = payload.get("data")
        if not isinstance(items, list):
            return []
        names = [
            str(item["id"])
            for item in items
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ]
        return sorted(set(names))

    async def embed(self, text: str) -> list[float]:
        """Return the embedding of ``text``, validated against the schema width."""
        vectors = await self.embed_batch([text])
        return vectors[0]

    async def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed several texts in one request, preserving input order."""
        if not texts:
            return []
        payload = await self._post(
            EMBEDDINGS_PATH,
            {"model": settings.ai_model_embedding, "input": list(texts)},
        )
        return self._parse_embeddings(payload, len(texts))

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int = 16,
        temperature: float = 0.0,
        system: str | None = None,
    ) -> str:
        """Run one chat completion and return the assistant message, stripped."""
        messages: list[dict[str, str]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = await self._post(
            CHAT_COMPLETIONS_PATH,
            {
                "model": model or settings.ai_model_writer,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        return self._parse_completion(payload)

    def _parse_completion(self, payload: Mapping[str, Any]) -> str:
        """Pull ``choices[0].message.content`` out of a completion response."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIResponseError("chat completion response carried no choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise AIResponseError(
                f"chat completion choice is {type(first).__name__}, expected an object"
            )
        message = first.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise AIResponseError(
                "chat completion choice carried no textual message content"
            )
        return content.strip()

    async def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
        max_tokens: int,
        temperature: float,
        response_format: Mapping[str, Any] | None = None,
    ) -> str:
        """Run a multi-turn completion that must answer with a JSON document.

        Distinct from :meth:`complete` in three ways the content engine needs: a
        full message list (so a rejected draft and its correction can be replayed
        as conversation turns), a caller-supplied token budget, and a
        ``response_format`` block asking the provider to enforce a JSON schema
        server-side.

        Returns the raw assistant message. Parsing and schema validation are the
        caller's job: this client stays unopinionated about payload shape, and
        the content engine already owns the article contract.
        """
        payload: dict[str, Any] = {
            "model": model or settings.ai_model_writer,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        return self._parse_completion(await self._post(CHAT_COMPLETIONS_PATH, payload))

    async def complete_json_with_usage(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
        max_tokens: int,
        temperature: float,
        response_format: Mapping[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Like :meth:`complete_json` but also returns the usage block.

        Returns ``(content, usage)`` where ``usage`` is the raw ``usage`` dict
        from the gateway (``prompt_tokens``, ``completion_tokens``,
        ``total_tokens`` and optionally ``cost``) or ``None`` when the gateway
        did not report one. Callers that need to account spend should prefer
        this over :meth:`complete_json`.
        """
        payload: dict[str, Any] = {
            "model": model or settings.ai_model_writer,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            payload["response_format"] = dict(response_format)
        response = await self._post(CHAT_COMPLETIONS_PATH, payload)
        content = self._parse_completion(response)
        usage = response.get("usage")
        if not isinstance(usage, dict):
            usage = None
        return content, usage

    async def ask_yes_no(self, prompt: str, *, model: str | None = None) -> bool | None:
        """Ask a strict YES/NO question deterministically.

        Returns ``None`` when the answer cannot be parsed. Callers must map that
        to an explicit, safe default instead of inferring one here — the
        deduplicator, for instance, treats it as "different event".
        """
        answer = await self.complete(
            prompt,
            model=model or settings.ai_model_arbiter,
            max_tokens=5,
            temperature=0.0,
            system=(
                "You are a strict classifier. Reply with exactly one word: "
                "YES or NO. Never explain."
            ),
        )
        verdict = parse_yes_no(answer)
        if verdict is None:
            logger.warning(
                "Arbiter returned an unparseable answer (%d chars); caller will "
                "apply its safe default",
                len(answer),
            )
        return verdict


_shared_client: AIClient | None = None


def get_ai_client() -> AIClient:
    """Return the process-wide client, creating it on first call.

    Sharing one instance keeps a single HTTP connection pool across workers and
    request handlers. Constructing it never raises, even without credentials.
    """
    global _shared_client  # noqa: PLW0603 - module-level singleton accessor
    if _shared_client is None:
        _shared_client = AIClient()
    return _shared_client


async def close_ai_client() -> None:
    """Release the shared client's connections. Call on application shutdown."""
    global _shared_client  # noqa: PLW0603 - module-level singleton accessor
    client = _shared_client
    _shared_client = None
    if client is not None:
        await client.close()


__all__ = [
    "BACKOFF_MULTIPLIER",
    "CHAT_COMPLETIONS_PATH",
    "DEFAULT_MAX_ATTEMPTS",
    "EMBEDDINGS_PATH",
    "INITIAL_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "MAX_RETRY_AFTER_SECONDS",
    "MODELS_PATH",
    "NO_TOKENS",
    "YES_TOKENS",
    "AIClient",
    "AIClientError",
    "AIRateLimitError",
    "AIResponseError",
    "AIStatusError",
    "AITransportError",
    "NoCredentialsError",
    "close_ai_client",
    "get_ai_client",
    "parse_yes_no",
]


def _chunk_content(chunk: Mapping[str, Any]) -> tuple[str, str | None]:
    """Extract the text fragment and finish reason from one stream chunk.

    Handles both shapes a gateway may send: an incremental ``delta`` and a
    complete ``message``, the latter appearing when a provider buffers its answer
    and emits it as a single chunk.
    """
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", None
    first = choices[0]
    if not isinstance(first, Mapping):
        return "", None

    reason = first.get("finish_reason")
    delta = first.get("delta")
    if isinstance(delta, Mapping) and isinstance(delta.get("content"), str):
        return str(delta["content"]), reason
    message = first.get("message")
    if isinstance(message, Mapping) and isinstance(message.get("content"), str):
        return str(message["content"]), reason
    return "", reason
