# ruff: noqa: RUF001, E501
"""OCR watermark detector: decides whether a scraped image is publishable.

Telegram news channels brand their photos — a `@channel` handle in a corner, a
`t.me/...` strip along the bottom, a translucent logo plate. Republishing those
credits a competitor on our own site, so every candidate image is scanned before
it reaches the WordPress media library.

The pipeline this module implements, per docs/06 and the ROADMAP:

1. Decode the bytes with OpenCV.
2. Scan five zones — the four corners plus the bottom-centre overlay strip —
   because that is where channel branding is placed. A full-frame scan would be
   several times slower and would flag legitimate text inside the photo (a shop
   sign, a licence plate, a protest banner) as a watermark.
3. In each zone, look for two independent signals:
   * **Text**, via Tesseract, matched against watermark-shaped patterns
     (`@handle`, `t.me/`, a bare domain, a copyright string).
   * **A high-contrast overlay mask**, via edge density and bimodal
     brightness — a logo plate that OCR cannot read is still a watermark.
4. Compute `coverage_ratio`: the fraction of the whole frame occupied by
   contaminated zones.
5. Decide. At or below :data:`MAX_CLEAN_COVERAGE_RATIO` (0.15) with no critical
   text, the image can be salvaged by cropping. Above it, or with a channel
   handle anywhere, the image is unusable and Package 3 must regenerate it.

**Blocking work runs in a thread.** OpenCV and Tesseract are synchronous C
libraries; a 4000x3000 JPEG costs hundreds of milliseconds to decode and OCR.
Per the strict-asyncio rule in ``AGENTS.md`` the whole analysis is dispatched
with :func:`asyncio.to_thread`, so the ARQ heartbeat and concurrent scrapes keep
making progress.

**A missing Tesseract binary degrades to mask-only detection** rather than
failing the image. The container ships Tesseract with `rus+eng`, but a developer
machine may not have it, and an unreadable OCR stage must not stop the pipeline
from noticing an obvious logo plate.

Nothing here writes to disk or to the database: bytes in, verdict out.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

import cv2
import numpy as np
import pytesseract
from numpy.typing import NDArray

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

#: OpenCV's own return type. Its stubs describe results as unions of ``Mat`` and
#: differently-typed ndarrays, so pinning every intermediate to
#: ``Image`` would require a cast at each call site without buying any
#: real safety — the dtype is guaranteed by ``IMREAD_COLOR`` and the greyscale
#: conversions, not by the annotation.
Image = NDArray[Any]

__all__ = [
    "CORNER_ZONE_RATIO",
    "MAX_CLEAN_COVERAGE_RATIO",
    "MIN_ANALYSABLE_EDGE",
    "WatermarkError",
    "WatermarkReport",
    "WatermarkZone",
    "analyse",
    "crop_watermarks",
    "crop_watermarks_async",
    "detect_watermarks",
    "detect_watermarks_sync",
]

#: Above this fraction of the frame the image is unusable and must be replaced.
#: The ROADMAP fixes it at 15%: below that a crop still leaves a publishable
#: photo, above it the crop would remove the subject along with the branding.
MAX_CLEAN_COVERAGE_RATIO: Final[float] = 0.15

#: Side of a corner zone, as a fraction of the frame. 28% per side means one
#: corner is ~7.8% of the frame, so two contaminated corners land just above the
#: 15% threshold — which matches the intent: one badge is croppable, branding in
#: two corners is not.
CORNER_ZONE_RATIO: Final[float] = 0.28

#: The bottom-centre strip, where `t.me/channel` captions are burned in.
BOTTOM_STRIP_HEIGHT_RATIO: Final[float] = 0.16
BOTTOM_STRIP_WIDTH_RATIO: Final[float] = 0.72

#: The top-centre strip, where news channel badge plates (e.g. ТОПОР)
#: are burned in.
TOP_STRIP_HEIGHT_RATIO: Final[float] = 0.22
TOP_STRIP_WIDTH_RATIO: Final[float] = 0.75

#: Images smaller than this in either dimension are rejected as unanalysable: a
#: thumbnail carries no readable watermark and OCR on it returns noise that would
#: produce false positives.
MIN_ANALYSABLE_EDGE: Final[int] = 64

#: Longest edge the analysis works on. A 4000px photo is downscaled first: OCR
#: gains nothing from the extra pixels for text this size, and the decode plus
#: edge detection cost grows with area. Ratios are resolution-independent, so the
#: verdict is unchanged.
MAX_ANALYSIS_EDGE: Final[int] = 1600

#: Edge-density floor for an overlay mask. A logo plate has hard synthetic
#: borders, so its Canny edge fraction sits well above a photographic region's.
#: Calibrated against rendered plates, which measure ~0.03 while a blurred photo
#: region measures ~0.000; the floor sits between the two rather than at the
#: intuitive-but-wrong "a few percent of pixels are edges".
MIN_OVERLAY_EDGE_DENSITY: Final[float] = 0.012

#: A watermark plate is close to two-tone — bright text on a dark bar, or the
#: reverse — so a disproportionate share of its pixels sit in the outer
#: brightness bands. A photographic region concentrates in the midtones instead.
#: A rendered plate measures ~0.28 against a blurred photo's ~0.0, so the floor
#: is well below the "mostly two-tone" figure a plate would reach only if it
#: filled the whole zone.
MIN_BIMODAL_RATIO: Final[float] = 0.10

#: Minimum OCR confidence for a word to count. Below this Tesseract is guessing
#: at texture, and treating that as text would reject clean photos.
def _ocr_confidence_floor() -> int:
    """Confidence floor, read from settings so an operator can tune it."""
    return settings.ocr_watermark_confidence


#: Patterns that identify a *watermark* rather than incidental text in the photo.
#: A Telegram handle or a `t.me` link is conclusive; a bare domain or a copyright
#: notice is strong. Written to tolerate OCR noise: Tesseract routinely reads
#: `@` as `©` or drops the dot in `t.me`.
#: A Telegram handle. The leading class is wider than ``@`` on purpose: Tesseract
#: reliably *misreads* ``@`` as one of ``y u w o c e`` or ``©`` depending on the
#: font and the plate's contrast, and requiring a literal ``@`` therefore misses
#: the majority of real watermarks. The trailing requirement carries the
#: discrimination instead — an underscore or a digit inside a lowercase run is a
#: handle shape, not a Russian word — so widening the prefix costs no precision.
_HANDLE_RE: Final[re.Pattern[str]] = re.compile(
    r"[@©yuwoce]\s?[a-z][a-z0-9]*[_0-9][a-z0-9_]{2,30}", re.I
)
_TELEGRAM_RE: Final[re.Pattern[str]] = re.compile(
    r"t\s?\.?\s?me\s?/|telegram\s?\.\s?me|tlgrm\s?\.\s?ru", re.I
)
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[a-z0-9][a-z0-9-]{1,30}\.(?:ru|com|net|org|info|news|media|tv|online|su)\b",
    re.I,
)
_COPYRIGHT_RE: Final[re.Pattern[str]] = re.compile(
    r"©|\(c\)|copyright|все\s+права|фото\s*:|источник\s*:", re.I
)
_CHANNEL_NAMES_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:топор|baza|база|shot|шот|mash|мэш|readovka|ридовка|силовики|прямой\s+эфир|"
    r"горячие\s+новости|breaking\s+news|тг-канал|telegram|112|тасс|tass|риа|риа\s+новости|ria|"
    r"известия|izvestia|коммерсант|kommersant|лента(?:\.ру)?|lenta(?:\.ru)?|f-news)\b",
    re.I,
)

#: Text that makes an image unusable outright, however small the coverage. A
#: competitor's channel handle burned into a photo cannot be published even if it
#: occupies 2% of the frame, because cropping it out is not always possible
#: without destroying the composition — and if it is, the crop path handles it.
_CRITICAL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _HANDLE_RE,
    _TELEGRAM_RE,
    _CHANNEL_NAMES_RE,
)

#: Every pattern, for reporting.
_ALL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _HANDLE_RE,
    _TELEGRAM_RE,
    _DOMAIN_RE,
    _COPYRIGHT_RE,
    _CHANNEL_NAMES_RE,
)

#: OCR is restricted to a single-line page layout (PSM 7): a corner badge is one
#: line of text, and the default full-page segmentation wastes time looking for
#: paragraph structure that is not there.
_TESSERACT_CONFIG: Final[str] = "--psm 7"

_ZONE_NAMES: Final[tuple[str, ...]] = (
    "top_left",
    "top_right",
    "top_center",
    "bottom_left",
    "bottom_right",
    "bottom_center",
    "center",
)


class WatermarkError(RuntimeError):
    """The image could not be analysed at all (undecodable or too small)."""


@dataclass(frozen=True, slots=True)
class WatermarkZone:
    """Analysis of one scanned region.

    ``area_ratio`` is the zone's share of the whole frame, so summing the
    contaminated zones gives the coverage ratio directly.
    """

    name: str
    area_ratio: float
    text: tuple[str, ...] = ()
    has_overlay_mask: bool = False
    edge_density: float = 0.0

    @property
    def is_contaminated(self) -> bool:
        """Whether this zone carries a watermark by either signal."""
        return bool(self.text) or self.has_overlay_mask


@dataclass(frozen=True, slots=True)
class WatermarkReport:
    """Full verdict for one image."""

    has_watermark: bool
    coverage_ratio: float
    detected_text: tuple[str, ...]
    needs_ai_generation: bool
    zones: tuple[WatermarkZone, ...] = ()
    critical_text: tuple[str, ...] = ()
    is_banner: bool = False
    is_text_card: bool = False
    width: int = 0
    height: int = 0
    ocr_available: bool = True
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_croppable(self) -> bool:
        """Whether cropping can salvage this image.

        True only when the contamination is small *and* confined to the top or
        bottom bands: a watermark in both a top and a bottom zone would require
        cutting from both ends, which leaves a letterboxed strip rather than a
        photograph.
        """
        if not self.has_watermark or self.needs_ai_generation or self.is_banner or self.is_text_card:
            return False
        return bool(_crop_band(self.zones))

    def to_dict(self) -> dict[str, Any]:
        """Render the verdict as the plain dict the worker persists."""
        return {
            "has_watermark": self.has_watermark,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "detected_text": list(self.detected_text),
            "needs_ai_generation": self.needs_ai_generation,
            "critical_text": list(self.critical_text),
            "is_banner": self.is_banner,
            "is_text_card": self.is_text_card,
            "is_croppable": self.is_croppable,
            "width": self.width,
            "height": self.height,
            "ocr_available": self.ocr_available,
            "zones": [
                {
                    "name": zone.name,
                    "area_ratio": round(zone.area_ratio, 4),
                    "text": list(zone.text),
                    "has_overlay_mask": zone.has_overlay_mask,
                    "edge_density": round(zone.edge_density, 4),
                }
                for zone in self.zones
                if zone.is_contaminated
            ],
            "warnings": list(self.warnings),
        }


def _configure_tesseract() -> bool:
    """Point pytesseract at the configured binary; report whether it works.

    The configured path is honoured when it exists, otherwise the binary is left
    to ``PATH``: the container installs Tesseract at ``/usr/bin/tesseract`` while
    a developer machine typically has it under ``/usr/local/bin``, and hard-coding
    either would break the other.
    """
    configured = settings.tesseract_cmd
    if configured.exists():
        pytesseract.pytesseract.tesseract_cmd = str(configured)
    try:
        pytesseract.get_tesseract_version()
    # pytesseract raises its own error type for a missing binary, but a broken
    # install surfaces as OSError; neither may abort the analysis.
    except Exception:
        logger.warning(
            "Tesseract is unavailable (configured path: %s); watermark detection "
            "will use overlay-mask analysis only",
            configured,
            exc_info=True,
        )
        return False
    return True


#: Resolved once per process: probing the binary costs a subprocess spawn.
_OCR_AVAILABLE: Final[bool] = _configure_tesseract()


def _decode(image_bytes: bytes) -> Image:
    """Decode image bytes into a BGR array, rejecting anything unusable."""
    if not image_bytes:
        raise WatermarkError("image payload is empty")
    buffer = np.frombuffer(image_bytes, dtype=np.uint8)
    decoded = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if decoded is None:
        raise WatermarkError(
            f"OpenCV could not decode the image ({len(image_bytes)} bytes); "
            "it is not a supported raster format"
        )
    height, width = decoded.shape[:2]
    if min(height, width) < MIN_ANALYSABLE_EDGE:
        raise WatermarkError(
            f"image is {width}x{height}; smaller than {MIN_ANALYSABLE_EDGE}px on "
            "an edge it carries no readable watermark and OCR would return noise"
        )
    return decoded


def _downscale(image: Image) -> Image:
    """Shrink an oversized image so analysis cost stays bounded.

    Ratios are resolution-independent, so the verdict does not change; only the
    time spent does. ``INTER_AREA`` is the correct filter for downscaling and
    preserves the edges the mask detector relies on.
    """
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= MAX_ANALYSIS_EDGE:
        return image
    scale = MAX_ANALYSIS_EDGE / longest
    resized = cv2.resize(
        image,
        (max(int(width * scale), 1), max(int(height * scale), 1)),
        interpolation=cv2.INTER_AREA,
    )
    return resized


@dataclass(frozen=True, slots=True)
class _Rect:
    """A zone's pixel bounds within the analysed frame."""

    name: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def area(self) -> int:
        return max(self.right - self.left, 0) * max(self.bottom - self.top, 0)


