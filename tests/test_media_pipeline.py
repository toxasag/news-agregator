"""Tests for the media pipeline: watermark detection and Flux cover generation.

No external services. Images are synthesised with OpenCV in-process, and the Fal
AI API is served by `respx`, which intercepts the transport — no DNS lookup and no
socket.

The synthetic photos are deliberately *blurred noise* rather than flat colour: a
flat background has almost no edges anywhere, so a watermark plate would be
detected against it trivially and the mask heuristic would appear to work without
being tested. Blurred noise has photographic texture, which is what the detector
has to discriminate against.

OCR needs the Tesseract binary. Text-detection tests skip when it is absent
rather than fail, because the container ships it and a developer machine may not;
the mask-based tests run everywhere.
"""

# Test fixtures include Russian watermark text, so Cyrillic letters that look
# like Latin ones are intended.

from __future__ import annotations

from typing import Any, Final

import cv2
import httpx
import numpy as np
import pytest
import respx

from app.services.flux_generator import (
    ASPECT_RATIOS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_MODEL,
    MAX_ATTEMPTS,
    NEGATIVE_STYLE_SUFFIX,
    FluxAuthError,
    FluxClient,
    FluxGenerationError,
    FluxNotConfiguredError,
    FluxRateLimitError,
    FluxTimeoutError,
    build_prompt,
    generate_flux_image,
    validate_aspect_ratio,
)
from app.services.watermark_detector import _OCR_AVAILABLE as OCR_AVAILABLE
from app.services.watermark_detector import (
    MAX_CLEAN_COVERAGE_RATIO,
    MIN_ANALYSABLE_EDGE,
    WatermarkError,
    analyse,
    crop_watermarks,
    detect_watermarks,
)

FAL_URL: Final[str] = "https://fal.test"
FAL_KEY: Final[str] = "fal-test-key-3d81bc"

#: A tiny valid PNG, used as the downloaded payload.
PNG_BYTES: Final[bytes] = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d4944415478da63f8ffff3f0005fe02fea735c0f5"
    "0000000049454e44ae426082"
)

requires_ocr = pytest.mark.skipif(
    not OCR_AVAILABLE, reason="Tesseract binary is not installed in this environment"
)


# ============================================================ image fixtures


def photo(width: int = 900, height: int = 600, *, seed: int = 7) -> np.ndarray:
    """Return a synthetic photograph: blurred noise with real texture.

    Blur matters. Unblurred noise has an enormous edge density everywhere, which
    would mask the plate signal; a flat fill has none, which would make detection
    trivially easy. Blurred noise sits where a real photograph sits.
    """
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 190, (height, width, 3), dtype=np.uint8)
    return cv2.GaussianBlur(base, (31, 31), 0)


def encode(image: np.ndarray, *, quality: int = 95) -> bytes:
    """JPEG-encode an image the way a scraped photo would arrive."""
    success, buffer = cv2.imencode(
        ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    )
    assert success, "the fixture image could not be encoded"
    return bytes(buffer.tobytes())


def with_corner_handle(
    corner: str = "top_right", text: str = "@moscow_news", *, seed: int = 7
) -> np.ndarray:
    """Paint a channel-handle badge into one corner, as TG channels do."""
    image = photo(seed=seed)
    height, width = image.shape[:2]
    box_w, box_h = 260, 50
    positions = {
        "top_left": (20, 20),
        "top_right": (width - box_w - 20, 20),
        "bottom_left": (20, height - box_h - 20),
        "bottom_right": (width - box_w - 20, height - box_h - 20),
    }
    left, top = positions[corner]
    cv2.rectangle(image, (left, top), (left + box_w, top + box_h), (10, 10, 10), -1)
    cv2.putText(
        image,
        text,
        (left + 10, top + 37),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
    )
    return image


def with_bottom_strip(text: str = "t.me/moscow_news", *, seed: int = 7) -> np.ndarray:
    """Paint a bottom-centre caption bar, the other common branding style."""
    image = photo(seed=seed)
    height, width = image.shape[:2]
    cv2.rectangle(image, (150, height - 70), (width - 150, height - 10), (15, 15, 15), -1)
    cv2.putText(
        image,
        text,
        (170, height - 27),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
    )
    return image


