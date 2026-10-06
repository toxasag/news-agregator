# SvodkaPress NextGen — Project TODO

## Status Legend

- `[ ]` — not started
- `[/]` — in progress (set this BEFORE beginning work on a task)
- `[x]` — complete and verified (set this AFTER verification passes)

Per `AGENTS.md`, a task must be marked `[/]` before work starts and `[x]` only after type checks and tests pass.

## Milestones

- [x] Package 1: Infrastructure, Docker, Async DB Engine, SQLAlchemy Models, Alembic Migrations.
- [x] Package 2: Ingestion Engine & Zero-Cost Deduplication.
- [x] Package 3: AI Content Generation & Media Pipeline.
- [x] Package 4: Multi-Platform Publisher Engine.
- [x] Package 5: Web UI Dashboard & SEO Analytics.
- [x] Package 6: Production deployment to VPS (144.31.141.17) behind Nginx Proxy Manager (150.241.98.134 / svodkanews.team-big.ru).
- [x] Решение проблемы дублей: двухэтапная дедупликация (нормализация стемов, топонимы, пост-генерационный фильтр), зачистка WP.
- [x] Строгий контроль релевантности обложек: блокировка portal-banner/snippet/opengraph URL, обязательное подтверждение AI.
- [x] Инспектор инцидентов в веб-панели: оперативные алерты 429/квот/сессий с автообновлением в шапке UI.
- [x] Восстановление данных SEO-панели (/seo): ингестия Яндекс.Вебмастер (query-analytics) в очередь CTR, кеширование Метрики от 429.
- [x] Ликвидация дублей в Вебмастере (чистка legacy-клонов WP, noindex пагинации Rank Math) и статистика рубрик по периодам/датам на странице обзора.
- [x] Левая колонка рубрик в стиле Setka.ru (svodka-setka v2.53.0): sticky-навигация 20 рубрик на десктопе, свайп-чипсы на мобильных, подсветка активного раздела.

## Где мы сейчас (2026-09-03)

Пакеты 1–11 ниже — история реализации. **Актуальный план — `PROJECT.md`
раздел 10 (Фазы 0–5), состояние — `STATE.md`.** При конфликте с этим файлом
побеждают они: пакеты описывают, что было сделано, а не что делать дальше.

Согласование функционала с владельцем завершено 2026-09-03. Утверждённый бриф —
`PROJECT.md` раздел 8. Ключевые решения: ручной режим первым, WordPress по SSH,
OAuth для соцсетей, строгая экономия на моделях, полный журнал прогона.

## Фазы утверждённого плана

Задачи заводятся по мере входа в фазу. Порядок менять нельзя: каждая фаза
опирается на предыдущую.

- [/] **Фаза 0 — Подготовка и фундамент**
  - [ ] Полная очистка базы от старых материалов — инструмент готов; фактическое удаление ждёт разрешения на конкретную scratch-базу
  - [x] Журнал состояния на каждую статью: привязка артефактов к материалу
  - [x] Инфраструктура SSH-доступа к сайту
- [/] **Фаза 1 — Панель: настройки и подключения**
  - [x] Страница `/sources`: подключение Telegram-аккаунтов (QR / телефон+2FA), полный список диалогов со ссылками и описанием, добавление/удаление сайтов; first pass / после простоя — только сегодня с 00:00 MSK, затем лимит 50
  - [x] Подключение WordPress по SSH (адрес, креды, путь, проверка связи)
  - [x] Шифрованное хранение доступов в базе вместо `.env`
  - [x] Каталог рубрик и теги читаются с подключённого сайта
  - [ ] Редактор промтов по рубрикам
  - [/] «Лаборатория» как центр управления: завершить ручное одобрение и версии этапов, безопасные ключи/реальную маршрутизацию, полный учёт расходов, безопасную очистку дня; Camoufox Research и TailAdmin. Прежняя отметка готовности была преждевременной.
    - [x] Пошаговая лаборатория Apple HIG: честный статус системы, сброс очереди (сбор по 50 постов с активных каналов), Шаг 1: группы новостей с разворачиванием всех первоисточников и дублей, Шаг 2: пошаговая обработка выбранной новости (ресерч, медиа OCR, генерация, публикация).
    - [x] Отдельная страница `/settings/ai` и пул ротации 20 ключей Google AI Studio: массовый ввод, раздельные тесты (текст, медиа, эмбеддинги), автоматический Failover/Round-Robin.
    - [x] Фиксация Студии (Шаг 2) при скролле списка новостей (`sticky top-6` со своим скроллом).
    - [x] Карточка «Отфильтровано»: полный просмотр текста и медиа, причины срабатывания правил, кнопка разблокировки в сюжеты, смягчение ложных срабатываний (RT, SHOT).
    - [x] Умная рубрикация ИИ с обоснованием; предложение и создание новых рубрик с автопривязкой промтов.
    - [x] Медиа-блок: реальные превью фото и видео, метки OCR (чистое / желтый кроп 15% / брак), Fallback-генерация иллюстраций по промту.
    - [x] Генерация статьи: прозрачность источников (посты, факты, ресерч), редактирование промта, ключи Wordstat/SEO, предпросмотр WP и обязательная внутренняя перелинковка.
    - [x] Чистка /settings (удаление дублирующих блоков матрицы и цепочек), мульти-провайдерные сворачиваемые пулы /settings/ai (без лимита ключей) и динамический опрос моделей Gemini (ListModels) с умным тестированием способностей.
    - [x] Каталог проверенных моделей и умная матрица назначения ИИ: сводный статус работающих моделей, селекторы с проверенными моделями, автоподбор лучших моделей в 1 клик, поштучный тест моделей и роутинг в AIClient.
    - [x] Устранение скролл-джампа при тесте моделей (локальный swap), полный вывод ошибок API, умное тестирование кастомных провайдеров (dollariuy.fun), авто-восстановление моделей при пиках, роль «Генератор картинок» в матрице и история логов на /logs с очисткой.
    - [x] Строгое ограничение выбора моделей в матрице только подключёнными провайдерами (для картинок — только подключённый Fal/Gemini или Отключено), прозрачный автоподбор и failover-устойчивость при сбоях моделей, полноценная поддержка SSE streaming для шлюзов (dollariuy.fun/Noir), массовое обнаружение и тестирование моделей кастомных провайдеров.
    - [x] Самовосстанавливающийся роутинг ИИ: классификация ошибок (classify_model_error), автозамена моделей при сбоях (429, 502/503, 404, 402), кросс-провайдерные цепочки отказоустойчивости (Fallback Chains) и актуализация активных моделей в БД.
    - [x] Полноценный медиа-блок: он-деманд показ Telegram-фото, извлечение репортажных картинок из внешнего веб-ресерча (og:image) и профессиональный LLM-генератор фотореалистичных промтов обложек.
    - [x] Исправление замечаний тестирования Лаборатории: точный поисковый запрос (без обрезки точек в именах Z.ai/моделях), проксирование веб-медиа СМИ (обход 403), исправление генератора промтов обложек, интерактивное переключение стилей новости с глубокими промтами, индикация неподключенного WordPress на Шаге 4 и интерфейс настроек сервисов Яндекса (Wordstat, Метрика, Вебмастер).
    - [x] Умный анализ релевантности статей веб-ресерча (Entity/Keyword matching), классификация и фильтрация медиа (отсечение графиков/таблиц/логотипов), ручной выбор обложки из веб-медиа с загрузкой в WordPress, надежное отображение и интерактивное переключение развернутых журналистских промтов в Студии.
    - [x] Модернизация конвейера SvodkaPress (2.45.0): кликабельные ссылки Дзен, WP Telegram CTA, фильтрация опубликованного в /lab, Brand Safety override, FastEmbed дедупликация и сущностная склейка, статистика рубрик за 120ч и подключение VK через авторизованную браузерную сессию (как Дзен).
    - [x] Подключение сервисов Google (GA4 Data API v1beta + MCP, Search Console), Bing Webmaster API и IndexNow с шифрованием Fernet в БД и мгновенной отправкой URL опубликованных статей в Bing/Яндекс через IndexNow.
    - [x] Умное авто-определение доменных ресурсов Search Console (sc-domain:), исправление формата дат ISO (YYYY-MM-DD) в Search Analytics API и автоматическое сохранение подтвержденного идентификатора сайта.
    - [x] Подключение отдельного счётчика Яндекс Метрики для Яндекс Дзен: сохранение в Fernet, общий OAuth-токен аккаунта, отдельная кнопка проверки и отображение статуса в панели настроек.
    - [x] Оптимизация Студии Лаборатории по итогам тестирования: индексация архива 8 700+ статей WordPress для перелинковки, нативное сохранение Rank Math (/wp-json/rankmath/v1/updateMeta), передача стилей и глубокого контекста ресерча в генератор, защита от публикации без фото.
    - [x] Массовое тестирование 100% моделей всех подключенных провайдеров (авто-батч с семафором, параллельная проверка сотен моделей шлюзов и наполнение полного Каталога проверенных моделей: 43 уникальные модели доступны на проде).
    - [x] Сквозной учет расхода токенов на каждую статью и всей системы в разрезе провайдеров (модель `ai_usage` / токен-бухгалтерия, себестоимость, оценка стабильности и Uptime).
    - [x] Надежный публикатор в Яндекс Дзен через Playwright Stealth (сохранение сессии `storage_state`, отдельный текст и баннеры под Дзен, нормализация кук, кнопка тестового черновика и публикация из Лаборатории).
    - [x] Алгоритмический расчет привлекательности статей (индекс AEVI на базе цифр Wordstat/Метрики/Крипты), Студия статьи с мультиканальным предпросмотром (Сайт, Дзен, TG, VK) и замкнутый контур сверки гипотез.
    - [x] Стандарт «ДНК Победителей» для редакционных и нативных новостей (Zero Hallucination + глубокий ресерч), модуль рекламируемых проектов, Brand Safety фильтр и сквозной Closed-Loop с Метрикой заказчика (v2.31.0).
    - [x] Интеллектуальный роутер ИИ: дифференцированные кулдауны по сигнатурам ошибок (24ч при исчерпании квоты/ошибке авторизации, 15с при 429), кэш мертвых моделей шлюзов (`_DEAD_MODELS_CACHE`) и авто-ротация до 3 ключей Google Gemini (v2.44.0).
    - [x] Ликвидация дублей новостей (Вариант 3): устранение логического разрыва L1/L2 в deduplicator.py (семантический HNSW pgvector для всех сырых постов без глухой MinHash отсечки), калибровка порогов и Redis Event Lock в content_worker.py, чистка ретроспективных дублей за 4 дня на сайте и в БД.
  - [x] Ручной режим (Human-in-the-Loop): интерактивный Split-View инспектор статьи в `/articles`, одобрение публикации, ручной перерайт с кастомным промтом, переключатель допуска медиа с водяным знаком.
  - [ ] Режимы объёма: максимальный, умеренный, дозированный
  - [x] Подбор ключевых запросов через Wordstat или аналог (официальный API + семантический ИИ-фоллбек)
