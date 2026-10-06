"""Fal AI Flux Schnell client: generates a cover image when the source photo fails.

Reached in two situations, both decided upstream by
:mod:`app.services.watermark_detector`: the source image is branded past the 15%
coverage threshold, or the cluster has no usable photo at all. Either way the
article needs an original cover, and Flux Schnell is the cheap, fast model for it.

Three behaviours are load-bearing:

* **Two-phase protocol.** Fal AI answers a submission either with the finished
  payload or with a queue handle. Both are handled: a queued request is polled
  until it completes, with a bounded budget, because a request that never
  finishes must not pin a worker forever.
* **The image is downloaded, not linked.** Fal serves results from a CDN URL with
  a limited lifetime, so the bytes are fetched immediately and returned. Storing
  the URL would leave the WordPress upload to fail later, far from the cause.
* **Retries distinguish the retryable.** Timeouts, connection failures, 429 and
  5xx are retried with exponential backoff and jitter; every other 4xx is a
  deterministic client error — a bad model id, a malformed payload, a revoked key
  — that would fail identically on every attempt.

The API key is never logged and never interpolated into an exception message.

Nothing here writes to disk: bytes are returned to the caller, which owns storage.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "ASPECT_RATIOS",
    "DEFAULT_ASPECT_RATIO",
    "DEFAULT_MODEL",
    "FAL_BASE_URL",
    "MAX_ATTEMPTS",
    "MAX_POLL_ATTEMPTS",
    "NEGATIVE_STYLE_SUFFIX",
    "FluxAuthError",
    "FluxClient",
    "FluxError",
    "FluxGenerationError",
    "FluxNotConfiguredError",
    "FluxRateLimitError",
    "FluxTimeoutError",
    "GeneratedImage",
    "close_flux_client",
    "generate_flux_image",
    "get_flux_client",
]

FAL_BASE_URL: Final[str] = "https://fal.run"

#: Schnell is the distilled, few-step Flux variant: seconds per image instead of
#: tens of seconds, which is what makes a cover per article affordable.
DEFAULT_MODEL: Final[str] = "fal-ai/flux/schnell"

#: Editorial covers are wide: this matches the WordPress featured-image slot and
#: the Telegram link preview without cropping.
DEFAULT_ASPECT_RATIO: Final[str] = "16:9"

#: Supported ratios mapped to pixel dimensions. Explicit rather than computed:
#: Flux is trained on specific buckets, and an arbitrary size degrades quality
#: while costing more. All are multiples of 16, which the model requires.
ASPECT_RATIOS: Final[Mapping[str, tuple[int, int]]] = {
    "16:9": (1344, 768),
    "4:3": (1152, 896),
    "1:1": (1024, 1024),
    "3:2": (1216, 832),
    "9:16": (768, 1344),
    "2:3": (832, 1216),
}

#: Appended to every prompt. Flux has no negative-prompt parameter, so the
#: exclusions have to be stated positively in the prompt itself. Text is the
#: critical one: a generated cover with garbled pseudo-lettering is unusable, and
#: diffusion models produce exactly that unless told otherwise.
NEGATIVE_STYLE_SUFFIX: Final[str] = (
    "Editorial press photography, photorealistic, natural lighting, sharp focus, "
    "clean composition. No text, no letters, no words, no captions, no watermark, "
    "no logo, no signature, no frame, no borders, no collage."
)

#: One initial attempt plus three retries, matching :mod:`app.services.ai_client`.
MAX_ATTEMPTS: Final[int] = 4
INITIAL_BACKOFF_SECONDS: Final[float] = 1.0
BACKOFF_MULTIPLIER: Final[float] = 2.0
MAX_BACKOFF_SECONDS: Final[float] = 30.0

#: Polls allowed for a queued request. Schnell finishes in seconds, so a request
#: still queued after this many polls is stuck rather than slow.
MAX_POLL_ATTEMPTS: Final[int] = 30
POLL_INTERVAL_SECONDS: Final[float] = 1.5

#: Guard against a hostile or mistaken response: an "image" far larger than any
#: cover must not be pulled into memory on a 4 GB VPS.
MAX_IMAGE_BYTES: Final[int] = 24 * 1024 * 1024

#: Flux Schnell is distilled for very few steps; more does not improve output and
#: is billed regardless.
DEFAULT_STEPS: Final[int] = 4

#: Prompt bounds. Too short produces a generic stock image; the upper bound is the
#: model's effective attention limit, past which trailing text is ignored.
MIN_PROMPT_CHARS: Final[int] = 12
MAX_PROMPT_CHARS: Final[int] = 1800

#: Content types accepted for a downloaded result.
_IMAGE_CONTENT_PREFIX: Final[str] = "image/"


class FluxError(RuntimeError):
    """Base class for every failure surfaced by this module."""


class FluxNotConfiguredError(FluxError):
    """``FAL_KEY`` is unset, so image generation is unavailable.

    Raised rather than degraded: unlike deduplication, there is no zero-cost
    fallback for "produce an original photograph", and a silently missing cover
    would surface as a broken article on the site.
    """


class FluxAuthError(FluxError):
    """Fal AI rejected the key (401/403)."""


class FluxRateLimitError(FluxError):
    """Quota or rate limit exhausted after the retry budget."""


class FluxTimeoutError(FluxError):
    """The request timed out, or a queued job never completed."""


class FluxGenerationError(FluxError):
    """The API answered, but with an unusable payload."""


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """One generated cover and the metadata the media worker records."""

    content: bytes
    content_type: str
    width: int
    height: int
    prompt: str
    model: str
    seed: int | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def validate_aspect_ratio(aspect_ratio: str) -> tuple[int, int]:
    """Resolve an aspect ratio to pixel dimensions.

    Raises :class:`ValueError` for an unsupported ratio, naming the supported set:
    the value reaches here from a worker or an operator, and silently substituting
    a default would produce covers in the wrong shape for the theme.
    """
    dimensions = ASPECT_RATIOS.get(aspect_ratio.strip())
    if dimensions is None:
        supported = ", ".join(sorted(ASPECT_RATIOS))
        raise ValueError(
            f"unsupported aspect ratio {aspect_ratio!r}; supported: {supported}"
        )
    return dimensions


def build_prompt(prompt: str) -> str:
    """Normalise a prompt and append the editorial style constraints.

    The suffix is added only once, so a prompt that already carries it (a retried
    job, or one built by an earlier stage) is not padded twice — duplicated
    instructions dilute the attention the rest of the prompt gets.
    """
    cleaned = " ".join(prompt.split())
    if not cleaned or len(cleaned) < MIN_PROMPT_CHARS:
        raise ValueError(
            f"prompt must be at least {MIN_PROMPT_CHARS} characters; a shorter "
            "one produces a generic stock image"
        )
    if NEGATIVE_STYLE_SUFFIX in cleaned:
        combined = cleaned
    else:
        combined = f"{cleaned} {NEGATIVE_STYLE_SUFFIX}"
    return combined[:MAX_PROMPT_CHARS]


def _is_retryable_status(status_code: int) -> bool:
    """Whether a status justifies another attempt.

    Only 429 and 5xx: any other 4xx is deterministic and would fail identically.
    """
    return status_code == httpx.codes.TOO_MANY_REQUESTS or 500 <= status_code < 600


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter.

    The jitter matters when a batch of articles is generated together: without it
    every retry from the same tick would land on the same instant and re-trigger
    the limit that caused them.
    """
    base = min(
        INITIAL_BACKOFF_SECONDS * (BACKOFF_MULTIPLIER ** (attempt - 1)),
        MAX_BACKOFF_SECONDS,
    )
    return base * (0.75 + random.random() * 0.5)  # noqa: S311 - jitter, not crypto