def with_logo_plate(*, seed: int = 11) -> np.ndarray:
    """Paint a textless high-contrast logo plate.

    This is the mask-only case: OCR finds nothing readable, so detection has to
    come from edge density and bimodal brightness alone.
    """
    image = photo(seed=seed)
    cv2.rectangle(image, (30, 20), (200, 90), (250, 250, 250), -1)
    cv2.rectangle(image, (45, 35), (185, 75), (5, 5, 5), -1)
    for x in range(50, 185, 12):
        cv2.line(image, (x, 35), (x, 75), (250, 250, 250), 3)
    return image


# ================================================== detector: clean images


async def test_a_clean_photo_is_reported_clean() -> None:
    result = await detect_watermarks(encode(photo()))

    assert result["has_watermark"] is False
    assert result["coverage_ratio"] == 0.0
    assert result["detected_text"] == []
    assert result["needs_ai_generation"] is False
    assert result["is_croppable"] is False


async def test_the_report_carries_the_original_dimensions() -> None:
    result = await detect_watermarks(encode(photo(1280, 720)))

    # Reported from the source image, not from the downscaled analysis copy.
    assert result["width"] == 1280
    assert result["height"] == 720


async def test_a_large_photo_is_analysed_without_error() -> None:
    # Exercises the downscale path: ratios are resolution-independent, so a 4K
    # frame must reach the same verdict as a small one.
    result = await detect_watermarks(encode(photo(3200, 1800)))

    assert result["has_watermark"] is False
    assert result["width"] == 3200


# ============================================== detector: watermark detection


@requires_ocr
@pytest.mark.parametrize(
    "corner", ["top_left", "top_right", "bottom_left", "bottom_right"]
)
async def test_a_handle_badge_is_detected_in_every_corner(corner: str) -> None:
    result = await detect_watermarks(encode(with_corner_handle(corner)))

    assert result["has_watermark"] is True
    assert result["coverage_ratio"] > 0
    # A channel handle makes the image unusable regardless of how little of the
    # frame it occupies: it credits a competitor.
    assert result["needs_ai_generation"] is True
    assert result["critical_text"]
    assert any(zone["name"] == corner for zone in result["zones"])


@requires_ocr
async def test_a_telegram_link_strip_is_detected() -> None:
    result = await detect_watermarks(encode(with_bottom_strip()))

    assert result["has_watermark"] is True
    assert result["needs_ai_generation"] is True
    assert any("me/" in text.lower() for text in result["detected_text"])


@requires_ocr
async def test_a_cyrillic_source_credit_is_detected() -> None:
    image = photo()
    height = image.shape[0]
    cv2.rectangle(image, (150, height - 70), (750, height - 10), (12, 12, 12), -1)
    cv2.putText(
        image,
        "Foto: RIA",
        (170, height - 27),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
    )

    result = await detect_watermarks(encode(image))

    assert result["has_watermark"] is True


async def test_a_textless_logo_plate_is_detected_by_the_mask() -> None:
    # Runs without OCR: the mask heuristic is the only signal here, which is the
    # point — a logo Tesseract cannot read is still a watermark.
    result = await detect_watermarks(encode(with_logo_plate()))

    assert result["has_watermark"] is True
    assert result["detected_text"] == []
    contaminated = {zone["name"] for zone in result["zones"]}
    assert "top_left" in contaminated
    assert any(zone["has_overlay_mask"] for zone in result["zones"])


async def test_the_mask_does_not_fire_on_photographic_texture() -> None:
    # The discrimination that matters: a busy but photographic corner has the edge
    # density of a plate without its two-tone histogram.
    image = photo(seed=3)
    rng = np.random.default_rng(5)
    patch = rng.integers(0, 255, (168, 252, 3), dtype=np.uint8)
    image[0:168, 0:252] = cv2.GaussianBlur(patch, (5, 5), 0)

    result = await detect_watermarks(encode(image))

    assert result["has_watermark"] is False


# ================================================== detector: the 15% threshold