- [/] **Фаза 3 — Контентная машина**
  - [x] Глубокий ресерч: веб-поиск через DuckDuckGo с разворачиванием редиректов, SSRF-защита, извлечение чистого текста Trafilatura, фактологический контекст и аугментация промтов для статей 2000–4000 знаков, интеграция в Студию Лаборатории.
  - [x] Органичное внедрение ключей в текст, не в промт (стандарты Excalibur: Title, первые 150 знаков лида, H2)
  - [x] Тематическая релевантная перелинковка, фотообложка в Дзен, транслитерация URL и исправление промта рубрик (v2.34.0)
  - [x] Загрузка видео в VK Video из Лаборатории (MTProto on-demand + VK Video API + инлайн-плеер) и автоматический веб-ресерч с естественным retry-циклом (`enrich_task` в ARQ) (v2.35.0)
  - [/] Умная очистка веб-медиа от дублей (dHash) и водяных знаков + распределение фото по статье (inline) или слайдшоу (v2.36.0)
  - [ ] Объём материала 2000–4000 знаков
  - [ ] Разрешение владельца на медиа с водяным знаком
- [/] **Фаза 4 — Публикатор WordPress**
  - [ ] Публикация по SSH с доступом к файловой системе
  - [ ] Ручное подтверждение перед публикацией
  - [x] Разработка и деплой мобильной темы `svodka-setka` (Светлая/Темная тема по системе пользователя, токены Сетки hh.ru, бесконечный скролл `infinite_scroll.js`, нативные карточки подписки на Telegram)
  - [x] Доработка темы `svodka-setka` v2.51.0: векторный логотип + набор фавиконок (`logo-design`), нижняя панель действий («Предложить новость», «Заказать рекламу», «Контакты»), ротация рекламодателей в ленте и статьях (Telegram / VPN / FreeAPI AI) и защита JSON-LD Schema.org от `wpautop`
  - [x] Доработка `svodka-setka` v2.52.0 и `/ads`: контакты редакции (`@genadich_agency`, `anton@genadich.com`), редактирование контактов и карточек рекламы из админки WordPress + авто-синхронизация с `/ads`, генерация `/llms.txt` и `/llm.txt` для ИИ-ассистентов (с рекомендацией сайта и рекламодателей)
  - [x] Доработка источников и рекламы в статьях v2.58.0: активные кликабельные ссылки для всех источников текста (`Источники информации: ...`) и фото (`Фото: ...` / `<figcaption>`), а также строго 1 ротируемый рекламный блок на 1 пост (по аналогии с главной страницей, без дублирования 3 карточек подряд)