def _zone_rects(width: int, height: int) -> tuple[_Rect, ...]:
    """Build the five scan regions for a frame of this size.

    Corners are square-ish blocks scaled to the frame; the bottom-centre strip is
    wide and shallow, matching how `t.me/channel` captions are burned in.
    """
    corner_w = max(int(width * CORNER_ZONE_RATIO), 1)
    corner_h = max(int(height * CORNER_ZONE_RATIO), 1)
    strip_h = max(int(height * BOTTOM_STRIP_HEIGHT_RATIO), 1)
    strip_w = max(int(width * BOTTOM_STRIP_WIDTH_RATIO), 1)
    strip_left = (width - strip_w) // 2
    top_strip_h = max(int(height * TOP_STRIP_HEIGHT_RATIO), 1)
    top_strip_w = max(int(width * TOP_STRIP_WIDTH_RATIO), 1)
    top_strip_left = (width - top_strip_w) // 2
    center_w = max(int(width * 0.70), 1)
    center_h = max(int(height * 0.60), 1)
    center_left = (width - center_w) // 2
    center_top = (height - center_h) // 2
    return (
        _Rect("top_left", 0, 0, corner_w, corner_h),
        _Rect("top_right", width - corner_w, 0, width, corner_h),
        _Rect(
            "top_center",
            top_strip_left,
            0,
            top_strip_left + top_strip_w,
            top_strip_h,
        ),
        _Rect("bottom_left", 0, height - corner_h, corner_w, height),
        _Rect("bottom_right", width - corner_w, height - corner_h, width, height),
        _Rect(
            "bottom_center",
            strip_left,
            height - strip_h,
            strip_left + strip_w,
            height,
        ),
        _Rect(
            "center",
            center_left,
            center_top,
            center_left + center_w,
            center_top + center_h,
        ),
    )


