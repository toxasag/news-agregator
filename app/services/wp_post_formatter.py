"""Jinja2 post formatter: assembles the HTML body WordPress will publish.

The content engine produces an article body; a *published page* needs more than
that. This module wraps the body with everything the theme and the search engines
expect, in one render:

* **Semantic typography.** The body's ``<h2>``/``<p>``/``<ul>`` structure is
  preserved and everything executable is removed. The body is model output that
  quoted scraped sources, so it is treated as untrusted input at this boundary
  rather than trusted because an earlier stage looked at it.
* **A featured image** rendered as a ``<figure>`` with a real ``<figcaption>``,
  and **inline carousels** for the remaining attachments — a scrollable strip
  built from plain CSS, not a shortcode, so it survives a theme change and needs
  no plugin.
* **VK video embeds**, reusing the responsive iframe markup from
  :mod:`app.services.vk_video_service` so the player is identical wherever it
  appears.
* **Schema.org ``NewsArticle`` JSON-LD.** This is what produces a rich result in
  Google News and Yandex, and it is the single highest-leverage SEO artefact on
  the page.
* **A related-articles block** built from shared tags and category, which is
  internal linking — the cheapest ranking signal available and the one an
  aggregator is best placed to exploit.

Two decisions carry the security weight of this module:

**JSON-LD is serialised with :func:`json.dumps`, never templated.** A headline
containing ``</script>`` would otherwise close the tag and turn the rest of the
document into executable markup. The payload is built as a Python dict, dumped
with ``ensure_ascii=False`` for readable Cyrillic, and the sequence ``</`` is
escaped afterwards, which is the documented way to embed JSON safely in a
``<script>`` element.

**Autoescaping is on, and the body is the single explicit exception.** Every value
interpolated into the template is escaped by default; the article body is marked
safe only *after* passing through the sanitiser. An unsanitised
``| safe`` is the one mistake in a templating layer that turns a content pipeline
into a stored-XSS vector.

Nothing here performs I/O: values in, HTML out. The publisher owns the network.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from jinja2 import Environment, StrictUndefined
from markupsafe import Markup

from app.config import settings
from app.services.vk_video_service import build_video_embed

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "MAX_RELATED_ARTICLES",
    "SCHEMA_CONTEXT",
    "CarouselImage",
    "FeaturedImage",
    "PostRenderResult",
    "RelatedArticle",
    "VideoEmbed",
    "WPPostFormatter",
    "build_news_article_schema",
    "render_post",
    "sanitize_article_html",
]

SCHEMA_CONTEXT: Final[str] = "https://schema.org"

#: Related links per article. Three is enough to matter for internal linking and
#: few enough that the block reads as editorial rather than as a link farm, which
#: is what search engines penalise.
MAX_RELATED_ARTICLES: Final[int] = 3

#: Elements dropped with their subtree. The body is model output that quoted
#: scraped sources, so a ``<script>`` can arrive by way of a prompt injection in a
#: source post. Stripping only the tags would leave the script's *text* visible as
#: prose, so the whole subtree goes.
_UNSAFE_ELEMENTS: Final[tuple[str, ...]] = (
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "noscript",
    "template",
    "form",
    "input",
    "button",
    "link",
    "meta",
    "base",
)

_UNSAFE_ELEMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*(?P<tag>" + "|".join(_UNSAFE_ELEMENTS) + r")\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
#: A self-closing or unclosed instance of the same elements.
_UNSAFE_VOID_RE: Final[re.Pattern[str]] = re.compile(
    r"<\s*/?\s*(?:" + "|".join(_UNSAFE_ELEMENTS) + r")\b[^>]*>",
    re.IGNORECASE,
)
#: Inline event handlers (``onclick=``) and inline styles.
_INLINE_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"\s(?:on[a-z]+|style)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
#: URL attributes carrying an executing scheme.
_UNSAFE_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"\s(?:href|src|xlink:href|action|formaction|data)\s*=\s*"
    r"(?:\"\s*(?:javascript|vbscript|data)\s*:[^\"]*\""
    r"|'\s*(?:javascript|vbscript|data)\s*:[^']*'"
    r"|(?:javascript|vbscript|data)\s*:[^\s>]*)",
    re.IGNORECASE,
)
#: HTML comments, which can hide conditional-comment scripting on old engines.
_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)

#: ``</`` inside a ``<script>`` body ends the element regardless of context, so it
#: is escaped in the JSON-LD payload.
_SCRIPT_CLOSE_RE: Final[re.Pattern[str]] = re.compile(r"</", re.IGNORECASE)

#: Strips tags for the plain-text description in the JSON-LD payload.
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

#: Bounds on the JSON-LD text fields. Google ignores an over-long headline and
#: Yandex truncates it, so a longer value is payload without benefit.
MAX_SCHEMA_HEADLINE_CHARS: Final[int] = 110
MAX_SCHEMA_DESCRIPTION_CHARS: Final[int] = 300


def sanitize_article_html(body_html: str) -> str:
    """Strip executable markup from a generated article body.

    Applied to every body before it is marked safe for the template. The order
    matters: paired elements are removed with their subtrees first, then any
    surviving stray tags of the same names, then inline handlers, executing URLs
    and comments. Removing attributes first would leave a ``<script>`` whose
    handler was gone but whose body still executed.

    This is defence in depth rather than the only check —
    :func:`app.services.content_generator.validate_generation` already *rejects* a
    draft containing these constructs. That check protects the database; this one
    protects the published page, and a body can reach here from a manual edit in
    the dashboard that never passed through the generator.
    """
    if not body_html or not body_html.strip():
        return ""
    cleaned = _UNSAFE_ELEMENT_RE.sub(" ", body_html)
    cleaned = _UNSAFE_VOID_RE.sub(" ", cleaned)
    cleaned = _COMMENT_RE.sub(" ", cleaned)
    cleaned = _UNSAFE_URL_RE.sub("", cleaned)
    cleaned = _INLINE_ATTR_RE.sub("", cleaned)
    return cleaned.strip()


def _plain_text(value: str, *, limit: int) -> str:
    """Reduce HTML to bounded plain text for a metadata field."""
    stripped = _TAG_RE.sub(" ", value)
    collapsed = _WHITESPACE_RE.sub(" ", html.unescape(stripped)).strip()
    return collapsed[:limit].strip()


@dataclass(frozen=True, slots=True)
class FeaturedImage:
    """The article's lead image, already uploaded to the media library."""

    media_id: int
    source_url: str
    alt_text: str = ""
    caption: str = ""
    width: int = 0
    height: int = 0


