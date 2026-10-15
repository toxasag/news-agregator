# ruff: noqa: RUF001, E501, PLR0915, PLR0912, BLE001, SIM102
"""Image cleaning, perceptual deduplication, and quality inspection for news media.

Provides zero-token, local OpenCV/NumPy inspection for external web images:
1. **Perceptual Deduplication (dHash)**: Computes a 64-bit difference hash
   and checks Hamming distance. Images with distance <= 6 are marked as duplicates.
2. **Quality & Blur/Solid Detection**: Discards low-resolution thumbnails (<350x200),
   extreme banner aspect ratios, and blurry/uniform placeholder gradients.
3. **Watermark & Logo Detection**: Integrates with :mod:`app.services.watermark_detector`
   to detect media agency logos, channel tags, and watermark plates.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import cv2
import httpx
import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.ai_client import get_ai_client
from app.services.model_routing import resolve_chain
from app.services.watermark_detector import analyse, crop_watermarks

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_HAMMING_DUPLICATE_THRESHOLD",
    "MIN_IMAGE_HEIGHT",
    "MIN_IMAGE_WIDTH",
    "clean_web_images",
    "compute_dhash",
    "filter_and_deduplicate_web_images",
    "hamming_distance",
    "inject_article_images",
    "inspect_image_quality",
    "is_blurry_or_solid",
    "verify_images_with_ai",
]

#: Minimum dimensions for editorial illustrations (excludes avatars, icons, buttons).
MIN_IMAGE_WIDTH: Final[int] = 450
MIN_IMAGE_HEIGHT: Final[int] = 300

#: Aspect ratio bounds: 0.45 (tall portrait) to 2.7 (wide panoramic banner).
MIN_ASPECT_RATIO: Final[float] = 0.45
MAX_ASPECT_RATIO: Final[float] = 2.7

#: Maximum Hamming distance out of 64 bits to consider two images duplicates.
#: A distance of <= 6 corresponds to > 90% perceptual similarity.
MAX_HAMMING_DUPLICATE_THRESHOLD: Final[int] = 6

#: Minimum Laplacian variance to distinguish a sharp photograph from a blurred gradient.
MIN_LAPLACIAN_VARIANCE: Final[float] = 35.0

#: Minimum standard deviation of pixel intensities (rejects blank/solid rectangles).
MIN_PIXEL_STD: Final[float] = 12.0

#: Substrings in URL path indicating non-editorial graphic assets.
_BLACKLIST_URL_SUBSTRINGS: Final[tuple[str, ...]] = (
    "logo",
    "avatar",
    "favicon",
    "banner",
    "promo",
    "advert",
    "placeholder",
    "button",
    "pixel",
    "spacer",
    "spinner",
    "brandmark",
    "sharing",
    "share",
    "og_image",
    "og-image",
    "opengraph",
    "snippet",
    "setka",
    "social",
    "preview_share",
    "preview",
    "splash",
    "default",
    "widget",
    "thumb",
    "header",
)

#: Telegram aggregators and channel-index sites — their "images" are always
#: screenshots of channel previews or promotional banners, never editorial photos.
_AGGREGATOR_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "tvgram.ru",
        "tgstat.ru",
        "tgstat.com",
        "telemetr.io",
        "combot.org",
        "tg.guru",
        "tgdb.ru",
        "push.ru",
        "teleg.run",
        "t.me",           # direct Telegram links produce preview images
    }
)

#: Mean pixel brightness threshold — images darker than this are considered
#: dark promotional screens (e.g. channel announcement on black background).
MIN_MEAN_BRIGHTNESS: Final[float] = 28.0


def compute_dhash(image_bytes: bytes) -> int | None:
    """Calculate 64-bit difference hash (dHash) from image bytes.

    Resizes to 9x8 grayscale and computes horizontal gradient differences.
    Returns a 64-bit unsigned integer or None if decoding fails.
    """
    if not image_bytes:
        return None
    try:
        arr = np.frombuffer(image_bytes, np.uint8)
        gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if gray is None or gray.size == 0:
            return None
        # Resize to 9 columns x 8 rows
        resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
        # Compute difference between adjacent horizontal pixels: (8 rows, 8 cols)
        diff = resized[:, 1:] > resized[:, :-1]
        # Pack 64 boolean values into a 64-bit integer
        hash_val = 0
        for bit in diff.flatten():
            hash_val = (hash_val << 1) | int(bit)
        return hash_val
    except Exception as exc:
        logger.debug("Failed to compute dHash: %s", exc)
        return None


def hamming_distance(hash1: int, hash2: int) -> int:
    """Return the bitwise Hamming distance between two 64-bit hashes."""
    return (hash1 ^ hash2).bit_count()


def is_blurry_or_solid(
    image_bytes: bytes,
    min_variance: float = MIN_LAPLACIAN_VARIANCE,
    min_std: float = MIN_PIXEL_STD,
) -> bool:
    """Return True if image is blurred, solid block, dark promo, or lacks edges."""
    if not image_bytes:
        return True
    try:
        arr = np.frombuffer(image_bytes, np.uint8)
        gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if gray is None or gray.size == 0:
            return True

        total_pixels = float(gray.size)
        mean_b = float(np.mean(gray))
        dark_ratio = float(np.count_nonzero(gray < 25)) / total_pixels
        white_ratio = float(np.count_nonzero(gray > 245)) / total_pixels
        std_val = float(np.std(gray))
        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        return bool(
            mean_b < MIN_MEAN_BRIGHTNESS
            or dark_ratio > 0.65
            or white_ratio > 0.65
            or std_val < min_std
            or lap_var < min_variance
        )
    except Exception:
        return True


def _check_dimensions_and_blur(
    img: np.ndarray, image_bytes: bytes
) -> tuple[bool, str]:
    """Validate image resolution, aspect ratio, square logos, and blur/solid status."""
    h, w = img.shape[:2]
    if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
        err = f"Resolution too low ({w}x{h} < {MIN_IMAGE_WIDTH}x{MIN_IMAGE_HEIGHT})"
        return False, err

    ratio = w / float(h)
    if ratio < MIN_ASPECT_RATIO or ratio > MAX_ASPECT_RATIO:
        return False, f"Abnormal aspect ratio ({ratio:.2f})"

    # Square icon or abstract logo check: editorial news photos are wide (16:9 or 3:2), not square badges
    if 0.82 <= ratio <= 1.22 and (w < 800 or h < 800):
        small_rgb = cv2.resize(img, (64, 64), interpolation=cv2.INTER_AREA)
        quant = (small_rgb // 32) * 32
        unique_colors = len(np.unique(quant.reshape(-1, 3), axis=0))
        if unique_colors < 96 or w < 600 or h < 600:
            return False, f"Square logo/badge graphic ({w}x{h}, {unique_colors} colors)"

    if is_blurry_or_solid(image_bytes):
        return False, "Blurry placeholder or flat solid graphic"

    return True, "ok"


def _check_watermark(
    image_bytes: bytes, *, allow_watermarked: bool = True
) -> tuple[bool, str, bytes]:
    """Check for watermark plates, splash banners, or logos and attempt border cropping."""
    report = analyse(image_bytes)

    # 1. Unpublishable banners, text cards and channel badges (TASS, 112, RIA text cards, Baza, etc.)
    # These must NEVER be published regardless of allow_watermarked!
    if report.is_banner or report.is_text_card:
        reason = "monochrome splash banner" if report.is_banner else "text-heavy card"
        crit_sample = ", ".join(report.critical_text[:2]) or reason
        return False, f"Unusable media: {reason} ({crit_sample})", image_bytes

    if report.critical_text:
        crit_sample = ", ".join(report.critical_text[:2])
        # If the channel logo is in the center or occupies significant area, reject outright
        return False, f"Channel splash logo detected: {crit_sample}", image_bytes

    # 2. Border watermarks / handles: attempt safe border crop
    if report.has_watermark:
        if report.is_croppable:
            with contextlib.suppress(Exception):
                cropped = crop_watermarks(image_bytes)
                if cropped and len(cropped) > 4096 and cropped != image_bytes:
                    c_arr = np.frombuffer(cropped, np.uint8)
                    c_img = cv2.imdecode(c_arr, cv2.IMREAD_COLOR)
                    if c_img is not None:
                        ch, cw = c_img.shape[:2]
                        if cw >= MIN_IMAGE_WIDTH and ch >= MIN_IMAGE_HEIGHT:
                            return True, "Watermark cropped safely", cropped

        crit_sample = f"coverage {report.coverage_ratio:.1%}"
        if allow_watermarked and not report.needs_ai_generation:
            return True, f"Source photo preserved ({crit_sample})", image_bytes

        if not allow_watermarked or report.needs_ai_generation:
            return False, f"Watermark detected: {crit_sample}", image_bytes

    return True, "Clean photo", image_bytes


def inspect_image_quality(
    image_bytes: bytes,
    *,
    url: str = "",
    allow_watermarked: bool = True,
) -> tuple[bool, str, bytes]:
    """Inspect image bytes for resolution, aspect ratio, blur, and watermarks."""
    if not image_bytes or len(image_bytes) < 100:
        return False, "Payload too small or empty", image_bytes

    try:
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False, "Could not decode image", image_bytes

        valid_dims, dim_err = _check_dimensions_and_blur(img, image_bytes)
        if not valid_dims:
            return False, dim_err, image_bytes

        if url:
            url_lower = url.lower()
            for b in _BLACKLIST_URL_SUBSTRINGS:
                if b in url_lower:
                    return (
                        False,
                        f"URL rejected by non-editorial blacklist ({b})",
                        image_bytes,
                    )

        return _check_watermark(
            image_bytes, allow_watermarked=allow_watermarked
        )
    except Exception as exc:
        logger.debug("Image quality inspection error for %s: %s", url, exc)
        return False, f"Inspection error: {exc}", image_bytes


def _normalize_image_url(url: str) -> str:
    """Strip dynamic resizing and tracking query parameters for base deduplication."""
    parsed = urlparse(url)
    clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return clean_url.lower()


async def _fetch_image_bytes(
    url: str,
    client: httpx.AsyncClient,
    cache_dir: Path,
) -> bytes:
    """Fetch image bytes with disk caching under settings.media_storage_path."""
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    cache_file = cache_dir / f"{url_hash}.img"

    if cache_file.exists():
        with contextlib.suppress(OSError):
            data = cache_file.read_bytes()
            if len(data) > 0:
                return data

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 200 and len(resp.content) > 1024:
            with contextlib.suppress(OSError):
                cache_file.write_bytes(resp.content)
            return resp.content
    except Exception:
        logger.debug("Failed to fetch image for inspection: %s", url)
    return b""


def _is_perceptual_duplicate(
    dhash: int | None,
    accepted_hashes: Sequence[int],
    max_threshold: int = MAX_HAMMING_DUPLICATE_THRESHOLD,
) -> bool:
    """Return True if dhash is within max_threshold distance of accepted hashes."""
    if dhash is None:
        return False
    return any(hamming_distance(dhash, prev) <= max_threshold for prev in accepted_hashes)


def _filter_url_candidates(
    images: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter image URLs against blacklist and deduplicate normalized paths."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for img in images:
        raw_url = str(img.get("url") or "").strip()
        if not raw_url.startswith(("http://", "https://")):
            continue
        raw_lower = raw_url.lower()
        if any(b in raw_lower for b in _BLACKLIST_URL_SUBSTRINGS):
            continue
        # Reject images from Telegram aggregator/index sites
        img_domain = urlparse(raw_url).netloc.lower().removeprefix("www.")
        if img_domain in _AGGREGATOR_DOMAINS or any(
            img_domain.endswith("." + agg) for agg in _AGGREGATOR_DOMAINS
        ):
            logger.debug("Rejected aggregator image from %s: %s", img_domain, raw_url)
            continue
        norm = _normalize_image_url(raw_url)
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append(dict(img))
    return candidates


#: Substrings in image URLs, filenames or titles that signal non-article sidebar or presenter media.
_OFFTOPIC_URL_HINTS: Final[frozenset[str]] = frozenset(
    {
        "anchor", "presenter", "vedusch", "vedushaya", "vedushiy", "televedusch",
        "diktora", "diktor", "studio", "studiya",
        "futbol", "football", "match", "soccer", "hokkey", "hockey",
        "sport", "chempionat", "turnir",
        "recipe", "recept", "eda", "kulinari", "syrniki", "blyudo",
        "avatar", "author", "avtor", "columnist", "speaker-headshot",
    }
)


async def verify_images_with_ai(
    images: Sequence[dict[str, Any]],
    *,
    story_title: str,
    story_text: str = "",
    session: AsyncSession | None = None,
    request_timeout: float = 12.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify web images using LLM to ensure they genuinely illustrate the news story.

    Filters out off-topic images such as TV studio news anchors, sports players/matches,
    random traffic cars, food/recipes, and advertisements.

    Returns:
        tuple of (approved_images, rejected_images)
    """
    if not images:
        return [], []

    candidates_for_ai: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []

    st_title_lower = story_title.strip().lower()

    for img in images:
        raw_url = str(img.get("url") or "").lower()
        title_val = str(img.get("title") or img.get("alt") or "").lower()
        combined = f"{raw_url} {title_val}"

        is_obvious_offtopic = False
        matched_hint = ""
        for hint in _OFFTOPIC_URL_HINTS:
            if hint in combined:
                if hint not in st_title_lower:
                    is_obvious_offtopic = True
                    matched_hint = hint
                    break

        if is_obvious_offtopic:
            off_img = dict(img)
            off_img["clean_verdict"] = f"Отсеяно: оффтопик ({matched_hint})"
            off_img["badge_label"] = "Оффтопик"
            off_img["badge_type"] = "warn"
            off_img["ai_verified"] = True
            off_img["is_ai_approved"] = False
            rejected.append(off_img)
        else:
            candidates_for_ai.append(dict(img))

    if not candidates_for_ai:
        return approved, rejected

    if not story_title.strip():
        return candidates_for_ai, rejected

    # Batch prompt for LLM evaluation
    batch = candidates_for_ai[:8]
    prompt_lines = [
        f"НОВОСТЬ: {story_title.strip()}",
    ]
    if story_text.strip():
        prompt_lines.append(f"КОНТЕКСТ: {story_text.strip()[:600]}")
    prompt_lines.append("\nКАНДИДАТЫ НА ИЛЛЮСТРАЦИЮ:")
    for idx, c in enumerate(batch):
        c_url = str(c.get("url") or "")
        c_file = c_url.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
        c_title = str(c.get("title") or c.get("alt") or "").strip()
        c_domain = str(c.get("domain") or "").strip()
        prompt_lines.append(
            f"[{idx}] Источник: {c_domain} | Заголовок/Альт: {c_title} | Файл: {c_file}"
        )

    user_prompt = "\n".join(prompt_lines)
    system_instruction = (
        "Ты — строгий главный бильд-редактор федерального новостного издания. "
        "Твоя задача — отобрать только те фотографии, которые действительно иллюстрируют главных героев и суть новости.\n\n"
        "КРИТЕРИИ ОДОБРЕНИЯ (relevant: true):\n"
        "1. Фото непосредственно показывает главных действующих лиц, упомянутых в новости (если новость о персонах — на фото должны быть именно они, а не случайные люди или абстрактные силовики!).\n"
        "2. Для происшествий: непосредственное место события, спецтехника, спасатели или последствия.\n\n"
        "КРИТЕРИИ ОТКЛОНЕНИЯ (relevant: false — СТРОГО БРАКОВАТЬ):\n"
        "1. Архивные общие планы других событий: если новость о семье Трампа или политиках — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО одобрять абстрактных агентов ФБР в куртках, силовиков или здания, если на фото нет ключевой персоны новости!\n"
        "2. Телеведущие/дикторы в студии (лица за студийным столом, микрофоны студии, вечерние платья/костюмы).\n"
        "3. Случайные легковые автомобили на дороге, общие планы трафика (если новость не про это конкретное ДТП).\n"
        "4. Спорт, футболисты, стадионы (если новость не про этот матч).\n"
        "5. Еда, рецепты, блюда (если новость не кулинарная).\n"
        "6. Рекламные баннеры, графики, случайные фото из соседних разделов портала.\n\n"
        "Верни СТРОГО JSON массив без пояснений:\n"
        '[{"index": 0, "relevant": true, "reason": "портрет Мелании Трамп"}, {"index": 1, "relevant": false, "reason": "абстрактные агенты ФБР со спины без персоны"}]'
    )

    eval_map: dict[int, dict[str, Any]] = {}
    try:
        chain: list[str] = []
        if session is not None:
            chain = await resolve_chain(session, "arbiter")
        if not chain:
            chain = [settings.ai_model_arbiter]

        model_name = chain[0] if chain else settings.ai_model_arbiter
        client = get_ai_client()
        resp_text = await asyncio.wait_for(
            client.complete(
                prompt=user_prompt,
                system=system_instruction,
                model=model_name,
                temperature=0.1,
                max_tokens=400,
            ),
            timeout=request_timeout,
        )

        clean_json = resp_text.strip()
        if "```" in clean_json:
            clean_json = re.sub(r"^```(?:json)?\s*", "", clean_json)
            clean_json = re.sub(r"\s*```$", "", clean_json)
        match_arr = re.search(r"\[\s*\{.*\}\s*\]", clean_json, re.DOTALL)
        if match_arr:
            clean_json = match_arr.group(0)

        eval_data = json.loads(clean_json)
        if isinstance(eval_data, list):
            for entry in eval_data:
                if isinstance(entry, dict) and "index" in entry:
                    eval_map[int(entry["index"])] = entry
    except Exception as ai_err:
        logger.warning(
            "AI image verification failed (%s); applying heuristic rules",
            ai_err,
        )

    # Critical event keywords for heuristic fallback when AI is unavailable or omits an entry
    critical_words = {"пожар", "огон", "горит", "плам", "дтп", "авари", "взрыв", "дрон", "суд"}
    story_has_critical = any(w in st_title_lower for w in critical_words)

    for idx, item in enumerate(batch):
        ev = eval_map.get(idx)
        if ev and isinstance(ev, dict):
            is_rel = bool(ev.get("relevant"))
            reason = str(ev.get("reason") or ("соответствует теме" if is_rel else "оффтопик"))
            if is_rel:
                item["clean_verdict"] = f"Одобрено ИИ: {reason}"
                item["badge_label"] = "ИИ: Одобрено"
                item["badge_type"] = "good"
                item["ai_verified"] = True
                item["is_ai_approved"] = True
                approved.append(item)
            else:
                item["clean_verdict"] = f"Отклонено ИИ (оффтопик): {reason}"
                item["badge_label"] = "Оффтопик"
                item["badge_type"] = "warn"
                item["ai_verified"] = True
                item["is_ai_approved"] = False
                rejected.append(item)
        else:
            # Heuristic decision
            c_url = str(item.get("url") or "").lower()
            if story_has_critical and "пожар" in st_title_lower and "дтп" not in st_title_lower:
                if any(w in c_url for w in ("auto", "mashin", "car", "dorog", "traff")):
                    item["clean_verdict"] = "Отклонено эвристикой: случайный автомобиль"
                    item["badge_label"] = "Оффтопик"
                    item["badge_type"] = "warn"
                    item["is_ai_approved"] = False
                    rejected.append(item)
                    continue
            item["clean_verdict"] = "Одобрено (проверено)"
            item["badge_label"] = "Одобрено"
            item["badge_type"] = "good"
            item["is_ai_approved"] = True
            approved.append(item)

    # Any candidates beyond the 8-item batch
    for item in candidates_for_ai[8:]:
        item["clean_verdict"] = "Одобрено"
        item["badge_label"] = "Одобрено"
        item["badge_type"] = "good"
        item["is_ai_approved"] = True
        approved.append(item)

    return approved, rejected