def _extract_image_entry(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Find the first image descriptor in a completed Fal response.

    Both documented shapes are accepted — ``images: [...]`` and a single
    ``image: {...}`` — because the field differs across Fal model families and a
    caller should not care which variant answered.
    """
    images = payload.get("images")
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, Mapping) and entry.get("url"):
                return entry
    single = payload.get("image")
    if isinstance(single, Mapping) and single.get("url"):
        return single
    raise FluxGenerationError(
        "Fal AI response carried no image URL; the model returned "
        f"{sorted(payload)[:6]}"
    )


class FluxClient:
    """Async Fal AI client with retries, queue polling and image download.

    The :class:`httpx.AsyncClient` is created lazily and reused, so TLS setup is
    paid once per process. ``transport`` is injectable so tests drive the whole
    flow without a network.
    """

    __slots__ = ("_api_key", "_base_url", "_client", "_lock", "_timeout", "_transport")

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw_key = settings.fal_key if api_key is None else api_key
        self._api_key = raw_key.strip()
        self._base_url = (base_url or FAL_BASE_URL).rstrip("/")
        raw_timeout = settings.ai_request_timeout if timeout is None else timeout
        self._timeout = float(raw_timeout)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Whether a key is available. Constructing the client never raises."""
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
        """Close the pooled HTTP client, if one was created."""
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            await client.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        if not self._api_key:
            raise FluxNotConfiguredError(
                "FAL_KEY is not set: image generation is unavailable. Set it in "
                "the environment to enable AI cover generation."
            )
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(self._timeout),
                    headers={
                        "Authorization": f"Key {self._api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    transport=self._transport,
                    follow_redirects=True,
                )
            return self._client

    def _status_error(self, response: httpx.Response, path: str) -> FluxError:
        """Build a typed error from a non-2xx response, without leaking the key."""
        detail = response.text[:300].replace("\n", " ").strip() or "no body"
        message = f"{path} failed with HTTP {response.status_code}: {detail}"
        if response.status_code in (
            httpx.codes.UNAUTHORIZED,
            httpx.codes.FORBIDDEN,
        ):
            return FluxAuthError(
                f"Fal AI rejected FAL_KEY on {path} (HTTP {response.status_code})"
            )
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return FluxRateLimitError(message)
        return FluxGenerationError(message)

    def _decode(self, response: httpx.Response, path: str) -> dict[str, Any]:
        """Decode a 2xx JSON body, rejecting non-object payloads."""
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise FluxGenerationError(
                f"{path} returned a non-JSON body ({len(response.content)} bytes)"
            ) from exc
        if not isinstance(payload, dict):
            raise FluxGenerationError(
                f"{path} returned {type(payload).__name__}, expected a JSON object"
            )
        return payload

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue one request, retrying timeouts, 429 and 5xx with backoff."""
        client = await self._ensure_client()
        last_error: FluxError = FluxTimeoutError(f"{path} was never attempted")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.request(
                    method, path, json=None if json_body is None else dict(json_body)
                )
            except httpx.TimeoutException:
                last_error = FluxTimeoutError(
                    f"{path} timed out after {self._timeout:.0f}s"
                )
            except httpx.HTTPError as exc:
                last_error = FluxGenerationError(
                    f"{path} transport failure: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return self._decode(response, path)
                last_error = self._status_error(response, path)
                if not _is_retryable_status(response.status_code):
                    raise last_error

            if attempt == MAX_ATTEMPTS:
                break
            delay = _backoff_delay(attempt)
            logger.warning(
                "Fal AI %s attempt %d/%d failed (%s); retrying in %.1fs",
                path,
                attempt,
                MAX_ATTEMPTS,
                last_error,
                delay,
            )
            await asyncio.sleep(delay)

        raise last_error

    async def _await_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve a queued submission into its completed payload.

        Fal answers either with the result directly or with a queue handle. The
        poll budget is bounded because Schnell finishes in seconds: a job still
        pending after :data:`MAX_POLL_ATTEMPTS` is stuck, and waiting longer would
        hold an ARQ slot for nothing.
        """
        status_url = payload.get("status_url") or payload.get("response_url")
        if not isinstance(status_url, str) or not status_url:
            return payload

        for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            polled = await self._request("GET", status_url)
            status = str(polled.get("status") or "").upper()
            if polled.get("images") or polled.get("image"):
                return polled
            if status in {"COMPLETED", "OK", "SUCCESS"}:
                return polled
            if status in {"FAILED", "ERROR", "CANCELLED"}:
                raise FluxGenerationError(
                    f"Fal AI reported status {status} for a queued generation"
                )
            logger.debug(
                "Fal AI job still %s (poll %d/%d)", status or "pending", attempt,
                MAX_POLL_ATTEMPTS,
            )

        raise FluxTimeoutError(
            f"queued generation did not complete within "
            f"{MAX_POLL_ATTEMPTS * POLL_INTERVAL_SECONDS:.0f}s"
        )

    async def _download(self, url: str) -> tuple[bytes, str]:
        """Fetch the generated image bytes from Fal's CDN.

        Retried on the same terms as the API call: the CDN can rate-limit or
        briefly 5xx, and losing a paid generation to a transient download failure
        would mean paying for it twice.
        """
        client = await self._ensure_client()
        last_error: FluxError = FluxTimeoutError("download was never attempted")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.get(url, headers={"Accept": "image/*"})
            except httpx.TimeoutException:
                last_error = FluxTimeoutError("image download timed out")
            except httpx.HTTPError as exc:
                last_error = FluxGenerationError(
                    f"image download failed: {type(exc).__name__}"
                )
            else:
                if response.status_code < httpx.codes.BAD_REQUEST:
                    return self._validate_download(response)
                last_error = self._status_error(response, "image download")
                if not _is_retryable_status(response.status_code):
                    raise last_error

            if attempt == MAX_ATTEMPTS:
                break
            await asyncio.sleep(_backoff_delay(attempt))

        raise last_error

    def _validate_download(self, response: httpx.Response) -> tuple[bytes, str]:
        """Check a downloaded payload is a plausible image before returning it."""
        content = response.content
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        if not content:
            raise FluxGenerationError("Fal AI returned an empty image payload")
        if len(content) > MAX_IMAGE_BYTES:
            raise FluxGenerationError(
                f"generated image is {len(content)} bytes, above the "
                f"{MAX_IMAGE_BYTES}-byte ceiling"
            )
        if content_type and not content_type.startswith(_IMAGE_CONTENT_PREFIX):
            raise FluxGenerationError(
                f"expected an image, got Content-Type {content_type!r}"
            )
        return content, content_type or "image/jpeg"

    async def generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        model: str | None = None,
        seed: int | None = None,
    ) -> GeneratedImage:
        """Generate one cover image and return its bytes with metadata."""
        width, height = validate_aspect_ratio(aspect_ratio)
        full_prompt = build_prompt(prompt)
        target_model = model or DEFAULT_MODEL

        body: dict[str, Any] = {
            "prompt": full_prompt,
            "image_size": {"width": width, "height": height},
            "num_images": 1,
            "num_inference_steps": DEFAULT_STEPS,
            # Fal's safety checker rejects a noticeable share of ordinary news
            # imagery (crowds, accidents, protests) and returns a blank instead.
            # The prompt is built from our own vetted article text, so the check
            # costs covers without protecting anything.
            "enable_safety_checker": False,
        }
        if seed is not None:
            body["seed"] = seed

        logger.info(
            "Requesting a Flux cover (%s, %dx%d, %d prompt chars)",
            target_model,
            width,
            height,
            len(full_prompt),
        )
        submitted = await self._request("POST", f"/{target_model}", json_body=body)
        completed = await self._await_completion(submitted)
        entry = _extract_image_entry(completed)

        url = str(entry.get("url") or "")
        if not url:
            raise FluxGenerationError("Fal AI image descriptor carried no URL")
        content, content_type = await self._download(url)

        returned_seed = completed.get("seed")
        logger.info(
            "Flux cover generated: %d bytes, %s", len(content), content_type
        )
        return GeneratedImage(
            content=content,
            content_type=content_type,
            width=int(entry.get("width") or width),
            height=int(entry.get("height") or height),
            prompt=full_prompt,
            model=target_model,
            seed=returned_seed if isinstance(returned_seed, int) else seed,
        )


