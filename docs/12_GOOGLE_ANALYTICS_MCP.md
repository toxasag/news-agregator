# 12_GOOGLE_ANALYTICS_MCP.md — Google Analytics 4, Search Console & MCP Интеграция

Данный модуль описывает архитектуру подключения и взаимодействия SvodkaPress с сервисами Google:
- **Google Analytics 4 (GA4)** через Google Analytics Data API v1beta
- **Google Search Console (GSC)** через Webmasters Search Analytics API
- **Google Analytics MCP Server** (`@googleanalytics/google-analytics-mcp`)
- **RS256 JWT Service Account Minting** (асинхронная генерация OAuth2-токенов без внешних тяжелых SDK)

---

## 1. Архитектура и компоненты

### 1.1 Сервисный слой (`app/services/google_connection.py`)
1. **Шифрование данных (Fernet)**:
   - Приватные ключи и сертификаты сервисного аккаунта (`service_account_json`), а также OAuth-токены шифруются ключом `FERNET_KEY` перед сохранением в PostgreSQL (`system_settings`, ключ `google_services_connection`).
   - При отображении в веб-интерфейсе конфиденциальные данные маскируются (`••••••••{last4}`).

2. **Асинхронная генерация токенов доступа (RS256 JWT)**:
   - Вместо установки тяжелых зависимостей (`google-auth`, `google-api-python-client`), занимающих сотни мегабайт, генерация Bearer-токена реализована на чистом `jwt` + `cryptography` + `httpx.AsyncClient`.
   - Запрос к `https://oauth2.googleapis.com/token` с `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`.
   - Кэширование токена в памяти на 55 минут с мьютексом (`asyncio.Lock`), предотвращающим повторные запросы при параллельных вызовах.

3. **Диагностические зонды**:
   - `probe_google_analytics(config)`: отправляет проверочный отчет за последние 7 дней к `https://analyticsdata.googleapis.com/v1beta/{property_id}:runReport` (метрики: `activeUsers`, `sessions`, `screenPageViews`).
   - `probe_google_search_console(config)`: выполняет запрос к `https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query` (клики, показы за 7 дней).

---

## 2. Google Analytics MCP Server Integration

### 2.1 Назначение протокола MCP для GA4
Официальный MCP-сервер Google Analytics ([github.com/googleanalytics/google-analytics-mcp](https://github.com/googleanalytics/google-analytics-mcp)) предоставляет агентам стандартизированный интерфейс Model Context Protocol для:
- Чтения трафика в реальном времени (`get_realtime_report`)
- Построения когортных отчетов по статьям и рубрикам (`run_report`)
- Получения метаданных кастомных параметров и конверсий (`get_metadata`)

### 2.2 Спецификация прав доступа (Scopes)
Для полноценной работы с GA4 и Search Console через Service Account требуются следующие скоупы:
```
https://www.googleapis.com/auth/analytics.readonly
https://www.googleapis.com/auth/webmasters.readonly
```

### 2.3 Конфигурация MCP Sidecar в Docker Compose
MCP-сервер Google Analytics может запускаться как Docker-сайдкар или подключаться в Antigravity/Claude Code:
```json
{
  "mcpServers": {
    "google-analytics": {
      "command": "npx",
      "args": ["-y", "@googleanalytics/google-analytics-mcp"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/app/secrets/google_service_account.json"
      }
    }
  }
}
```

Внутри SvodkaPress для всех автоматических проверок и аналитики используется встроенный async Python-клиент `app/services/google_connection.py`, который не требует внешних Node.js процессов на сервере.

---

## 3. Настройка и подключение через интерфейс `/settings`

1. **Создание сервисного аккаунта в Google Cloud Console**:
   - Создать проект в [console.cloud.google.com](https://console.cloud.google.com/).
   - Включить **Google Analytics Data API** и **Google Search Console API**.
   - Создать сервисный аккаунт (`IAM & Admin → Service Accounts`).
   - Скачать ключ в формате JSON (`Add Key → Create new key → JSON`).

2. **Предоставление прав**:
   - **В GA4**: Администратор → Управление доступом к ресурсу → Добавить пользователя (email сервисного аккаунта) с ролью **Читатель (Viewer)**.
   - **В Search Console**: Настройки → Пользователи и разрешения → Добавить пользователя с доступом **Полный** или **Ограниченный**.

3. **Ввод данных в панели SvodkaPress**:
   - Вставить содержимое JSON-файла в поле «JSON-ключ сервисного аккаунта».
   - Указать `GA4 Property ID` (9 цифр, например `123456789`).
   - Указать `URL сайта в Search Console` (например `https://svodka.press/`).
   - Нажать кнопки **«Проверить GA4»** и **«Проверить Search Console»** для проверки живой связи.
