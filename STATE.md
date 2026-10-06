# STATE — где мы остановились

Живой журнал. Обновляется **в конце каждой сессии** и при смене фазы.
Читать сразу после `PROJECT.md`. При конфликте с `TODO.md` и `CHANGELOG.md`
верить этому файлу.

---

## Текущая фаза

## Текущая фаза

**SvodkaPress 2.61.0: Редизайн /settings по стандарту `record-drawer-sheet` (macOS HIG Service Grid + Slide-Over Drawer), активные ссылки источников в статьях, ротация промо-блоков и сквозное SEO-обучение.**
На боевом сервере `144.31.141.17` (`svodkanews.team-big.ru`) и `150.241.98.133` (`svodka.press`) работают:
- **Редизайн страницы настроек `/settings` (macOS HIG Service Control Grid + Slide-Over Drawer)**: разделение всех объединенных сервисов на 18 независимых карточек со статусами и локальными тестами (`#card-test-<id>`), выезжающая шторка настройки с поддержкой URL `?service=<id>`, истории браузера и закрытия по `Esc`;
- **Активные кликабельные ссылки на источники текста и фото**: авто-резолвер СМИ/Telegram-каналов в теге `the_content` и подписи к фото обложки `single.php` со строгой ротацией 1 рекламного блока на статью;
- **Высококонтрастная палитра всех страниц админки (Harmonization Layer v2 в `base.html`)**: темные контрастные цвета для всех шрифтов и акцентов, устранение невидимого белого текста на карточках и полях ввода, темный фон `#0d1117` для терминала логов;
- **Сквозное обучение ИИ (`seo_learning.py` ↔ `content_worker.py` / `lab_pipeline.py`)**: правила успешных оптимизаций (CTR > +0.5%) и цели рекламодателей из `/ads` автоматически передаются в промты при генерации всех новых статей;
- **Анти-мусорный фильтр обложек и 4-уровневая дедупликация**: 7-зонный OCR, фильтр сплэш-плашек, кросс-статейный фильтр по 500 статьям и распределенный Redis Event Lock.

## Сессия 2026-10-05 — Кликабельные ссылки на источники фото и новостей (2.71.3)

### Сделано
1. **Подписи к фото с активными ссылками (`lab_pipeline.py`, `content_worker.py`, `wp_post_formatter.py`, `functions.php`)**:
   - Главная обложка (`hero`) и все инлайн-фото теперь снабжаются `<figcaption>Фото: <a href="<url>" target="_blank" rel="nofollow noopener noreferrer">Название источника</a></figcaption>`.
   - Если пост из Telegram (включая MTProto посты с числовым channel ID) — ссылка восстанавливается напрямую на пост (например, `https://t.me/breakingmash/78326`).
2. **Очистка блока «Источники информации:» от галлюцинаций ИИ**:
   - Любые нераспознанные абстрактные фразы («данные госпиталей...», «сообщения туристов...») полностью удаляются.
   - В блоке остаются строго кликабельные ссылки `<a href="...">` на подтвержденные каналы и веб-источники.
   - Если источников нет, блок полностью очищается, не оставляя мусорного текста.
3. **Обновление существующих статей и живая проверка**:
   - В теме WordPress `svodka-setka/functions.php` динамический фильтр `the_content` вычищает галлюцинации и при рендере существующих статей.
   - Пост 39045 (вирус Коксаки) в базе и в WP обновлен: под фото активная ссылка на Mash, в источниках активная ссылка на Mash, текст про госпитали удален.
   - Пост 39052 (Китай-город) проверен живьем на `svodka.press`: обложка и блок источников содержат прямую ссылку на Baza.

## Сессия 2026-10-04 — Редизайн /settings: Service Grid и Slide-Over Drawer (2.61.0)

### Сделано

000000. **Редизайн страницы настроек `/settings` по стандарту `record-drawer-sheet` (2.61.0):**
   - **Service Control Grid (`partials/settings_service_grid.html`)**:
     - 18 независимых карточек сервисов (без объединения): Яндекс Метрика, Яндекс Вебмастер, Яндекс Wordstat, Google Analytics 4, Google Search Console, Bing Webmaster, IndexNow, WordPress & SSH, Telegram, VK Video, Яндекс Дзен, и 6 браузерных соцсетей (Сетка, TenChat, Threads, Facebook, Pinterest, YouTube).
     - Каждая карточка оснащена: бейджем честного статуса, 2-строчной сводкой параметров, кнопкой быстрого тестирования `⚡ Проверить` и кнопкой `⚙️ Настроить`.
   - **Slide-Over Drawer Sheet (`settings.html`)**:
     - Шторка выезжает справа при клике на любую карточку или кнопку «Настроить», синхронизируя URL (`?service=<id>`).
     - Поддержка истории браузера (`pushState`/`popstate`), закрытие по клику на затемненный backdrop и по клавише `Escape` (LIFO).
     - Изолированные панели: отображается строго одна запрошенная панель конфигурации.
   - **Локализованная диагностика (Zero distant notifications)**:
     - Результаты тестов появляются непосредственно внутри карточки сетки или прямо под кнопками в шторке.
     - Добавлена кнопка «Закрыть ✕» для мгновенного схлопывания блока диагностики.
   - **Бэкенд и тесты (`app/api/dashboard.py`, `tests/test_dashboard_ui.py`)**:
     - Добавлен эндпоинт `GET /api/ui/settings/grid` для авто-обновления сетки на `htmx:afterSwap`.
     - Написаны тесты `test_the_settings_service_grid_and_drawer_panes` (100% passed).
   - **Деплой и верификация на боевом VPS `144.31.141.17`**:
     - Файлы синхронизированы в контейнеры `svodkapress_app`, `svodkapress_worker_content`, `svodkapress_worker_publisher`, `svodkapress_worker_lab` с перезапуском приложения.
     - Проверена работа через Playwright snapshots (десктоп, мобильный, открытие шторки, тестирование на сетке и в шторке).

## Сессия 2026-10-04 — Контрастность админки (WCAG AAA) и сквозное SEO-обучение при генерации статей (2.60.1)

### Сделано

00000. **Полная гармонизация контрастности шрифтов и сквозное SEO-обучение (2.60.1):**
   - **Гармонизационный CSS-слой v2 (`app/templates/dashboard/base.html`)**:
     - Устранен скрытый дефект светлой темы: после инверсии палитры `text-white` рендерился белым на светлых поверхностях (`bg-slate-900/70` $\to$ `#f8fafc`). Внедрено глобальное правило перевода `text-white` в темный чернильный `#0f172a` на светлых карточках с точечным сохранением белого только на намеренно темных элементах (`.btn-primary`, `.btn-accent`, `.chip-solid`, активные пункты меню, цветные кнопки `bg-*-600`).
     - Поля ввода (`input`, `textarea`, `select`) защищены: темный текст на белом фоне.
     - Отреставрирован терминал логов (`/logs`): возвращен глубокий темный фон `#0d1117` с четкой цветовой палитрой GitHub Dark для уровней логов (`DEBUG`, `INFO`, `WARN`, `ERROR`, `CRIT`).
   - **Сквозное влияние Базы знаний ИИ на генерацию новых статей (`app/services/lab_pipeline.py`, `app/workers/content_worker.py`)**:
     - Ранее `get_learned_patterns` вызывался только при ручном 1-click дожиме сниппета на странице `/seo`.
     - Теперь `get_learned_patterns` встроен в `_prepare_studio_aligned_payload` (фоновый воркер) и `run_cluster_generate_step` (Лаборатория). Все выявленные формулировки-победители (длина Title/Description, ключевики в первой трети, конверсионные цели рекламодателей) автоматически передаются ИИ-автору при написании каждого нового материала.
   - **Верификация и деплой на VPS (`144.31.141.17`)**:
     - Проверено линтером `ruff check` (0 errors) и тестами (162 passed).
     - Код и шаблоны скопированы в контейнеры `svodkapress_app`, `svodkapress_worker_content` и `svodkapress_worker_lab` через `docker cp` с перезапуском и проверкой логов.

0000. **Железная защита обложек от сплэш-плашек/текстовых карточек, устранение параллельных дублей и пересборка контейнеров воркеров (2.60.0):**
   - **Вскрыта и устранена корневая причина на VPS (`144.31.141.17`)**: контейнеры воркеров (`svodkapress_worker_content`, `svodkapress_worker_ingestion`, `svodkapress_worker_publisher`) работали на старом запеченном образе от 27 сентября (`cbfde98c9e91`), так как в `docker-compose.prod.yml` нет bind-mount для `/app/app`. Выполнена полная пересборка `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` и зафиксирован жесткий протокол деплоя в `AGENTS.md`, `CLAUDE.md` и `docs/11_DEPLOYMENT_DEVOPS.md`.
   - **Отсев сплэш-плашек, OG-карточек и заглушек (`watermark_detector.py`, `image_cleaner.py`, `web_researcher.py`, `content_worker.py`)**:
     - Добавлена 7-я центральная зона OCR `center` (70%×60%, `can_crop = False`) и расширен словарь брендов (`112`, `тасс`, `tass`, `риа`, `ria`, `лента`, `известия`, `коммерсант`, `f-news`).
     - Реализованы `_detect_monochrome_splash_banner` (отсекает плашки ТАСС/112 с $\ge 55\%$ плоского фона) и `_detect_text_heavy_card` (отсекает текстовые сводки с $\ge 12$ словами).
     - В `_judge_source_image` и `_salvage_enrichment_image` подключена безусловная валидация с автоматическим уходом на ИИ-генерацию чистой обложки (`_generate_cover`).
   - **Блокировка параллельных дублей и зачистка (`content_worker.py`, `cleanup_duplicate_articles.py`)**:
     - Реализован атомарный замок `_check_and_acquire_event_lock` в Redis (`dedup:active_event_generations`) и снятие суффиксов `-ск/-енн/-анн` в `_extract_title_stems`.
     - Прогон `cleanup_duplicate_articles.py --days 1 --apply` на VPS вычистил **111 дублей за сегодня** из БД и WordPress (по сюжету в Жулебино осталась ровно 1 каноническая публикация `#2135`, все 10 дублей снесены).

## Сессия 2026-10-03 — Максимальный SEO-пакет для выхода в ТОП, Яндекс.Вебмастер, Google Inspection, GA4 и фиды (2.58.0)

### Сделано

000. **Максимальный SEO-пакет для выхода в ТОП (Яндекс.Вебмастер, Google Inspection, GA4, RSS/Турбо-фиды):**
   - `app/services/yandex_connection.py` & `app/services/seo_learning.py` & `app/api/dashboard.py`:
     - Интегрированы методы `get_yandex_recrawl_quota`, `recrawl_yandex_url`, `fetch_yandex_sitemaps`, `add_yandex_sitemap`, `fetch_yandex_diagnostics`.
     - Авто-триггер переобхода: при применении любого оптимизированного сниппета в WordPress статья мгновенно уходит в очередь переобхода Яндекса (`/recrawl/queue`) в дополнение к IndexNow.
     - Добавлены HTMX-эндпоинты `/api/ui/seo/yandex/recrawl` и `/api/ui/seo/yandex/sitemap/add`.
   - `app/services/google_connection.py` & `app/api/dashboard.py`:
     - Реализован метод `inspect_google_url` через официальный Google URL Inspection API (`searchconsole.googleapis.com/v1/urlInspection/index:inspect`).
     - Метод `fetch_ga4_report` с чтением метрик посетителей и сессий через GA4 Data API v1beta (`:runReport`).
     - Добавлен HTMX-эндпоинт `/api/ui/seo/google/inspect`.
   - `app/api/feeds.py` & `app/main.py`:
     - Реализованы высокопроизводительные XML-фиды: `/api/feeds/yandex-news.xml`, `/api/feeds/turbo.xml`, `/api/feeds/rss.xml`.
     - Покрыто тестами в `tests/test_feeds.py` (2/2 passed), полный набор SEO-тестов: 63/63 passed.
   - `app/templates/dashboard/seo.html`:
     - В KPI-радар добавлен блок GA4 (пользователи, сессии, просмотры).
     - Добавлены интерактивные вкладки: «🚀 Яндекс.Вебмастер (Очередь & Sitemaps)», «🔍 Google URL Inspector & GA4», «📡 Новостные Фиды & Турбо».

### Сделано

00. **Динамический `/llms.txt` для ИИ, контакты `@genadich_agency` и авто-синк рекламы `/ads` → WordPress (`svodka-setka` v2.52.0):**
   - `app/templates/wordpress/svodka-setka/functions.php` & `footer.php`:
     - Все кнопки и ссылки в трех модальных окнах нижней панели («Предложить новость», «Заказать рекламу», «Контакты») и в футере переведены на **`@genadich_agency`** (`https://t.me/genadich_agency`) и **`anton@genadich.com`**.
     - Добавлена страница настроек в админке WordPress: **Внешний вид → Сводка.Пресс: Контакты и Реклама** (`wp-admin/themes.php?page=svodka-setka-settings`), где контакты меняются без правки кода.
     - Поднят динамический эндпоинт `/llms.txt` и `/llm.txt` (`text/plain; charset=utf-8`), прописан `LLMs-Txt: https://svodka.press/llms.txt` в `robots.txt` и `<link rel="llms-txt">` в `wp_head`. В файл автоматически попадают описание издания, правила цитирования, блоки рекомендаций активных рекламодателей (`FreeAPI AI`, `OutVPN`), 20 рубрик и 15 свежих материалов.
   - `app/services/promoted_projects_service.py`:
     - Добавлен метод `PromotedProjectsService.sync_to_wordpress()`, который при любом создании/редактировании/переключении/удалении рекламодателя в `https://svodkanews.team-big.ru/ads` автоматически пушит список активных кампаний по SSH в WordPress (`wp option update svodka_promoted_projects`).
     - В каталог `/ads` добавлен `OutVPN` (`id="outvpn"`, `https://t.me/OutVPNBuy_bot?start=641470324`) наравне с `FreeAPI AI` (`id="freeapiai"`), оба синхронизированы на `svodka.press`.

-1. **Левая колонка рубрик в стиле Setka.ru (`svodka-setka` v2.53.0):**
    - `header.php` & `footer.php`: внедрена семантическая раскладка `.setka-layout-wrapper` с липкой навигационной колонкой `<aside class="setka-sidebar-left">` (заголовок «Рубрики», бейдж «24/7», ссылка «🔥 Все новости» и каталог рубрик).
    - `functions.php`: функция `svodka_setka_get_rubrics_catalog()` динамически отдает 20 категорий издания с эмодзи, короткими названиями и счетчиками из БД WordPress; версия темы поднята до `2.53.0`.
    - `style.css`: на экранах $\ge 960\text{px}$ зафиксирована колонка шириной 230px (`sticky top-72px`) с плавным ховером, скроллбаром `none` и активной подсветкой; на мобильных экранах колонка скрыта, управление переключается на горизонтальный свайп-ряд.
    - Деплой выполнен на боевой сервер `150.241.98.133`, объектный кэш WordPress сброшен (`wp cache flush`), проверено на десктопе (`1280x800`) и мобильном (`390x844`).

0. **Окно подключения Яндекс.Метрики и целей конверсий в `/ads` + Closed-Loop обучение `/seo` (2.56.0):**
   - `app/templates/dashboard/partials/ads_projects_list.html` & `app/templates/dashboard/ads.html`: на карточке каждого рекламодателя (`FreeAPI AI` и др.) добавлены живой блок целевых визитов и целей, модальное окно `#metrika-modal-{{ proj.id }}` («📊 Метрика и цели»), кнопки «🔄 Авто-загрузка целей из Метрики», «⚡ Проверить визиты» и «✏️ Изменить».
   - `app/services/promoted_projects_service.py` & `app/api/dashboard.py`: реализованы `discover_client_metrika_goals`, расширенный `fetch_client_metrika_stats(..., include_counter_totals=True)` (с мультиплатформенным фильтром UTM `svodkapress`/`web`/`dzen`/`tg`/`vk`), `collect_advertiser_campaigns_report` и HTMX-эндпоинты `/api/ui/ads/{id}/metrika/{save,discover,probe}`.
   - `app/templates/dashboard/seo.html` & `app/services/seo_learning.py`: на странице `/seo` добавлена панель «🎯 Целевые визиты и срабатывание целей рекламодателей (Closed-Loop Learning)» с CQI и ИИ-гипотезами обучения, а `get_learned_patterns` обогащён целями активных рекламодателей.
   - Развернуто на боевом VPS `144.31.141.17` (`svodkapress_app`).