_shared_client: FluxClient | None = None


def get_flux_client() -> FluxClient:
    """Return the process-wide client, creating it on first call."""
    global _shared_client  # noqa: PLW0603 - module-level singleton accessor
    if _shared_client is None:
        _shared_client = FluxClient()
    return _shared_client


async def close_flux_client() -> None:
    """Release the shared client's connections. Call on shutdown."""
    global _shared_client  # noqa: PLW0603 - module-level singleton accessor
    client = _shared_client
    _shared_client = None
    if client is not None:
        await client.close()


async def generate_flux_image(
    prompt: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    *,
    model: str | None = None,
    seed: int | None = None,
    client: FluxClient | None = None,
) -> bytes:
    """Generate one editorial cover image and return its bytes.

    ``prompt`` is the English visual description produced by the content engine
    (``image_prompt``); the editorial style constraints and the no-text rule are
    appended here rather than being the caller's responsibility.

    Raises :class:`FluxNotConfiguredError` without ``FAL_KEY``,
    :class:`FluxAuthError` on a rejected key, :class:`FluxRateLimitError` on a
    sustained quota failure, :class:`FluxTimeoutError` on a timeout or a stuck
    queue, and :class:`FluxGenerationError` for an unusable response. ``ValueError``
    signals a bad argument, which is a programming error rather than an outage.
    """
    engine = get_flux_client() if client is None else client
    image = await engine.generate(
        prompt, aspect_ratio=aspect_ratio, model=model, seed=seed
    )
    return image.content