- [/] **Фаза 5 — Аналитика и соцсети**
  - [x] Подключение мульти-поисковой аналитики и вебмастеров: Google Analytics 4 (Data API v1beta + MCP), Google Search Console (JWT RS256 Service Account), Bing Webmaster & IndexNow (мгновенная отправка новых статей на индексацию) с шифрованием Fernet и интерактивной справкой в `/settings`.
  - [x] Самообучаемая система SEO-оптимизации сниппетов (Low-Hanging Fruits) с фиксацией решений ДО/ПОСЛЕ, базой выученных паттернов, 1-Click обновлением в WordPress и выводом реальных данных в `/seo`
  - [x] Устранение сбоя 500 на `/seo`: применение миграции 0014_seo_optimizations на VPS, транзакционная защита с session.rollback() и отказоустойчивая загрузка истории оптимизаций и паттернов
  - [x] Модуль аналитики аудитории и предпочтений (Сайт vs Дзен, 20 рубрик), расчет индекса рекламного потенциала (Ad Potential Score), сохранение срезов в БД (AudienceCategoryMetric) и фундамент для страницы /ads
  - [x] Интеграция реального демографического и поведенческого портрета ЦА (Метрика: пол, возраст, интересы Крипты, гео/города, устройства, пиковые часы) и специализация рекомендаций под размещение баннеров в статьях (/ads)
  - [x] Устранение артефактов альбомов Telegram (пустые посты `raw_news #...`), расширение watermark по ID медиа-вложений, фильтрация постов без текста и настраиваемый лимит сюжетов (50/100/250/Все) в Лаборатории `/lab`
  - [x] Точная вставка фото-обложки в Яндекс Дзен (React SelectionState Sync), автоматизация диалогов условий канала и подтверждение карточки в ленте канала (v2.41.0)
  - [x] Внедрение механики Slide-Over Sheet (Drawer) для «Студии сюжета» по эталону GETVOXLY (URL-driven state, scroll lock, history back, LIFO escape) и создание универсального Skill (v2.42.0)
  - [x] Комплексная доработка контура публикации и Лаборатории: 1) Автоматические кликабельные HTML-ссылки на сайт и Telegram в Дзене; 2) Заметный блок подписки на Telegram в статьях WordPress перед источниками; 3) Статус «Опубликовано» с прямыми ссылками на сайт и Дзен, фильтр опубликованных/неопубликованных сюжетов в /lab; 4) Контекстное смягчение Brand Safety для культуры/сериалов и ручной override в Drawer; 5) Устранение дублей (Dmitry Portnyagin, Boeing 737 Dubai-Tel Aviv) через FastEmbed и Entity-matching; 6) Аналитика потенциала новых рубрик за 48 часов; 7) Настройка и тестирование токена VK Video и сообщества в /settings (v2.45.0)
  - [/] Исправление боевой публикации в Дзен (минуя черновики, прохождение VK SmartCaptcha iframe) и публикатор 244 накопившихся черновиков
  - [/] Двухконтурная защита от дублей на сайте (Zero Duplicates Policy: семантическая сверка кластеров с Article за 48ч + расширение L2 LSH)
  - [/] Исправление прикрепления фото-обложек во ВКонтакте (OpenGraph сниппет attachments=wp_post_url)
  - [x] Исправление селекторов публикации в YouTube Community (обход Google Cookie Consent, фикс селектора превью картинки, эмуляция клавиатуры Lit, обработка модалки лимита квот и защита от дублей) и Сетка (/posts/new composer)
  - [x] Внедрение архитектуры Persistent Chromium Profiles (storage/browser_profiles) для YouTube, TenChat и Сетки вместо слетающих сессий кук
  - [x] Исправление публикатора Сетка (валидация UUID /posts/, отсечение /new), авто-дожим и ручной retry соцсетей для WP_PUBLISHED, тумблер приоритета медиа источников с ватермарками
  - [x] Восстановление извлечения фото из постов-источников Telegram (включая превью видеопостов и MTProto fallback), мульти-поиск веб-ресёрча (RIA + Lenta + Bing RSS вместо заблокированного DuckDuckGo), сохранение фото СМИ с ватермарками и пересборка всех контейнеров воркеров (v2.54.0)
  - [x] Максимальный SEO-пакет для выхода в ТОП: Яндекс.Вебмастер (очередь переобхода /recrawl/queue, диагностика проблем /diagnostics, sitemaps API), Google URL Inspection API (проверка индексации), вывод Google Analytics 4 на `/seo`, и новостные RSS/Турбо-фиды Яндекса и Google
  - [x] Редизайн `/settings` по стандарту `record-drawer-sheet` (v2.57.0): разделение всех объединенных карточек (Яндекс Метрика, Яндекс Вебмастер, Яндекс Wordstat, Google Analytics 4, Google Search Console, Bing Webmaster, IndexNow, WordPress & SSH, Telegram, VK, Дзен и 6 браузерных соцсетей) на компактную сетку независимых карточек со статусами + выезжающее окно настройки (`?service=<id>`) и локальные уведомления прямо под кнопкой тестирования
  - [x] Интеграция платформы Spark.ru (https://spark.ru/startup/svodka-press): автоматическая авторизация и публикация новостей в блог проекта, загрузка обложек, валидация сессии через серверный IP и интеграция в конвейер публикации
  - [ ] Переписывание материала под каждую площадку
  - [ ] Подключение Telegram, ВК, ОК, Дзен через OAuth
  - [ ] Отслеживание видимости в ответах ИИ

## Незакрытые вопросы (продуктовые, не технические)

1. **Правка опубликованной статьи.** Меняется заголовок уже вышедшего
   материала: переписывать живой пост, повторять распространение или учитывать
   только в будущих? Этот же вопрос блокирует применение SEO-правок. Панель
   считает и показывает список работ, но не выполняет его.
2. **Повторное распространение** статьи в статусе `PARTIAL` — нужна кнопка.
3. **Переключатели вкл/выкл на площадку** — адаптеры умеют докладывать
   `DISABLED`, интерфейса нет.

## Что не проверено живыми сервисами

Ни один внешний API не подтверждён живым аккаунтом. OpenRouter, Fal AI, VK, OK,
Dzen, Telegram Bot API, WordPress, Метрика, Search Console — все покрыты моками
транспорта. Это проверяет наше обращение с нашей моделью чужого контракта, а не
сам контракт.

Состояние прода на 2026-09-03: панель, база и очередь работают; три воркера
остановлены вручную 25 августа и сами не поднимутся; конвейер выключен
переключателем; публикаций наружу ноль. Полная таблица — `PROJECT.md` раздел 3.

## Package 1: Infrastructure, Docker, Async DB Engine, SQLAlchemy Models, Alembic Migrations

- [x] Module 1.1: Environment & Dependencies
  - [x] `.env.example` with all DB/Redis/JWT/API/WordPress/SSH/AI/threshold variables
  - [x] `requirements.txt` with pinned versions
  - [x] `pyproject.toml` with ruff + mypy strict config
- [x] Module 1.2: Docker Infrastructure
  - [x] Multi-stage `Dockerfile` (python:3.12-slim-bookworm, Tesseract rus+eng, OpenCV libs, Playwright Chromium, non-root uid 1000, tini)
  - [x] `docker-compose.yml` base stack
  - [x] `docker-compose.override.yml` (loopback-only ports, hot reload)
  - [x] `docker-compose.prod.yml` (no datastore exposure, gunicorn, memory limits)
  - [x] `.dockerignore`
- [x] Module 1.3: Core Application
  - [x] `app/config.py` (Pydantic v2 BaseSettings, production secret guards, embedding-dimension validation)
  - [x] `app/database.py` (async engine, `AsyncSessionLocal`, `Base`, `get_db`)
  - [x] `app/redis_client.py` (BlockingConnectionPool)
  - [x] `app/main.py` (lifespan, `/health` liveness, `/health/ready` readiness)
- [x] Module 1.4: SQLAlchemy Models
  - [x] Domain models
    - [x] `user.py`
    - [x] `source.py`
    - [x] `raw_news.py`
    - [x] `cluster.py`
    - [x] `article.py`
    - [x] `tag_cache.py`
    - [x] `setting.py`
  - [x] `enums.py` native enum definitions
  - [x] `types.py` (JSONB mutation tracking)
  - [x] `vector.py` (single embedding-dimension constant)
  - [x] `__init__.py` model registry
- [x] Module 1.5: Alembic Async Migrations
  - [x] Async `migrations/env.py`
  - [x] Initial migration creates `uuid-ossp` + `vector` extensions
  - [x] Initial migration creates 7 tables and 4 native enums
  - [x] HNSW cosine indexes
  - [x] Partial queue indexes
  - [x] Functional `lower(tag_name)` unique index
- [x] Module 1.6: Verification
  - [x] `scripts/smoke_test.py` with production-database guards
  - [x] Migration upgrade/downgrade/upgrade roundtrip
  - [x] `alembic check` reports no drift
  - [x] ruff + mypy strict clean
  - [x] Full Docker stack healthy

## Package 2: Ingestion Engine & Zero-Cost Deduplication (Phase 1)

- [x] Module 2.1: Ingestion Engine
  - [x] Telegram Ingestor via GramJS API
    - [x] Fetch `/dialogs` & `/messages` with overlap `min_id - 10`
    - [x] HTML entity parser for TG bold/italic/links (UTF-16 offsets)
    - [x] Album grouper by `groupedId` using lowest message ID as primary
    - [x] Background task to mark read via `/messages/read`
  - [x] Web Crawler via Playwright Async
    - [x] Headless Chromium singleton `BrowserPool`, max 2 concurrent pages, resource blocking
    - [x] Parser using CSS/XPath rules from `sources.crawl_rules`
    - [x] Trafilatura cleaner for main article content
    - [x] `content_html`: sanitised article-body markup from both paths, persisted to `raw_news.clean_html`
  - [x] Heuristic Filter (0 tokens)
    - [x] Strip numbered digests ("Главное к этому часу", "1.", "2.")
    - [x] Strip pure PR/ads (erid:, promo codes, bot links)
- [x] Module 2.2: Deduplication Engine
  - [x] Level 1: MinHash LSH fingerprint in Redis, no external API
  - [x] Level 2: semantic vector search via `pgvector` cosine distance against `news_clusters` for last `DEDUP_LOOKBACK_HOURS`
  - [x] Level 3: novelty checker
    - [x] Entity & fact delta comparison
    - [x] Skip duplicates without new facts
    - [x] Create updates when new details emerge
- [x] Module 2.3: Orchestration & API
  - [x] ARQ ingestion worker: per-source transaction, run + per-source Redis locks, `ON CONFLICT DO NOTHING` idempotency
  - [x] Admin API: source upserts, selector dry-run, manual trigger, soft delete
- [x] Module 2.4: Verification
  - [x] 166 tests pass (83 ingestion, 83 deduplication) with no external services
  - [x] ruff clean, mypy strict clean

**Not verified in this package** (needs a live environment, deferred with the
dashboard in Package 5): the run against a real GramJS bridge and a real
PostgreSQL/Redis pair. Level 2 is exercised against a session double and
`fakeredis`, not against pgvector itself.

## Package 3: AI Content Generation & Media Pipeline (Phase 2)

- [x] Module 3.1: Content Engine
  - [x] Micro-step 3.1: Generator, editorial categories & slop blocklist
    - [x] `app/services/slop_filter.py` — anti-cliché validator: 30 fatal + 10 repairable patterns, `validate_ai_text`, `clean_ai_slop`, density scoring per 1000 words
    - [x] `app/services/content_prompts.py` — system/user prompts for 20 editorial categories, JSON Schema for the article contract
    - [x] `app/services/content_generator.py` — async OpenRouter generation, schema + slop validation, one corrective retry
    - [x] `AIClient.complete_json` — multi-turn completion with `response_format`, added for the generator
    - [x] `tests/test_content_engine.py` — 112 tests: blocklist, prompt matrix, response-schema validation, retry paths
    - [x] Verified: 278 tests pass, ruff clean, mypy strict clean
  - [x] Micro-step 3.2: Fixer/QA sub-agent & article embedding generator
    - [x] `app/services/embedding_service.py` — 1536-dim embeddings, Redis cache (TTL 24h, model-scoped keys), batching with duplicate collapsing, outer rate-limit backoff
    - [x] `app/services/content_fixer.py` — free QA review (fact fidelity, structure, metadata limits, coherence, residual slop) plus targeted LLM repair
    - [x] `tests/test_content_fixer.py` — 48 tests: invented/dropped facts, cache TTL and model scoping, 1536-dim verification, repair flow
    - [x] Embedding client released on API and worker shutdown
    - [x] Verified: 326 tests pass, ruff clean, mypy strict clean
- [x] Module 3.2: Media Pipeline
  - [x] Micro-step 3.3: OCR watermark detector & Fal AI Flux generator
    - [x] `app/services/watermark_detector.py` — five-zone analysis (4 corners + bottom strip), Tesseract text detection, overlay-mask detection, overlap-free coverage ratio, crop helper
    - [x] `app/services/flux_generator.py` — async Fal AI `flux/schnell` client: queue polling, CDN download, typed errors, retries with jitter
    - [x] `tests/test_media_pipeline.py` — 66 tests: synthetic watermarked images, mask-vs-texture discrimination, 15% threshold, crop, mocked Fal flow, parameter validation
    - [x] Fal client released on API and worker shutdown
    - [x] Verified: 392 tests pass, ruff clean, mypy strict clean
  - [x] Micro-step 3.4: VK Video service, WP Media uploader & Content/Media pipeline orchestrator
    - [x] `app/services/vk_video_service.py` — async VK API client: `video.save`, multipart upload, responsive iframe embed, typed error-code mapping
    - [x] `app/services/wp_media_service.py` — async WP REST API client: Cyrillic slug transliteration, alt/title/caption from SEO, `media_id` + `source_url` + `thumbnail_url`
    - [x] `app/workers/content_worker.py` — end-to-end orchestrator: claim cluster, generate, QA-fix, embed, process media, VK video, persist `Article` at `MEDIA_READY`
    - [x] `tests/test_wp_media.py` (69), `tests/test_content_worker.py` (33)
    - [x] VK and WP clients released on API and worker shutdown
    - [x] Verified: 494 tests pass, ruff clean, mypy clean
  - [ ] Fast inpainting / clean image worker — deferred. The watermark path already
        covers the two outcomes that matter (crop a light badge, regenerate a
        heavy one); inpainting is a quality refinement on the middle ground and is
        not required for Package 3 to publish.

## Package 4: Multi-Platform Publisher Engine (Phase 3)

- [x] Module 4.1: WordPress Publisher
  - [x] Micro-step 4.1: WordPress master platform publisher
    - [x] `app/services/wp_post_formatter.py` — Jinja2 universal post template: semantic typography, featured `<figure>`, CSS scroll-snap carousel, VK video embeds, Schema.org `NewsArticle` JSON-LD, related-articles block, body sanitiser
    - [x] `app/services/wp_publisher.py` — WP REST adapter: create/update posts, `featured_media`, category resolution by slug, tag sync through `wp_tags_cache`, Rank Math meta injection with write verification
    - [x] `build_basic_auth_header` extracted so the media service and the publisher share one credential path
    - [x] `tests/test_wp_publisher.py` — 89 tests: sanitiser XSS probes, JSON-LD structure and script-breakout escaping, template blocks, tag-cache dedup and lost-race recovery, mocked REST creation with Rank Math
    - [x] Publisher client released on API shutdown
    - [x] Verified: 583 tests pass, ruff clean, mypy strict clean
  - [x] Infinite scroll theme snippet — delivered in Micro-step 4.3
  - [x] Publishing worker wiring — delivered in Micro-step 4.3
- [x] Module 4.2: Social Media Adapters
  - [x] Micro-step 4.2: Pluggable social adapters
    - [x] `app/services/social_base.py` — `BaseSocialAdapter` ABC, `PublishResult` contract, `safe_publish` isolation, shared teaser/hashtag builders
    - [x] `app/services/social_telegram.py` — Bot API `sendPhoto`/`sendMessage`, UTF-16-aware 1024-char caption, inline CTA button, `retry_after` handling, photo→text fallback
    - [x] `app/services/social_vk.py` — `wall.post` with `from_group=1` and a negative `owner_id`, video/photo/link attachment serialisation, hashtags
    - [x] `app/services/social_ok.py` — `mediatopic.post` with the documented MD5 `sig`, session-secret derivation, typed media JSON array
    - [x] `app/services/social_dzen.py` — canonical-URL-guarded rich HTML, tag-subset reduction, cached auth state with expiry, two-step draft→publish lifecycle
    - [x] `normalise_group_id` / `raise_for_vk_error` promoted to public in `vk_video_service` so both VK services share one error table
    - [x] `tests/test_social_adapters.py` — 93 tests: caption limits, attachment serialisation, hand-computed OK signature, per-adapter error paths
    - [x] Verified: 676 tests pass, ruff clean, mypy strict clean
  - [x] Micro-step 4.3: Publisher orchestrator worker & infinite-scroll snippet
    - [x] `app/workers/publisher_worker.py` — claim `MEDIA_READY`, WP first, concurrent social fan-out with `return_exceptions=True`, per-platform `platform_statuses`, terminal status, exponential backoff
    - [x] `app/templates/wordpress/infinite_scroll.js` — IntersectionObserver pair, `history.pushState`, Yandex Metrika + GA4 virtual hits, full teardown on unmount
    - [x] `WPMediaService.get_media_url` — resolves `wp_image_id` to a URL so social posts carry a cover
    - [x] `tests/test_publisher_worker.py` — 34 tests: state transitions, partial publication, idempotency, retry budget
    - [x] Verified: 710 tests pass, ruff clean, mypy clean

## Package 5: Web UI Dashboard & SEO Analytics (Phase 4)

- [/] Module 5.1: Web UI Dashboard (FastAPI + Jinja2 + HTMX + Tailwind)
  - [x] Micro-step 5.1: Dashboard, SSE logs, articles editor & sources manager
    - [x] `app/core/security.py` — bcrypt hashing, JWT issue/verify with pinned algorithm, Fernet secret box, `hash`/`genkey` CLI
    - [x] `app/api/auth.py` — `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `get_current_admin_user` (401) and `require_admin_page` (303 redirect)
    - [x] `app/api/logs_sse.py` — `GET /api/logs/stream` over Redis pub/sub, `RedisLogHandler` for the workers, heartbeat and teardown
    - [x] `app/api/dashboard.py` + `app/templates/dashboard/` — base, login, index, articles, sources, settings, logs + 3 HTMX partials
    - [x] `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` / `SESSION_COOKIE_NAME` / `FERNET_KEY` settings, validated in production, documented in `.env.example`
    - [x] `tests/test_dashboard_ui.py` — 60 tests: auth, guards, open-redirect, cookie flags, partials, XSS escaping, SSE
    - [x] Verified: 770 tests pass, ruff clean, mypy clean
  - [ ] Platform toggles (per-platform enable/disable, API / WebUI mode)
  - [ ] Articles editor: saving edits (read-only for now — a save has to decide what editing a *published* article means)
  - [ ] Settings: writing keys through the UI into an encrypted store (currently read from `.env` and displayed masked)
- [x] Module 5.2: SEO Analytics & Ranking Watcher
  - [x] Micro-step 5.2: SEO watcher, DevOps finalisation & VPS deployment
    - [x] `app/services/seo_analytics.py` — Metrika + GSC clients, position-aware CTR watcher, value-ranked optimisation queue
    - [x] `/seo` dashboard route + `app/templates/dashboard/seo.html` with the HTMX optimisation trigger
    - [x] `scripts/deploy_vps.sh` — prerequisite checks, `.env` and secret-length validation, compose validation, migrations, health gates
    - [x] `scripts/remote_ssh.py` — SSH executor with host-key verification and `shlex`-quoted commands
    - [x] `tests/test_seo_analytics.py` — 59 tests: CTR curve, every threshold gate, both API parsers, dashboard route, command quoting
    - [x] Verified: 829 tests pass, ruff clean, mypy clean
  - [ ] One-click soft optimisation *execution* — the queue is computed and displayed; enqueuing the regeneration needs the same "what happens to the live post" decision as the articles editor's save
- [x] Module 5.3: DevOps & Dual Environment
  - [x] Local dev compose (ports open, hot reload) — delivered in Package 1
  - [x] Prod VPS compose (isolated network, healthchecks, auto-restart) — delivered in Package 1
  - [x] Remote SSH script executor for target WordPress server

## Package 6: Production deployment (VPS 144.31.141.17)

- [x] Module 6.1: Host provisioning
  - [x] Docker Engine 29.7.2 + Compose v5.5.0 installed on Ubuntu 24.04 from Docker's apt repository
  - [x] SSH key authentication established, replacing password auth for deployment commands
  - [x] Production `.env` generated: 86-char `SECRET_KEY`, 32-char Postgres/Redis passwords, bcrypt `ADMIN_PASSWORD_HASH`, Fernet key
  - [x] `docker-compose.override.yml` removed from the host so the dev datastore ports are never published
- [x] Module 6.2: ARQ workers as first-class services
  - [x] `worker-ingestion` (2G, `shm_size` 512m for Chromium), `worker-content` (1G), `worker-publisher` (1G) added to `docker-compose.prod.yml`
  - [x] Redis-ping healthcheck overriding the image's HTTP check, which a worker cannot answer
  - [x] Per-worker `queue_name` — fixes cross-worker job theft on ARQ's shared default queue
  - [x] `check_workers()` gate in `deploy_vps.sh`
- [x] Module 6.3: Reverse proxy support
  - [x] `ALLOWED_HOSTS` / `TRUSTED_PROXY_HOSTS` / `CORS_ALLOW_ORIGINS` settings with CSV parsing and production guards
  - [x] `TrustedHostMiddleware` and optional CORS in `app/main.py`; `FORWARDED_ALLOW_IPS` wired to uvicorn via compose
  - [x] `APP_BIND_ADDRESS` for a proxy on a different host
  - [x] Pre-flight checks for CIDR in `TRUSTED_PROXY_HOSTS`, loopback in `ALLOWED_HOSTS`, and `DOCKER-USER` filtering
- [x] Module 6.4: Network exposure
  - [x] `ufw` enabled, default-deny inbound, SSH allowed
  - [x] Port 8000 filtered in the `DOCKER-USER` iptables chain — `ufw` alone does not cover a published Docker port
  - [x] Rules persisted with `iptables-persistent`
  - [x] Verified from an external address: blocked; verified from loopback and the bridge: allowed
- [x] Module 6.5: Live verification
  - [x] Migrations applied (`0002_ingestion_dedup_fields`), 8 tables, `pgvector` + `uuid-ossp` present
  - [x] All six services healthy; all three cron schedules observed firing and completing
  - [x] Login, session cookie (`HttpOnly`, `Secure`, `SameSite=lax`), and all six dashboard pages return 200
  - [x] `X-Forwarded-For` honoured from the proxy and ignored from an untrusted source
  - [x] Verified: 835 tests pass (random order), ruff clean, mypy clean
- [x] Module 6.6: Review fixes
  - [x] Production refuses to boot on an unset `ALLOWED_HOSTS`, any bare `*` entry, or an allowlist omitting `127.0.0.1`
  - [x] Matching three-way check in `deploy_vps.sh`, matching `*` as a delimited element rather than a substring
  - [x] Worker healthchecks switched to `arq --check` with `health_check_interval = 30` (ARQ's 3600s default left a wedged worker healthy for an hour)
  - [x] Corrected the `enqueue_job` docstring that omitted `_queue_name`, and the comment misdescribing the dashboard trigger
  - [x] Host-allowlist tests rebuilt from `Settings` so they fail when a guard is removed
  - [x] Verified: 841 tests pass (random order), ruff clean, mypy clean; worker liveness proven by freezing a container
- [x] Module 6.7: Reverse-proxy access & dashboard actions
  - [x] Port 8000 opened to the NPM host `150.241.98.134` in `ufw` and in the `DOCKER-USER` chain, persisted across reboot
  - [x] Reinstalled `ufw`, which `iptables-persistent` had silently uninstalled during the 2.0.1 deployment
  - [x] Three cookie-authenticated HTMX endpoints on `/api/ui`: ingestion trigger, `skip_media` toggle, selector dry-run
  - [x] Three response partials rendering success, warnings and failure without leaking a traceback into the page
  - [x] Template guard test parsing every `hx-*` URL against the registered routes
  - [x] Verified: 853 tests pass (random order), ruff clean, mypy clean; all three buttons exercised live on the VPS
- [ ] Module 6.8: Public access — needs the DNS A record for `svodkanews.team-big.ru`; the NPM host entry is already forwarding

## Package 7: Multi-account Telegram ingestion & admin profile

- [x] Module 7.1: Admin profile manager
  - [x] `app/services/admin_credentials.py` — credentials in `system_settings`, `.env` as bootstrap fallback
  - [x] `/profile` page + clickable username in the navigation bar
  - [x] `POST /api/ui/profile/username` and `/password`, both requiring the current password
  - [x] Session subject checked against the live username, so a rename ends old sessions
- [x] Module 7.2: Multi-account Telegram
  - [x] `telegram_accounts` table + migration `0003` (`alembic check` clean)
  - [x] Fernet-encrypted `session_string`, `is_active`, `last_used_at`, `flood_wait_until`, `failure_count`
  - [x] `TelegramAccountPool` — least-recently-used rotation, persisted flood-wait penalty, failure deactivation
  - [x] `app/services/telegram_auth.py` — phone → code → 2FA sign-in producing a `StringSession`
  - [x] `scripts/auth_telegram.py` — interactive CLI, `--list`, upsert by phone number
  - [x] `app/services/telegram_mtproto.py` — Telethon reader emitting the same value objects as the bridge
  - [x] Account pool wired into the ingestion worker, one client per pass, GramJS fallback retained
  - [x] Accounts panel on `/sources` with active / flood-wait / auth-required / disabled states
- [x] Module 7.3: Verification
  - [x] `tests/test_telegram_accounts.py` — 53 tests: encryption, key rotation, rotation order, flood wait, 2FA fallback, credential store
  - [x] Verified: 907 tests pass (random order), ruff clean, mypy clean across 63 source files
- [x] Module 7.4: Authorised `+79055342403` (Antonios / @Genadich2403) through the code + 2FA path; verified reading 50 live messages from `@telegram` through the pooled session

## Package 8: OmniRoute gateway & first live pipeline run

- [x] Module 8.1: OmniRoute wiring
  - [x] `OPENROUTER_BASE_URL=https://omniroute.team-big.ru/v1`, key installed, `auto/fast` for writer/arbiter/SEO
  - [x] Verified from inside the container: 1315-model catalogue and a live completion
- [x] Module 8.2: Selectable models
  - [x] `app/services/model_routing.py` — per-stage overrides in `system_settings`, `.env` as fallback
  - [x] Editable matrix on `/settings` with a datalist fed from the gateway catalogue
  - [x] `POST /api/ui/settings/models`; empty field clears an override
  - [x] Writer model resolved per cluster, so a change applies without a restart
- [x] Module 8.3: Honest worker panel
  - [x] Ingestion status now reflects the account pool instead of the obsolete `GRAMJS_TOKEN`
- [x] Module 8.4: Live control run
  - [x] 3 Telegram sources (ТАСС, РИА, Интерфакс); 151 raw posts, 143 clusters, 27 articles
  - [x] Zero-token filter rejected 8 posts (7 digests, 1 ad) before any API call
  - [x] **Fixed: response scaffolding leaked into `content_html`** on 4 of the first 11 articles
- [ ] Module 8.5: Embeddings and covers — gateway has no embedding credentials (`403`/`400`), `FAL_KEY` unset. Both degrade gracefully; dedup level 2 and AI covers are inactive until provided.

## Package 9: Ingestion worker contract (Telethon)

- [x] Module 9.1: Entity filtering — only broadcast channels, public or private
  - [x] `is_broadcast_channel` / `describe_entity`; megagroups, gigagroups, small groups, private chats and bots are refused
  - [x] `gigagroup` checked before `broadcast`: a converted megagroup reports `broadcast=True` while still carrying group traffic
- [x] Module 9.2: First pass — exactly `INGEST_MAX_MESSAGES_PER_SOURCE` (50) posts, `min_id=0`
- [x] Module 9.3: Later passes — `min_id` from the stored watermark, minus the overlap window
- [x] Module 9.4: Read acknowledgement — `send_read_acknowledge` sent by the worker *after* the batch commits
  - [x] Removed the bridge's pre-commit `mark_read` call so there is a single ordering
- [x] Module 9.5: Verified
  - [x] 16 new tests; each rule proven to fail when its implementation is broken
  - [x] Live: channels accepted, `@python`/`@durovschat` (супергруппы) and `@BotFather` (бот) refused; first pass 50 posts ascending; watermark pass returned only the overlap window; acknowledgement returned True

## Package 10: Specification alignment (in progress)

- [x] Module 10.1: Category catalogue and rewrite styles (spec section 4)
  - [x] `app/services/categories.py` — `CATEGORY_MAP` with all 20 WordPress term ids
  - [x] Six rewrite voices; the spec's five cover 18 categories, astrology and esports got a sixth rather than the incident register
  - [x] Moderation overrides: incidents, military, spam, default-on-doubt
  - [x] Body-format rules (clean HTML, no wrappers, no channel promotion) stated in the prompt
  - [x] `RiskLevel` -> green/yellow/red/spam; `spam.blocks_generation` drops a cluster before any API spend
  - [x] `ArticleStatus` gains `media_planned`, completing draft -> draft_tag -> media_planned -> media_ready -> wp_published
- [x] Module 10.2: Local embeddings (spec section 5)
  - [x] `app/services/local_embeddings.py` — fastembed MiniLM-L12-v2, 384 dims, zero tokens, no network
  - [x] Deduplication window set to 48h
  - [x] Thresholds calibrated to the model by measurement (0.55/0.76) instead of the spec's 0.75/0.92
  - [x] Named volume for the 220 MB model cache; Dockerfile creates the mount point so the non-root user can write it
- [x] Module 10.3: Migration `0004_spec_alignment`
  - [x] pgvector 1536 -> 384 with HNSW rebuild, `risk_level` enum swap, `media_planned`, four new `sources` columns
  - [x] `posts_collected` backfilled from `raw_news`
- [x] Module 10.4: Verified — 992 tests pass, ruff and mypy clean, applied on the VPS and embedding confirmed in-container
- [x] Module 10.5: Model fallback pool (spec section 1) — delivered as Module 11.9; see that entry for the detail
- [x] Module 10.6: Sources page (spec section 2)
  - [x] `MTProtoIngestor.list_channels()` — walks the dialog list, keeps broadcast channels only, avatars as data URIs
  - [x] `POST /api/ui/sources/telegram/import` — upsert by identifier; new channels arrive inactive
  - [x] `POST /api/ui/sources/{id}/flags` — both checkboxes in one request; row fragment re-rendered from the DB
  - [x] `POST /api/ui/sources/telegram/bulk` — select all / clear all / media-only
  - [x] Live: 33 channels imported from the session, table renders 108 rows, bulk and single toggles verified
- [x] Module 10.7: Articles page (spec section 3)
  - [x] Pagination at 20/page; count query shares the filter predicates
  - [x] Filters: category, status, period (24h/3d/all), has-media, title search
  - [x] Rows: thumbnail (302 to WordPress or inline SVG placeholder), category badge with colour groups, DD.MM.YYYY HH:MM date
  - [x] Readiness grid: rewrite / tags / media; distribution grid: WP / TG / VK / Dzen chips from platform_statuses
  - [x] Preview modal: rendered HTML in a sandboxed pane + quick edit (title, SEO, category) persisting wp_category_id in step with the slug
- [x] Module 10.8: Moderation prompt wiring — delivered as Module 11.7; the verdict JSON is returned and spam drops the cluster before any writer call

## Package 11: Observable pipeline (Лаборатория) & production switch

- [x] Module 11.1: Stage contract and artifacts
  - [x] `app/services/pipeline_stages.py` — `StageResult`, `RunSummary`, `ArtifactStore`
  - [x] Artifacts on disk under `storage/runs/<run_id>/NN-stage.json`, bounded by `prune(keep=50)`
  - [x] `run_id` validated against path traversal: it arrives from a URL parameter
- [x] Module 11.2: Run orchestrator
  - [x] `app/services/pipeline_runner.py` — collect → dedup → cluster → research → article → media
  - [x] Reuses the production services rather than reimplementing them, so the run shows real behaviour
  - [x] Test mode writes no articles and publishes nothing
  - [x] Research stage (borrowed from the Excalibur pipeline) is token-free: archive facts + similar past articles
- [x] Module 11.3: Лаборатория page
  - [x] `/lab` with «Сбор + прогон» and «Прогон без сбора»
  - [x] Timeline per stage: verdict, model, latency, warnings, prompt sent, rendered article
  - [x] Dedup rows show similarity, ladder level and the threshold crossed
  - [x] Run history replayed from artifacts
- [x] Module 11.4: Production switch and schedule
  - [x] `app/services/pipeline_control.py` — flag + interval in `system_settings`, Redis-cached 10s, fails open
  - [x] Cron tasks in all three workers end immediately when paused; the lab keeps working
  - [x] `/settings` section: switch + collection interval (5–120 min, default 15), cron minutes derived
- [x] Module 11.5: Verified — 1029 tests, ruff and mypy clean, live run on the VPS
- [x] Module 11.6: Publishing modules in the lab
  - [x] M6 Telegram: caption built by the production `build_caption`, length against the 1024-unit limit, hashtags
  - [x] M7 Дзен: body reduced by `build_dzen_html` to the accepted tag subset, before/after lengths
  - [x] M8 ВКонтакте: wall text by `build_wall_message`; the link travels as an attachment, so the text carries none
  - [x] Adaptation only — a test run posts nothing, stated in each module's panel
- [x] Module 11.7: Moderation as the first content stage
  - [x] `app/services/moderator.py` — verdict JSON (rubric, risk, reason), 20-rubric enum schema
  - [x] Spam dropped before any writer call; the cluster is marked processed so it does not return
  - [x] The two absolute rules re-applied in code, not only in the prompt: incidents and military
  - [x] `risk_level` reaches the writer: `yellow` softens, incidents and military get the emergency shape
  - [x] Moderation stage in the lab timeline with the prompt and the override note
- [ ] Module 11.8: Per-module enable toggles and live posting from the lab
- [x] Module 11.9: Model fallback pool (60s cooldown, ordered chain)
  - [x] `app/services/model_pool.py` — `run_with_fallback` walks primary → fallback 1 → fallback 2
  - [x] A transient failure moves to the next model: 429, 5xx, a transport error and an unusable reply all say the same thing about this model right now
  - [x] A configuration error is not retried: 400/401/403/404 repeat identically on every model, and cycling the chain would bury the real cause
  - [x] `validate` hook lets a caller reject a syntactically fine but unusable answer, counting it as that model failing
  - [x] Cooldown in Redis rather than per-process, so three workers and the web tier do not each rediscover a dead model
  - [x] Cooldown keyed per role: a model healthy for a short classification can fail on a long article
  - [x] Flat 60s, no exponential growth — a growing penalty would migrate every stage onto its worst model
  - [x] A fully cooling chain still attempts the primary: a stalled pipeline is worse than one wasted call
  - [x] `probe_models` — the gateway advertises hundreds of models and says nothing about this account, so availability is established by calling; an empty reply counts as a failure because that is this gateway's main mode
  - [x] `/settings`: per-stage chain table, «Проверить модели» and «Сбросить кулдауны», report with latencies and live cooldowns
  - [x] `load_chains` / `save_chains` in `model_routing.py`, `CHAIN_LENGTH = 3`, de-duplicated per role
  - [x] Wired into the moderator (`arbiter`) and the writer (`writer`) in the production worker; a fallback writes a warning naming the model into the article report
  - [x] Verified: 1076 tests pass, ruff clean, mypy (strict) clean across 70 source files
- [x] Module 11.10: Queue reset and the zero-token intake inspection
  - [x] `app/services/pipeline_reset.py` — clears `raw_news`, article-less clusters, the LSH buckets, the cached signatures and the model cooldowns
  - [x] Rewinds `sources.last_scraped_at` and `posts_collected`, which is what makes the next pass take `INGEST_MAX_MESSAGES_PER_SOURCE` posts from **every** channel instead of the overlap window
  - [x] Clusters carrying an article survive (`articles.cluster_id` is NOT NULL ON DELETE CASCADE, so deleting one deletes a published article) and the report says what that costs
  - [x] Refused while the ingestion run lock is held: a mid-pass reset would delete the rows that pass compares against
  - [x] PostgreSQL first in one transaction, Redis after: a leftover bucket over-reports duplicates visibly, the reverse under-reports them silently
  - [x] «Очистить очередь» on `/lab`, available in test and production mode, with per-store counts rather than "done"
  - [x] `app/services/pipeline_inspection.py` — collect → dedup → watermarks → research, no paid call anywhere
  - [x] Dedup report: every post with its channel, verdict, ladder level, similarity, the threshold it was compared against, the filter rules that fired, and all attached media
  - [x] Watermark stage downloads the photos (the one thing metadata cannot answer) and reports clean / croppable / rejected / unchecked, with OCR text and coverage; Tesseract and the overlay mask are local and free
  - [x] `MTProtoIngestor.download_photo` — per-message fetch bounded by `MAX_IMAGE_BYTES`, one pooled client per run
  - [x] "Never looked" is kept apart from "looked and found nothing": media collection off and `TEXT_ONLY` report `unchecked`, `ALWAYS_REGENERATE` reports `rejected`
  - [x] Research stage reports every cluster: facts, source texts, media and the archive matches over 30 days
  - [x] `app/services/research.py` — `collect_facts` / `find_related_articles` shared by the lab run and the inspection, so the two cannot disagree
  - [x] Lab buttons split by cost: the two free passes first, the two token-spending ones behind a confirmation
  - [x] Fixed: the suite inherited the developer's `.env`, so setting `ALLOWED_HOSTS` locally registered `TrustedHostMiddleware` and failed 60 dashboard tests with "Invalid host header"
  - [x] Verified: 1111 tests pass (random order), ruff clean, mypy (strict) clean across 73 source files
- [x] Module 11.11: Writing the chosen cluster from the inspection timeline
  - [x] `PipelineRunner.run(cluster_id=...)` writes the story the operator picked instead of the newest unwritten one
  - [x] «Написать статью» per cluster in the research report, with `collect=0` because the posts are already stored
  - [x] A cluster that already has an article offers no button: paying twice for one story is the mistake this prevents
  - [x] A missing cluster fails the stage instead of falling back to «newest» — silently writing a different story than the one chosen is worse than writing nothing
  - [x] A malformed id is refused with 422 for the same reason
  - [x] `chosen_by_operator` recorded on the cluster stage, so a replayed run says which story was picked deliberately
  - [x] Verified: 1115 tests pass (random order), ruff clean, mypy (strict) clean across 73 source files
- [x] Module 11.12: Мульти-фото ресерч, тематическая перелинковка, транслитерация слагов и обложка Дзена (v2.34.0)
  - [x] Мульти-извлечение репортажных фото из веб-ресерча (до 6 чистых фото через XPath, srcset, article/figure)
  - [x] Тематическая кластеризация перелинковки (`CATEGORY_CLUSTERS`) и русский стемминг (`_stem_ru`) — устранение оффтопика
  - [x] Транслитерированные безопасные слаги (`slugify`) без кириллицы и точек в URL
  - [x] Обогащение 4-словных заглушек промпта рубрики богатым стилем и устранение рассинхрона рубрик
  - [x] Адаптивная верстка Студии: фикс наложения селектора стиля и доп. указаний, перелинковки и AEVI-лейблов
  - [x] Инъекция обложки в тело статьи Дзена через Draft.js (`<p><img src="..." /></p>`) и формирование точного WordPress URL первоисточника
  - [x] Verified: 29 тестов модуля pass, ruff clean, деплой на VPS 144.31.141.17 выполнен
- [x] Module 11.14: Перцептивная дедупликация фото (dHash), фильтрация ватермарок и распределение фото по статье / слайдшоу (v2.36.0)
  - [x] Модуль `app/services/image_cleaner.py`: 64-битный разностный хеш (dHash), расстояние Хэмминга ($\le 6$), отсечение дубликатов с разных доменов СМИ
  - [x] Фильтрация низкого разрешения (<350x200), аномальных пропорций (баннеры), однотонных/размытых градиентов (Laplacian variance < 35, std < 12)
  - [x] Интеграция с `watermark_detector.py` для отсечения логотипов СМИ и каналов
  - [x] Распределение фото по тексту статьи (`layout="inline"`): семантические `<figure>` с адаптивными отступами и указанием первоисточника `Фото: {domain}`
  - [x] Горизонтальное слайдшоу/карусель (`layout="slideshow"`): чистый CSS `.svodka-carousel` без внешних JS-библиотек
  - [x] Селектор размещения фото в Студии (Шаг 3) и передача параметра в `run_cluster_generate_step`
  - [x] Интеграция очистки в `web_researcher.py:conduct_web_research`
  - [x] Verified: 7 тестов модуля pass, 32 регрессионных теста pass, ruff clean across repository
- [x] Module 11.15: Фильтрация рекламных заставок, потоковое видео, точная перелинковка и авто-ресерч (v2.37.0)
  - [x] Блокировка доменов агрегаторов Telegram (`tvgram.ru`, `tgstat.ru` и др.) в `_AGGREGATOR_DOMAINS` и `_DISALLOWED_DOMAINS`
  - [x] Отсечение тёмных заставок и экранов с заливкой фона (`MIN_MEAN_BRIGHTNESS = 28.0`, `dark_ratio > 0.65`, `white_ratio > 0.65`)
  - [x] Интерактивный Lightbox: клик по фото открывает оригинал в новой вкладке через серверный прокси
  - [x] Потоковое видео по требованию (`GET /api/ui/lab/media/video/{post_id}`): HTML5 `<video controls preload="metadata">`
  - [x] Строгая перелинковка: блокировка военных ссылок для мирных тем, минимум 2 стемма для межкатегорийных связей
  - [x] Авто-ресерч перед генерацией: если факты пусты, `run_cluster_generate_step` автоматически вызывает ресерч
  - [x] Восстановление кнопки публикации в Дзен в интерфейсе и размещение в шапке результата и в табе Дзена
  - [x] Verified: 38 тестов pass (100%), ruff clean across app/ and tests/
- [x] Module 11.16: ИИ-валидация релевантности медиа, точная перелинковка и двухканальный блок публикации Дзен + WP (v2.38.0)
  - [x] Селективное извлечение картинок в `web_researcher.py` (og:image, twitter:image, целевые фигурные блоки, max 2 на статью, без generic классов)
  - [x] Верификация медиа через ИИ (`verify_images_with_ai`): отсечение телеведущих в студии, случайных машин, спорта, рекламы
  - [x] Отображение статуса проверки ИИ в Студии медиа (Шаг 2): бейджи одобрения и отсеянные оффтопики
  - [x] Исправление перелинковки в `lab_pipeline.py`: изолирование `voennye-novosti`, фильтрация стоп-стеммов («облас», «регион» и др.), защита от врезки в HTML-теги и кавычки
  - [x] Двухканальный постоянный блок публикации Шага 4 (WordPress + Яндекс Дзен): независимые карточки, индикаторы и раздельные контейнеры
  - [x] Verified: 8 тестов модуля pass, 36 тестов набора pass (100%), ruff clean across repo, deployed to VPS (svodkapress_app, svodkapress_worker_lab, svodkapress_worker_content, svodkapress_worker_publisher healthy)
- [x] Module 11.17: Верхний плашечный OCR/Watermark-детектор, перенаправление HTMX при 401, реестр рекламодателей (FreeAPI AI) и мульти-публикация статьи 302 (v2.39.0)
  - [x] Расширение `watermark_detector.py`: добавлена зона `top_center` (`TOP_STRIP_HEIGHT_RATIO = 0.22`, `TOP_STRIP_WIDTH_RATIO = 0.75`), регулярные выражения названий каналов (`_CHANNEL_NAMES_RE`: топор, baza, shot, mash, readovka и др.), инспекция Telegram-медиа `storage/media/telegram/{id}.jpg`
  - [x] Защита от тихих зависаний HTMX: заголовок `"HX-Redirect": "/login"` при HTTP 401 и глобальные обработчики событий `htmx:responseError`, `htmx:sendError` в `base.html`
  - [x] Исправление вызова ИИ в `image_cleaner.py`: замена ошибочного `system_instruction` на `system` в `AIClient.complete`
  - [x] Реестр рекламодателей: регистрация первого рекламодателя `FreeAPI AI` (`freeapiai`, target URL `https://freeapiai.ru/r/tdCxTE7k`, UTM-метки, счетчик Метрики `113062256`)
  - [x] Переработка статьи 302 («1,6 млрд заработали авторы на «ВКонтакте»: доходы на шопсах и новая экономика контента») с нативной интеграцией FreeAPI AI, успешная синхронная публикация в WordPress (ID 33787, Rank Math 100%) и Яндекс Дзен Студию (ID 6abbd287ffc0bf29f2be13b0)
  - [x] Verified: 37 тестов набора pass (100%), ruff clean across app/ and tests/
- [x] Module 11.18: Нативное форматирование Яндекс Дзена (ClipboardEvent), богатые блоки рекламы (FreeAPI AI) и конверсионная карточка сайта (v2.40.0)
  - [x] Замена `document.execCommand('insertHTML')` на синтетический `ClipboardEvent('paste')` с `DataTransfer` (`text/html` + `text/plain`) в `dzen_publisher.py`
  - [x] Поддержка нативных элементов Дзена: заголовки H2/H3, цитаты `<blockquote>`, активные ссылки `<a>`, встраивание обложки и галерей изображений
  - [x] Очистка всплывающих модальных окон и туториалов Дзена (`[class*="help-popup"]`, `.ReactModalPortal`)
  - [x] Интеграция промо-блока рекламодателя в Дзен через `<blockquote>` с кликабельными ссылками и UTM-метками
  - [x] Оформление промо-карточки FreeAPI AI на сайте (WordPress) с темным градиентом, бейджем и кнопкой действия (`build_promo_card`)
  - [x] Verified: 101 тест social/dzen pass, 37 регрессионных тестов pass, ruff clean, деплой на VPS 144.31.141.17 выполнен успешно
- [x] Module 11.19: Авто-проверка ВК без привязки к IP, подключение 6 соцсетей через браузерные сессии (Setka, TenChat, Threads, Facebook, Pinterest, YouTube Community) и 100% синхронизация «Боевого режима» со Студией (v2.46.0)
  - [x] Живая проверка токена ВК при сохранении + 1-клик получение токена без IP-привязки (`client_id=6121396`)
  - [x] Подсистема `app/services/social_browser_publisher.py` для Setka, TenChat, Threads, Facebook, Pinterest, YouTube Community
  - [x] Интерфейс управления 6 новыми соцсетями в `/settings` (`social_browser_cards.html`) и подключение в `publisher_worker.py`
  - [x] 100% синхронизация `content_worker.py` со Студией (`/lab`): предрасчет Яндекс Вордстата (`_generate_wordstat_and_seo`), `Schema.org JSON-LD`, авто-подбор рекламодателя из `/ads` с Brand Safety
  - [x] Verified: 167 тестов (`test_social_browser_publisher.py`, `test_social_adapters.py`, `test_publisher_worker.py`, `test_content_worker.py`) pass, ruff 0 errors, deployed to VPS `144.31.141.17`
- [x] Module 11.20: Исправление публикации ВКонтакте по ключу сообщества, восстановление обложек статей (/articles), прямое скачивание фото из Telegram (MTProto) и backfill (v2.47.0)
  - [x] VK Wall Publishing: поддержка ключей доступа сообщества (`vk1.a.*`), устранение ошибки API 100 (`Violated: link_photo_sizing_rule`), встраивание ссылок в текст записи, успешная живая публикация на стене сообщества 236626422 (`https://vk.com/wall-236626422_7`, `_8`, `_9`)
  - [x] Обложки на `/articles`: исправление резолвера `article_thumbnail_partial` в `app/api/dashboard.py` (загрузка реквизитов WP из БД `system_settings`), HTTP 302 редирект на реальные изображения WordPress
  - [x] Telegram Photo Extraction в `content_worker.py`: двухуровневая система получения фото (публичные эмбеды с `telesco.pe` CDN + прямое скачивание через MTProto для закрытых/числовых каналов)
  - [x] Скрипт восполнения обложек `scripts/backfill_article_covers.py`: загружено и привязано 47 обложек к статьям в WordPress и базе данных
  - [x] Прямые ссылки на публикации в Инспекторе статьи (`article_preview.html`): рабочие кнопки Сайта, Дзена, ВКонтакте и Телеграма
  - [x] Module 11.21: Нативная публикация ВК с фото (Messages Upload Server), богатое форматирование и CTA, промо-блоки FreeAPI AI на сайте и подключение Telegram Bot (v2.48.0)
  - [x] Загрузка и прикрепление реальных обложек к записям ВКонтакте через API (фоллбек `photos.getMessagesUploadServer` + `photos.saveMessagesPhoto` для community token)
  - [x] Богатый лонгрид ВКонтакте в `build_wall_message`: 3-5 содержательных абзацев фактуры из тела статьи, кликабельная ссылка на сайт, призыв в Telegram и рекламный блок FreeAPI AI
  - [x] Конверсионный футер статьи на сайте (`publisher_worker.py`): стильный блок подписки на Telegram (@svodka_press) и нативная промо-карточка спонсора FreeAPI AI (`build_promo_card`)
  - [x] Настройки Telegram Bot в интерфейсе `/settings` и поддержка публикации через Bot API из БД `SystemSetting`
  - [x] Verified: ruff clean, 138 тестов pass (100%), живая публикация VK post 26 на стене сообщества 236626422 с реальным фото-аттачем, сайт post 33979 с промо-карточкой FreeAPI AI и блоком Telegram, деплой на VPS 144.31.141.17 выполнен успешно
- [x] Module 11.22: Починка дублей кластеров (LSH топонимы + ambiguous_threshold=0.40), исправление BrowserPool.page_with_storage, нативный веб-паблишер TenChat и публичные ссылки Дзена (v2.49.0)
  - [x] Расширение `_SIGNIFICANT_ENTITY_RE` в `deduplicator.py` — охват русских топонимов (от 5 букв), авиакомпаний и именованных сущностей
  - [x] Снижение `lsh_ambiguous_threshold` с 0.60 до 0.40 в `config.py` для предотвращения деления событий на 16 дублирующих кластеров
  - [x] Исправление `AttributeError: 'BrowserPool' object has no attribute 'page_with_storage'` — добавление метода `page_with_storage(storage_state, block_resources=False, timeout_seconds=...)` в `BrowserPool`
  - [x] Реализация нативного паблишера `_publish_tenchat_post` в `social_browser_publisher.py`: авто-закрытие обучающих окон (`Далее` $\to$ `Приступить`), заполнение заголовка (`div.text-3xl`), Quill-редактора (`div.ql-editor`), выбор тематики (`Добавить тематику`) и клик `Опубликовать`
  - [x] Исправление живой проверки сессий `probe_social_browser_session`: учет редиректов авторизации (`/sign-in`, `/auth/`, `oauth.`, `passport.`) и устранение маскировки ошибок сессии
  - [x] Публичные канонические ссылки Дзена (`https://dzen.ru/a/{pub_id}`) вместо `/edit` ссылок редактора и расширение селекторов модального окна публикации
  - [x] Verified: ruff clean (0 errors), 126 тестов pass (100%), включая `test_social_browser_publisher.py`, `test_deduplicator.py`, `test_publisher_worker.py`
- [x] Module 11.23: Комплексный экран «Обзор» — суточная статистика по дням, детальная разбивка публикаций и сбоев по соцсетям, финансовый радар ИИ (токены, модели, провайдеры) (v2.50.0)
- [x] Module 11.25: Ликвидация мусорных обложек (отсев сплэш-заставок ТАСС/112/РИА, шеринговых плашек и абстрактных заглушек с fallback на ИИ-генерацию) и абсолютная защита от параллельных дублей (Redis Distributed Event Lock по ключевым сущностям + очистка) (v2.60.0)
  - [x] Отсев мусорных картинок: детектор сплэш-плашек/логотипов каналов (ТАСС, 112, РИА), шеринговых OG-карточек соцсетей со сплошным текстом и геометрических заглушек
  - [x] Строгая валидация медиа в content_worker.py: отказ от слепого пропуска при allow_watermarked, проверка картинок из веб-ресёрча, принудительный fallback на генерацию обложки через ИИ
  - [x] Распределенный Redis Event Lock на сущностях в content_worker.py: предотвращение race condition при параллельной обработке конкурирующих новостей
  - [x] Очистка базы и сайта от дублей за сегодня (`cleanup_duplicate_articles.py --days 1 --apply` — удалено 111 дублей из БД и WordPress)
  - [x] Полная пересборка Docker-контейнеров на VPS (`docker compose up -d --build`) с проверкой в рантайме
- [/] Module 11.27: Починка публикации в YouTube Community (селектор dropzone загрузки обложки, отлов таймаута превью, дедупликация в ленте) и ликвидация голодания очереди паблишера (квота ретраев 3-5, обработка суточного лимита ВК 214) (v2.60.2)
  - [/] Исправление селектора загрузки фото YouTube в `social_browser_publisher.py` (устранение ложного срабатывания глобального поиска `input[type='file'][accept*='image']`, изоляция в dropzone backstage)
  - [/] Защита от дублей в YouTube Community (проверка заголовка среди верхних постов вкладки Записи) + корректный fallback при таймауте превью
  - [/] Ограничение ретраев `WP_PUBLISHED` до 3-5 на тик в `publisher_worker.py` (защита от голодания новых статей `MEDIA_READY`)
  - [/] Корректная обработка ошибки VK 214 (500 постов в сутки) — исключение бесконечных повторов и застревания статей
  - [ ] Верификация тестами (ruff + pytest), деплой на VPS (`svodkapress_worker_publisher` и `svodkapress_app`), разблокировка очереди и боевой тест публикации поста с фото в YouTube