1. **Разработка и доработка темы `svodka-setka` v2.51.0 (`app/templates/wordpress/svodka-setka/`):**
   - `assets/icons/`: геометрический знак `svodka-symbol-color.svg` (100/100 по `svg_audit.py`) и полный набор веб-иконок (`favicon.ico`, `svodka-favicon.svg`, `apple-touch-icon.png`, `icon-192.png`, `icon-512.png`). Установлен в шапку и в `site_icon` WordPress (`ID=36149`).
   - `footer.php` & `style.css`: закрепленная нижняя полоска с кнопками «Предложить новость», «Заказать рекламу», «Контакты» и выезжающим окном (Slide-Up Sheet).
   - `functions.php` & `index.php`: ротация промо-карточек (Telegram → FreeAPI AI → OutVPN) каждые 3 новости в ленте + фильтр `svodka_setka_unwrap_json_ld` для чистой микроразметки `NewsArticle`.
   - `app/workers/publisher_worker.py` & `app/services/promoted_projects_service.py`: round-robin чередование активных рекламодателей из `/ads` (`candidates[article.id % len(candidates)]`) и динамический рендер УТП/CTA.
2. **Деплой по SSH и живая проверка в Playwright (`390x844`):**
   - Тема и иконки развернуты на `150.241.98.133`, кэш WordPress сброшен, контейнеры на `144.31.141.17` обновлены.
   - Проверены светлая и темная темы, открытие шторки «Заказать рекламу», чередование карточек Telegram / FreeAPI AI / OutVPN в ленте и валидность SEO-тегов.

## Сессия 2026-10-01 — Нативная публикация ВК с фото, богатый лонгрид, промо-блоки сайта и Telegram Bot (2.48.0)

### Сделано

000. **Нативная публикация ВКонтакте с фото-обложкой, богатый лонгрид и CTA (2.48.0):**
    - В `app/services/social_vk.py`:
      - Реализована загрузка обложки `upload_wall_photo`: скачивание фото, первичное обращение к `photos.getWallUploadServer` (для токенов пользователей) и автоматический фоллбек на `photos.getMessagesUploadServer` + `photos.saveMessagesPhoto` (для community tokens `vk1.a.*`). Возвращает `photo{owner_id}_{id}`, прикрепляемый к `wall.post`.
      - Обновлен метод `_call`: снято строгое требование возврата `dict` в ответе API, теперь корректно принимаются списки и скалярные ответы ВК.
      - Функция `build_wall_message`: замена 1-предложного тизера на полноценный лонгрид (3–5 абзацев фактуры из `body_html` через `html_to_plain_text`, лимит 3800 символов), ссылки на сайт, призыв подписаться на Telegram-канал `https://t.me/svodka_press`, рекламный блок FreeAPI AI `https://freeapiai.ru/r/tdCxTE7k` и чистые хештеги.
      - **Live-проверка на стене сообщества 236626422**: опубликован пост 26 (`https://vk.com/wall-236626422_26`) с прикрепленным реальным фото, полным текстом статьи (1546 символов), ссылкой, CTA и рекламой.
    - В `app/workers/publisher_worker.py` и `app/models/promoted_project.py`:
      - В футер статей на сайте (`build_web_telegram_subscription_card`) добавлен адаптивный темный градиентный баннер приглашения в Telegram (`@svodka_press`).
      - Внедрена нативная промо-карточка спонсора FreeAPI AI (`build_promo_card(chosen_promo, platform="web")`) в футер статей WordPress.
      - Исправлен `PromotedProject.build_tracked_url` (поддержка `cluster_id_or_source` и `campaign_override`).
      - **Live-проверка на WordPress**: проверена статья 33979 (`https://svodka.press/chp-i-proisshestvij/kot-ustroil-moschnyy-vzryv-v-kvartire-v-odintsove-26c4a3c4/`), в футере присутствуют и карточка Telegram, и карточка спонсора FreeAPI AI.
    - В `app/services/telegram_connection.py`, `app/templates/dashboard/settings.html`, `app/templates/dashboard/partials/telegram_services_card.html`, `app/api/dashboard.py`:
      - Создана служба `TelegramChannelConfig` с Fernet-шифрованием в `system_settings` (`key="telegram_channel_config"`).
      - Реализована функция `probe_telegram_bot_connection` для живой проверки токена бота (`getMe`), доступности канала (`getChat`) и прав администратора на публикацию (`getChatMember: can_post_messages`).
      - В `/settings` внедрена интерактивная Bento-карточка с 3-шаговой инструкцией, полями ввода и кнопкой мгновенного теста связи.
      - В `publisher_worker.py` подключена автоподгрузка конфигурации бота из БД при публикациях.
    - Верификация:
      - `ruff check`: 0 ошибок.
      - `pytest tests/test_telegram_connection.py tests/test_social_adapters.py tests/test_publisher_worker.py tests/test_social_browser_publisher.py`: 138/138 passed.
      - Деплой на production VPS `144.31.141.17`: обновлены и перезапущены контейнеры `svodkapress_app`, `svodkapress_worker_publisher`.

## Сессия 2026-10-01 — Токен ВК без IP-привязки, 6 браузерных соцсетей, снятие лимитов и 100% выравнивание «Боевого режима» со Студией (2.46.0)

### Сделано

00. **Диагностика ВК, подключение 6 браузерных соцсетей, снятие лимита в 5 новостей и 100% выравнивание Боевого режима (2.46.0):**
   - В `app/api/dashboard.py` и `vk_services_card.html`:
     - Выяснено, что сохраненный на VPS токен `••••••••coGA` (от `client_id=6287487`) отклонялся API ВК с ошибкой `access_token was given to another ip address`, а `6121396` заблокирован ВК.
     - Добавлена живая проверка `_verify_vk_token_live` при сохранении/проверке в `/settings` и проверенная ссылка (HTTP 200 OK) на **Kate Mobile (`client_id=2685278`)**, а также инструкция по Ключу доступа сообщества и выпуску токена через куки на сервере.
   - В `app/models/enums.py`, `app/services/social_browser_publisher.py`, `app/workers/publisher_worker.py`, `social_browser_cards.html`:
     - Подключены 6 новых соцсетей через авторизованные браузерные сессии (`Cookie-Editor` JSON + Playwright Stealth): **Сетка (`setka.ru`)**, **TenChat (`tenchat.ru`)**, **Threads (`threads.com`)**, **Facebook (`facebook.com`)**, **Pinterest (`ru.pinterest.com`)**, **YouTube Сообщество (`youtube.com`)**.
   - В `app/workers/content_worker.py` и `app/workers/publisher_worker.py`:
     - Сняты старые ограничения на 5/10 материалов за тик (`MAX_CLUSTERS_PER_RUN = 100`, `MAX_ARTICLES_PER_RUN = 100`, таймаут `3600` сек), публикация идет сначала по самым свежим статьям (`Article.created_at.desc()`) и запускается автоматически сразу после генерации.
     - Веб-ресерч (`_ensure_cluster_web_research`) выполняется безусловно для каждого сюжета.
     - Автоматический «Боевой режим» дополнен предрасчетом Яндекс Вордстата (`_generate_wordstat_and_seo`), авто-подбором рекламодателя из `/ads` (`PromotedProjectsService.get_matching_projects` + `is_brand_safe_for_commercial_integration`) и генерацией `Schema.org JSON-LD`.
     - В `/articles` починена кнопка «Одобрить и опубликовать» и добавлена кнопка «🚀 Опубликовать готовые сейчас».

### Ранее в сессии (2.45.1)

0. **Восстановление автоматической генерации и публикации в «Боевом режиме» (`content_worker.py`, `publisher_worker.py`, `wp_media_service.py`):**
   - В `content_worker.py`:
     - QA-редактор (`_review_draft_safe`) привязан к цепочке `seo` из БД (`resolve_chain(session, "seo")`), устранен крэш `NoCredentialsError: OPENROUTER_API_KEY is not configured`.
     - В `run_content_pipeline` устранен `sqlalchemy.exc.MissingGreenlet` после `session.commit()` / `session.rollback()` за счет иммутабельного среза `(c.id, str(c.category), c)` и повторного `session.get(NewsCluster, cid)`.
     - Изменен порядок выборки `_load_pending_clusters` на `NewsCluster.created_at.desc()` (сначала свежие горячие сюжеты).
     - Подключены автоматический веб-ресерч (`_ensure_cluster_web_research`), спасение обложек из веб-источников (`_salvage_enrichment_image`) и HTML-декор (`_enrich_and_decorate_draft`: внутренние ссылки `_inject_internal_links`, инлайн-фото и блок подписки на Telegram `_ensure_sources_block`).
   - В `publisher_worker.py` и `wp_media_service.py`:
     - Вызов `get_wp_publisher()` (читавший только пустые переменные `.env`) заменен на `await get_active_wp_publisher(session)` (читающий зашифрованные креды `wp_connection` из PostgreSQL).
     - В `upload_article_image` и `_resolve_cover_url` добавлена автоподгрузка `load_wp_connection(db_session)`, благодаря чему обложки штатно загружаются в медиабиблиотеку WordPress.
     - После публикации статьи в WordPress сюжет автоматически получает `cluster.is_published = True`.
   - Live-проверка на VPS `144.31.141.17`: автоматически сгенерирована и опубликована статья #311 (`https://svodka.press/chp-i-proisshestvij/bespilotniki-atakovali-yuzhnyy-most-v-kieve-28d63043/`, `wp_post_id=33792`, Дзен `6abe310a89d9f23450473196`).

1. **Кликабельные ссылки в Дзене и каноническая атрибуция (`dzen_publisher.py`, `dzen_publisher_card.html`):**
   - Реализована функция `linkify_dzen_promo`, которая на лету оборачивает фразы «сайте», «сайте SvodkaPress», «телеграм канале», «Telegram-канале» в рабочие гиперссылки на `https://svodka.press/` и `https://t.me/svodka_press` с `target="_blank"` и `rel="noopener noreferrer"`.
   - В промо-карточке Дзена добавлен хинт о поддержке автоматической линковки и HTML-разметки.
   - В `canonical_attribution` добавлены прямые кликабельные ссылки на `https://svodka.press/` и Telegram-канал `https://t.me/svodka_press`.

2. **Блок подписки на Telegram в статьях WordPress (`lab_pipeline.py`):**
   - В `_ensure_sources_block` перед перечнем источников встроен заметный отзывчивый блок `.svodka-tg-subscription-box` с синей иконкой Telegram, призывом к подписке и кнопкой перехода на `@svodka_press`.

3. **Статус «Опубликовано» и фильтрация сюжетов в Лаборатории (`lab_pipeline.py`, `lab_groups_list.html`):**
   - В `get_news_groups_summary` интегрирован сбор факта публикации из таблицы `Article` с получением `wp_post_url` и `dzen_post_url`.
   - В карточке сюжета выводится зеленый бейдж `✅ Опубликовано` со ссылками `[🌐 На сайт ↗]` и `[📱 В Дзен ↗]`.
   - В шапке добавлены быстрые фильтры `🆕 Неопубликованные (X)` и `✅ Опубликованные (Y)`.

4. **Brand Safety: контекстное смягчение и ручной Override (`content_prompts.py`, `lab_pipeline.py`, `dashboard.py`, `lab_cluster_detail.html`):**
   - Слово «смерть» в контексте фильмов, сериалов и культуры (Мэттью Перри в сериале «Друзья») больше не блокирует коммерческие рекламные интеграции, если в материале нет криминала, катастроф и боевых действий.
   - Добавлен ручной чекбокс `[x] Снять блокировку Brand Safety (разрешить интеграцию рекламы)` в Студии сюжета (Шаг 3).

5. **Семантическая дедупликация по сущностям (NER) и локальный FastEmbed (`deduplicator.py`):**
   - Внедрено регулярное выражение `_SIGNIFICANT_ENTITY_RE` для фамилий, бортовых номеров авиалайнеров и авиакомпаний.
   - Реализован метод `_find_entity_candidate_cluster`, находящий сюжеты с совпадающими сущностями за 24 часа.
   - При недоступности OpenRouter Level 2 автоматически использует локальный сервис эмбеддингов `FastEmbed` (0 токенов), предотвращая создание дублирующих сюжетов (Портнягин, рейс Flydubai).

6. **Аналитика потенциала рубрик за 120 часов (`lab_pipeline.py`, `lab_rubric_block.html`):**
   - Реализована функция `calculate_rubric_potential`, оценивающая количество релевантных сюжетов в базе за последние 120 часов (5 суток) с семантическим расширением корней слов при предложении ИИ новой рубрики (например, «Кино и сериалы» $\to$ `трейлер`, `фильм`, `сезон`, `актер`, `премьер`).

7. **VK Video & Сообщество ВКонтакте через браузерную сессию в `/settings` (`vk_video_service.py`, `publisher_worker.py`, `dashboard.py`, `vk_services_card.html`):**
   - Расширена модель `VKVideoConfig` с Fernet-шифрованием как токена (`encrypted_access_token`), так и браузерной сессии (`encrypted_storage_state`) в `system_settings` (`vk_video_config`).
   - Реализована функция `probe_vk_browser_session`: проверяет авторизацию в Playwright Stealth (`https://vk.com/club236626422`) и автоматически получает живой Web-токен `vk1.a...` (`act=web_token&app_id=6287487`) без необходимости создавать Standalone-приложение VK.
   - В карточке `/settings` добавлен пошаговый гид по экспорту кук через `Cookie-Editor` (по аналогии с Дзеном), поле `storage_state_json` и кнопка «Проверить сессию и получить токен».
   - Загрузка видео (`upload_article_video`, `lab_cluster_post_upload_vk`) и публикация на стену сообщества (`publisher_worker.py`) автоматически извлекают/обновляют токен из сохраненной сессии.

8. **Верификация и Деплой:**
   - 115 модульных тестов пройдены успешно (`pytest`).
   - 0 ошибок линтинга (`ruff check app/ tests/`).
   - Код доставлен на боевой VPS `144.31.141.17`, все 5 контейнеров обновлены, перезапущены и находятся в статусе `healthy`.

### Сделано

1. **Исследование проекта octopusx-farm:**
   - Изучен репозиторий `octopusx-farm`: проект ориентирован исключительно на шлюз `octopusx.ai` (форк OneAPI с приветственным балансом $0.83), все сгенерированные ключи вынесены в `.gitignore`.
   - Выявлен критический изъян в `octopusx_alive.py`: проверка живых ключей через `GET /v1/models` выдает ложноположительный 200 OK даже для ключей с нулевым балансом или исчерпанной квотой.
   - Выделены и адаптированы ценные архитектурные паттерны: дифференциация кулдаунов по сигнатурам текста ошибок и кэширование отсутствующих моделей шлюза.

2. **Дифференцированные кулдауны ключей по сигнатурам ответов API:**
   - В `app/services/gemini_key_pool.py` функция `mark_key_cooldown` расширена параметром `health: str = "rate_limited"` / `"error"`.
   - В `app/services/ai_client.py` создана функция `_handle_provider_failure`:
     - Ошибки исчерпания квоты / баланса (`quota`, `arrearage`, `insufficient`, `额度`, `good standing`) переводят ключ в кулдаун на 24 часа (`seconds=86400`, `health="error"`).
     - Ошибки авторизации (401/403, `invalid api key`, `unauthorized`) отправляют ключ в кулдаун на 24 часа (`seconds=86400`, `health="error"`).
     - Превышение частоты (429 Rate Limit) отправляет ключ в мягкий кулдаун на 15 секунд.
     - Ошибки серверов шлюза (5xx) отправляют ключ в микро-кулдаун на 5 секунд.

3. **Кэш неподдерживаемых моделей шлюза (`_DEAD_MODELS_CACHE`):**
   - Добавлен in-memory TTL-кэш (TTL 300 секунд) для пар `base_url:model`.
   - При кодах 400/404/503 с сигнатурами отсутствия модели (`not found`, `unknown model`, `no channel`, `model_not_found`, `does not exist`) модель маркируется как мертвая для конкретного шлюза.
   - Диспетчер `_dispatch_custom_keys` перед попыткой вызова ключа проверяет кэш: если модель не поддерживается шлюзом, запросы не отправляются, оставшиеся ключи пула не сжигаются вхолостую.