async def filter_and_deduplicate_web_images(
    images: Sequence[dict[str, Any]],
    *,
    cache_dir: Path | None = None,
    fetch_timeout: float = 6.0,
    max_keep: int = 12,
    story_title: str = "",
    story_text: str = "",
    session: AsyncSession | None = None,
    return_rejected: bool = False,
) -> Any:
    """Clean, deduplicate, and filter a list of web image dictionaries.

    Applies:
    1. URL blacklist filter (icons, banners, logos).
    2. URL path normalization.
    3. Resolution, aspect ratio, blur, and watermark check.
    4. Perceptual dHash deduplication (Hamming distance <= 6).
    5. AI verification for story topic relevance if story_title is provided.

    Returns:
        List of unique, relevant images (or tuple of (approved, rejected) if return_rejected=True).
    """
    if not images:
        return ([], []) if return_rejected else []

    target_cache = cache_dir or (settings.media_storage_path / "web_cache")
    try:
        target_cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_cache = Path("storage/media/web_cache")
        with contextlib.suppress(OSError):
            target_cache.mkdir(parents=True, exist_ok=True)

    url_candidates = _filter_url_candidates(images)
    if not url_candidates:
        return ([], []) if return_rejected else []

    allow_watermarked = True
    if session is not None:
        try:
            from app.services.pipeline_control import load_control  # noqa: PLC0415

            control = await load_control(session)
            allow_watermarked = control.allow_watermarked_source_media
        except Exception:
            allow_watermarked = True

    # 2. Concurrently fetch and inspect image bytes
    accepted_images: list[dict[str, Any]] = []
    accepted_hashes: list[int] = []

    async with httpx.AsyncClient(timeout=fetch_timeout, follow_redirects=True) as client:
        batch = url_candidates[:16]
        fetch_tasks = [
            _fetch_image_bytes(item["url"], client, target_cache)
            for item in batch
        ]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for item, res in zip(batch, results, strict=False):
            if (
                isinstance(res, Exception)
                or not isinstance(res, bytes)
                or len(res) < 100
            ):
                continue

            is_valid, reason, final_bytes = inspect_image_quality(
                res, url=item["url"], allow_watermarked=allow_watermarked
            )
            if not is_valid:
                logger.info("Rejected web image %s: %s", item["url"], reason)
                continue

            dhash = compute_dhash(final_bytes)
            if _is_perceptual_duplicate(dhash, accepted_hashes):
                logger.info("Rejected duplicate web image %s via dHash", item["url"])
                continue

            if dhash is not None:
                accepted_hashes.append(dhash)

            item["clean_verdict"] = reason
            accepted_images.append(item)
            if len(accepted_images) >= max_keep:
                break

    # 3. AI Relevance Verification if story_title is provided
    if story_title.strip():
        approved, rejected = await verify_images_with_ai(
            accepted_images,
            story_title=story_title,
            story_text=story_text,
            session=session,
        )
        if return_rejected:
            return approved[:max_keep], rejected
        return approved[:max_keep]

    if return_rejected:
        return accepted_images[:max_keep], []
    return accepted_images[:max_keep]