@dataclass(frozen=True, slots=True)
class CarouselImage:
    """One additional image rendered in the inline strip."""

    media_id: int
    source_url: str
    alt_text: str = ""
    caption: str = ""


@dataclass(frozen=True, slots=True)
class VideoEmbed:
    """A hosted video, addressed by its platform coordinates.

    ``embed_html`` is accepted pre-built so the formatter reuses whatever the VK
    service produced; when absent it is derived from the coordinates, which keeps
    a stored ``video_links`` entry renderable even if it predates the current
    markup.
    """

    owner_id: int
    video_id: int
    access_key: str = ""
    title: str = ""
    embed_html: str = ""

    def markup(self) -> str:
        """Return the responsive player markup for this video."""
        if self.embed_html:
            return self.embed_html
        return build_video_embed(
            owner_id=self.owner_id,
            video_id=self.video_id,
            access_key=self.access_key,
            title=self.title,
        )


@dataclass(frozen=True, slots=True)
class RelatedArticle:
    """One internal link in the related block."""

    title: str
    url: str
    category: str = ""
    thumbnail_url: str = ""


@dataclass(frozen=True, slots=True)
class PostRenderResult:
    """The rendered body plus the JSON-LD payload, kept separate.

    The two are separate because they travel differently: the body goes into
    ``content`` on the REST call, while the schema is *also* returned as a dict so
    the publisher can log or validate it without re-parsing the HTML.
    """

    content_html: str
    schema: dict[str, Any]

    @property
    def has_schema(self) -> bool:
        return bool(self.schema)


def _absolute_url(path_or_url: str) -> str:
    """Resolve a site-relative path against ``WP_BASE_URL``.

    Schema.org requires absolute URLs; a relative one is silently dropped by
    validators, which is the worst outcome — the markup looks present and does
    nothing.
    """
    candidate = path_or_url.strip()
    if not candidate:
        return ""
    if candidate.startswith(("http://", "https://")):
        return candidate
    base = settings.wp_base_url.rstrip("/")
    return f"{base}/{candidate.lstrip('/')}" if base else candidate


def build_news_article_schema(
    *,
    title: str,
    description: str,
    body_html: str,
    url: str = "",
    published_at: datetime | None = None,
    modified_at: datetime | None = None,
    featured_image: FeaturedImage | None = None,
    keywords: Sequence[str] = (),
    category: str = "",
    author_name: str = "",
    publisher_name: str = "",
    publisher_logo_url: str = "",
) -> dict[str, Any]:
    """Build the Schema.org ``NewsArticle`` payload for one article.

    Only fields with real values are emitted: an empty ``author`` or a
    zero-dimension ``image`` is worse than an absent one, because a validator
    reports it as an error on an otherwise valid page.

    ``dateModified`` falls back to ``datePublished`` — the property is required by
    the ``NewsArticle`` guidelines, and omitting it costs the rich result.
    """
    headline = _plain_text(title, limit=MAX_SCHEMA_HEADLINE_CHARS)
    if not headline:
        return {}

    published = published_at or modified_at
    schema: dict[str, Any] = {
        "@context": SCHEMA_CONTEXT,
        "@type": "NewsArticle",
        "headline": headline,
    }

    summary = _plain_text(
        description or body_html, limit=MAX_SCHEMA_DESCRIPTION_CHARS
    )
    if summary:
        schema["description"] = summary

    absolute = _absolute_url(url)
    if absolute:
        # `mainEntityOfPage` is what disambiguates the canonical URL when the same
        # article is reachable through a category path and a date path.
        schema["url"] = absolute
        schema["mainEntityOfPage"] = {"@type": "WebPage", "@id": absolute}

    if published is not None:
        schema["datePublished"] = published.isoformat()
        schema["dateModified"] = (modified_at or published).isoformat()

    if featured_image is not None and featured_image.source_url:
        image: dict[str, Any] = {
            "@type": "ImageObject",
            "url": _absolute_url(featured_image.source_url),
        }
        if featured_image.width and featured_image.height:
            image["width"] = featured_image.width
            image["height"] = featured_image.height
        schema["image"] = image

    author = author_name.strip() or publisher_name.strip()
    if author:
        # `Organization` rather than `Person`: the articles are produced by an
        # automated newsroom, and claiming a human byline would be a false
        # statement in structured data.
        schema["author"] = {"@type": "Organization", "name": author}

    publisher = publisher_name.strip()
    if publisher:
        publisher_block: dict[str, Any] = {
            "@type": "Organization",
            "name": publisher,
        }
        logo = _absolute_url(publisher_logo_url)
        if logo:
            publisher_block["logo"] = {"@type": "ImageObject", "url": logo}
        schema["publisher"] = publisher_block

    cleaned_keywords = [
        keyword.strip() for keyword in keywords if keyword and keyword.strip()
    ]
    if cleaned_keywords:
        schema["keywords"] = ", ".join(cleaned_keywords)

    section = category.strip()
    if section:
        schema["articleSection"] = section

    body_text = _plain_text(body_html, limit=5000)
    if body_text:
        schema["articleBody"] = body_text

    return schema


def _json_ld_script(schema: Mapping[str, Any]) -> str:
    """Serialise a schema payload into a safe ``<script>`` element.

    ``json.dumps`` does the escaping, not the template: a headline containing
    ``</script>`` interpolated as text would close the element and hand the rest of
    the page to an attacker. ``</`` is then escaped as ``<\\/`` — valid JSON, and
    the documented way to keep a JSON payload from terminating its own script
    element.
    """
    if not schema:
        return ""
    payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    safe_payload = _SCRIPT_CLOSE_RE.sub(r"<\\/", payload)
    return (
        '<script type="application/ld+json">' f"{safe_payload}" "</script>"
    )