4. **Мульти-ключевая ротация Google Gemini:**
   - В `_call_google_provider` добавлен цикл авто-ротации до 3 ключей.
   - При возникновении 429 текущий ключ отправляется в кулдаун на 15 секунд, а запрос мгновенно передается следующему доступному ключу из 42 ключей пула.

5. **Тестирование и деплой на VPS `144.31.141.17`:**
   - Написаны модульные тесты `test_dead_models_cache_and_provider_failure` в `tests/test_ai_key_pools.py`.
   - Все 52 теста пулов ИИ успешно пройдены.
   - Проверка `ruff check` по всему проекту: 0 ошибок.
   - Файлы синхронизированы на VPS через rsync, скопированы в контейнеры `svodkapress_app`, `svodkapress_worker_lab`, `svodkapress_worker_content`, `svodkapress_worker_publisher`.
   - Контейнеры перезапущены, статус `/health` — `{"status":"ok"}`.
   - Прямой live-тест `auto_select_best_models` на проде подтвердил выбор: `writer`: `claude-sonnet-5`, `arbiter`: `gemini-3.1-flash-lite`, `seo`: `gemini-3.1-flash-lite`.

## Сессия 2026-09-30 — Мгновенный медиа-чек, генерация глубоких статей (2000–4000 знаков) на Claude Sonnet 5 и панель управления рекламой (/ads) (2.43.0)

### Сделано

1. **Мгновенный отклик проверки медиа в Drawer («Студия сюжета»):**
   - Устранена 25-секундная блокировка HTMX-ответа при клике «Проверить медиа»: генерация промта обложки (`generate_editorial_image_prompt`) теперь вызывается **только при реальной необходимости фоллбека** (`fallback_needed is True`).
   - Если в сюжете уже есть качественные фото первоисточников или веб-ресерча, генерация промта обложки пропускается, а ответ возвращается мгновенно (<1 секунды).
   - Установлен жесткий таймаут 3.5 секунды (`asyncio.wait_for`) на случай вызова фоллбек-промта.
   - В эндпоинт `lab_cluster_media_partial` переданы недостающие ключи контекста (`rejected_web_images`, `fallback_prompt`, `generation_error`) для чистого HTMX swap фрагмента.

2. **Исправление роутинга AIClient и генерация глубоких статей (2000–4000 знаков):**
   - Проанализирована причина обрезки статьи 303 на 400 знаках («Портнягин в…»): в цепочке `writer` стояли несуществующие/заблокированные модели, из-за чего срабатывал аварийный эвристический фоллбек, а повторный клик падал с 500 из-за `UnboundLocalError: 'pool_res'`.
   - В `app/services/ai_client.py` метод `_dispatch_provider_call` обновлен: добавлен join с `AIProviderModel`, учет `is_working.is_(True)`, сортировка по здоровью ключей и ротация на следующий ключ при 404/429/500.
   - Выявлено, что модель `deepseek-v4-flash` на `vsllm.cc` тратила весь бюджет токенов (4096) исключительно на рассуждение (`reasoning_tokens: 4096`), возвращая пустой `content: ""`.
   - В качестве флагманской рабочей модели `writer` активирована `claude-sonnet-5` (с фоллбеками на `claude-sonnet-4-6`, `grok-4.5`, `gemma-4-26b-a4b-it`).
   - В `run_cluster_generate_step` модерация переведена на `moderate_with_fallback` с использованием рабочей цепочки `arbiter` (`gemini-3.1-flash-lite`, `grok-4.5`, `claude-sonnet-5`).
   - Статья 303 для сюжета `613fd5a2-245a-4c13-9b14-e9f1eb320c64` успешно перегенерирована на `claude-sonnet-5`: объем статьи вырос с 400 до **2960 знаков** качественного, структурированного журналистского текста с подзаголовками H2, цитатами, точными фактами дела, штрафами и ссылками на первоисточники (rg.ru, 7info.ru, Telegram).

3. **Создание раздела управления рекламой и рекламных вставок (`/ads`):**
   - Разработана страница `/ads` в стиле Apple HIG:
     - Сводная панель метрик (активные кампании, процент интеграций, прогноз CTR 3.8–5.2%, статус сквозного учета в Метрике);
     - Список активных проектов рекламодателей (`PromotedProject`) с карточками брендов, переключателем активности (`toggle`), автогенератором UTM-меток, списком целевых категорий и кнопкой удаления;
     - Демонстрационный блок анатомии рекламных вставок: живой интерактивный макет нативной рекомендации (карточка бренда) и адаптивного виджета РСЯ (Яндекс Директ);
     - Модальное окно создания и редактирования проектов рекламодателей с валидацией полей, привязкой счетчика Метрики и генерацией сквозных UTM-параметров.
   - Добавлен пункт меню «Реклама» с иконкой рупора в навигационную шапку панели (`base.html`).
   - Реализованы эндпоинты `GET /ads`, `POST /api/ui/ads/create`, `POST /api/ui/ads/{id}/toggle`, `DELETE /api/ui/ads/{id}` в `app/api/dashboard.py`.

4. **Тестирование и деплой:**
   - Линтер `ruff check`: 0 ошибок.
   - Проверено создание и переключение рекламодателей через TestClient (200 OK).
   - Все изменения синхронизированы на VPS `144.31.141.17`, контейнеры `svodkapress_app` и `svodkapress_worker_lab` перезапущены и находятся в статусе `healthy`.
   - Проверено прямое открытие и отображение сюжета `613fd5a2-245a-4c13-9b14-e9f1eb320c64` и страницы `/ads`.

### Сделано

1. **Анализ эталонной архитектуры GETVOXLY:**
   - Исследована реализация карточки сделки (`useRecordParam`, `useOverlay`, `Sheet`).
   - Выделены 4 ключевых принципа: URL-driven state, история переходов (pushState на открытие, replaceState на закрытие), изолированный скролл-лок и мгновенный скелетон.

2. **Создание универсального навыка Antigravity (Skill):**
   - В `~/.gemini/config/skills/record-drawer-sheet/SKILL.md` сохранен подробный гайд и готовые сниппеты для React/Next.js и HTMX/Vanilla JS/SSR.

3. **Внедрение Slide-Over Drawer в «Студию сюжета» (`svodkapress`):**
   - Создан компонент `app/templates/dashboard/partials/lab_cluster_drawer.html`.
   - Основной список новостей в `lab_main_container.html` развернут на всю ширину страницы.
   - Кнопка «Студия сюжета →» и клик по заголовку карточки в `lab_groups_list.html` вызывают `ClusterDrawer.open(clusterId, title)`.
   - В `lab.html` интегрирован контроллер `ClusterDrawer`: мгновенный скелетон, `pushState` в URL, блокировка скролла, плавное появление, закрытие по крестику, Escape, клику по бэкдропу и системной кнопке «Назад» в браузере (`popstate`).
   - В `dashboard_lab` (`app/api/dashboard.py`) добавлена поддержка параметров `cluster` и `record`: прямые ссылки `/lab?cluster=<uuid>` автоматически открывают шторку с нужным сюжетом при загрузке.

4. **Исправление ошибки UndefinedError в карточке нативной рекламы:**
   - Устранена ошибка 500 (`jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'target_audience_segment'`), возникавшая при открытии сюжета с активной рекламной интеграцией FreeAPI AI.
   - В модель `PromotedProject` добавлены свойства `product_name` и `target_audience_segment`, включенные в `to_dict()`.
   - В шаблоне `lab_cluster_detail.html` реализован защитный рендеринг свойств рекламодателя с фоллбеками на `usp` и `name`.

5. **Тестирование и верификация:**
   - Создан тест `test_dashboard_lab_drawer_query_param` в `tests/test_lab_stepwise.py` (10 тестов pass, 100%).
   - Прогон `tests/test_dzen_publisher.py` и `test_social_adapters.py`: 101 тест pass (100%).
   - Линтер `ruff check`: 0 ошибок.
   - Прямая верификация URL: `https://svodkanews.team-big.ru/lab?cluster=0efce174-f4ba-489c-9e83-a077a8d10d40` — шторка с сюжетом 302 («1,6 млрд заработали авторы на «ВКонтакте»») и нативной врезкой FreeAPI AI успешно открывается и рендерится без ошибок (232 КБ HTML).
   - Деплой на VPS `144.31.141.17`, контейнер `svodkapress_app` перезапущен со статусом `healthy`.

## Сессия 2026-09-30 — Точная вставка фото-обложки в Яндекс Дзен, условия канала и лента (2.41.0)

### Сделано

1. **Точная вставка лид-обложки в Draft.js Дзена (`dzen_publisher.py`):**
   - Устранена рассинхронизация DOM Selection с внутренним React SelectionState Draft.js: прямой клик Playwright `first_block.click(position={"x": 5, "y": 5})` после прокрутки `window.scrollTo(0, 0)` переводит фокус в начало редактора.
   - Инициирование новой пустой верхней строки через `Home` -> `Enter` -> `ArrowUp`.
   - Вставка обложки `<figure><img src="{cover_url}" alt="{title}" /></figure>` через `ClipboardEvent('paste')` с `DataTransfer`.
   - Таймер ожидания 6 секунд для обработки CDN Дзена (`avatars.dzeninfra.ru/get-zen_doc/...`).

2. **Автоматизация диалогов публикации (`_trigger_publish`):**
   - Распознавание модального окна условий канала: поиск чекбокса `input.article-editor-desktop--checkbox-v2__input-1y`, установка через `check(force=True)` и отправка формы `button[type='submit']:has-text('Продолжить')`.
   - Целевой клик по модальной кнопке `button[type='submit']:has-text('Опубликовать')` и ожидание редиректа на постоянный публичный адрес статьи `https://dzen.ru/a/...`.

3. **Fallback для обложки без ключей WordPress:**
   - Чтение `article.platform_statuses['wp']['cover_url']`, если реквизиты WP REST не прописаны в `.env`.

4. **Тестирование и Closed-Loop верификация:**
   - 101 тест `test_dzen_publisher.py` и `test_social_adapters.py` — pass (100%).
   - Линтер `ruff check` — 0 ошибок.
   - Боевая статья опубликована: `https://dzen.ru/a/ar0HxXFWvGriUB0E` (Post ID `6abd07c57156bc6ae2501d04`).
   - Проверена лента канала `https://dzen.ru/svodka_press`: карточка отображается с полноцветной обложкой (размер `smart_crop_516x290`, класс `content--zen-image-cover__image-2x`).
   - Синхронизированы файлы на VPS `144.31.141.17`, все контейнеры healthy.

## Сессия 2026-09-29 — Нативное форматирование Яндекс Дзена, богатые блоки рекламы и карточка сайта (2.40.0)

### Сделано

1. **Решение проблемы форматирования и неактивных ссылок в Яндекс Дзене (`dzen_publisher.py`):**
   - Устранен вызов `document.execCommand('insertHTML')`, который в React Draft.js стирал всю структуру.
   - Внедрена вставка через `ClipboardEvent('paste')` с `DataTransfer` (`text/html` + `text/plain`).
   - Draft.js теперь нативно рендерит заголовки `<h2>`/`<h3>`, жирный/курсив, списки, активные ссылки и загружает изображения в виджеты Дзена.
   - Добавлена очистка оверлеев `_DISMISS_POPUPS_JS` (`.ReactModalPortal`, `[class*="help-popup"]`).
   - Обеспечено надежное ожидание сохранения («Сохранено»).

2. **Интеграция рекламодателя FreeAPI AI в Дзене (`lab_pipeline.py`, `dzen_publisher.py`):**
   - Промо-блок оформлен через тег `<blockquote>` с кликабельными ссылками и UTM-метками Дзена (`utm_source=dzen&utm_medium=article_promo`).
   - Автоматическое извлечение активного клиента из `PromotedProjectsService`.

3. **Конверсионная карточка сайта на WordPress (`promoted_projects_service.py`):**
   - Метод `PromotedProjectsService.build_promo_card`: премиальная темная карточка с бейджем, заголовком, текстом выгод и кнопкой действия.
   - Живая статья 302 на WordPress (ID 33787) обновлена с новой карточкой.

4. **Тестирование и верификация:**
   - 101 тест `test_dzen_publisher.py` и `test_social_adapters.py` — pass (100%).
   - 37 регрессионных тестов `test_v23*.py` — pass (100%).
   - Линтер `ruff check app/ tests/` — 0 ошибок.
   - Деплой на VPS `144.31.141.17`, контейнеры healthy. Живая проверка WordPress и Дзен Студии подтвердила наличие всех блоков, картинок и ссылок.


### Сделано

1. **Распознавание верхних брендированных плашек новостных каналов (`watermark_detector.py`, `lab_pipeline.py`):**
   - Добавлена 6-я зона `top_center` (`TOP_STRIP_HEIGHT_RATIO = 0.22`, `TOP_STRIP_WIDTH_RATIO = 0.75`), где размещаются плашки каналов с заголовками.
   - Внедрен паттерн `_CHANNEL_NAMES_RE` (`топор`, `baza`, `shot`, `mash`, `readovka`, `силовики`, `горячие новости`).
   - В `run_cluster_media_step` подключена инспекция байтов Telegram-фото из `storage/media/telegram/{p.id}.jpg`. Скриншоты с плашками теперь помечаются `badge_type: "warn"`, а не «чистое фото».

2. **Защита от зависаний интерфейса при истечении сессии (`auth.py`, `base.html`):**
   - В `_UNAUTHORIZED_HEADERS` добавлен `"HX-Redirect": "/login"`.
   - В глобальный шаблон `base.html` внедрены слушатели HTMX-событий `htmx:responseError` (авто-редирект на `/login?next=...`) и `htmx:sendError` (уведомление о сетевой ошибке).

3. **Исправление сигнатуры ИИ-верификатора (`image_cleaner.py`):**
   - Параметр `system_instruction` заменен на `system` при вызове `AIClient.complete`. Устранен `TypeError`.

4. **Реестр рекламодателей и нативная интеграция FreeAPI AI:**
   - В `PromotedProjectsService` зарегистрирован проект `FreeAPI AI` (`freeapiai`, целевой URL `https://freeapiai.ru/r/tdCxTE7k`, цели конверсий в Метрике `113062256`).
   - Написана органичная интеграция в статью: использование нейросетей для генерации контента и масштабирования заработка авторов видео.

5. **Публикация статьи 302:**
   - Новый кликабельный заголовок: `«1,6 млрд заработали авторы на «ВКонтакте»: доходы на шопсах и новая экономика контента»`.
   - Переработан структурированный лид с цифрами.
   - Опубликовано на сайт WordPress: `https://svodka.press/novosti-it-i-tehnologij/1-6-mlrd-zarabotali-avtory-na-vkontakte-dohody-na-shopsah-i-novaya-ekonomika-kontenta/` (Post ID `33787`).
   - Опубликовано в Яндекс Дзен: `https://dzen.ru/profile/editor/id/6862c9ab6b86a517037aaaae/6abbd287ffc0bf29f2be13b0/edit` (Post ID `6abbd287ffc0bf29f2be13b0`).

6. **Тестирование и верификация:**
   - Регрессионный прогон: 37 тестов pass (100%).
   - Линтер: `ruff check app/ tests/` — 0 ошибок.


### Сделано

1. **Верификация медиа через ИИ и очистка от оффтопика (`image_cleaner.py`, `web_researcher.py`, `research.py`):**
   - Реализована функция `verify_images_with_ai`: оценка кандидатов через цепочку `arbiter` / Gemini по заголовку и фактам новости.
   - Критерии одобрения: место события, спецтехника, МЧС/полиция, разрушения, официальные спикеры. Отсев дикторов в студии, спортивных матчей, случайного автотрафика, кулинарии и баннеров.
   - Fast-path эвристика `_OFFTOPIC_URL_HINTS` для мгновенного отсева явных оффтопиков.
   - Селективный сбор картинок в `web_researcher.py:fetch_clean_article`: заменен жадный XPath, оставлены `og:image`, `twitter:image` и `<article>//figure//img` (макс 3 на материал).
   - В Студии медиа (`lab_media_result.html`) добавлены бейджи `🟢 ИИ: Одобрено` и секция отсеянных оффтопиков `🚫 Отклонено ИИ как оффтопик ([N])`.