def _match_patterns(text: str) -> tuple[str, ...]:
    """Return the watermark-shaped fragments found in one OCR result."""
    found: list[str] = []
    for pattern in _ALL_PATTERNS:
        found.extend(match.group(0).strip() for match in pattern.finditer(text))
    return tuple(dict.fromkeys(item for item in found if item))


def _is_critical(text: str) -> bool:
    """Whether text makes the image unusable regardless of coverage."""
    return any(pattern.search(text) is not None for pattern in _CRITICAL_PATTERNS)


def _prepare_for_ocr(region: Image) -> Image:
    """Binarise a zone so overlay text becomes legible to Tesseract.

    Watermarks are usually light text over a darker photo, at low contrast. Otsu
    thresholding on the greyscale zone separates the two populations without a
    hand-tuned cut-off, and upscaling small zones first gives Tesseract the
    ~20px glyph height it needs to be reliable.
    """
    grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    # Upscaling *before* thresholding is what makes the difference: Tesseract
    # wants ~30px glyphs, and interpolating the greyscale keeps the anti-aliased
    # stroke edges that Otsu then separates cleanly. Thresholding first and
    # scaling the binary image afterwards magnifies the jagged mask instead and
    # measurably degrades the reading.
    grey = cv2.resize(grey, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def _ocr_zone(region: Image) -> str:
    """Read text from one zone, tolerating every OCR failure mode.

    Both the light-on-dark and dark-on-light renderings are tried, because a
    watermark may be either and Otsu picks one polarity. Any exception degrades
    to "no text": a zone Tesseract cannot read must not fail the image, since the
    overlay-mask signal still applies.
    """
    binary = _prepare_for_ocr(region)
    readings: list[str] = []
    for candidate in (binary, cv2.bitwise_not(binary)):
        try:
            readings.append(
                pytesseract.image_to_string(
                    candidate,
                    lang=settings.tesseract_languages,
                    config=_TESSERACT_CONFIG,
                )
            )
        except Exception:
            logger.debug("Tesseract failed on a zone", exc_info=True)
    return " ".join(reading.strip() for reading in readings if reading.strip())


def _has_overlay_mask(region: Image) -> tuple[bool, float]:
    """Detect a high-contrast overlay plate by edge density and bimodality.

    Two signals must agree, which is what keeps the false-positive rate usable:

    * **Edge density** — a logo plate has hard synthetic borders, so its Canny
      edge fraction is far above that of a photographic region.
    * **Bimodal brightness** — a plate is close to two-tone, so most of its
      pixels sit in the dark or bright tail with little in between.

    Requiring both means a busy but photographic corner (foliage, a crowd, a
    brick wall) is not flagged: it has the edges but not the bimodality.
    """
    grey = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(grey, 100, 200)
    edge_density = float(np.count_nonzero(edges)) / float(edges.size or 1)

    histogram = cv2.calcHist([grey], [0], None, [32], [0, 256]).flatten()
    total = float(histogram.sum()) or 1.0
    # Outer sixths of the range: the two tails a two-tone plate occupies.
    tails = float(histogram[:5].sum() + histogram[-5:].sum())
    bimodal_ratio = tails / total

    detected = edge_density >= MIN_OVERLAY_EDGE_DENSITY and (
        bimodal_ratio >= MIN_BIMODAL_RATIO
    )
    return detected, edge_density


def _analyse_zone(
    image: Image, rect: _Rect, frame_area: int, *, ocr: bool
) -> WatermarkZone:
    """Scan one region for text and for an overlay plate."""
    region = image[rect.top : rect.bottom, rect.left : rect.right]
    if region.size == 0:
        return WatermarkZone(name=rect.name, area_ratio=0.0)

    text_hits: tuple[str, ...] = ()
    if ocr:
        raw = _ocr_zone(region)
        if raw:
            text_hits = _match_patterns(raw)

    has_mask, edge_density = _has_overlay_mask(region)
    return WatermarkZone(
        name=rect.name,
        area_ratio=rect.area / float(frame_area or 1),
        text=text_hits,
        has_overlay_mask=has_mask,
        edge_density=edge_density,
    )


def _crop_band(zones: tuple[WatermarkZone, ...]) -> str | None:
    """Which horizontal band can be cropped away, if any.

    Returns ``"top"``, ``"bottom"`` or ``None``. Contamination in both bands has
    no crop that preserves a usable photograph, so it is reported as
    uncroppable rather than cropped twice.
    """
    contaminated = {zone.name for zone in zones if zone.is_contaminated}
    if not contaminated:
        return None
    # Center zone contamination can never be safely cropped
    if "center" in contaminated:
        return None
    top = contaminated & {"top_left", "top_right", "top_center"}
    bottom = contaminated & {"bottom_left", "bottom_right", "bottom_center"}
    if top and bottom:
        return None
    return "top" if top else "bottom"


def _detect_monochrome_splash_banner(image: Image) -> tuple[bool, str]:
    """Detect flat solid/gradient splash cards (e.g. TASS, 112, RIA text banners)."""
    h, w = image.shape[:2]
    if h == 0 or w == 0:
        return False, ""
    small = cv2.resize(image, (120, 80), interpolation=cv2.INTER_AREA)
    # Check if a truly flat background color exists with near-zero variance
    quant = (small // 16) * 16
    pixels = quant.reshape(-1, 3)
    unique_colors, counts = np.unique(pixels, axis=0, return_counts=True)
    if len(counts) == 0:
        return False, ""

    max_idx = int(np.argmax(counts))
    max_count = int(counts[max_idx])
    dominant_fraction = max_count / float(pixels.shape[0])

    if dominant_fraction >= 0.55:
        dominant_color = unique_colors[max_idx]
        b_val, g_val, r_val = (
            int(dominant_color[0]),
            int(dominant_color[1]),
            int(dominant_color[2]),
        )
        brightness = (r_val + g_val + b_val) / 3.0
        color_spread = max(r_val, g_val, b_val) - min(r_val, g_val, b_val)
        # Splash banners are either bright white (>=210), dark/black (<=55), or saturated color (>=30)
        if brightness >= 210 or brightness <= 55 or color_spread >= 30:
            grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(grey, 80, 180)
            edge_ratio = float(np.count_nonzero(edges)) / float(edges.size or 1)
            if edge_ratio >= 0.008:
                return True, f"monochrome banner card ({dominant_fraction:.0%} solid background)"
    return False, ""


def _detect_text_heavy_card(text: str) -> tuple[bool, str]:
    """Detect if OCR found full news sentences/press-release cards instead of a photo."""
    words = [w for w in re.findall(r"[A-Za-zА-Яа-я0-9]{3,}", text) if not w.isdigit()]
    if len(words) >= 12:
        snippet = " ".join(words[:5])
        return True, f"text-heavy card ({len(words)} words: '{snippet} ...')"
    return False, ""


def detect_watermarks_sync(image_bytes: bytes) -> dict[str, Any]:
    """Synchronous analysis. Call :func:`detect_watermarks` from async code.

    Exposed for the ARQ media worker's own thread and for tests; production async
    callers must use the wrapper so the event loop is never blocked.
    """
    return analyse(image_bytes).to_dict()


def analyse(image_bytes: bytes) -> WatermarkReport:
    """Analyse one image and return the structured verdict.

    Raises :class:`WatermarkError` when the payload cannot be decoded or is too
    small to carry a readable watermark; the caller treats that as "no usable
    image" and regenerates.
    """
    original = _decode(image_bytes)
    original_h, original_w = original.shape[:2]
    image = _downscale(original)
    height, width = image.shape[:2]
    frame_area = height * width

    zones = tuple(
        _analyse_zone(image, rect, frame_area, ocr=_OCR_AVAILABLE)
        for rect in _zone_rects(width, height)
    )

    detected_text: list[str] = []
    critical: list[str] = []
    for zone in zones:
        for phrase in zone.text:
            detected_text.append(phrase)
            if _is_critical(phrase):
                critical.append(phrase)

    # Check for flat monochrome splash cards (TASS blue banner, 112 white banner)
    is_banner, banner_reason = _detect_monochrome_splash_banner(image)
    if is_banner:
        detected_text.append(banner_reason)
        critical.append(banner_reason)

    # Check for text-heavy cards (news release snippets, infographics with sentences)
    all_raw_text = " ".join(detected_text)
    is_text_card, text_card_reason = _detect_text_heavy_card(all_raw_text)
    if is_text_card:
        detected_text.append(text_card_reason)
        critical.append(text_card_reason)

    # Guard against diagrams, maps, blueprints, and line-art:
    # A genuine watermark overlay plate is local (1-3 edge zones). If the overlay mask
    # trips across 4+ zones without ANY detected text, critical branding, banner or card,
    # the image is a graphic diagram or map (e.g. metro plan or blueprint), NOT a watermarked photo.
    mask_only_zones = [z for z in zones if z.has_overlay_mask and not z.text]
    if len(mask_only_zones) >= 4 and not detected_text and not critical and not is_banner and not is_text_card:
        logger.info(
            "Graphic/diagram detected: overlay mask across %d zones without text, treating as clean graphic content",
            len(mask_only_zones),
        )
        zones = tuple(
            WatermarkZone(
                name=z.name,
                area_ratio=z.area_ratio,
                text=z.text,
                has_overlay_mask=False,
                edge_density=z.edge_density,
            )
            for z in zones
        )

    # Overlapping zones are not double-counted: the bottom-centre strip shares
    # pixels with both bottom corners, so summing raw areas would inflate the
    # ratio past the threshold on a single caption bar.
    coverage_ratio = _coverage(zones, width, height)

    has_watermark = any(zone.is_contaminated for zone in zones) or is_banner or is_text_card
    needs_ai = bool(critical) or coverage_ratio > MAX_CLEAN_COVERAGE_RATIO or is_banner or is_text_card

    warnings: list[str] = []
    if not _OCR_AVAILABLE:
        warnings.append(
            "Tesseract unavailable: only high-contrast overlay plates were detected"
        )

    report = WatermarkReport(
        has_watermark=has_watermark,
        coverage_ratio=coverage_ratio,
        detected_text=tuple(dict.fromkeys(detected_text)),
        needs_ai_generation=needs_ai,
        zones=zones,
        critical_text=tuple(dict.fromkeys(critical)),
        is_banner=is_banner,
        is_text_card=is_text_card,
        width=original_w,
        height=original_h,
        ocr_available=_OCR_AVAILABLE,
        warnings=tuple(warnings),
    )
    logger.debug(
        "Watermark scan %dx%d: watermark=%s coverage=%.3f regenerate=%s banner=%s text_card=%s text=%s",
        original_w,
        original_h,
        has_watermark,
        coverage_ratio,
        needs_ai,
        is_banner,
        is_text_card,
        report.detected_text[:3],
    )
    return report


def _coverage(
    zones: tuple[WatermarkZone, ...], width: int, height: int
) -> float:
    """Fraction of the frame occupied by contaminated zones, without overlap.

    A boolean mask is used rather than a sum of ``area_ratio`` values because the
    bottom-centre strip overlaps both bottom corners. Summing would report ~23%
    for a single caption bar and wrongly send a salvageable image to
    regeneration.
    """
    contaminated = [zone.name for zone in zones if zone.is_contaminated]
    if not contaminated:
        return 0.0
    mask = np.zeros((height, width), dtype=bool)
    for rect in _zone_rects(width, height):
        if rect.name in contaminated:
            mask[rect.top : rect.bottom, rect.left : rect.right] = True
    return float(np.count_nonzero(mask)) / float(mask.size or 1)


async def detect_watermarks(image_bytes: bytes) -> dict[str, Any]:
    """Analyse one image off the event loop.

    Returns ``has_watermark``, ``coverage_ratio``, ``detected_text``,
    ``needs_ai_generation``, plus the per-zone breakdown, ``critical_text``,
    ``is_croppable`` and the image dimensions.

    OpenCV and Tesseract are blocking C extensions, so the work is dispatched to
    the default thread pool per the strict-asyncio rule in ``AGENTS.md``.
    """
    return await asyncio.to_thread(detect_watermarks_sync, image_bytes)


def crop_watermarks(image_bytes: bytes) -> bytes:
    """Return the image with its watermarked band cropped away.

    Only meaningful when :attr:`WatermarkReport.is_croppable` is true — the
    contamination is light and confined to one horizontal band. The band is cut
    to the depth of the deepest contaminated zone plus a small margin, so a
    caption bar is removed rather than merely clipped.

    Raises :class:`WatermarkError` when the image is not croppable, rather than
    returning it unchanged: a caller that received bytes back would reasonably
    assume the watermark was gone.
    """
    report = analyse(image_bytes)
    band = _crop_band(report.zones)
    if not report.has_watermark or report.needs_ai_generation or band is None:
        raise WatermarkError(
            "image is not croppable: contamination is absent, too large, or "
            "present in both the top and bottom bands"
        )

    image = _decode(image_bytes)
    height, width = image.shape[:2]
    contaminated = {zone.name for zone in report.zones if zone.is_contaminated}

    if band == "top":
        depth = int(height * CORNER_ZONE_RATIO)
        cropped = image[depth:height, 0:width]
    else:
        # The bottom strip is shallower than a corner, so a caption-only
        # watermark costs less of the photo than a corner badge does.
        only_strip = contaminated == {"bottom_center"}
        ratio = BOTTOM_STRIP_HEIGHT_RATIO if only_strip else CORNER_ZONE_RATIO
        depth = int(height * ratio)
        cropped = image[0 : height - depth, 0:width]

    if cropped.size == 0 or min(cropped.shape[:2]) < MIN_ANALYSABLE_EDGE:
        raise WatermarkError(
            "cropping the watermarked band would leave an unusably small image"
        )

    success, encoded = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not success:
        raise WatermarkError("the cropped image could not be re-encoded")
    return bytes(encoded.tobytes())


async def crop_watermarks_async(image_bytes: bytes) -> bytes:
    """Crop the watermarked band off the event loop.

    OpenCV decode, crop and re-encode are blocking, so they run in a thread like
    the analysis itself.
    """
    return await asyncio.to_thread(crop_watermarks, image_bytes)