@requires_ocr
async def test_light_contamination_stays_under_the_threshold() -> None:
    report = analyse(encode(with_corner_handle("top_right")))

    # One corner is ~7.8% of the frame, comfortably inside the 15% budget: the
    # image is only unusable because of the *critical text*, not the coverage.
    assert report.coverage_ratio <= MAX_CLEAN_COVERAGE_RATIO


async def test_branding_in_both_bands_exceeds_the_threshold() -> None:
    image = with_logo_plate()
    height, width = image.shape[:2]
    cv2.rectangle(
        image, (width - 210, height - 90), (width - 30, height - 20), (250, 250, 250), -1
    )
    cv2.rectangle(
        image, (width - 195, height - 75), (width - 45, height - 35), (5, 5, 5), -1
    )
    for x in range(width - 190, width - 45, 12):
        cv2.line(image, (x, height - 75), (x, height - 35), (250, 250, 250), 3)

    report = analyse(encode(image))

    assert report.coverage_ratio > MAX_CLEAN_COVERAGE_RATIO
    assert report.needs_ai_generation is True
    # Contamination in both bands has no crop that leaves a photograph.
    assert report.is_croppable is False


async def test_overlapping_zones_are_not_double_counted() -> None:
    # The bottom-centre strip shares pixels with both bottom corners. Summing raw
    # zone areas would report ~23% for one caption bar and wrongly condemn a
    # salvageable image, so coverage is measured with a mask.
    image = photo()
    height, width = image.shape[:2]
    cv2.rectangle(image, (0, height - 80), (width, height - 10), (250, 250, 250), -1)
    cv2.rectangle(image, (20, height - 65), (width - 20, height - 25), (5, 5, 5), -1)
    for x in range(30, width - 30, 14):
        cv2.line(image, (x, height - 65), (x, height - 25), (250, 250, 250), 3)

    report = analyse(encode(image))
    summed = sum(zone.area_ratio for zone in report.zones if zone.is_contaminated)

    assert report.coverage_ratio < summed
    assert report.coverage_ratio <= 1.0


# ================================================== detector: cropping helper


async def test_a_croppable_image_loses_only_the_branded_band() -> None:
    original = with_logo_plate()
    report = analyse(encode(original))
    assert report.is_croppable is True

    cropped = crop_watermarks(encode(original))
    decoded = cv2.imdecode(np.frombuffer(cropped, np.uint8), cv2.IMREAD_COLOR)

    assert decoded is not None
    # The full width survives; only the contaminated band is cut.
    assert decoded.shape[1] == original.shape[1]
    assert decoded.shape[0] < original.shape[0]
    # And the result is clean.
    assert analyse(cropped).has_watermark is False


async def test_cropping_a_clean_image_is_refused() -> None:
    # Returning bytes unchanged would let a caller believe a watermark was
    # removed when none was found.
    with pytest.raises(WatermarkError):
        crop_watermarks(encode(photo()))


async def test_cropping_an_uncroppable_image_is_refused() -> None:
    image = with_logo_plate()
    height, width = image.shape[:2]
    cv2.rectangle(
        image, (width - 210, height - 90), (width - 30, height - 20), (250, 250, 250), -1
    )
    cv2.rectangle(
        image, (width - 195, height - 75), (width - 45, height - 35), (5, 5, 5), -1
    )
    for x in range(width - 190, width - 45, 12):
        cv2.line(image, (x, height - 75), (x, height - 35), (250, 250, 250), 3)

    with pytest.raises(WatermarkError, match="not croppable"):
        crop_watermarks(encode(image))


# ================================================== detector: invalid input


async def test_an_empty_payload_is_rejected() -> None:
    with pytest.raises(WatermarkError, match="empty"):
        analyse(b"")


async def test_a_non_image_payload_is_rejected() -> None:
    with pytest.raises(WatermarkError, match="could not decode"):
        analyse(b"<html><body>not an image</body></html>")


async def test_a_thumbnail_is_rejected_as_unanalysable() -> None:
    # OCR on a thumbnail returns noise, which would produce false positives.
    tiny = photo(MIN_ANALYSABLE_EDGE - 16, MIN_ANALYSABLE_EDGE - 16)

    with pytest.raises(WatermarkError, match="carries no readable watermark"):
        analyse(encode(tiny))