2. **Смысловая внутренняя перелинковка и токенизация (`lab_pipeline.py`):**
   - Строгая изоляция `voennye-novosti` (военные новости ссылаются только на военные; происшествия и мирные темы никогда не ссылаются на военные).
   - Список `GEOGRAPHIC_AND_GENERIC_STOP_STEMS` (область, регион, район, город, россия, страна и др.) исключен из расчета тематического пересечения.
   - `_inject_link_into_paragraphs`: токенизированный парсинг абзаца с сохранением кавычек `«...»` снаружи анкора и предотвращением обрыва текста вроде `«облас`.

3. **Постоянный двухканальный блок публикации Шага 4 (`lab_cluster_detail.html`, `lab_publish_result.html`, `lab_generate_result.html`, `dashboard.py`):**
   - Шаг 4 разделен на две параллельные независимые карточки:
     - `#step-publish-wp-container` (Сайт издания WordPress)
     - `#step-publish-dzen-container` (Яндекс Дзен Студия)
   - Публикация на сайт не перекрывает Дзен, кнопки работают параллельно, состояние каждой карточки обновляется независимо.

4. **Тестирование и деплой:**
   - 8 тестов в `tests/test_v238_ai_media_and_interlinking.py` (100% pass).
   - Прогон `tests/test_v23*.py`: 36 тестов pass (100%).
   - Линтер `ruff check app/ tests/` — 0 ошибок.
   - Деплой на VPS `144.31.141.17`, обновлены и перезапущены `svodkapress_app`, `svodkapress_worker_lab`, `svodkapress_worker_content`, `svodkapress_worker_publisher`. Все сервисы healthy, `/health` 200 OK.


### Сделано

1. **Фильтрация Telegram-агрегаторов и рекламных заставок (`image_cleaner.py`, `web_researcher.py`):**
   - Добавлен список `_AGGREGATOR_DOMAINS` (`tvgram.ru`, `tgstat.ru`, `telemetr.io`, `combot.org`, `tg.guru`, `tgdb.ru`, `push.ru`, `teleg.run`, `t.me`).
   - Исключение агрегаторов из веб-поиска (`_DISALLOWED_DOMAINS`) и предварительная фильтрация URL в `_filter_url_candidates`.
   - В `is_blurry_or_solid()` внедрен контроль средней яркости (`MIN_MEAN_BRIGHTNESS = 28.0`) и долей монохромных пикселей (`dark_ratio > 0.65`, `white_ratio > 0.65`) — 100% отсечение темных заставок с текстом каналов и белых слайдов с логотипом.
2. **Интерактивный просмотр (Lightbox) и потоковое видео (`lab_media_result.html`, `dashboard.py`):**
   - Превью фотографий обернуты в ссылки на серверный прокси `/api/ui/lab/media/web-image?url=...` с атрибутом `target="_blank"` и `cursor-zoom-in`.
   - Реализован эндпоинт `GET /api/ui/lab/media/video/{post_id}`: стриминг кэшированного MP4 или on-demand скачивание через MTProto Telethon с заголовками `video/mp4`.
   - В карточке медиа встроен видеоплеер `<video controls preload="metadata">`.
3. **Контроль соответствия событий и строгая перелинковка (`web_researcher.py`, `lab_pipeline.py`):**
   - Внедрен `_CRITICAL_EVENT_STEMS` (пожары, ДТП, суды, взрывы, дроны) и Event Mismatch Guard: если инфоповод о происшествии, статьи без маркеров события бракуются (устранено подмешивание рецептов/сырников в сюжеты о пожарах).
   - В `_inject_internal_links` заблокирована вставка ссылок из военных и криминальных рубрик (`voennye-novosti`, `chp-i-proisshestvij`) в мирные статьи.
   - Межкатегорийная перелинковка теперь требует минимум 2 общих стемма.
4. **Автоматический ресерч перед генерацией статьи (`lab_pipeline.py`):**
   - Если кластер не обогащен фактами из сети, `run_cluster_generate_step` автоматически вызывает `conduct_cluster_research()` с тайм-аутом до генерации.
5. **Интерфейс публикации в Яндекс Дзен (`lab_generate_result.html`, `lab_cluster_detail.html`):**
   - Кнопка «📱 В Дзен» выведена в шапку блока результатов генерации, непосредственно в заголовок таба «📱 Яндекс Дзен» и сохранена в Шаге 4.
6. **Тестирование и верификация:**
   - 6 новых тестов в `tests/test_v237_fixes.py`, полный прогон 38 тестов (100% pass в 15.5s).
   - Линтер `ruff check app/ tests/` — 0 ошибок.
   - Актуализированы `docs/06_MEDIA_PROCESSING.md`, `TODO.md`, `CHANGELOG.md`, `STATE.md`.

## Сессия 2026-09-29 — Очистка и распределение веб-фото в статье (2.36.0)

### Сделано

1. **Новый модуль `app/services/image_cleaner.py`:**
   - `compute_dhash`: 64-битный difference hash через OpenCV (9×8 grayscale, горизонтальные градиенты).
   - `hamming_distance`: подсчёт разности битов через `.bit_count()`.
   - `is_blurry_or_solid`: Laplacian variance < 35 или std < 12.
   - `inspect_image_quality`: разрешение ≥ 350×200, соотношение сторон 0.45–2.7, blur/solid, watermark через `watermark_detector.analyse`.
   - `_filter_url_candidates`: URL-блэклист + нормализация, дедупликация.
   - `filter_and_deduplicate_web_images`: главный async-конвейер с `cache_dir: Path | None`.
   - `inject_article_images`: диспетчер трёх режимов (`inline`, `slideshow`, `cover_only`).
   - `_inject_inline_figures`, `_inject_slideshow`: HTML-рендереры с `html.escape()`.
2. **Интеграция в `app/services/web_researcher.py`:** вызов `filter_and_deduplicate_web_images` после дедупликации `unique_facts`; `WebResearchReport` получает `clean_images`.
3. **Интеграция в `app/services/lab_pipeline.py`:** параметр `image_layout` в `run_cluster_generate_step`; шаг инъекции между `_inject_internal_links` и `_ensure_sources_block`.
4. **`app/api/dashboard.py`:** `lab_cluster_generate_partial` принимает `image_layout: Annotated[str, Form()] = "inline"`.
5. **Шаблоны:**
   - `lab_cluster_detail.html`: Step 3 grid → 3 cols; новый `<select name="image_layout">`.
   - `lab_generate_result.html`: бейдж выбранного режима фото.
6. **Документация:** обновлены `docs/06_MEDIA_PROCESSING.md` (раздел 1.12), `TODO.md` (Module 11.14 [x]), `CHANGELOG.md` ([2.36.0]), `STATE.md`.
7. **Тесты (`tests/test_v236_image_cleaner_and_distribution.py`):** 7 тестов, 100% pass (12.6s).
8. **Верификация:** `ruff check .` — 0 ошибок; `pytest tests/test_v23*.py tests/test_web_research*.py` — 32 passed.
9. **Деплой на VPS `144.31.141.17`:** синхронизированы изменённые файлы, контейнеры `svodkapress_app`, `svodkapress_worker_lab`, `svodkapress_worker_content` перезапущены, `GET /health` → `{"status":"ok"}`.



### Сделано

1. **Загрузка видео в VK Video по требованию из Лаборатории (`telegram_mtproto.py`, `dashboard.py`, `lab_media_result.html`):**
   - Реализован метод `download_video()` в `MTProtoIngestor` для скачивания видеодокументов (`MessageMediaDocument`) через Telethon напрямую из Telegram-канала с потолком 500 МБ.
   - Карточка видео в Шаге 2 Студии оснащена HTMX-кнопкой «⬆️ Загрузить в VK Video» с лоадером и последующей мгновенной заменой на инлайн-плеер `https://vk.com/video_ext.php`.
   - Добавлен HTMX-эндпоинт `POST /api/ui/lab/cluster/{cluster_id}/video/{post_id}/upload-to-vk`: скачивание через прямой URL или MTProto, загрузка в VK API (`upload_article_video`), фиксация метаданных в `raw_news.media_data` и `article.video_links`.
2. **Автоматический контур веб-ресерча и retry-цикл (`app/workers/content_worker.py`):**
   - Добавлена периодическая задача `enrich_task` с расписанием `ENRICH_CRON_MINUTES` ({7, 17, 27, 37, 47, 57}) за 5 минут до `content_task`.
   - Автоматический сбор свежих кластеров (< 6 ч) без статьи и с пустым `web_sources`, запуск `conduct_cluster_research()` и сохранение в `cluster.enrichment_data`.
   - Реализован ответ на вопрос оператора: система в автоматическом режиме естественным образом повторяет ресерч каждые 10 минут, пока публикации СМИ не появятся в поисковиках.
3. **Верификация и синхронизация:**
   - 8 новых тестов в `tests/test_v235_vk_upload_and_enrich.py`, общий тестовый набор 38 тестов (100% pass в 8.05s).
   - Линтер `ruff check app/ tests/` — 0 ошибок.
   - Актуализирована документация: `docs/01_DATA_PIPELINE.md` (расписание `enrich_task`) и `docs/06_MEDIA_PROCESSING.md` (загрузка видео в VK Video).

## Сессия 2026-09-28 — Мульти-фото ресерч, тематическая перелинковка, транслитерация слагов и обложка Дзена (2.34.0)

### Сделано

1. **Мульти-извлечение репортажных изображений веб-ресерча (`app/services/web_researcher.py`):**
   - Расширен XPath-парсинг: сканирование `//figure//img`, `//article//img`, `//main//img`, `//picture//source`, классов репортажных фото.
   - Поддержка `srcset` и `data-src` / `data-lazy-src` с автоматическим выбором максимального разрешения.
   - Сбор до 6 качественных кадров в `article_res.images` и `all_images` с дедупликацией по URL.
2. **Тематическая перелинковка и русский стемминг (`app/services/lab_pipeline.py`):**
   - Внедрена группировка 20 рубрик по 4 кластерам `CATEGORY_CLUSTERS` (`politics_military`, `economy_business`, `tech_science`, `lifestyle_culture`).
   - Реализован легковесный стеммер русского языка `_stem_ru` для поиска релевантных связей независимо от падежей.
   - Полное устранение оффтопика: рекомендация выдается только при наличии общих стемм или совпадении кластера тематик; случайный fallback на последнюю статью из БД исключен.
3. **Безопасные транслитерированные слаги (`app/services/lab_pipeline.py`):**
   - Перевод генерации слагов на `slugify(seo_title_val or title_val, max_chars=180)`: никаких русских букв и замыкающих точек в ссылках.
   - Автоматическая транслитерация и нормализация старых слагов при обращении к статьям.
4. **Обогащение промптов рубрик и синхронизация в Студии (`lab_pipeline.py`, `templates/`):**
   - Замена 4-словных заглушек («Информационный стиль, сдержанный тон.») на полный 80-словный журналистский стандарт с фокусом рубрики.
   - Исправлена привязка заголовка рубрики (`cluster.category`) в бейджах и подсказках Студии.
   - Динамический селектор стилей в интерфейсе на лету обновляет текстовое поле на богатый структурированный промпт.
5. **Обложка и точная ссылка в публикации Дзена (`app/services/dzen_publisher.py`, `lab_pipeline.py`):**
   - Внедрение тега `<p><img src="{cover_url}" alt="{article.title}" /></p>` в начало тела статьи: Draft.js рендерит блок, а Дзен автоматически назначает его главной обложкой карточки ленты.
   - Иерархия подбора обложки: WP-обложка -> выбор из ресерча -> первое фото поиска -> чистое фото первоисточника.
   - Формирование канонической ссылки строго по реальному ответу WordPress REST API (`wp_stat["url"]`) или каноническому URL `https://svodka.press/{category}/{slug}/`.
6. **Адаптивная верстка Студии Лаборатории (`templates/`):**
   - Устранено наложение селектора стиля и поля доп. указаний в Шаге 3 (`xl:grid-cols-2 min-w-0 box-border`).
   - Оптимизирована сетка карточек веб-изображений Шага 2 (`sm:grid-cols-2 xl:grid-cols-3`).
   - Исправлены вылеты названий в блоке перелинковки (`min-w-0 flex-1 truncate`).
   - Оптимизированы плашки AEVI 4-векторов с защитой от разрыва строк («Крипта ЦА:»).
7. **Тестирование и деплой на VPS `144.31.141.17`:**
   - 29 тестов модуля и дашборда (`test_v234_interlinking_and_media.py`, `test_dzen_publisher.py`, `test_lab_features.py`, `test_lab_stepwise.py`) — 100% pass.
   - Линтер `ruff check app/ tests/` — 0 ошибок.
   - Код синхронизирован на VPS `144.31.141.17`, скопирован в контейнеры `svodkapress_app`, `svodkapress_worker_lab`, `svodkapress_worker_publisher`, `svodkapress_worker_content`, сервисы перезапущены и здоровы (`{"status":"ok"}`).

## Сессия 2026-09-28 — ДНК Победителей, Brand Journalism и Closed-Loop CQI (2.31.0)

### Сделано

1. **Редакционный стандарт «ДНК Победителей» (`app/services/content_prompts.py`):**
   - Во все 20 системных промптов категорий издания внедрен стандарт «ДНК Победителей» на базе анализа 51 000+ визитов:
     - Заголовок: `Субъект + Действие/Поворот + Выгода/Интрига + 1-2 эмодзи` (🍔❤️, 🤖⚡, 🚇👑, 🍫❤️, 📈🔥), до 75 знаков, без кавычек вокруг всего заголовка.
     - Лид: 2–3 емких предложения (150–250 знаков) с ответами на Кто, Что, Где, Почему важно сейчас.
     - Zero Hallucination: строжайший запрет выдумывания дат, цифр, имен и цитат. Обогащение статьи строится только на исходниках и проверенном веб-ресерче (`conduct_cluster_research`).
     - Структура: 1800–2500 знаков HTML, минимум два подзаголовка `<h2>`, минимум один маркированный список `<ul><li>`.
2. **Модуль рекламируемых проектов (`app/models/promoted_project.py`, `app/services/promoted_projects_service.py`):**
   - Модель `PromotedProject` и `ConversionGoal` с полями бренда, УТП, проблемы ЦА, целевых категорий, сегментов, UTM (`build_tracked_url`) и параметрами Метрики клиента (`client_metrika_counter_id`, `client_metrika_token`).
   - Сервис `PromotedProjectsService`: сохранение в `SystemSetting('promoted_projects_registry')` с шифрованием Fernet токенов авторизации клиента.
   - Метод `fetch_client_metrika_stats`: асинхронный сбор статистики с сайта рекламодателя по UTM-метке статьи (визиты, отказы, среднее время, достижения целей конверсий).
   - Генератор нативного формата `build_native_advertorial_prompt`: написание статьи как увлекательной новости о продукте с органичным встраиванием ссылки.
3. **Фильтр безопасности бренда Brand Safety Filter (`app/services/content_prompts.py`):**
   - Функция `is_brand_safe_for_commercial_integration`: 100% блокировка рекламы в некоммерческих рубриках (`voennye-novosti`, `chp-i-proisshestvij`) и при наличии стоп-слов («погиб», «жертвы», «теракт», «дрон», «атака дронов», «бпла», «взрыв», «крушение», «ракета», «трагедия» и др.).
   - Принудительное переключение статьи в чисто редакционный режим при срабатывании фильтра.
4. **Сквозной индекс качества кампании CQI (`app/services/aevi_scoring.py`):**
   - Расчет `CQI (0–100)`: глубина чтения на медиа ($Q_{\text{read}}$) + фактор отказов ($Q_{\text{bounce}}$) + нативный CTR ссылки ($Q_{\text{ctr}}$) + конверсии на сайте заказчика ($Q_{\text{target}}$).
   - Автоматическое обнуление при нарушении Brand Safety.