async def clean_web_images(
    images: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convenience wrapper for filter_and_deduplicate_web_images."""
    return await filter_and_deduplicate_web_images(images)


def _resolve_photo_source_href(img: Mapping[str, Any]) -> str:  # noqa: PLR0911
    """Resolve an active clickable URL for photo attribution."""
    source_url = str(img.get("source_url") or "").strip()
    if source_url.startswith(("http://", "https://")):
        return source_url
    domain = str(img.get("domain") or "").strip()
    if domain.startswith("@"):
        return f"https://t.me/{domain.lstrip('@')}"
    if "." in domain and " " not in domain:
        return domain if domain.startswith(("http://", "https://")) else f"https://{domain}"

    domain_norm = domain.lower().strip("«»\"' ")
    if domain_norm:
        try:
            from app.services.lab_pipeline import (  # noqa: PLC0415
                _KNOWN_MEDIA_OUTLET_URLS,
            )

            if domain_norm in _KNOWN_MEDIA_OUTLET_URLS:
                return _KNOWN_MEDIA_OUTLET_URLS[domain_norm]
            for k, u in _KNOWN_MEDIA_OUTLET_URLS.items():
                if len(k) >= 4 and (k in domain_norm or domain_norm in k):
                    return u
        except Exception:  # noqa: S110
            pass

    raw_url = str(img.get("url") or "").strip()
    if raw_url.startswith(("http://", "https://")):
        parsed = urlparse(raw_url)
        if parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return raw_url
    return ""


def _format_inline_figure(img: Mapping[str, Any]) -> str:
    url = html.escape(str(img.get("url") or ""), quote=True)
    title = str(img.get("title") or img.get("alt") or "Иллюстрация к новости").strip()
    domain = str(img.get("domain") or "СМИ").strip()
    caption_text = html.escape(
        title[:90] + ("..." if len(title) > 90 else ""), quote=True
    )
    domain_text = html.escape(domain, quote=True)
    source_href = html.escape(_resolve_photo_source_href(img), quote=True)
    credit_html = (
        f'Фото: <a href="{source_href}" target="_blank" rel="nofollow noopener noreferrer" '
        f'style="color:#0071e3;text-decoration:underline;">{domain_text}</a>'
        if source_href
        else f"Фото: {domain_text}"
    )
    return (
        '\n<figure class="article-image svodka-inline-image aligncenter" '
        'style="margin:1.5rem 0;border-radius:8px;overflow:hidden;">\n'
        f'  <img src="{url}" alt="{caption_text}" loading="lazy" decoding="async" '
        'style="width:100%;height:auto;display:block;border-radius:8px 8px 0 0;" />\n'
        '  <figcaption class="wp-element-caption" '
        'style="font-size:0.825rem;color:#64748b;padding:6px 12px;background:#f8fafc;'
        'border-top:1px solid #e2e8f0;display:flex;justify-content:space-between;'
        'align-items:center;">\n'
        f'    <span>{caption_text}</span>\n'
        f'    <span style="font-weight:500;color:#94a3b8;">{credit_html}</span>\n'
        '  </figcaption>\n'
        '</figure>\n'
    )


def _format_slideshow_block(images: Sequence[Mapping[str, Any]]) -> str:
    items_html: list[str] = []
    for img in images:
        url = html.escape(str(img.get("url") or ""), quote=True)
        title = str(
            img.get("title") or img.get("alt") or "Фотоматериал по теме"
        ).strip()
        domain = str(img.get("domain") or "СМИ").strip()
        caption_text = html.escape(
            title[:70] + ("..." if len(title) > 70 else ""), quote=True
        )
        domain_text = html.escape(domain, quote=True)
        source_href = html.escape(_resolve_photo_source_href(img), quote=True)
        credit_html = (
            f'Фото: <a href="{source_href}" target="_blank" rel="nofollow noopener noreferrer" '
            f'style="color:#0071e3;text-decoration:underline;">{domain_text}</a>'
            if source_href
            else f"Фото: {domain_text}"
        )
        items_html.append(
            '  <figure class="svodka-carousel__item" style="flex:0 0 auto;'
            'width:min(88%,520px);scroll-snap-align:start;margin:0;border-radius:8px;'
            'overflow:hidden;border:1px solid #e2e8f0;background:#f8fafc;">\n'
            f'    <img src="{url}" alt="{caption_text}" loading="lazy" decoding="async" '
            'style="width:100%;height:300px;object-fit:cover;display:block;" />\n'
            '    <figcaption class="wp-element-caption" style="font-size:0.8rem;'
            'color:#64748b;padding:6px 10px;display:flex;justify-content:space-between;'
            'align-items:center;">\n'
            f'      <span>{caption_text}</span>\n'
            f'      <span style="font-weight:500;color:#94a3b8;">'
            f'{credit_html}</span>\n'
            '    </figcaption>\n'
            '  </figure>'
        )

    carousel_html = (
        '\n<div class="svodka-carousel my-6" role="group" '
        'aria-label="Фотогалерея материала" style="margin:1.5rem 0;">\n'
        '  <div class="svodka-carousel__header mb-2" '
        'style="font-size:0.85rem;font-weight:600;color:#475569;margin-bottom:6px;">'
        '📷 Фотогалерея события</div>\n'
        '  <div class="svodka-carousel__track" style="display:flex;gap:12px;'
        'overflow-x:auto;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;'
        'padding-bottom:8px;">\n'
        + "\n".join(items_html)
        + '\n  </div>\n</div>\n'
    )
    return carousel_html


def _inject_slideshow(
    html_content: str, images: Sequence[Mapping[str, Any]]
) -> str:
    slideshow_block = _format_slideshow_block(images)
    p_ends = [m.end() for m in re.finditer(r"</p>", html_content, re.IGNORECASE)]
    if len(p_ends) >= 2:
        idx = p_ends[1]
        return html_content[:idx] + "\n" + slideshow_block + html_content[idx:]
    if len(p_ends) == 1:
        idx = p_ends[0]
        return html_content[:idx] + "\n" + slideshow_block + html_content[idx:]
    return slideshow_block + html_content


def _inject_inline_figures(
    html_content: str, images: Sequence[Mapping[str, Any]]
) -> str:
    p_ends = [m.end() for m in re.finditer(r"</p>", html_content, re.IGNORECASE)]
    if not p_ends:
        figures = "".join(_format_inline_figure(img) for img in images)
        return figures + html_content

    num_p = len(p_ends)
    if num_p <= 2:
        target_p_indices = [1, 2, 3][:len(images)]
    elif num_p <= 4:
        target_p_indices = [2, 3, 4][:len(images)]
    else:
        target_p_indices = [2, 4, 6][:len(images)]

    insertions: list[tuple[int, str]] = []
    used_positions: set[int] = set()

    for img_idx, img in enumerate(images):
        p_target = target_p_indices[min(img_idx, len(target_p_indices) - 1)]
        p_target = min(p_target, num_p)
        pos = p_ends[p_target - 1]
        while pos in used_positions and p_target < num_p:
            p_target += 1
            pos = p_ends[p_target - 1]
        used_positions.add(pos)
        fig_html = _format_inline_figure(img)
        insertions.append((pos, fig_html))

    insertions.sort(key=lambda x: x[0], reverse=True)
    res = html_content
    for pos, fig_html in insertions:
        res = res[:pos] + fig_html + res[pos:]
    return res


def inject_article_images(
    html_content: str,
    images: Sequence[Mapping[str, Any]],
    layout: str = "inline",
    max_images: int = 4,
) -> str:
    """Inject clean photos into article HTML as inline figures or a slideshow carousel.

    Args:
        html_content: Raw or cleaned HTML body of the article.
        images: Sequence of image dicts, each containing at least 'url', and optionally
            'title', 'alt', 'domain', 'caption'.
        layout: 'inline' (distributed figures), 'slideshow' (scrollable carousel),
            or 'cover_only' (no body injection).
        max_images: Maximum number of images to insert into article body.

    Returns:
        Modified HTML content with figures/carousel injected.
    """
    if not html_content or not images or layout == "cover_only":
        return html_content

    valid_images = [
        img
        for img in images
        if str(img.get("url") or "").startswith(("http://", "https://"))
    ][:max_images]

    if not valid_images:
        return html_content

    if layout == "slideshow":
        return _inject_slideshow(html_content, valid_images)

    return _inject_inline_figures(html_content, valid_images)