# =============================================== flux: parameter validation


@pytest.mark.parametrize("ratio", sorted(ASPECT_RATIOS))
def test_every_supported_ratio_resolves_to_model_friendly_dimensions(
    ratio: str,
) -> None:
    width, height = validate_aspect_ratio(ratio)

    assert width > 0
    assert height > 0
    # Flux requires multiples of 16; an arbitrary size degrades output quality.
    assert width % 16 == 0
    assert height % 16 == 0


def test_the_default_ratio_is_landscape() -> None:
    width, height = validate_aspect_ratio(DEFAULT_ASPECT_RATIO)

    assert DEFAULT_ASPECT_RATIO == "16:9"
    assert width > height


@pytest.mark.parametrize("ratio", ["21:9", "5:4", "", "16x9", "banana"])
def test_an_unsupported_ratio_is_refused(ratio: str) -> None:
    # Substituting a default silently would produce covers in the wrong shape for
    # the theme.
    with pytest.raises(ValueError, match="unsupported aspect ratio"):
        validate_aspect_ratio(ratio)


def test_a_ratio_is_accepted_with_surrounding_whitespace() -> None:
    assert validate_aspect_ratio("  16:9  ") == ASPECT_RATIOS["16:9"]


def test_the_prompt_gains_the_editorial_style_constraints() -> None:
    built = build_prompt("A newly opened metro station platform at midday")

    assert built.startswith("A newly opened metro station")
    # Flux has no negative-prompt parameter, so the exclusions must be stated in
    # the prompt itself; garbled pseudo-text on a cover makes it unusable.
    assert "No text" in built
    assert "no watermark" in built


def test_the_style_suffix_is_not_appended_twice() -> None:
    once = build_prompt("A metro station platform at midday, wide shot")
    twice = build_prompt(once)

    # Duplicated instructions dilute the attention the rest of the prompt gets.
    assert twice.count(NEGATIVE_STYLE_SUFFIX) == 1


def test_prompt_whitespace_is_normalised() -> None:
    built = build_prompt("A   metro\n\nstation   platform,\twide shot")

    assert "  " not in built.replace(NEGATIVE_STYLE_SUFFIX, "")
    assert "\n" not in built


@pytest.mark.parametrize("prompt", ["", "   ", "short", "\n\t"])
def test_a_too_short_prompt_is_refused(prompt: str) -> None:
    with pytest.raises(ValueError, match="at least"):
        build_prompt(prompt)


def test_a_very_long_prompt_is_truncated() -> None:
    built = build_prompt("editorial photograph of a city street " * 200)

    # Past the model's effective attention limit the trailing text is ignored,
    # so it is dropped rather than paid for.
    assert len(built) <= 1800


# ==================================================== flux: transport helpers


PROMPT: Final[str] = (
    "A newly opened modern metro station platform at midday, wide editorial shot"
)


def submission_response(**overrides: Any) -> httpx.Response:
    """A completed Fal submission carrying an image descriptor."""
    payload: dict[str, Any] = {
        "images": [
            {
                "url": f"{FAL_URL}/files/cover.png",
                "width": 1344,
                "height": 768,
                "content_type": "image/png",
            }
        ],
        "seed": 4242,
    }
    payload.update(overrides)
    return httpx.Response(200, json=payload)


def image_response(content: bytes = PNG_BYTES) -> httpx.Response:
    """The CDN download of a generated image."""
    return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})