5. **Интеграция в Студию Лаборатории и интерфейс (`lab_pipeline.py`, `dashboard.py`, шаблоны):**
   - `lab_cluster_detail.html`: селектор режима («📰 Редакционная новость» vs «🚀 Нативная новость о продукте») + выпадающий список проектов + защитный бейдж Brand Safety при чувствительных темах.
   - `lab_generate_result.html`: плашка нативного продукта с UTM-ссылкой и карточка CQI с разбивкой 4 факторов.
6. **Тестирование и деплой на VPS `144.31.141.17`:**
   - Написаны тесты: `tests/test_dna_prompts.py` и `tests/test_promoted_projects.py`.
   - Полный прогон `pytest` (134 теста контентного движка, 16 тестов ДНК/проектов, 9 тестов Лаборатории, 103 теста дашборда) — 100% pass.
   - `ruff check app/ tests/` — 0 ошибок.
   - Синхронизировано на VPS `144.31.141.17`, скопировано в контейнеры (`svodkapress_app`, `svodkapress_worker_lab`, `svodkapress_worker_content`), сервисы перезапущены и здоровы (`{"status":"ok"}`).

### Сделано

1. **Движок расчета привлекательности статей AEVI (`app/services/aevi_scoring.py`):**
   - Реализован детерминированный расчет индекса вовлечения `Audience Engagement & Virality Index (0.0–100.0)` по 4 независимым векторам данных без платных запросов к LLM (0 токенов):
     - `Поисковый спрос (0–30)`: семантические маркеры читательского интереса и трендов.
     - `Историческое удержание рубрики (0–30)`: фактура Яндекс Метрики из `AudienceCategoryMetric` (глубина сессии, отказы, доля читателей).
     - `Аффинитивность Яндекс.Крипты (0–25)`: сопоставление сущностей сюжета с профилями коммерчески привлекательной ЦА (`finance`, `tech`, `property`, `auto`, `health`, `family`).
     - `Фактура источников (0–15)`: авторитетность каналов, плотность цифр, цитат спикеров и хронометраж.
   - Метод `calculate_clusters_aevi_batch`: высокопроизводительный расчет сотен сюжетов за 1 SQL-запрос.
   - Модель `HypothesisRecord` и алгоритм сверки с Метрикой `evaluate_hypothesis_outcome` (Closed-Loop Feedback).
2. **Интеграция AEVI и мультиканального контура в Лабораторию (`app/services/lab_pipeline.py`):**
   - `get_news_groups_summary`: обогащение каждого сюжета метриками `aevi_score`, `aevi_level`, `aevi_badge`, `predicted_views`, `predicted_reading_sec`, `predicted_ctr_pct`, `target_audience`.
   - Автоматическая сортировка очереди сюжетов Лаборатории: топ-виральные сюжеты всегда первыми (`aevi_score` desc).
   - Сквозной сбор превью для 4 каналов (`_build_multichannel_previews`):
     - Сайт WP: статья со встроенными блоками РСЯ и внутренней перелинковкой.
     - Дзен: адаптация под редактор Дзена с нативным промо-баннером/врезкой (`inject_dzen_promo`), изолированной от сайта `svodka.press`.
     - Telegram: готовый пост с обложкой, текстом, тегами и CTA-кнопкой.
     - ВКонтакте: пост стены со сниппетом ссылки, обложкой и реакциями.
3. **Пользовательский интерфейс Лаборатории (`app/templates/dashboard/partials/`):**
   - `lab_groups_list.html`: кнопка `🔥 Топ-интерес (AEVI ≥ 55)`, бейджи с тултипом 4 векторов, нижняя плашка с сегментом ЦА Крипты, прогнозом визитов и CTR.
   - `lab_cluster_detail.html`: верхний аналитический Bento-блок «🎯 Привлекательность сюжета (AEVI)» с 4 карточками векторов.
   - `lab_generate_result.html`: интерактивный 4-Tab Split-View Live Preview (Сайт WP, Дзен, Telegram, ВКонтакте) + карточка гипотезы ИИ с замкнутым контуром сверки.
4. **Багфикс типов и боевая верификация на VPS `144.31.141.17`:**
   - Исправлена колонка `top_keywords` в `AudienceCategoryMetric` (`app/models/audience_metric.py`) на `MutableJSONBList`.
   - Все 5 рабочих контейнеров (`svodkapress_app`, `svodkapress_worker_lab`, `svodkapress_worker_content`, `svodkapress_worker_publisher`, `svodkapress_worker_ingestion`) обновлены и перезапущены.
   - Проверено вживую на проде: расчет AEVI успешно отработал на базе из 972 сюжетов, генерация мультиканальных превью (WP, Dzen promo, TG, VK) и карточек гипотез функционирует со 100% стабильностью.

1. **Модель данных и миграция (`app/models/audience_metric.py`, миграция `0015`):**
   - Создана модель `AudienceCategoryMetric` с полями платформы (`website`, `dzen`), слага и заголовка рубрики, просмотров, уникальных читателей, среднего времени на странице, отказов, доли аудитории, ключевых поисковых фраз (JSONB), скоринга `ad_potential_score` (0–100), уровня готовности (`high`, `medium`, `low`) и рекомендаций ИИ.
   - Миграция `0015_audience_category_metrics` успешно применена на проде в PostgreSQL (`alembic upgrade head`).
2. **Сервис аналитики и скоринга рекламы (`app/services/audience_analytics.py`):**
   - Прямой опрос Reporting API Яндекс.Метрики по срезу путей первого уровня (`ym:s:startURLPath`) с метриками визитов, пользователей, отказов и продолжительности сессий (свыше 2 100 путей сайта `svodka.press`).
   - Опрос счётчика канала Яндекс Дзен (`106439611`): подтверждена активность счётчика и сбор визитов/читателей.
   - Функция `_extract_category_slug_from_path` парсит URL-структуру `svodka.press/<category_slug>/<post_slug>/`, сопоставляя реальный трафик с 20 редакционными рубриками.
   - Алгоритм расчета `Ad Potential Index` (0.0 — 100.0 баллов):
     - `Volume Score (0-35)`: относительный объём аудитории рубрики.
     - `Engagement Score (0-35)`: глубина чтения (оптимум 120+ сек) и удержание (минимизация отказов).
     - `Commercial Intent (0-30)`: рыночный вес тематики по ставкам eCPM (IT, AI, финансы, недвижимость, медицина).
   - Накопление снимков в БД для последующего обучения модели авто-таргетинга на странице `/ads`.
   - Защита транзакций: проверка `hasattr(session, "rollback")` и `commit` для безопасности при любых ошибках.
3. **Интерфейс дашборда на `/seo` (`app/api/dashboard.py`, `app/templates/dashboard/seo.html`):**
   - Вкладка «👥 Читатели: Сайт vs Дзен & /ads».
   - Bento-сравнение каналов: сайт (визиты, пользователи, среднее время, отказы) vs канал Дзен.
   - Карточки лидеров конверсионного потенциала с персональными рекомендациями ИИ.
   - Интерактивная матрица всех 20 редакционных рубрик с бейджами готовности (`Готово к монетизации`, `Тестирование`, `Низкий потенциал`) и прогресс-барами долей аудитории.
   - Подготовлен фундамент для будущего модуля управления рекламой `/ads`.
4. **Сквозной аудит системы (проведён на VPS `144.31.141.17`):**
   - Все 8 Docker-контейнеров работают в статусе `healthy`.
   - Ресурсы сервера: 2.7 GiB из 9.7 GiB RAM (7.1 GiB свободно), диск 80G из 99G.
   - База данных: 18 таблиц, `pgvector` и `uuid-ossp` в норме.
   - Интеграции: Telegram MTProto (`+79055342403`), WordPress REST API (8 701 статья), Rank Math, Яндекс Метрика, Яндекс Вебмастер, Wordstat, GA4, Google Search Console, IndexNow, ИИ-пул (42 активных ключа Google AI Studio).
5. **Верификация и деплой на VPS:**
   - `ruff check app/ tests/`: 0 ошибок.
   - 170+ тестов (`test_audience_analytics.py`, `test_dashboard_ui.py`, `test_seo_analytics.py`, `test_yandex_connection.py`, `test_google_connection.py`, `test_bing_connection.py`): 100% pass.
   - Деплой на боевой VPS `144.31.141.17`, контейнеры `svodkapress_app` и `svodkapress_worker_lab` обновлены. Прямой live-запрос к `/seo` подтвердил HTTP 200 OK (80 449 байт).

### Сделано

1. **Индексация архива WordPress (`app/services/wp_archive.py`):**
   - Индексация и кэширование постов через REST API (`GET /wp/v2/posts?per_page=100&_fields=id,title,slug,link,date`).
   - Получение честного общего количества постов по HTTP-заголовку `x-wp-total` (8 701 статья на `svodka.press`).
   - Кэширование в Redis (`wp:archive:posts`, TTL 1 час) с автоматическим резервным хранением в памяти.
   - Метод `find_related` для умной внутренней перелинковки в Лаборатории.
   - Бейджи и плашки в интерфейсе обновлены (`8 701 статья на сайте`, статус «Тема новая для сайта» вместо ошибочного «Архив пуст»).
2. **Прямое сохранение Rank Math (`app/services/wp_publisher.py`):**
   - Интегрирован REST-эндпоинт `/wp-json/rankmath/v1/updateMeta` с телом `{objectType: "post", objectID, meta: {...}}`.
   - Гарантированная фиксация `rank_math_title`, `rank_math_description`, `rank_math_focus_keyword`, `rank_math_robots` в обход дефолтных ограничений `show_in_rest`.
3. **Anti-Boring директива и глубокий ресерч (`app/services/content_prompts.py`, `app/services/content_generator.py`, `app/services/lab_pipeline.py`):**
   - Лимит контекста внешнего веб-ресерча расширен до 1500 знаков на источник с разделом `=== КОНТЕКСТ И АНАЛИТИЧЕСКИЕ ДАННЫЕ ИЗ ВНЕШНЕГО РЕСЕРЧА ===`.
   - В системный промт внедрено строгое требование использовать фактуру ресерча (цифры, цитаты, хронологию событий), исключая водянистые пересказы.
   - Параметр `custom_style_prompt` / `prompt_voice` теперь честно передаётся из Студии Лаборатории в генератор.
   - В публикаторе исправлена передача `alt_text` и добавлен автоподбор первого чистого изображения из внешнего веб-ресерча при отсутствии ручного выбора.
