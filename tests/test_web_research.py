# ruff: noqa: RUF001
"""Unit tests for external web research, SSRF guard, and Trafilatura integration."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cluster import NewsCluster
from app.models.raw_news import RawNews
from app.services.research import conduct_cluster_research
from app.services.web_researcher import (
    WebArticleContent,
    WebResearchReport,
    WebSourceItem,
    conduct_web_research,
    extract_search_query,
    fetch_clean_article,
    is_safe_web_url,
)
from app.services.web_search import (
    SearchResult,
    _parse_results,
)


def test_is_safe_web_url_blocks_ssrf_and_private_networks() -> None:
    """SSRF guard must reject loopback, link-local, private IPs and non-http schemes."""
    # Disallowed schemes and formats
    assert not is_safe_web_url("")
    assert not is_safe_web_url("file:///etc/passwd")
    assert not is_safe_web_url("ftp://ftp.example.com/file")
    assert not is_safe_web_url("javascript:alert(1)")

    # Disallowed IPs
    assert not is_safe_web_url("http://127.0.0.1:8000/secret")
    assert not is_safe_web_url("https://127.0.0.1/admin")
    assert not is_safe_web_url("http://localhost:8080/")
    assert not is_safe_web_url("http://169.254.169.254/latest/meta-data/")
    assert not is_safe_web_url("http://10.0.0.5/api")
    assert not is_safe_web_url("http://192.168.1.100/router")
    assert not is_safe_web_url("http://172.16.0.1/")
    assert not is_safe_web_url("http://service.local/data")
    assert not is_safe_web_url("http://host.internal/keys")

    # Allowed public addresses
    assert is_safe_web_url("https://www.m24.ru/news/city/2026/09/25")
    assert is_safe_web_url("https://ria.ru/20260925/moscow-1987654321.html")
    assert is_safe_web_url("http://example.com/article")


def test_extract_search_query_cleans_emojis_and_prefixes() -> None:
    """Search query derivation must strip prefixes and format concise keywords."""
    raw = "⚡️ СРОЧНО: В Москве открыли движение по новому участку метро"
    q1 = extract_search_query(raw)
    assert "В Москве открыли движение" in q1
    assert "⚡️" not in q1
    assert "СРОЧНО" not in q1

    q2 = extract_search_query("🔥 Внимание! ЦБ РФ поднял ключевую ставку до 20% годовых")
    assert "ЦБ РФ поднял ключевую ставку" in q2
    assert "🔥" not in q2

    # Fallback to text when title is empty
    fallback = "Строительство моста завершилось в установленный срок."
    q3 = extract_search_query("", fallback_text=fallback)
    assert "Строительство моста завершилось" in q3


def test_parse_duckduckgo_results_unwraps_uddg_urls() -> None:
    """HTML parser must extract clean titles, snippets, and unwrap uddg redirect URLs."""
    sample_html = """
    <div class="result">
        <h2 class="result__title">
            <a class="result__a"
               href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.m24.ru%2Fnews%2F123&rut=1">
                Новый участок метро открылся в Москве
            </a>
        </h2>
        <a class="result__snippet">
            Мэр Москвы открыл новую станцию метрополитена на западе столицы.
        </a>
    </div>
    <div class="result">
        <h2 class="result__title">
            <a class="result__a" href="https://tass.ru/obschestvo/456">
                Прямая ссылка ТАСС без редиректа
            </a>
        </h2>
        <a class="result__snippet">
            Подробности движения поездов и график работы.
        </a>
    </div>
    """
    results = _parse_results(sample_html, max_results=5)
    assert len(results) == 2

    assert results[0].title == "Новый участок метро открылся в Москве"
    assert results[0].url == "https://www.m24.ru/news/123"
    assert "Мэр Москвы открыл" in results[0].snippet

    assert results[1].title == "Прямая ссылка ТАСС без редиректа"
    assert results[1].url == "https://tass.ru/obschestvo/456"


@pytest.mark.asyncio
async def test_fetch_clean_article_extracts_body_with_trafilatura() -> None:
    """Article fetcher must strip navigation/footers and return article text."""
    p1 = "В Москве состоялось торжественное открытие новой станции метрополитена."
    p2 = "Пассажиропоток составит около сорока тысяч человек в сутки."
    p3 = "На церемонии присутствовали представители администрации и жители района."
    html_page = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Открытие станции метро — Новости Москвы</title></head>
    <body>
        <nav><a href="/">Главная</a> | <a href="/news">Новости</a></nav>
        <header>Рекламный баннер сверху</header>
        <article>
            <h1>Открытие новой станции метро</h1>
            <p>{p1}</p>
            <p>{p2}</p>
            <p>{p3}</p>
        </article>
        <footer>© 2026 Новости. Все права защищены. Политическая реклама.</footer>
    </body>
    </html>
    """
    with respx.mock:
        respx.get("https://www.m24.ru/news/test").respond(
            status_code=200,
            html=html_page,
        )

        content = await fetch_clean_article("https://www.m24.ru/news/test")
        assert content is not None
        assert content.domain == "www.m24.ru"
        assert "Открытие станции метро" in content.title
        assert "торжественное открытие новой станции метрополитена" in content.clean_text
        assert "Рекламный баннер" not in content.clean_text
        assert "Политическая реклама" not in content.clean_text


@pytest.mark.asyncio
async def test_conduct_web_research_end_to_end() -> None:
    """Web research workflow must search, fetch, and compile background facts."""
    mock_search_results = [
        SearchResult(
            title="Открытие метро в Пыхтино",
            url="https://www.m24.ru/news/metro1",
            snippet="В Москве открыли новую станцию метро 25 сентября.",
            position=1,
        ),
        SearchResult(
            title="Телеграм пост про метро",
            url="https://t.me/mash/1234",
            snippet="Пост из тг",
            position=2,
        ),
    ]

    mock_article = WebArticleContent(
        url="https://www.m24.ru/news/metro1",
        domain="www.m24.ru",
        title="Открытие метро в Пыхтино",
        clean_text=(
            "Мэр Москвы сообщил об открытии новой станции метро 25 сентября. "
            "По данным столичного департамента транспорта, станцией воспользуются "
            "до 30 тысяч пассажиров ежедневно. Напомним, работы начались три года назад."
        ),
        char_count=230,
    )

    with (
        patch(
            "app.services.web_researcher.search_duckduckgo",
            new_callable=AsyncMock,
            return_value=mock_search_results,
        ),
        patch(
            "app.services.web_researcher.fetch_clean_article",
            new_callable=AsyncMock,
            return_value=mock_article,
        ),
    ):
        report = await conduct_web_research("Метро в Пыхтино", max_sources=2)
        assert report.is_success is True
        # t.me link should be excluded
        assert len(report.sources) == 1
        assert report.sources[0].domain == "www.m24.ru"
        assert len(report.background_facts) > 0
        assert any(
            "сообщил" in f or "25 сентября" in f for f in report.background_facts
        )


@pytest.mark.asyncio
async def test_conduct_cluster_research_session() -> None:
    """Cluster research must orchestrate facts, archive, and web sources."""
    cluster_id = uuid.uuid4()
    cluster = NewsCluster(
        id=cluster_id,
        title="Новая ветка метро в Москве",
        enrichment_data={},
    )
    raw_text = (
        "Строители завершили проходку тоннеля метро в Москве. "
        "Подробности сообщили в мэрии."
    )
    raw_post = RawNews(
        id=1,
        cluster_id=cluster_id,
        raw_text=raw_text,
    )

    fake_report = WebResearchReport(
        search_query="Новая ветка метро в Москве",
        sources=[
            WebSourceItem(
                title="Метро Москвы получило новый тоннель",
                url="https://tass.ru/metro",
                domain="tass.ru",
                snippet="Длина тоннеля составила 2.5 км.",
                extracted_text="Тоннель соединит две крупные станции.",
                key_facts=["Длина тоннеля составила 2.5 км."],
            )
        ],
        background_facts=["Длина тоннеля составила 2.5 км."],
        is_success=True,
    )

    mock_session = AsyncMock(spec=AsyncSession)

    with (
        patch(
            "app.services.research.find_related_articles",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.research.conduct_web_research",
            new_callable=AsyncMock,
            return_value=fake_report,
        ),
    ):
        res = await conduct_cluster_research(
            mock_session, cluster, [raw_post], enable_web_search=True
        )
        assert res["cluster_id"] == str(cluster_id)
        assert len(res["facts"]) > 0
        assert len(res["web_sources"]) == 1
        assert res["web_sources"][0]["domain"] == "tass.ru"
        assert "2.5 км" in res["web_facts"][0]
        assert res["search_query"] == "Новая ветка метро в Москве"