def new_flux_client() -> FluxClient:
    """A Flux client pointed at the mocked API."""
    return FluxClient(api_key=FAL_KEY, base_url=FAL_URL, timeout=5.0)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse backoff and poll waits so retry paths run instantly.

    The delays are real seconds in production; without this a single rate-limit
    test would sit for tens of them.
    """
    monkeypatch.setattr(
        "app.services.flux_generator.INITIAL_BACKOFF_SECONDS", 0.0
    )
    monkeypatch.setattr("app.services.flux_generator.MAX_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.services.flux_generator.POLL_INTERVAL_SECONDS", 0.0)


# ==================================================== flux: generation flow


async def test_a_generation_returns_the_downloaded_bytes() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        submit = mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        download = mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            content = await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert content == PNG_BYTES
    assert submit.call_count == 1
    # The bytes are fetched, not linked: Fal's CDN URLs expire, and a stored URL
    # would fail at WordPress-upload time instead.
    assert download.call_count == 1


async def test_the_request_carries_the_prompt_size_and_step_count() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        submit = mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            await generate_flux_image(PROMPT, "4:3", client=client)
        finally:
            await client.close()

    sent = submit.calls[0].request
    body = sent.content.decode()

    assert "metro station" in body
    assert "No text" in body
    assert '"width": 1152' in body.replace("  ", " ") or '"width":1152' in body
    # Schnell is distilled for very few steps; more is billed for no gain.
    assert '"num_inference_steps": 4' in body or '"num_inference_steps":4' in body


async def test_the_api_key_is_sent_as_a_key_header() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        submit = mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert submit.calls[0].request.headers["Authorization"] == f"Key {FAL_KEY}"


async def test_the_metadata_of_a_generation_is_returned() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            image = await client.generate(PROMPT, aspect_ratio="16:9")
        finally:
            await client.close()

    assert image.content == PNG_BYTES
    assert image.content_type == "image/png"
    assert (image.width, image.height) == (1344, 768)
    assert image.model == DEFAULT_MODEL
    assert image.seed == 4242
    assert image.size_bytes == len(PNG_BYTES)


async def test_an_explicit_model_is_used() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        submit = mock.post("/fal-ai/flux/dev").mock(
            return_value=submission_response()
        )
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            await generate_flux_image(PROMPT, model="fal-ai/flux/dev", client=client)
        finally:
            await client.close()

    assert submit.call_count == 1


async def test_a_seed_is_forwarded_for_reproducibility() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        submit = mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            await generate_flux_image(PROMPT, seed=99, client=client)
        finally:
            await client.close()

    assert '"seed": 99' in submit.calls[0].request.content.decode().replace(
        '"seed":99', '"seed": 99'
    )


async def test_a_single_image_field_is_accepted() -> None:
    # Fal's field name differs across model families; a caller should not care.
    descriptor = {
        "url": f"{FAL_URL}/files/cover.png",
        "width": 1344,
        "height": 768,
    }
    single = httpx.Response(200, json={"image": descriptor})
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=single)
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            content = await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert content == PNG_BYTES


# ==================================================== flux: queued requests


async def test_a_queued_request_is_polled_until_it_completes() -> None:
    queued = httpx.Response(
        200, json={"status": "IN_QUEUE", "status_url": f"{FAL_URL}/status/job-1"}
    )
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=queued)
        status = mock.get("/status/job-1").mock(
            side_effect=[
                httpx.Response(200, json={"status": "IN_PROGRESS"}),
                submission_response(),
            ]
        )
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            content = await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert content == PNG_BYTES
    assert status.call_count == 2


async def test_a_failed_queued_job_raises() -> None:
    queued = httpx.Response(
        200, json={"status": "IN_QUEUE", "status_url": f"{FAL_URL}/status/job-2"}
    )
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=queued)
        mock.get("/status/job-2").mock(
            return_value=httpx.Response(200, json={"status": "FAILED"})
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError, match="FAILED"):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()


async def test_a_stuck_queue_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bounded on purpose: Schnell finishes in seconds, so a job still pending
    # must not hold an ARQ slot indefinitely.
    monkeypatch.setattr("app.services.flux_generator.MAX_POLL_ATTEMPTS", 3)
    queued = httpx.Response(
        200, json={"status": "IN_QUEUE", "status_url": f"{FAL_URL}/status/job-3"}
    )
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=queued)
        status = mock.get("/status/job-3").mock(
            return_value=httpx.Response(200, json={"status": "IN_QUEUE"})
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxTimeoutError, match="did not complete"):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert status.call_count == 3


# ==================================================== flux: error handling


async def test_a_missing_key_raises_rather_than_degrading() -> None:
    # There is no zero-cost fallback for "produce an original photograph", and a
    # silently missing cover would surface as a broken article.
    client = FluxClient(api_key="", base_url=FAL_URL)
    try:
        with pytest.raises(FluxNotConfiguredError, match="FAL_KEY"):
            await generate_flux_image(PROMPT, client=client)
    finally:
        await client.close()


@pytest.mark.parametrize("status_code", [401, 403])
async def test_a_rejected_key_raises_an_auth_error(status_code: int) -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        route = mock.post(f"/{DEFAULT_MODEL}").mock(
            return_value=httpx.Response(status_code, text="unauthorized")
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxAuthError):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    # A rejected key is deterministic: retrying would waste the budget.
    assert route.call_count == 1


async def test_a_client_error_is_not_retried() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        route = mock.post(f"/{DEFAULT_MODEL}").mock(
            return_value=httpx.Response(422, text="invalid image_size")
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert route.call_count == 1


async def test_a_rate_limit_is_retried_then_raised() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        route = mock.post(f"/{DEFAULT_MODEL}").mock(
            return_value=httpx.Response(429, text="slow down")
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxRateLimitError):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert route.call_count == MAX_ATTEMPTS


async def test_a_transient_server_error_recovers() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        route = mock.post(f"/{DEFAULT_MODEL}").mock(
            side_effect=[
                httpx.Response(503, text="unavailable"),
                submission_response(),
            ]
        )
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            content = await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert content == PNG_BYTES
    assert route.call_count == 2


async def test_a_timeout_is_retried() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        route = mock.post(f"/{DEFAULT_MODEL}").mock(
            side_effect=[httpx.TimeoutException("timed out"), submission_response()]
        )
        mock.get("/files/cover.png").mock(return_value=image_response())
        client = new_flux_client()
        try:
            content = await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert content == PNG_BYTES
    assert route.call_count == 2


async def test_a_response_without_an_image_url_raises() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(
            return_value=httpx.Response(200, json={"detail": "nothing to see"})
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError, match="no image URL"):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()


async def test_an_empty_download_raises() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(
            return_value=httpx.Response(
                200, content=b"", headers={"Content-Type": "image/png"}
            )
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError, match="empty image"):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()


async def test_a_non_image_download_raises() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(
            return_value=httpx.Response(
                200, json={"error": "gone"}, headers={"Content-Type": "application/json"}
            )
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError, match="expected an image"):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()


async def test_an_oversized_download_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guard for a 4 GB VPS: an "image" far larger than any cover must not be
    # pulled into memory.
    monkeypatch.setattr("app.services.flux_generator.MAX_IMAGE_BYTES", 16)
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        mock.get("/files/cover.png").mock(return_value=image_response(b"x" * 64))
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError, match="ceiling"):
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()


async def test_a_failed_download_is_retried() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(return_value=submission_response())
        download = mock.get("/files/cover.png").mock(
            side_effect=[httpx.Response(503, text="cdn down"), image_response()]
        )
        client = new_flux_client()
        try:
            content = await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    # Losing a paid generation to a transient CDN failure would mean paying twice.
    assert content == PNG_BYTES
    assert download.call_count == 2


async def test_the_api_key_never_appears_in_an_error() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=True) as mock:
        mock.post(f"/{DEFAULT_MODEL}").mock(
            return_value=httpx.Response(422, text="invalid request")
        )
        client = new_flux_client()
        try:
            with pytest.raises(FluxGenerationError) as info:
                await generate_flux_image(PROMPT, client=client)
        finally:
            await client.close()

    assert FAL_KEY not in str(info.value)


async def test_a_bad_ratio_fails_before_any_request() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=False):
        client = new_flux_client()
        try:
            with pytest.raises(ValueError, match="unsupported aspect ratio"):
                await generate_flux_image(PROMPT, "21:9", client=client)
        finally:
            await client.close()


async def test_a_bad_prompt_fails_before_any_request() -> None:
    with respx.mock(base_url=FAL_URL, assert_all_called=False):
        client = new_flux_client()
        try:
            with pytest.raises(ValueError, match="at least"):
                await generate_flux_image("hi", client=client)
        finally:
            await client.close()