4. **Аудит ИИ-провайдеров и ключей (`app/services/gemini_key_pool.py`, `app/services/ai_provider_keys.py`):**
   - Диагностирован кастомный шлюз `@iishkogatewaybot` (`https://grok.reforrm.me/v1`, ключ #56): удалённый сервер New API возвращает HTTP 401 `Invalid token (new_api_error)` — ключ отклонён шлюзом как недействительный/неактивированный.
   - Проверены все 50 ключей Google AI Studio в базе данных: 42 ключа полностью исправны и активны (включая все 10 вновь добавленных ключей ID 46–55).
5. **Устранение ошибки 500 на `/seo` (`app/api/dashboard.py`, миграция `0014`):**
   - Применена миграция `0014_seo_optimizations` в PostgreSQL на проде через `alembic upgrade head` (таблица `seo_optimizations` создана).
   - В маршрут `dashboard_seo` внедрён `await session.rollback()` при перехвате ошибок запросов и отказоустойчивые fallback-значения для истории и паттернов, предотвращающие `InFailedSQLTransactionError`.
6. **Верификация и деплой на VPS:**
   - `ruff check app/ tests/`: 0 ошибок.
   - 103 теста интерфейса (`test_dashboard_ui.py`) и 102 unit-теста (`test_wp_archive.py`, `test_wp_publisher.py`, `test_lab_stepwise.py`): 100% pass.
   - Деплой на боевой VPS `144.31.141.17`, контейнеры перезапущены. Страница `/seo` открывается со статусом 200 OK (30,9 КБ HTML, виджеты поисковой аналитики и самообучения активны).

## Сессия 2026-09-27 — Gemini Native Image (Nano Banana) via Interactions API (2.23.1)

### Сделано

1. **Генерация изображений Gemini (`app/services/image_generator.py`):**
   - Реализована функция `_generate_gemini_native_image` с поддержкой нового Google Interactions API (`POST /interactions`).
   - Поддерживает модели `gemini-3.1-flash-image`, `gemini-2.5-flash-image`, `gemini-3-pro-image`, `gemini-3.1-flash-lite-image`.
   - Автоматическое извлечение base64-изображений из свойства `output_image` или блоков `model_output` -> `image`.
   - Чёткая классификация ошибок квот: при получении HTTP 429 система возвращает информативное сообщение `Google AI Studio требует подключённый биллинг (Billing) — 0 картинок/день на Free Tier`.
2. **Матрица моделей и диагностика (`app/services/gemini_key_pool.py`):**
   - В селектор роли «Генератор картинок» добавлены проверенные опции `gemini-3.1-flash-image` и `gemini-2.5-flash-image`.
   - В `test_individual_model_by_id` добавлено тестирование `*-image` моделей через Interactions API.
3. **Верификация и деплой на VPS:**
   - 104 unit-теста (`test_image_generator.py`, `test_deduplicator.py`, `test_yandex_connection.py`, `test_google_connection.py`, `test_bing_connection.py`): 100% pass.
   - Синхронизация кода на VPS `144.31.141.17`, контейнеры `svodkapress_app` и `svodkapress_worker_lab` обновлены и здоровы (`GET /health` -> 200).

## Сессия 2026-09-27 — Самообучаемый SEO-движок, фикс альбомов Telegram и лимит очереди Lab (2.23.0)

### Сделано

1. **Устранение расщепления альбомов и пустых постов (`app/workers/ingestion_worker.py`, `app/services/deduplicator.py`):**
   - Выявлена первопричина: при сборе альбомов Telegram подпись есть только у одного сообщения, а остальные сообщения альбома имеют ID выше него. Водяной знак `telegram_watermark` отслеживал только `MAX(external_post_id)`. На следующем проходе сообщения-хвосты без подписи считывались как отдельные посты с пустым текстом (`raw_news #...`).
   - Функция `telegram_watermark` теперь учитывает не только `external_post_id`, но и все `original_post_id` из `media_data`, гарантируя корректный сдвиг `min_id`.
   - В `_insert_candidate` добавлена проверка: если у кандидата нет текста, проверяется наличие его медиа в уже сохранённых материалах источника.
   - В `Deduplicator.deduplicate`: пустые посты (`raw_text.strip() == ""`) теперь автоматически маркируются как `DedupStatus.SKIPPED_DUPLICATE` с причиной `empty_text` и `cluster_id = None`, что полностью исключает засорение базы пустыми карточками сюжетов.
   - В БД на сервере удалены 14 накопившихся пустых кластеров с шаблоном `raw_news #%`.
2. **Лимит и пагинация очереди сюжетов в Лаборатории (`app/services/lab_pipeline.py`, `app/api/dashboard.py`, `lab_groups_list.html`):**
   - Функция `get_news_groups_summary` доработана: выполняет честный `select(func.count()).select_from(NewsCluster)` для подсчёта общего числа сюжетов в базе (`total_clusters`), принимает параметры `limit` и `offset`, возвращает `displayed_clusters`.
   - В шаблоне списка сюжетов добавлен динамический бейдж `Все сюжеты ({total}) · показано {limit}` и интерактивный HTMX-переключатель лимита: `50 | 100 | 250 | Все ({total})`.
   - Добавлен эндпоинт `GET /lab/groups` в `pages_router` для мгновенной подгрузки выбранного количества сюжетов без перезагрузки всей страницы.
3. **Самообучаемая система SEO-оптимизации на `/seo` (`app/models/seo_optimization.py`, `app/services/seo_learning.py`, `app/services/wp_publisher.py`, `app/api/dashboard.py`):**
   - Создана модель `SeoOptimization` и применена миграция Alembic `0014_seo_optimizations` на VPS.
   - Сервис `seo_learning.py`: извлекает кандидатов с низким CTR (<3%) и высоким охватом (>50 показов), генерирует гипотезы улучшения SEO Title / Description / Focus Keyword, передаёт контекст успешных выученных кейсов (`is_winner = true`).
   - В `WPPublisherService` добавлен метод `update_post_seo_snippet`, обновляющий метаполя плагина Rank Math (`rank_math_title`, `rank_math_description`, `rank_math_focus_keyword`) через REST API `/wp/v2/posts/{id}`.
   - Интерактивное модальное окно на `/seo` (`seo_optimize_modal.html`): предпросмотр сниппета ДО/ПОСЛЕ в стиле Google/Yandex SERP, редактируемые поля и кнопка «⚡ Применить в WordPress и уведомить поисковики».
   - Мгновенный пинг IndexNow при применении оптимизации для ускоренной переиндексации.
   - Таблица истории принятых решений (`seo_history_table.html`): фиксация дельты CTR и статуса выученной эффективности.
4. **Верификация и деплой на VPS:**
   - Линтер `ruff check app/ tests/`: 0 ошибок.
   - 96 unit-тестов (`test_deduplicator.py`, `test_yandex_connection.py`, `test_google_connection.py`, `test_bing_connection.py`): 100% pass.
   - 103 теста пользовательского интерфейса `tests/test_dashboard_ui.py`: 100% pass.
   - Синхронизация кода на боевой VPS `144.31.141.17`, контейнеры `svodkapress_app` и `svodkapress_worker_lab` обновлены и работают в штатном режиме.

## Сессия 2026-09-27 — Яндекс Метрика: отдельный счётчик для Яндекс Дзен (2.22.2)

### Сделано

1. **Конфигурация и шифрование (`app/config.py`, `app/services/yandex_connection.py`):**
   - Добавлен параметр `yandex_metrika_dzen_counter_id` в настройки приложения и датакласс `YandexConnectionConfig`.
   - Поддержка сериализации/десериализации и сохранения в зашифрованный документ Fernet (`yandex_services_connection` в таблице `system_settings`).
   - Использование единого OAuth-токена Метрики для доступа как к счётчику сайта, так и к счётчику Дзена (права `metrika:read` распространяются на все доступные аккаунту счётчики).
2. **Диагностические зонды и API-эндпоинты (`yandex_connection.py`, `app/api/dashboard.py`):**
   - Параметризован зонд `probe_yandex_metrika(config, counter_id, service_label)`: корректно опрашивает API Метрики для любого заданного счётчика с понятными русскоязычными статусами.
   - Реализован выделенный зонд `probe_yandex_metrika_dzen(config)` и маршрут `POST /settings/yandex/test-metrika-dzen` для проверки связи без перезагрузки страницы.
   - Метод сохранения настроек `settings_yandex_save_partial` обновлён для приёма и персистенции `metrika_dzen_counter_id`.
3. **Интерфейс в `/settings` (`yandex_services_card.html`):**
   - Блок Метрики оформлен в 3-колоночный Apple HIG grid: «Счётчик сайта (WordPress)», «Счётчик Яндекс Дзен» и «OAuth-токен Метрики (общий)».
   - Раздельные кнопки тестирования «Проверить сайт» и «Проверить Дзен» с изолированными блоками вывода диагностических результатов (`#metrika-test-result` и `#metrika-dzen-test-result`).
   - Добавлен индикатор состояния Дзена в шапку карточки (`Дзен: {id}` или серый бейдж).
   - В интерактивную Bento-справку добавлено пояснение по подключению счётчика канала Яндекс Дзен.
4. **Тестирование и деплой на VPS:**
   - Unit-тесты `tests/test_yandex_connection.py`, `tests/test_google_connection.py`, `tests/test_bing_connection.py` (12 passed).
   - Тесты интерфейса `tests/test_dashboard_ui.py` (passed).
   - Деплой на боевой VPS `144.31.141.17`, контейнеры `svodkapress_app` и `svodkapress_worker_lab` обновлены.
   - Проверена загрузка настроек на живом сервере: сайт `106439629` активен, поле Дзена готово к приёму номера.

## Сессия 2026-09-27 — Google Search Console: авто-определение sc-domain и строгие даты ISO (2.22.1)

### Сделано

1. **Авто-обнаружение и нормализация ресурсов GSC (`app/services/google_connection.py`):**
   - Функция `resolve_gsc_site_url(input_url, available_sites)`: опрашивает `GET https://www.googleapis.com/webmasters/v3/sites` сервисного аккаунта.
   - Поддерживает ввод обычного домена (`svodka.press`), префикса (`https://svodka.press/`) или доменного ресурса (`sc-domain:svodka.press`) — извлекает нормализованный хост и сопоставляет с доступными свойствами аккаунта в GSC.
   - Если ресурс в GSC заведён как доменный (`sc-domain:`), а пользователь ввёл просто `svodka.press`, система на лету определяет точный идентификатор `sc-domain:svodka.press` и возвращает подтверждение с ролью (`siteFullUser`).
   - Автоматически сохраняет подтверждённый идентификатор в базу данных PostgreSQL (`system_settings`).
2. **Исправление контракта дат Search Analytics API (`app/services/google_connection.py`):**
   - Устранён сбой HTTP 400 из-за относительного формата `"7daysAgo"`: даты формируются в строгом ISO-формате `YYYY-MM-DD` (`datetime.now(UTC).date()`).
3. **Обновление интерфейса и справки (`google_services_card.html`):**
   - В подсказке и placeholder указана поддержка доменных свойств (`sc-domain:svodka.press` или `svodka.press`).
4. **Тестирование и деплой на VPS:**
   - 12 unit-тестов (`test_google_connection.py`, `test_bing_connection.py`, `test_yandex_connection.py`) — 100% pass.
   - 103 теста панели `tests/test_dashboard_ui.py` — 100% pass.
   - Изменения синхронизированы на VPS `144.31.141.17`, контейнеры `svodkapress_app` и `svodkapress_worker_lab` перезапущены.
   - Живой тест в контейнере подтвердил `ok: True`, сайт `sc-domain:svodka.press` подтверждён с ролью `siteFullUser`.

## Сессия 2026-09-27 — Мульти-поисковая аналитика и вебмастера: Google Analytics 4, Search Console, Bing & IndexNow (2.22.0)

### Сделано

1. **Сервисы Google (`app/services/google_connection.py`):**
   - Реализована безопасная асинхронная генерация OAuth2-токенов (`_mint_service_account_token`, `get_google_access_token`) по спецификации RS256 JWT на базе встроенных библиотек `pyjwt` и `cryptography` без установки ресурсоёмких пакетов GCP.
   - Кэширование токенов доступа в оперативной памяти на 55 минут с защитой мьютексом `asyncio.Lock`.
   - Диагностические зонды: `probe_google_analytics` (запрос сводки за 7 дней через GA4 Data API v1beta) и `probe_google_search_console` (проверка прав и видимости сайта за 7 дней через Search Console API).
   - Шифрование Fernet для `service_account_json` и `oauth_access_token` в таблице `system_settings` (`google_services_connection`).
2. **Bing Webmaster Tools и протокол IndexNow (`app/services/bing_connection.py`):**
   - Клиент Bing Webmaster API с зондом `probe_bing_webmaster` (проверка API-ключа и сопоставление подтверждённых сайтов через `GetUserSites`).
   - Реализация протокола IndexNow (`submit_to_indexnow`, `probe_indexnow`): пакетная и одиночная отправка URL новых статей сразу во все поисковые системы (Bing, Yandex, Seznam, Naver) с поддержкой кастомных путей ключа проверки (`keyLocation`).
   - Автоматический хук публикации: при успешном выходе статьи (как в плановом `publisher_worker`, так и при ручной публикации в Лаборатории `run_cluster_publish_step`) новый URL немедленно передаётся в IndexNow без задержки и блокировки основного потока.
3. **Пользовательский интерфейс и Bento-карточки в `/settings`:**
   - Карточка Google (`google_services_card.html`): статус-бейджи, ввод JSON сервисного аккаунта, Property ID, URL сайта, сворачиваемая Bento-справка с пошаговыми инструкциями создания Service Account и выдачи прав «Читатель»/«Пользователь».
   - Карточка Bing & IndexNow (`bing_services_card.html`): статус-бейджи, ввод API-ключа Bing, ключа IndexNow, сворачиваемое руководство по импорту из Search Console и подтверждению ключа в корне сайта.
   - Универсальный партиал результатов тестирования `service_test_result.html` с локальным выводом прямо под формой и кнопкой закрытия.
4. **Тестирование и документация:**
   - 11 unit-тестов (`test_google_connection.py`, `test_bing_connection.py`, `test_yandex_connection.py`) — 100% pass.
   - 103 теста панели `tests/test_dashboard_ui.py` — 100% pass.
   - Создана документация `docs/12_GOOGLE_ANALYTICS_MCP.md`, обновлены `docs/09_SEO_ANALYTICS.md`, `TODO.md` и `CHANGELOG.md`.

## Сессия 2026-09-27 — Анализ релевантности ресерча, классификация медиа и выбор обложки (2.21.0)

### Сделано

1. **Семантический анализ релевантности статей веб-ресерча (`app/services/web_researcher.py`):**
   - Реализован легковесный стеммер русского и английского языков (`_stem`), устраняющий расхождения в грамматических окончаниях без внешних библиотек.
   - Извлечение ключевых сущностей (`_extract_entities_and_terms`): фиксация названий брендов, моделей с точками (`GLM-5.3`, `Z.ai`), капитализированных терминов, чисел и семантических корней.
   - Расчёт композитной релевантности статьи (`evaluate_article_relevance`): 50% сущности, 30% стеммированные ключевые слова, 20% лексика заголовка.
   - Отсечение кандидатов ниже порога 0.30 или с нулевым совпадением сущностей темы — полное устранение оффтопика (например, статей об автопроме при ресерче моделей ИИ).
   - Сверхвыборка (`max_sources * 2`): сбор до 6 ссылок из поиска с отбором лучших для гарантированного сохранения 3 релевантных источников.
   - Визуализация статуса релевантности в Студии Шага 2 (`✓ В тему (XX%)` или `⚠️ Контекст (XX%)`).
2. **Классификация и фильтрация медиа (`app/services/web_researcher.py`):**
   - Функция `classify_web_image` классифицирует картинки по URL, пропорциям, классам и alt на 4 категории: `photo` (фоторепортаж), `infographic` (таблицы бенчмарков, графики), `logo` (логотипы, аватары), `graphic` (баннеры).
   - `fetch_clean_article` отсекает логотипы и графики при парсинге, отдавая приоритет репортажным кадрам.
3. **Ручной выбор обложки и загрузка в WordPress (`lab_pipeline.py`, `dashboard.py`, `lab_media_result.html`):**
   - На карточках медиа добавлены бейджи типа (`📸 Фото`, `📊 Таблица/График`, `🏷 Логотип`), кнопка «⭐ На обложку» и кнопка «✕ Скрыть».
   - Выбор обложки подсвечивает фото изумрудной рамкой, передаёт `selected_cover_url` в генератор и сохраняет в `cluster.enrichment_data["selected_cover_url"]`.
   - В `run_cluster_publish_step`: при наличии `selected_cover_url` изображение автоматически загружается в WordPress через `WPMediaService.upload_image()` и прикрепляется к публикации как `featured_media`.
4. **Редактируемый промт и динамическое переключение в Шаге 3 (`lab_cluster_detail.html`, `lab.html`):**
   - Текстовое поле промта расширено до `rows="7"` с моноширинным шрифтом `font-mono text-xs`.
   - Контейнер снабжен атрибутами `data-prompts`, содержащими полные тексты всех 5 профессиональных промтов.
   - Глобальная функция `switchLabStyle()` мгновенно подставляет выбранный журналистский стиль в поле редактирования, устраняя неинформативную 1-строчную заглушку.
5. **Интерактивная справка и локальная диагностика сервисов Яндекса (`yandex_services_card.html`, `yandex_connection.py`):**
   - Добавлена трёхколоночная сворачиваемая Bento-справка по кнопке «📖 Справка: как подключить» с подробными инструкциями, ссылками на консоли и точными именами прав OAuth/IAM для Метрики, Вебмастера и Wordstat.
   - Локализованы блоки вывода результатов проверки (прямо под полями проверяемого сервиса с индикацией загрузки `hx-disabled-elt`).
   - Исправлен параметр схемы запроса к Yandex Cloud Search API v2 (`phrase` вместо `phrases`).
6. **Верификация и тесты:**
   - Написаны тесты в `tests/test_web_research_relevance.py` (стемминг, сопоставление сущностей, отсечение оффтопика, классификация картинок).
   - `ruff check app/ tests/`: 0 ошибок.
   - Тесты `test_web_research_relevance.py` и `test_lab_stepwise.py`: 11 passed.
   - Боевые запросы к API Яндекс Метрики и Вебмастера подтверждены статусом HTTP 200 OK.

## Сессия 2026-09-27 — Исправление веб-ресерча, медиа-прокси, журналистские стили и Яндекс-сервисы (2.20.0)

### Сделано

1. **Точный поисковый запрос веб-ресерча (`app/services/web_researcher.py`):**
   - Устранено наивное разделение по точке (`raw.split('.')[0]`), которое ломало технические названия и версии (`Z.ai` -> `Китайская Z` -> поиск автоновостей вместо ИИ).
   - Внедрено регулярное выражение границ предложений `(?<=[.!?])\s+(?=[А-ЯA-Z0-9«\"])|\n+`, сохраняющее внутренние точки в именах сущностей, версиях и доменах.
   - Сохранены естественные русские предлоги для связных запросов в поисковиках (до 8 ключевых слов).
2. **Серверный прокси изображений СМИ (`app/api/dashboard.py`, `lab_media_result.html`):**
   - Реализован эндпоинт `GET /api/ui/lab/media/web-image?url=...` с SSRF-валидацией (`is_safe_web_url`).
   - Исключение заголовка `Referer` при запросе к CDN полностью устраняет ошибки 403 Forbidden (Коммерсантъ, РИА, РБК).
   - Дисковое кэширование SHA-256 (`/app/storage/media/web_cache/{hash}.img`) с заголовком `Cache-Control: public, max-age=86400` и SVG-фоллбеком при недоступности внешнего сервера.
3. **Глубокие журналистские стили и интерактивное переключение (`app/services/lab_pipeline.py`, `lab_cluster_detail.html`):**
   - Промты стилей переписаны из 1-строчных заглушек в развёрнутые профессиональные инструкции: *Информационный*, *Аналитический*, *Срочная молния*, *Лонгрид 2500–4000 знаков*, *Ироничный обзор*.
   - Клиентский хук `window.labStylePrompts` на `id="lab-prompt-override"`: выбор стиля в селекторе Шага 3 мгновенно меняет текст в поле редактирования промта.
   - Исправлена передача системной инструкции в `generate_editorial_image_prompt` (`system=...`).
   - Возврат структурированного `generation_error` и отображение информативного алерта со ссылкой в `/settings/ai` при отключенной/недоступной роли `image`.
4. **Безопасность публикации в WordPress (Шаг 4):**
   - Проверка `wp_configured` при загрузке деталей кластера.
   - Замена активной кнопки публикации на янтарную кнопку «⚙️ Подключить сайт» и вывод карточки с предупреждением о неподключенном сайте.
   - Бэкенд-защита в `run_cluster_publish_step`: запрет публикации с понятным описанием причины.
5. **Интеграция сервисов Яндекса (`app/services/yandex_connection.py`, `settings.html`):**
   - Единый сервис конфигурации `YandexConnectionConfig` для Wordstat, Метрики и Вебмастера.
   - Шифрованное хранение кредов в базе данных (`system_settings`, Fernet) с фоллбеком на `.env`.
   - Диагностические функции быстрой проверки связи (`probe_yandex_wordstat`, `probe_yandex_metrika`, `probe_yandex_webmaster`).
   - Bento-карточка в `/settings` с живыми статусами и кнопками тестирования через HTMX.
6. **Верификация и деплой:**
   - `ruff check app/ tests/`: 0 ошибок.
   - 134 локальных теста пройдены, 16 тестов в контейнере на VPS пройдены.
   - Контейнеры пересобраны и перезапущены на VPS `144.31.141.17`, live эндпоинты `/health` и `/api/ui/lab/media/web-image` проверены (200 OK).

## Сессия 2026-09-27 — Внешний веб-ресерч и аугментация контекста статей (2.18.9)

### Сделано

1. **XPath-парсер DuckDuckGo (`app/services/web_search.py`):**
   - Устранена зависимость от `bs4`/`cssselect`, заменена на нативный `lxml.html` XPath.
   - Реализовано авто-разворачивание редиректов DuckDuckGo (`uddg`).
2. **Сервис веб-ресерча (`app/services/web_researcher.py`):**
   - Полная SSRF-защита: фильтрация приватных подсетей (RFC 1918), loopback, link-local, cloud metadata (`169.254.169.254`), опасных портов и схем.
   - Очистка поисковых запросов от эмодзи и служебного мусора Telegram (`extract_search_query`).
   - Извлечение чистого контента статей через Trafilatura с таймаутом 6 с и лимитом 1 МБ.
   - Эвристическое извлечение фактов (`_extract_key_facts`): даты, цифры/метрики, цитаты без затрат токенов.
   - Исключение социальных сетей (Telegram, VK, OK, Twitter, Facebook).
3. **Оркестрация и аугментация (`app/services/research.py`, `app/services/lab_pipeline.py`):**
   - `conduct_cluster_research()` объединяет факты Telegram, 30-дневную перелинковку и внешние статьи.
   - `_build_augmented_cluster_data()` внедряет блок `=== КОНТЕКСТ И ПРЕДЫСТОРИЯ СОБЫТИЯ ИЗ СЕТИ ===` перед вызовом `generate_article()`.
4. **Интерфейс Студии Лаборатории Apple HIG:**
   - Шаг 1 обновлён на «Внешний веб-ресерч и связи с архивом» с кнопкой «🌐 Исследовать в сети».
   - Карточки внешних источников: домены, ссылки (↗), цитаты, ключевые факты и перелинковка.
5. **Верификация:**
   - 159 unit- и интеграционных тестов пройдено (включая `tests/test_web_research.py`).
   - `ruff` и `mypy` — 0 ошибок.

## Сессия 2026-09-26 — Восстановление сбора новостей в Лаборатории (2.18.8)

### Сделано

1. **Диагностика сбоя сбора:**
   - В логах `svodkapress_app` обнаружено: `Telegram source failed: GRAMJS_TOKEN is not set...` и `0 posts collected`.
   - Первичная причина: в `/opt/svodkapress/.env` был сгенерирован несоответствующий `FERNET_KEY` (`HsGi310n...`), из-за чего Fernet не мог расшифровать сессию Telethon в таблице `telegram_accounts` (`InvalidToken`).
   - После 3 попыток аккаунт был деактивирован (`failure_count = 3`, `is_active = False`), а конвейер откатывался на устаревший GramJS-мост.
2. **Восстановление ключей и сессий:**
   - Найден исходный рабочий `FERNET_KEY` (`WRx3Ep2KqrdIBsNB7BjY0DbWLdiRCjcQAnJgYz8nztc=`) в подтверждённом бэкапе `/root/.env.bak.1787519714`.
   - Проверена расшифровка: 100% ключей (45 из 45 AI-ключей и сессия Telegram) расшифровываются успешно.
   - Аккаунт +79055342403 (`@Genadich2403`) реактивирован в БД.
3. **Разрешение числовых Telegram-каналов (`app/services/telegram_mtproto.py`):**
   - Добавлена функция `_coerce_channel_target` для приведения строк вида `"-1001117628569"` (Mash и др.) к типу `int`, необходимому Telethon для поиска по ID.
   - Реализован прогрев кэша диалогов (`await self._client.get_dialogs()`) при ошибке резолвинга сущностей, гарантирующий доступ к любым закрытым/числовым каналам оператора.
4. **Верификация:**
   - Прогон сбора: получено 1345 сообщений, сохранено 1027 постов, сформировано 976 сюжетов.
   - В Лаборатории отображаются реальные сюжеты и группы. 99 тестов Telethon пройдены (100%).

## Сессия 2026-09-26 — Восстановление доступа к svodkanews.team-big.ru (2.18.7)

### Сделано

1. **Диагностика сетевой топологии и 502 Bad Gateway:**
   - Домен `svodkanews.team-big.ru` резолвился в IP `150.241.98.134` (OpenResty в контейнере `nginx-proxy-manager`).
   - В конфигурации NPM (`/data/nginx/proxy_host/4.conf`) апстрим был настроен на `http://144.31.141.17:8000`.
   - На сервере приложения `144.31.141.17` файрвол `DOCKER-USER` разрешал трафик на порт 8000 исключительно с `150.241.98.134`.
   - В `.env` приложения порт был выставлен как `58000`, биндинг оставался на `127.0.0.1` (отклоняя внешние запросы), а в `ALLOWED_HOSTS` отсутствовал домен `svodkanews.team-big.ru`.
2. **Синхронизация окружения на VPS:**
   - В `/opt/svodkapress/.env` установлены:
     - `APP_HOST_PORT=8000`
     - `APP_BIND_ADDRESS=0.0.0.0`
     - `ALLOWED_HOSTS=svodkanews.team-big.ru,127.0.0.1,localhost,144.31.141.17`
     - `TRUSTED_PROXY_HOSTS=127.0.0.1,150.241.98.134,150.241.98.133`
     - `APP_ENV=production`, `DEBUG=false`, права 600.
3. **Перезапуск и верификация:**
   - Стек пересоздан через `./scripts/deploy_vps.sh --skip-build`.
   - Все 7 контейнеров перешли в статус healthy.
   - Запрос `https://svodkanews.team-big.ru/` возвращает HTTP 303 (редирект на `/login?next=/`) и затем HTTP 200 со страницей авторизации Dark Apple HIG.

## Сессия 2026-09-25 — Каталог проверенных моделей, умная матрица и прямой роутинг (2.18.2)

### Сделано

1. **Каталог проверенных моделей (`get_working_models_catalog`):**
   - Автоматически сканирует активные ключи и модели со статусом `is_working = True`.
   - Группирует модели по провайдерам (Google Gemini, vsllm.cc, apinex и др.), устраняет дубликаты.
   - Разделяет модели на генеративные (`chat_options`) и векторные (`embed_options`).
   - Выводит красивый Bento-блок на `/settings/ai` с зелеными бейджами готовности и таймстемпами.

2. **Умная матрица назначения моделей конвейера:**
   - Поля ролей (`arbiter`, `writer`, `seo`, `embedding`) переведены на выпадающие списки `<select>`, отображающие проверенные модели с бейджами провайдера.
   - Индикация статуса: 🟢 Проверена (модель в каталоге) / ⚠️ Не проверена (ручной ввод / сбой).
   - Кнопка «✨ Автоподбор лучших моделей»: в 1 клик находит оптимальные рабочие модели (быстрые дешевые для арбитра и SEO, флагманские для автора, бесплатный FastEmbed для векторизации).

3. **Интерактивное тестирование моделей:**
   - В дровере «Все обнаруженные модели» рядом с каждой моделью добавлена кнопка «⚡ Тест» для мгновенного пинга модели.
   - В шапке каждого пула добавлена кнопка «🧪 Тест топ-5» для автоматического прогона основных моделей пула.
   - Реализована функция `test_individual_model_by_id`, обновляющая статус `is_working`, дату и ошибку.

4. **Прямой мульти-провайдерный роутинг (`AIClient`):**
   - Устранен сбой HTTP 401 на мертвом прокси `omniroute.team-big.ru`.
   - Запросы к Google Gemini направляются напрямую в `generativelanguage.googleapis.com` с Round-Robin по 32 проверенным ключам.
   - Запросы к кастомным шлюзам (`vsllm.cc`, `api.apinex.bond`) направляются напрямую к их `/v1/chat/completions`.
   - OpenRouter сохранен как прозрачный fallback.

5. **Тестирование и верификация:**
   - Добавлены тесты `test_working_models_catalog_and_auto_select` и `test_run_individual_model_test` в `tests/test_ai_key_pools.py`.
   - 9/9 тестов в `test_ai_key_pools.py`, 15/15 в `test_ai_provider_keys.py`, 101/101 в `test_dashboard_ui.py`.
   - Полный сьют: 1204 теста успешно пройдены.
   - 0 ошибок Ruff.

## Сессия 2026-09-25 — Свёрнутые пулы, имена провайдеров и индикация моделей (2.18.1)

### Сделано

1. **Свёрнутые пулы по умолчанию:**
   - Для всех пулов установлен `is_open = False`. На странице `/settings/ai` пулы отображаются компактными аккуратными строками Bento и разворачиваются только по клику пользователя.
2. **Именование и эндпоинты кастомных провайдеров:**
   - Создана миграция `0013_provider_custom_name` (колонка `custom_name VARCHAR(128)` в `ai_provider_keys`).
   - Форма подключения поддерживает ввод произвольного названия («vsllm.cc», «Мой vLLM кластер» и т.д.). При отсутствии названия система автоматически извлекает имя хоста из URL эндпоинта.
   - В шапке пула выводится кликабельный адрес эндпоинта (например, `https://vsllm.cc/v1`), открывающийся во вкладке без раскрытия аккордеона.
   - Ключ #41 в БД обновлен с именем `vsllm.cc`.
3. **Прозрачная индикация работающих моделей:**
   - На карточке каждого ключа добавлен блок «Работающие модели:» с зелеными бейджами активных протестированных моделей (`deepseek-v4-flash ✓ работает`, `gemma-4-26b-a4b-it ✓ работает`, `gemini-vision ✓ работает`, `gemini-embedding-001 ✓ работает`).
   - Добавлен разворачиваемый список «Все обнаруженные модели (X)» со статусами каждой модели (`✓ РАБОТАЕТ`, `доступна`, `✗ Ошибка`).
4. **Тестирование и деплой:**
   - Добавлен тест `test_custom_provider_named_pool_and_working_models` в `tests/test_ai_key_pools.py`. Все 7 тестов пройдены, линтеры чистые.
   - Выполнен деплой на боевой сервер (`svodkapress`), применена миграция Alembic `0013`, проверена работа пулов.

### Сделано

1. **Очистка страницы `/settings`:**
   - Удалены дублирующие секции «Отказоустойчивость моделей» и «Матрица моделей», перенесенные в «Настройки ИИ».
   - Bento-стилизация: карточка подключения WordPress & SSH, режим конвейера и таблица системных ключей окружения.
   - Баннер-ссылка на раздел «Настройки ИИ» (`/settings/ai`).
2. **Мульти-провайдерная архитектура и сворачиваемые пулы (`/settings/ai`):**
   - Нативные HTML5 `<details>` аккордеоны с брендовыми цветами и бейджами провайдеров (Google Gemini, OpenRouter, OpenAI, Anthropic Claude, DeepSeek, Fal AI, Custom).
   - Снято искусственное ограничение на 20 ключей: массовый ввод принимает любое число ключей.
   - Кнопка «+ Подключить дополнительного провайдера» с селектором и кастомным base_url.
   - Независимый Round-Robin диспетчер по каждому провайдеру с мгновенным Failover при 429.
3. **Динамический опрос моделей Gemini (`ModelService.ListModels`) и исправление ошибок:**
   - Реализован вызов `GET https://generativelanguage.googleapis.com/v1beta/models` с заголовком `x-goog-api-key`.
   - Полноценный разбор `supportedGenerationMethods` (`generateContent`, `embedContent`).
   - Динамический подбор рабочих flash-моделей для текста и vision.
   - Умная обработка эмбеддингов: если модель `text-embedding-004` отсутствует или выдаёт 404, ключ остаётся **полностью рабочим (🟢 Готов к работе)**, а для векторизации используется локальный zero-cost `fastembed`.
   - Информативные человекочитаемые ошибки для HTTP 401/403 (неверный ключ/ограничения проекта) и 429.
4. **Тестирование, деплой на прод и верификация:**
   - Создан `tests/test_ai_key_pools.py` (6 новых тестов: неограниченный ввод, мульти-провайдерные пулы, разбор ListModels, graceful fallback эмбеддингов, ошибки 401, изоляция Round-Robin).
   - 120 тестов успешно пройдены локально, 100% чистота `ruff` и `mypy app/`.
   - Выполнен деплой на VPS (`svodkapress`):
     - В `AIProviderKey.models` установлен `lazy="selectin"` и `populate_existing=True`, устранивший `MissingGreenlet`.
     - Добавлен фильтр нетекстовых моделей (`-tts`, `-image`, `lyria`, `robotics` и т.д.) и многоуровневый fallback-перебор сгенерированных моделей.
     - Провайдер `gemini` нормализован в `google`, все 40 ключей объединены в единый управляемый пул.
     - На боевом сервере протестированы все 40 ключей: **32 активных ключа успешно верифицированы (Text, Vision, Embeddings)**, 8 нерабочих ключей (удаленные сервисные аккаунты Google) корректно подсвечены в UI.
     - Проверен подключенный кастомный провайдер `vsllm.cc`: автоматически нормализован `base_url` (добавлен префикс `/v1`), обнаружено 72 модели, верифицирована генерация через `deepseek-v4-flash` (🟢 Готов к работе).

## Сессия 2026-09-24 (вечер) — Студия Лаборатории 2.17.0, AI-пул и деплой на прод

### Сделано

1. **Фиксация Студии (Шаг 2):**
   - Правая колонка Студии сюжета закреплена (`sticky top-6 self-start max-h-[calc(100vh-3rem)] overflow-y-auto`) и не уезжает при скролле длинного левого списка сюжетов.
2. **Отфильтрованные посты («Отфильтровано»):**
   - Полный просмотр текстов и медиа-превью отфильтрованных постов.
   - Человекочитаемые расшифровки правил отсева (`digest_structure`, `commercial_promo`, `ad_markers`).
   - Кнопка восстановления в один клик «Разблокировать в сюжеты» (`POST /api/ui/lab/unblock-post/{post_id}`) со сбросом флагов и созданием кластера.
   - Смягчены ложные срабатывания фильтра дайджестов для новостей с эмодзи молнии (⚡️) и каналов новостных агентств (SHOT, Mash, RT, TASS, NEWS).
3. **Умная рубрикация сюжетов и создание рубрик на лету:**
   - До анализа рубрика отображается нейтрально: `🕒 Рубрика определяется ИИ` (вместо дефолтных «Новостей общества»).
   - ИИ-сопоставление с рубриками сайта и вывод краткого обоснования выбора.
   - Автоматическое предложение новой рубрики и форма создания «+ Создать рубрику» в один клик (`POST /api/ui/lab/add-category`).
4. **Медиа-студия и OCR водяных знаков:**
   - Отображение реальных фото и видео из первоисточников.
   - Статусные плашки: 🟢 Чистое фото, 🟡 Водяной знак канала с рамкой безопасного кропа 15%, 🔴 Брак.
   - Блок Fallback-генерации (Flux Schnell / Imagen 3) с редактируемым промтом.
5. **Генерация статьи, SEO Rank Math и внутренняя перелинковка:**
   - Прозрачность источников (тексты постов, факты, архивные связи).
   - Редактор редакционного промта.
   - SEO-карточка Rank Math: Focus Keyword, SEO Title, SEO Description, Wordstat-запросы, llm.txt.
   - Обязательная внутренняя перелинковка со статьями архива с подсветкой ссылок `<a href="...">` в предпросмотре.
6. **Выделенная страница `/settings/ai` и пул 20 ключей Google AI Studio:**
   - Вкладка «Настройки ИИ» в сайдбаре.
   - Массовый ввод до 20 ключей Google AI Studio с шифрованием Fernet.
   - Раздельное тестирование способностей ключей (Текст `gemini-2.5-flash`, Зрение `gemini-vision`, Эмбеддинги `text-embedding-004`).
   - Round-Robin ротация с автоматическим переключением (Failover) при исчерпании квот (HTTP 429).
7. **Тестирование и деплой на production (VPS 144.31.141.17):**
   - 100% покрытие новыми тестами: `tests/test_lab_stepwise.py`, `tests/test_lab_features.py` (13 тестов), `tests/test_dashboard_ui.py` (101 тест), `tests/test_moderator.py`.
   - Полная чистота ruff и mypy.
   - Свежий бэкап БД (`/opt/backups/svodkapress-...-pre-2.17.0.sql.gz`) и кода.
   - Образы пересобраны, стек перезапущен, эндпоинты `/health`, `/settings/ai`, `/lab` проверены живыми запросами.

### Намеренно не делал

- Не включал автоматический конвейер (фоновый сбор по таймеру) — запуски контролируются вручную через панель/лабораторию.

### Не проверено

- Публикация на боевой WordPress реального поста (WP_APP_PASSWORD на VPS пуст).

---

## Текущая работа

Утверждён новый план завершения: `~/.claude/plans/hazy-marinating-goose.md`.
Владелец снова выбрал TailAdmin для интерфейса и Camoufox Research для ресерча.
Проверены публичные README и отдельные исходные файлы; установка и живой
интеграционный тест Camoufox ещё не выполнялись.

Локально начато исправление утечки Gemini-ключей: заголовок `x-goog-api-key`
вместо URL, безопасные ошибки без сырых ответов/транспортных исключений.
Добавлены регрессионные тесты. Проверка модели требует непустого ответа;
обнаружение новой модели больше не выставляет ей признак рабочей.
На прод эти изменения ещё не выложены. Ранее засвеченные в HTTP-логах ключи
рекомендовано заменить владельцу; ключи и журналы не удалялись.

## Следующий шаг

- Завершить отдельные статусы ключа/модели, пагинацию каталога, реальные
  адаптеры/назначения этапов и ограниченное фоновое тестирование.
- Устранить гонки JSONB прогона: отдельные попытки этапов, ARQ, одобрение
  конкретной версии, безопасные повторы и параллельно только выбранные новости.
- Подключить Camoufox как изолированный сервис и реальную OCR/дедупликацию медиа.
- Исправить удаление дня: текущий код удаляет все кластеры без статей, а не только
  затронутые осиротевшие; на production эту операцию не запускать.
- Завершить промты и учёт всех попыток; при сбоях конфигурации автомат выключен.
- TailAdmin UI и проверка всех сценариев в локальном Browser pane, затем
  документация по фактическим результатам и отдельное согласование деплоя.

Исторические записи ниже не заменяют проверку текущего кода и сервера.

Решения владельца (2026-09-04):

1. **Порты 8000 и 3005** — закрыты в `DOCKER-USER` (подтверждено: снаружи TCP
   таймаутится; прокси NPM продолжает ходить на `127.0.0.1:8000`).
2. **Конвейер** — не включать, пока не настроена публикация.
3. **Очередь на проде очищена**: 1077 постов, 978 кластеров, 36 источников
   сброшены, 31876 Redis-ключей. Источники, аккаунты, пользователь и настройки
   сохранены.
4. **Свежесть сбора:** первый проход и сбор после простоя — только посты с 00:00
   `Europe/Moscow`, затем лимит 50; вчерашними не добиваем.
5. **Диалоги:** показываем все типы, собираем только отмеченные.
6. **Непрочитанные** считаем по нашему watermark, а не по счётчику Telegram.

## Главный блокер

Публикация. Владелец подключит сайт сам, через панель — значит блокер снимается
не выдачей паролей, а реализацией Фазы 1. До этого конвейер держим выключенным.

---

## Сессия 2026-09-04 (вечер) — `/sources` на проде

### Сделано

1. **Выложено на прод, версия 2.11.0.** Бэкапы до изменений (дамп базы и архив
   кода), миграция `0006` применена, `/health` и `/health/ready` отвечают, три
   воркера работают, порты снаружи закрыты.
2. **Первый живой импорт диалогов** с аккаунта владельца — и сразу три дефекта,
   которых не поймали моки:
   - аватары не сохранялись вообще (`download_media` вместо
     `download_profile_photo`);
   - приватные каналы показывались как «нет публичной ссылки», хотя открываются
     по `t.me/c/<id>`;
   - один канал показывался двумя строками (`rian_ru` и `@rian_ru`,
     `@toporlive` и `-100…`).
3. **Все три исправлены и проверены на живой странице:** 38 строк, 38 ссылок,
   35 аватаров, дублей нет, ошибок в консоли нет.
6. **Четвёртый дефект, найден владельцем.** ТАСС и Интерфакс оставались без типа,
   ссылки, аватара и описания, и я списал это на «аккаунт не подписан». Владелец
   возразил, я проверил — и был неправ: `get_entity` открывает оба канала,
   Telegram даёт читать **публичный** канал без подписки. То есть «нет в списке
   диалогов» ничего не говорит о работоспособности источника. Добавлен
   `describe_peer()`: строки вне списка диалогов дополняются по идентификатору.
   На проде после импорта: 42 строки, 3 объединённых дубля, 37 аватаров,
   38 ссылок `t.me`, строк без типа — ноль.
4. **Склейка дублей безопасна:** сохраняется строка с историей постов или с
   отметкой владельца, остальные помечаются как объединённые, а не удаляются —
   удаление унесло бы собранные посты каскадом.
5. Добавлены тесты на все три дефекта; `pytest` 1145.

### Намеренно не делал

- Не включал конвейер.
- Не трогал WordPress SSH, OAuth, промты — это оставшаяся часть Фазы 1.

### Не проверено

- Живой вход в Telegram через QR и через телефон: аккаунт уже подключён, новый
  не заводил.
- `scripts/smoke_test.py` локально: нет роли `svodkapress` в локальном
  PostgreSQL. Миграция при этом проверена на проде.

### Открытые вопросы

- Нет. ТАСС и Интерфакс читаются без подписки, метаданные подтянуты.

---

## Сессия 2026-09-04 — `/sources` (локально, 2.11.0)

### Сделано

1. **Подключение Telegram из панели:** QR и телефон+2FA; сессии в Redis
   зашифрованы; кнопка «Отключить» стирает сессию.
2. **Диалоги:** импорт всех типов со всех аккаунтов, ссылка, описание, тип,
   чекбоксы сбора/медиа, «новее отметки» по watermark.
3. **Свежесть:** first pass / после простоя — только сегодня с 00:00 MSK,
   затем лимит 50.
4. **Сайты:** добавить и soft-delete из панели.
5. **Проверки:** `ruff` чисто, `mypy` чисто (78 файлов), `pytest` 1131,
   36 шаблонов компилируются.

### Намеренно не делал

- Не выкладывал на прод (нужна миграция `0006` и решение владельца).
- Не включал конвейер.
- Не трогал WordPress SSH, OAuth, промты.

### Не проверено

- Живой QR/телефонный вход в Telegram.
- Реальный импорт диалогов с аккаунта.
- Миграция `0006` против PostgreSQL (локально нет роли `svodkapress`).
- Вид страницы `/sources` в браузере на проде: код там ещё 2.10.0.

### Открытые вопросы

- Когда выкладывать 2.11.0 на прод.

---

## Сессия 2026-09-04 — решения и доступ к панели

### Сделано

1. **Playwright MCP** переустановлен с `--allowed-hosts *`, внешние URL
   открываются без ограничения localhost.
2. **502 на панели** устранён: контейнер `app` был пересоздан без
   `docker-compose.prod.yml` и потерял публикацию `8000` на хост; восстановлен
   через `docker compose -f docker-compose.yml -f docker-compose.prod.yml`.
3. **Порты снаружи закрыты** (повторная проверка): `8000` и `3005` таймаутятся;
   `https://svodkanews.team-big.ru/login` отдаёт 200 и новый дизайн входа.
4. Зафиксированы решения владельца: порты закрыты; конвейер до публикации не
   включаем.
5. **Очередь на проде очищена** по явному запросу: 1077 постов, 978 кластеров,
   36 источников сброшены к первому проходу, 31876 Redis-ключей удалены.
   Источники/аккаунты/пользователь/настройки сохранены.

### Намеренно не делал

- Не включал переключатель конвейера.
- Не начинал Фазу 1 без явного запроса.
- Не запускал полную очистку `wipe` (на `APP_ENV=production` она запрещена);
  хватило обычного сброса очереди, статьи и так были нулём.

### Не проверено

- Все экраны панели после входа глазами (Обзор / Материалы / …) — владелец
  смотрит сам.

### Открытые вопросы

- Нет. Можно переходить к Фазе 1.

---

## Сессия 2026-09-03 — Фаза 0 и деплой

### Деплой на прод (выполнен)

1. **Резервные копии до изменений.** Дамп базы
   `/opt/backups/svodkapress-20260903-154343-pre-0005.sql.gz` (1.8 МБ, целостность
   проверена, 9 таблиц) и архив прежнего кода
   `/opt/backups/code-20260903-154700-pre-2.10.0.tar.gz`.
2. **Код скопирован** в `/opt/svodkapress` без `.env`, `secrets/`, `storage/` и
   без dev-накладки `docker-compose.override.yml` — она публикует базу на хост.
3. **Валидация** `deploy_vps.sh --check-only` прошла, затем полный деплой:
   образ пересобран, миграция `0004 → 0005` применена, `/health` и
   `/health/ready` отвечают, все три воркера запущены и здоровы.
4. **Проверено живыми запросами:** версия 2.10.0, `article_journals` создана с
   нужными индексами и каскадом, данные сохранены (292 статьи, 1181 кластер,
   1282 поста, 36 источников, 1 Telegram-аккаунт), экран журнала открывается,
   кнопка «Журнал» есть у всех материалов страницы, анонимный доступ даёт 401,
   полная очистка на проде отклонена с 409 и текстом про `APP_ENV=production`.
   Ошибок в логах приложения и воркеров нет.

### Найдено при проверке (важное)

**Порты 8000 и 3005 доступны из интернета.** Цепочка `DOCKER-USER` пуста, а
правила `ufw` не покрывают опубликованные Docker-порты, потому что DNAT Docker
выполняется раньше. Внешняя проверка: TCP-соединение устанавливается; приложение
отвечает `400` только за счёт `ALLOWED_HOSTS`. Документация раньше утверждала
обратное — исправлено в `docs/11`. Правила не применял: это меняет доступность
прокси, нужно решение владельца.

### Сделано (Фаза 0)

1. **Постоянный журнал материала.** Добавлены `article_journals`, миграция
   `0005_article_journals`, запись production-журнала после создания статьи и
   кнопка «Журнал» в списке материалов. Подробные промты и результаты остаются
   в файлах `storage/runs/<run_id>/`, в БД хранится только компактная связь.
2. **Безопасная полная очистка.** В «Лаборатории» сначала показывается цель и
   объём очистки, а удаление требует второго подтверждения. Защита блокирует
   production, удалённые хосты и базы без `_verify`/`_test`/`_scratch`/`_ci`.
   Источники, Telegram-аккаунты, пользователь, настройки, теги и миграции не
   затрагиваются. Существующие данные ни локально, ни на VPS не удалялись.
3. **SSH-основа.** В `scripts/remote_ssh.py` добавлен проверяемый контракт
   `RemoteTarget`; он не хранит ключи и не подключается к серверу сам.
4. **Проверки.** `ruff check .`, `mypy app scripts migrations/env.py` и полный
   `pytest` прошли. Полная проверка smoke/Alembic против PostgreSQL не состоялась:
   локальный сервер принимает TCP на 5432, но не принимает роль `svodkapress`.

### Намеренно не делал

- Не удалял старые данные ни локально, ни на проде.
- Не включал переключатель конвейера: это начнёт расходовать деньги на ИИ.
- Не менял правила брандмауэра: закрытие 8000/3005 влияет на доступ прокси.
- Не трогал `.env`, секреты и доступы к WordPress.
- Не реализовывал ручные остановки/одобрения между этапами: это Фаза 2.

### Не проверено

- Локальный `scripts.smoke_test` и `alembic check`: локальный PostgreSQL не имеет
  роли `svodkapress`. **На проде миграция применена и схема проверена запросами**,
  поэтому дефект схемы исключён, но локальная процедура проверки всё ещё не
  воспроизводится.
- Панель глазами в браузере: проверял HTTP-ответами и разметкой, не визуально.
- Живая генерация и публикация: конвейер выключен, ключ Fal и доступы WP пусты.

### Открытые вопросы

- Закрывать ли 8000 и 3005 в `DOCKER-USER` (нужно подтверждение адресов прокси).
- Включать ли конвейер до настройки публикации.
- Что делать со старыми 292 статьями и 1181 кластером на проде: полная очистка
  там намеренно запрещена, нужна отдельная согласованная процедура или решение
  оставить данные.

---

## Сессия 2026-09-03

### Сделано

1. **Согласован функционал.** Владелец ответил на все вопросы; бриф и план по
   фазам зафиксированы в `PROJECT.md` (разделы 8 и 10). Решения: ручной режим
   первым, WordPress по SSH, OAuth для соцсетей, строгая экономия на моделях,
   Wordstat для ключей, SEO-правки с одобрением, полный журнал прогона,
   создание новой рубрики при необходимости.

2. **Установлены инструменты глобально** (резервная копия скилов —
   `~/.claude/backups/skills-before-install-20260902-185421`):
   - скилы 24 → **81**, все прошли проверку структуры, конфликтов имён нет;
   - MCP: **playwright** и **context7**, оба отвечают;
   - речевые хуки плагина `caveman` намеренно **не** установлены.

3. **Исправлены правила документации.** `AGENTS.md` отправлял читать семь
   файлов, которых не было на диске. Теперь список честный, и в обязательный
   цикл добавлен шаг: тронул модуль — обнови его файл в `docs/`.

4. **Заведены недостающие документы** `05`–`11` и переписаны заглушки `00`–`02`
   (в них были заявлены веб-ресерч и вектор 1536, которых в коде нет). Каждый
   файл делит содержание на «реализовано» и «запланировано».

5. **Сверка документов с кодом поймала две мои ошибки**, обе исправлены:
   - правил anti-slop 39 (29 жёстких, 10 правимых), а не 40;
   - соцсети публикуются параллельно через `asyncio.gather`, а не
     последовательно с задержкой.

### Намеренно не делал

- Не писал и не правил код проекта: только документацию и правила.
- Ничего не менял и не перезапускал на сервере.
- Не запускал живую генерацию — чтобы не тратить деньги.

### Не проверено

- Фактический расход на ИИ: старые логи затёрты перезапуском.
- Аналитика и соцсети: ключи пусты.
- Доступ к сайту по SSH: не запрашивал.
- Качество текстов по существу: читал заголовки и описания, не полные тексты.

---

## Сессия 2026-09-02

### Сделано

1. **Сверка состояния по факту** — код (73 модуля), сервер по SSH, база, логи,
   контейнеры, живые HTTP-запросы к сайту.
   - Локальные проверки: `ruff` чисто, `mypy` чисто (74 файла), `pytest` 1115
     тестов пройдено.
   - Найдено 9 проблем, см. `PROJECT.md` 3.3.
   - **Поправка к первичному отчёту:** сайт не отсутствует. Он есть на
     `svodka.press`, и все 20 рубрик кода совпали с ним точно, включая номера.
     Первичный вывод «WordPress нет» был основан на адресе из настроек сервера,
     который указывает на панель. Настоящий адрес найден в n8n-воркфлоу.

2. **Установлены инструменты глобально** (резервная копия скилов сделана в
   `~/.claude/backups/skills-before-install-20260902-185421`):
   - скилы: 24 → **81**, все валидны, конфликтов имён нет;
   - MCP: **playwright** и **context7**, оба подключены и отвечают;
   - речевые хуки плагина `caveman` намеренно **не** установлены — противоречат
     правилам стиля проекта.

3. **Изучены опорные проекты**: ваши 15 n8n-воркфлоу и Excalibur Blog Cloud.
   Выводы — `PROJECT.md` раздел 5.

4. **Создана документация**: `PROJECT.md`, `STATE.md`, `docs/TOOLING.md`.

### Намеренно не делал

- Ничего не менял и не перезапускал на сервере: ни `up -d`, ни миграций, ни
  правки настроек. Только чтение.
- Не писал и не правил код проекта.
- Не запускал живую генерацию — чтобы не тратить деньги на ИИ.

### Не проверено

- Фактический расход на ИИ: старые логи затёрты перезапуском контейнеров.
- Аналитика (Метрика, Search Console), публикация в соцсети: ключи пусты.
- Доступ к сайту по SSH: не запрашивал.
- Качество текстов по существу: читал заголовки и описания, полные тексты — нет.

---

## Как обновлять этот файл

В конце сессии: перепиши «Текущая фаза», «Следующий шаг», добавь блок сессии
сверху списка. Каждый блок сессии — четыре части: сделано, намеренно не делал,
не проверено, открытые вопросы. Не удаляй прошлые сессии: по ним видно, как
менялось понимание.

Правило честности: «сделано» — только то, что прошло проверку. Всё остальное
идёт в «не проверено», даже если код написан.
