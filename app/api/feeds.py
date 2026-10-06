# ruff: noqa: RUF001, E501
"""Syndicated news and mobile Turbo feeds for Yandex News, Yandex Turbo, and RSS 2.0.

Provides high-performance XML feeds generated dynamically from published articles
in PostgreSQL for news aggregators and search engines to achieve maximum organic reach.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.article import Article
from app.models.enums import ArticleStatus

router: Final[APIRouter] = APIRouter(tags=["Feeds"])

SITE_URL: Final[str] = "https://svodka.press"
SITE_TITLE: Final[str] = "СводкаПресс — Главные новости и аналитика"
SITE_DESC: Final[str] = (
    "Оперативная новостная картина дня, эксклюзивная аналитика, "
    "события политики, экономики и технологий."
)


def _rfc822_date(dt: datetime | None) -> str:
    """Format datetime to RFC 822 / RFC 2822 format required by RSS specifications."""
    if dt is None:
        dt = datetime.now(UTC)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return format_datetime(dt)


def xml_escape(text: str) -> str:
    """Escape XML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _clean_cdata(text: str) -> str:
    """Safely format text inside a CDATA block, avoiding nested CDATA closures."""
    return text.replace("]]>", "]]&gt;")


def _extract_cover_url(content_html: str | None) -> str | None:
    """Extract the first image URL from content_html to use as feed enclosure."""
    if not content_html:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content_html)
    return match.group(1) if match else None