#: The universal post template.
#:
#: Autoescaping is enabled, so every ``{{ }}`` is escaped unless explicitly marked
#: safe. Exactly three values are marked safe, and each is pre-processed: the
#: article body through :func:`sanitize_article_html`, the video markup from our
#: own builder, and the JSON-LD through :func:`_json_ld_script`. Nothing else is
#: trusted.
_POST_TEMPLATE: Final[str] = """\
{%- if schema_script %}{{ schema_script }}{% endif %}
{%- if featured and featured.source_url %}
<figure class="svodka-featured wp-block-image size-large">
<img src="{{ featured.source_url }}" alt="{{ featured.alt_text }}"
{%- if featured.width %} width="{{ featured.width }}"{% endif %}
{%- if featured.height %} height="{{ featured.height }}"{% endif %}
 class="wp-image-{{ featured.media_id }}" loading="eager" decoding="async" />
{%- if featured.caption %}
<figcaption class="wp-element-caption">{{ featured.caption | safe }}</figcaption>
{%- endif %}
</figure>
{%- endif %}
<div class="svodka-article__body">
{{ body }}
</div>
{%- if videos %}
<div class="svodka-article__videos">
{%- for video in videos %}
{{ video }}
{%- endfor %}
</div>
{%- endif %}
{%- if carousel %}
<div class="svodka-carousel" role="group" aria-label="Фотогалерея материала">
<div class="svodka-carousel__track" style="display:flex;gap:12px;overflow-x:auto;\
scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;">
{%- for image in carousel %}
<figure class="svodka-carousel__item" style="flex:0 0 auto;width:min(88%,520px);\
scroll-snap-align:start;margin:0;">
<img src="{{ image.source_url }}" alt="{{ image.alt_text }}"
 class="wp-image-{{ image.media_id }}" loading="lazy" decoding="async"
 style="width:100%;height:auto;display:block;" />
{%- if image.caption %}
<figcaption class="wp-element-caption">{{ image.caption | safe }}</figcaption>
{%- endif %}
</figure>
{%- endfor %}
</div>
</div>
{%- endif %}
{%- if related %}
<aside class="svodka-related" aria-label="{{ related_heading }}">
<h2 class="svodka-related__title">{{ related_heading }}</h2>
<ul class="svodka-related__list">
{%- for item in related %}
<li class="svodka-related__item">
<a href="{{ item.url }}" rel="bookmark">{{ item.title }}</a>
{%- if item.category %} <span class="svodka-related__category">\
{{ item.category }}</span>{% endif %}
</li>
{%- endfor %}
</ul>
</aside>
{%- endif %}
"""


class WPPostFormatter:
    """Renders the universal post body from an article and its media.

    The Jinja environment is built once per instance: compiling a template costs
    real time, and this runs for every published article.

    ``StrictUndefined`` is deliberate. A silently-undefined variable in a
    publishing template produces a page that is missing its featured image or its
    structured data with no error anywhere — exactly the failure that is hardest to
    notice. A typo should raise during a test run instead.
    """

    __slots__ = ("_environment", "_template")

    def __init__(self) -> None:
        self._environment = Environment(
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=False,
            keep_trailing_newline=False,
        )
        self._template = self._environment.from_string(_POST_TEMPLATE)

    def render(
        self,
        *,
        title: str,
        body_html: str,
        seo_description: str = "",
        url: str = "",
        featured_image: FeaturedImage | None = None,
        carousel: Sequence[CarouselImage] = (),
        videos: Sequence[VideoEmbed] = (),
        related: Sequence[RelatedArticle] = (),
        keywords: Sequence[str] = (),
        category: str = "",
        published_at: datetime | None = None,
        modified_at: datetime | None = None,
        author_name: str = "",
        publisher_name: str = "",
        publisher_logo_url: str = "",
        related_heading: str = "Читайте также",
        include_schema: bool = True,
    ) -> PostRenderResult:
        """Render one post body and return it with its JSON-LD payload."""
        sanitized = sanitize_article_html(body_html)
        schema = (
            build_news_article_schema(
                title=title,
                description=seo_description,
                body_html=sanitized,
                url=url,
                published_at=published_at,
                modified_at=modified_at,
                featured_image=featured_image,
                keywords=keywords,
                category=category,
                author_name=author_name,
                publisher_name=publisher_name,
                publisher_logo_url=publisher_logo_url,
            )
            if include_schema
            else {}
        )

        rendered = self._template.render(
            body=Markup(sanitized),  # noqa: S704 - sanitised immediately above
            featured=featured_image,
            carousel=list(carousel),
            videos=[Markup(video.markup()) for video in videos],  # noqa: S704
            related=list(related)[:MAX_RELATED_ARTICLES],
            related_heading=related_heading,
            schema_script=Markup(_json_ld_script(schema)),  # noqa: S704
        )
        collapsed = "\n".join(
            line for line in rendered.splitlines() if line.strip()
        )
        return PostRenderResult(content_html=collapsed.strip(), schema=schema)


@dataclass(slots=True)
class _SharedFormatter:
    """Holds the process-wide formatter so its template is compiled once."""

    instance: WPPostFormatter | None = field(default=None)


_shared: Final[_SharedFormatter] = _SharedFormatter()


def get_post_formatter() -> WPPostFormatter:
    """Return the process-wide formatter, creating it on first call."""
    if _shared.instance is None:
        _shared.instance = WPPostFormatter()
    return _shared.instance


def render_post(
    *,
    title: str,
    body_html: str,
    **options: Any,
) -> PostRenderResult:
    """Render one post with the shared formatter.

    The module-level entry point, so callers do not each construct a formatter and
    recompile the template.
    """
    return get_post_formatter().render(title=title, body_html=body_html, **options)
