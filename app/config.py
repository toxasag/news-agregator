"""Application settings loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Final, Literal

from pydantic import (
    Field,
    RedisDsn,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent

#: Width of the ``vector(N)`` columns in the schema. Declared here (rather than
#: imported from ``app.models.vector``) because the models import this module,
#: so importing them back would be circular. ``app.models.vector`` asserts the
#: two values are equal at import time, so they cannot drift apart.
#: Vector width of the pgvector columns. Set by the *local* embedding model
#: (fastembed MiniLM-L12-v2), not by a gateway: embeddings are generated on the
#: worker for zero tokens, so this is a property of the pinned model and
#: changing it is a migration rather than a setting.
SCHEMA_EMBEDDING_DIMENSIONS: Final[int] = 384


class Settings(BaseSettings):
    """Central, immutable configuration object for the whole pipeline."""

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # ------------------------------------------------------------------ runtime
    app_name: str = "SvodkaPress NextGen"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    timezone: str = "Europe/Moscow"

    # ----------------------------------------------------------------- postgres
    postgres_user: str = "svodkapress"
    postgres_password: str = "svodkapress"  # noqa: S105 - dev fallback, overridden by .env
    postgres_db: str = "svodkapress"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_recycle: int = Field(default=1800, ge=60)
    db_echo: bool = False

    # -------------------------------------------------------------------- redis
    redis_url: RedisDsn = Field(default=RedisDsn("redis://redis:6379/0"))
    redis_max_connections: int = Field(default=50, ge=1, le=1000)
    #: Seconds a caller waits for a pooled connection before erroring.
    redis_pool_timeout: int = Field(default=20, ge=1)

    # ----------------------------------------------------------------- security
    #: Placeholder values that must never reach a production deployment. Kept as
    #: a constant so the validator and `.env.example` cannot drift apart.
    INSECURE_PLACEHOLDERS: ClassVar[frozenset[str]] = frozenset(
        {
            "insecure-development-secret-key-change-me",
            "change_me_generate_a_64_byte_url_safe_secret",
            "change_me_postgres_password",
            "svodkapress",
            "",
        }
    )

    secret_key: str = Field(default="insecure-development-secret-key-change-me")
    algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=1440, ge=1)

    # -------------------------------------------------------------- dashboard
    #: Single administrator account. A `users` table is deliberately not created:
    #: this dashboard has exactly one operator, and a login form backed by one
    #: environment-provided credential has a far smaller attack surface than a
    #: user-management CRUD nobody needs.
    admin_username: str = "admin"
    #: bcrypt hash of the administrator password, generated with
    #: `python -m app.core.security hash <password>`. Storing a hash rather than
    #: the plaintext means a leaked `.env` does not immediately yield a working
    #: login, and it keeps the comparison constant-time.
    admin_password_hash: str = ""
    #: Name of the session cookie holding the JWT.
    session_cookie_name: str = "svodka_session"
    #: Fernet key encrypting third-party API keys at rest in the settings store.
    #: Separate from `SECRET_KEY` on purpose: rotating the JWT signing key should
    #: invalidate sessions, not render every stored API key undecryptable.
    fernet_key: str = ""

    # ------------------------------------------------------- AI / external APIs
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    fal_key: str = ""

    #: Comma-separated list of AI provider names (e.g., "openrouter,together,deepseek").
    #: Each provider must have corresponding {name}_api_key and {name}_base_url.
    ai_providers: str = ""

    gramjs_api_url: str = "http://gramjs:3000"
    gramjs_token: str = ""

    vk_access_token: str = ""
    vk_group_id: str = ""
    vk_api_version: str = "5.199"

    ok_access_token: str = ""
    ok_application_key: str = ""
    ok_session_secret_key: str = ""
    ok_group_id: str = ""

    dzen_token: str = ""
    dzen_channel_id: str = ""

    telegram_bot_token: str = ""
    telegram_channel_id: str = ""

    # ---------------------------------------------------------------- WordPress
    wp_base_url: str = ""
    wordpress_url: str = "https://svodka.press"
    wp_app_user: str = ""
    wp_app_password: str = ""

    # ---------------------------------------------------------------------- SSH
    ssh_remote_host: str = ""
    ssh_remote_user: str = "root"
    ssh_remote_port: int = Field(default=22, ge=1, le=65535)
    ssh_private_key_path: Path = Path("/app/secrets/id_ed25519")

    # ------------------------------------------------------------- AI defaults
    ai_model_arbiter: str = "openai/gpt-4o-mini"
    ai_model_writer: str = "anthropic/claude-3.5-sonnet"
    ai_model_seo: str = "openai/gpt-4o-mini"
    ai_model_image: str = "fal-ai/flux/schnell"
    #: Kept for the gateway path, which is now only a fallback: embeddings are
    #: produced locally. OmniRoute answers 403/400 for every embedding provider,
    #: which is what motivated moving them on-worker.
    ai_model_embedding: str = "local/fastembed-minilm-l12-v2"
    ai_embedding_dimensions: int = Field(default=384, ge=1)
    ai_max_tokens: int = Field(default=8192, ge=256)
    ai_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    ai_request_timeout: int = Field(default=120, ge=1)

    # --------------------------------------------------------------- ingestion
    #: Upper bound on messages pulled per source per run, so a stalled channel
    #: cannot flood the pipeline when it comes back online.
    ingest_max_messages_per_source: int = Field(default=50, ge=1, le=500)
    #: Messages re-fetched below the stored watermark (``min_id - overlap``) to
    #: recover posts edited or delivered out of order since the previous run.
    ingest_overlap_messages: int = Field(default=10, ge=0, le=100)
    ingest_http_timeout: int = Field(default=30, ge=1)
    ingest_http_retries: int = Field(default=3, ge=0, le=10)
    ingest_mark_read: bool = True
    #: Redis lock lifetime for the ingestion worker. Must comfortably exceed a
    #: normal run so a crashed worker releases the source instead of wedging it.
    ingest_lock_ttl: int = Field(default=600, ge=10)

    # ------------------------------------------------------ dedup / similarity
    # Calibrated against the pinned local model rather than copied from the
    # specification. Measured over 6 paraphrase pairs and 5 unrelated pairs of
    # real Russian news: duplicates score 0.765-0.911, unrelated stories
    # -0.07-0.713. The specification's 0.75/0.92 would send every duplicate to
    # the paid LLM arbiter, defeating the point of a zero-token stage.
    #
    # 0.55 keeps every duplicate out of the "new cluster" branch while still
    # resolving 4 of 5 unrelated pairs for free.
    similarity_lower_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    #: 0.76 catches 6/6 measured duplicates with 0 false merges; 0.78 and above
    #: starts losing genuine duplicates to the arbiter for no accuracy gain.
    similarity_upper_threshold: float = Field(default=0.76, ge=0.0, le=1.0)
    minhash_num_perm: int = Field(default=128, ge=16, le=1024)
    #: 48 hours, per the pipeline specification: a news event older than two
    #: days is a new story rather than a duplicate of the original report.
    dedup_lookback_hours: int = Field(default=48, ge=1)
    #: Where fastembed keeps the downloaded ONNX model. A named volume in
    #: production, so a container rebuild does not re-download 220 MB.
    fastembed_cache_dir: Path = Path("/app/storage/fastembed")
    #: Whether to embed on the worker instead of calling the gateway. On by
    #: default: it is free, offline and not rate limited. Turning it off falls
    #: back to the gateway, which is useful only where a provider exists.
    local_embeddings_enabled: bool = True
    #: Jaccard estimate at or above which level 1 rejects a post outright.
    lsh_duplicate_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    #: Jaccard estimate above which a post is escalated to the semantic and
    #: novelty levels instead of being accepted as unique. Lowered from 0.60
    #: to 0.40 so that posts from different channels about the same event
    #: (Jaccard ≈ 0.30-0.50) reach level-2 semantic matching instead of
    #: being immediately classified as unique, which was the root cause of
    #: the "16 clusters for one cat-explosion story" bug.

    lsh_ambiguous_threshold: float = Field(default=0.40, ge=0.0, le=1.0)

    #: Band count for the banded LSH index; must divide ``minhash_num_perm``.
    lsh_bands: int = Field(default=32, ge=1, le=256)
    dedup_shingle_size: int = Field(default=3, ge=1, le=10)
    dedup_enable_llm_arbiter: bool = True

    # --------------------------------------------------------------- Playwright
    playwright_max_concurrent_pages: int = Field(default=2, ge=1, le=8)
    playwright_navigation_timeout: int = Field(default=45_000, ge=1_000)
    playwright_headless: bool = True
    playwright_block_resources: bool = True

    # ------------------------------------------------------------------- media
    media_storage_path: Path = Path("/app/storage/media")
    tesseract_cmd: Path = Path("/usr/bin/tesseract")
    tesseract_languages: str = "rus+eng"
    ocr_watermark_confidence: int = Field(default=60, ge=0, le=100)

    # ------------------------------------------------------------------- SEO
    yandex_metrika_counter_id: str = ""
    yandex_metrika_dzen_counter_id: str = ""
    yandex_metrika_token: str = ""
    yandex_wordstat_token: str = ""
    yandex_wordstat_api_key: str = ""
    yandex_wordstat_folder_id: str = ""
    gsc_credentials_path: Path = Path("/app/secrets/gsc_service_account.json")
    gsc_site_url: str = ""
    google_analytics_property_id: str = ""
    google_service_account_json: str = ""
    bing_api_key: str = ""
    bing_site_url: str = ""
    indexnow_key: str = ""
    indexnow_key_location: str = ""

    # ----------------------------------------------------------- reverse proxy
    #: Hostnames this application answers to, comma-separated. Behind a reverse
    #: proxy this is what stops Host-header injection: without it a request
    #: carrying `Host: evil.test` would have that value reflected into the
    #: redirect after login, sending the operator's session somewhere else.
    #: Empty disables the check, which suits local dev where the host is
    #: `localhost`, `127.0.0.1` or a container name. Production refuses to boot
    #: without it, and refuses a `*` entry, because either one silently turns the
    #: check off while still looking configured.
    allowed_hosts: str = ""
    #: Networks the `X-Forwarded-*` headers are honoured from. Only a proxy that
    #: terminates TLS may assert `X-Forwarded-Proto: https`; trusting the header
    #: from any source would let a direct caller claim its plain-HTTP request was
    #: secure. `*` is rejected in production by the validator below.
    trusted_proxy_hosts: str = "127.0.0.1"
    #: Origins allowed to make browser cross-origin requests, comma-separated.
    #: Normally empty: the dashboard is server-rendered and same-origin, so it
    #: needs no CORS at all. Enabling it with credentials also forbids `*`.
    cors_allow_origins: str = ""

    @field_validator("wp_base_url", "openrouter_base_url", "gramjs_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _validate_thresholds(self) -> Settings:
        """Enforce ``lower < upper`` regardless of field declaration order.

        A ``field_validator`` on the upper bound only sees the lower bound when
        it was already validated, which silently skips the check if the lower
        value itself failed. Validating on the whole model removes that
        ordering dependency.
        """
        if self.similarity_upper_threshold <= self.similarity_lower_threshold:
            raise ValueError(
                "SIMILARITY_UPPER_THRESHOLD must be greater than "
                "SIMILARITY_LOWER_THRESHOLD "
                f"(got {self.similarity_upper_threshold} <= "
                f"{self.similarity_lower_threshold})"
            )
        return self

    @model_validator(mode="after")
    def _validate_lsh_thresholds(self) -> Settings:
        """Enforce ``ambiguous < duplicate`` for the level 1 LSH gate.

        The two values partition the MinHash similarity range into unique /
        ambiguous / duplicate. If the ambiguous cut-off met or exceeded the
        duplicate cut-off the ambiguous band would be empty, so every near
        duplicate would skip the semantic and novelty levels entirely.
        """
        if self.lsh_ambiguous_threshold >= self.lsh_duplicate_threshold:
            raise ValueError(
                "LSH_AMBIGUOUS_THRESHOLD must be less than "
                "LSH_DUPLICATE_THRESHOLD "
                f"(got {self.lsh_ambiguous_threshold} >= "
                f"{self.lsh_duplicate_threshold})"
            )
        return self

    @model_validator(mode="after")
    def _validate_lsh_bands(self) -> Settings:
        """Require equal-size LSH bands.

        Banded LSH splits the signature into ``lsh_bands`` rows of
        ``minhash_num_perm // lsh_bands`` permutations each. A remainder would
        leave a short trailing band whose hash covers fewer permutations, which
        silently skews the recall of the whole index.
        """
        if self.minhash_num_perm % self.lsh_bands != 0:
            raise ValueError(
                f"MINHASH_NUM_PERM={self.minhash_num_perm} must be divisible by "
                f"LSH_BANDS={self.lsh_bands}: banded LSH requires equal-size "
                "bands "
                f"(remainder {self.minhash_num_perm % self.lsh_bands})"
            )
        return self

    @model_validator(mode="after")
    def _validate_embedding_dimensions(self) -> Settings:
        """Reject an embedding model whose width does not match the schema.

        The ``vector(N)`` column width is fixed by the migration, so a mismatched
        ``AI_EMBEDDING_DIMENSIONS`` must fail at startup rather than at the first
        embedding write. ``app.models.vector`` asserts the reverse direction, so
        the two constants cannot drift apart.
        """
        if self.ai_embedding_dimensions != SCHEMA_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"AI_EMBEDDING_DIMENSIONS={self.ai_embedding_dimensions} does not "
                f"match the schema vector width ({SCHEMA_EMBEDDING_DIMENSIONS}). "
                "Changing it requires an ALTER TABLE and an index rebuild via a "
                "migration."
            )
        return self

    @model_validator(mode="after")
    def _reject_insecure_production_secrets(self) -> Settings:
        """Refuse to boot production with placeholder or weak credentials.

        Development keeps working defaults, but a production deployment that
        forgets ``SECRET_KEY`` must fail loudly instead of silently signing JWTs
        with a value published in this repository.
        """
        if self.app_env != "production":
            return self

        problems: list[str] = []
        if self.secret_key in self.INSECURE_PLACEHOLDERS:
            problems.append("SECRET_KEY is unset or still the example placeholder")
        elif len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        if self.postgres_password in self.INSECURE_PLACEHOLDERS:
            problems.append(
                "POSTGRES_PASSWORD is unset or still the example placeholder"
            )
        # Without a password hash the dashboard cannot authenticate anybody, and
        # the guard would be the only thing standing between the public internet
        # and the endpoints that repoint the pipeline. Failing to boot is the
        # correct outcome.
        if not self.admin_password_hash.strip():
            problems.append(
                "ADMIN_PASSWORD_HASH is unset: generate one with "
                "`python -m app.core.security hash <password>`"
            )
        if not self.fernet_key.strip():
            problems.append(
                "FERNET_KEY is unset: stored API keys would be unencrypted at rest"
            )
        # Honouring `X-Forwarded-Proto` from anywhere lets any direct caller
        # declare its plain-HTTP request to be HTTPS, which is enough to make the
        # app emit `Secure` cookies over cleartext and to forge client IPs in the
        # audit log. Behind a proxy the proxy's address is always knowable.
        if self.trusted_proxy_hosts.strip() == "*":
            problems.append(
                "TRUSTED_PROXY_HOSTS='*' accepts forged X-Forwarded-* headers from "
                "any client: set it to the reverse proxy's address"
            )
        # With credentials enabled, `*` is both rejected by browsers and unsafe.
        if "*" in self.cors_allow_origins_list:
            problems.append(
                "CORS_ALLOW_ORIGINS='*' cannot be combined with cookie "
                "authentication: list the dashboard origin explicitly"
            )
        # An empty allowlist skips `TrustedHostMiddleware` entirely, and a `*`
        # entry makes Starlette set `allow_any` and stop checking. Both leave a
        # forged `Host` free to reach the app, and neither is distinguishable from
        # a working configuration in a deployment log — which is precisely why
        # they must fail the boot rather than be reported as fine.
        if not self.allowed_hosts_list:
            problems.append(
                "ALLOWED_HOSTS is unset: the Host header would not be validated, "
                "so a forged Host is reflected into absolute URLs. List the public "
                "hostname plus 127.0.0.1 and localhost for the health probes"
            )
        elif "*" in self.allowed_hosts_list:
            problems.append(
                "ALLOWED_HOSTS contains '*', which disables Host validation "
                "entirely: list each hostname explicitly"
            )
        elif "127.0.0.1" not in self.allowed_hosts_list:
            # The image's HEALTHCHECK and the deploy script both probe
            # `http://127.0.0.1:8000/health`, which sends `Host: 127.0.0.1`. Omit
            # it and every probe gets a 400, so Docker restarts a container that
            # is serving correctly.
            problems.append(
                "ALLOWED_HOSTS omits 127.0.0.1, so the container healthcheck "
                "would be rejected with 400 and the container marked unhealthy"
            )

        if problems:
            raise ValueError(
                "Refusing to start in production with insecure configuration: "
                + "; ".join(problems)
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @staticmethod
    def _split_csv(value: str) -> tuple[str, ...]:
        """Split a comma-separated setting, dropping blanks."""
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @property
    def allowed_hosts_list(self) -> tuple[str, ...]:
        """Hostnames the app answers to, as a tuple."""
        return self._split_csv(self.allowed_hosts)

    @property
    def trusted_proxy_hosts_list(self) -> list[str] | str:
        """Proxy sources whose ``X-Forwarded-*`` headers are honoured.

        Returns the literal ``"*"`` unchanged, because that is what uvicorn's
        middleware expects for "any source" — a single-element list would be
        read as a host named ``*``. A ``list`` rather than a tuple because that
        is the type ``ProxyHeadersMiddleware`` accepts.
        """
        value = self.trusted_proxy_hosts.strip()
        if value == "*":
            return "*"
        return list(self._split_csv(value))

    @property
    def cors_allow_origins_list(self) -> tuple[str, ...]:
        """Browser origins permitted to send cross-origin requests."""
        return self._split_csv(self.cors_allow_origins)

    @property
    def has_ai_credentials(self) -> bool:
        """Whether OpenRouter is configured.

        Services check this to degrade gracefully — skipping the arbiter, the
        writer or the embedder instead of raising — so a deployment without an
        API key still ingests and deduplicates at zero AI spend.
        """
        return bool(self.openrouter_api_key.strip())

    @property
    def database_url_obj(self) -> URL:
        """Async DSN as a SQLAlchemy :class:`URL`.

        ``URL.create`` keeps credentials as separate components instead of
        interpolating them into a string, so passwords containing ``@``, ``/``,
        ``:``, ``?`` or ``%`` cannot corrupt the DSN or be misparsed as the host.
        """
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async DSN with the password rendered, for engine creation.

        Never log this value: use :attr:`database_url_safe` instead.
        """
        return self.database_url_obj.render_as_string(hide_password=False)

    @property
    def database_url_safe(self) -> str:
        """Async DSN with the password masked, safe for logs and error output."""
        return self.database_url_obj.render_as_string(hide_password=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()


settings: Final[Settings] = get_settings()