async def _get_published_articles(
    session: AsyncSession, limit: int = 50
) -> list[Article]:
    """Retrieve latest published articles for syndicated feeds."""
    stmt = (
        select(Article)
        .where(
            Article.status.in_([ArticleStatus.WP_PUBLISHED, ArticleStatus.DISTRIBUTED])
            | Article.published_at.is_not(None)
        )
        .order_by(Article.published_at.desc().nulls_last(), Article.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get(
    "/api/feeds/yandex-news.xml",
    response_class=Response,
    summary="Yandex News Syndication Feed (XML)",
)
@router.get(
    "/feeds/yandex.xml",
    response_class=Response,
    include_in_schema=False,
)
async def yandex_news_feed(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Generate Yandex.News compatible RSS 2.0 syndication feed."""
    articles = await _get_published_articles(session, limit=50)

    last_build = _rfc822_date(
        articles[0].published_at or articles[0].created_at if articles else None
    )

    items_xml: list[str] = []
    for art in articles:
        slug = (art.url_slug or "").strip("/")
        url = f"{SITE_URL}/{slug}/" if slug else f"{SITE_URL}/post/{art.id}/"
        title = xml_escape(art.seo_title or art.title or "Новость")
        pub_date = _rfc822_date(art.published_at or art.created_at)
        category = xml_escape(art.category or "Общество")
        desc_text = _clean_cdata(
            art.seo_description or (art.content_html[:300] if art.content_html else "")
        )
        body_text = _clean_cdata(art.content_html or "")
        cover_url = _extract_cover_url(art.content_html)

        enclosure_xml = ""
        if cover_url:
            enclosure_xml = (
                f'<enclosure url="{xml_escape(cover_url)}" '
                f'type="image/jpeg" length="1048576"/>\n'
                f'<media:content url="{xml_escape(cover_url)}" '
                f'medium="image" type="image/jpeg"/>'
            )

        items_xml.append(f"""    <item>
      <title>{title}</title>
      <link>{xml_escape(url)}</link>
      <pdalink>{xml_escape(url)}</pdalink>
      <pubDate>{pub_date}</pubDate>
      <author>Редакция СводкаПресс</author>
      <category>{category}</category>
      {enclosure_xml}
      <description><![CDATA[{desc_text}]]></description>
      <yandex:full-text><![CDATA[{body_text}]]></yandex:full-text>
    </item>""")

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:yandex="http://news.yandex.ru"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{SITE_TITLE}</title>
    <link>{SITE_URL}/</link>
    <description>{SITE_DESC}</description>
    <language>ru</language>
    <lastBuildDate>{last_build}</lastBuildDate>
    <yandex:logo>{SITE_URL}/assets/icons/svodka-symbol-color.svg</yandex:logo>
    <yandex:logo type="square">{SITE_URL}/assets/icons/icon-512.png</yandex:logo>
{chr(10).join(items_xml)}
  </channel>
</rss>"""

    return Response(
        content=feed_xml.strip(),
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get(
    "/api/feeds/turbo.xml",
    response_class=Response,
    summary="Yandex Turbo Pages Feed (XML)",
)
@router.get(
    "/feeds/turbo.xml",
    response_class=Response,
    include_in_schema=False,
)
async def yandex_turbo_feed(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Generate Yandex Turbo Pages RSS feed for instant mobile rendering."""
    articles = await _get_published_articles(session, limit=50)

    last_build = _rfc822_date(
        articles[0].published_at or articles[0].created_at if articles else None
    )

    items_xml: list[str] = []
    for art in articles:
        slug = (art.url_slug or "").strip("/")
        url = f"{SITE_URL}/{slug}/" if slug else f"{SITE_URL}/post/{art.id}/"
        title = xml_escape(art.title or "Новость")
        pub_date = _rfc822_date(art.published_at or art.created_at)
        author = "Редакция СводкаПресс"
        cover_url = _extract_cover_url(art.content_html)

        header_img = (
            f'<figure><img src="{xml_escape(cover_url)}"/><figcaption>{title}</figcaption></figure>'
            if cover_url
            else ""
        )
        turbo_html = f"""
<header>
  <h1>{title}</h1>
  {header_img}
</header>
{art.content_html or ""}
<footer>
  <p>© СводкаПресс. Свежие новости в Telegram: <a href="https://t.me/svodka_press">@svodka_press</a></p>
</footer>
"""

        items_xml.append(f"""    <item turbo="true">
      <title>{title}</title>
      <link>{xml_escape(url)}</link>
      <author>{author}</author>
      <category>{xml_escape(art.category or "Общество")}</category>
      <pubDate>{pub_date}</pubDate>
      <turbo:content><![CDATA[{_clean_cdata(turbo_html.strip())}]]></turbo:content>
    </item>""")

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:yandex="http://news.yandex.ru"
     xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:turbo="http://turbo.yandex.net">
  <channel>
    <title>{SITE_TITLE} — Турбо</title>
    <link>{SITE_URL}/</link>
    <description>{SITE_DESC}</description>
    <language>ru</language>
    <lastBuildDate>{last_build}</lastBuildDate>
{chr(10).join(items_xml)}
  </channel>
</rss>"""

    return Response(
        content=feed_xml.strip(),
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get(
    "/api/feeds/rss.xml",
    response_class=Response,
    summary="Standard RSS 2.0 Feed (XML)",
)
@router.get(
    "/feeds/rss.xml",
    response_class=Response,
    include_in_schema=False,
)
async def standard_rss_feed(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """Generate standard clean RSS 2.0 feed for news aggregators and readers."""
    articles = await _get_published_articles(session, limit=50)

    last_build = _rfc822_date(
        articles[0].published_at or articles[0].created_at if articles else None
    )

    items_xml: list[str] = []
    for art in articles:
        slug = (art.url_slug or "").strip("/")
        url = f"{SITE_URL}/{slug}/" if slug else f"{SITE_URL}/post/{art.id}/"
        title = xml_escape(art.title or "Новость")
        pub_date = _rfc822_date(art.published_at or art.created_at)
        desc_text = _clean_cdata(
            art.seo_description or (art.content_html[:300] if art.content_html else "")
        )
        cover_url = _extract_cover_url(art.content_html)
        enclosure = (
            f'<enclosure url="{xml_escape(cover_url)}" type="image/jpeg" length="1048576"/>'
            if cover_url
            else ""
        )

        items_xml.append(f"""    <item>
      <title>{title}</title>
      <link>{xml_escape(url)}</link>
      <guid isPermaLink="true">{xml_escape(url)}</guid>
      <pubDate>{pub_date}</pubDate>
      <category>{xml_escape(art.category or "Общество")}</category>
      {enclosure}
      <description><![CDATA[{desc_text}]]></description>
    </item>""")

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{SITE_TITLE}</title>
    <link>{SITE_URL}/</link>
    <description>{SITE_DESC}</description>
    <language>ru</language>
    <lastBuildDate>{last_build}</lastBuildDate>
    <atom:link href="{SITE_URL}/api/feeds/rss.xml" rel="self" type="application/rss+xml"/>
{chr(10).join(items_xml)}
  </channel>
</rss>"""

    return Response(
        content=feed_xml.strip(),
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )
