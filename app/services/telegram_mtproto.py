"""Telethon-backed Telegram source reader, driven by the account pool.

The original ingestor talks to a GramJS HTTP bridge. This module speaks MTProto
directly through Telethon, using the pooled user accounts, which is what makes
multi-account rotation and 2FA sign-in possible at all — the bridge exposes
neither.

It deliberately produces the *same*
:class:`~app.services.telegram_ingestor.TelegramMessage` values as the bridge
client. Everything downstream — album collapsing, the zero-token filter, the
dedup ladder, the ``ON CONFLICT`` upsert — therefore stays untouched and keeps its
tests. Only the transport changed.

No database access happens here beyond the pool's own bookkeeping: the worker owns
persistence, exactly as before.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    FloodWaitError,
    SessionRevokedError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    Channel,
    ChannelForbidden,
    Chat,
    ChatForbidden,
    Document,
    DocumentAttributeVideo,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
    User,
)

from app.config import settings
from app.models.enums import TelegramPeerKind
from app.models.source import Source
from app.services.ingest_freshness import (
    is_on_or_after_moscow_today,
    needs_freshness_cutoff,
)
from app.services.telegram_accounts import (
    AccountCredentials,
    NoAccountAvailableError,
    TelegramAccountPool,
)
from app.services.telegram_auth import APP_VERSION, DEVICE_MODEL, SYSTEM_VERSION
from app.services.telegram_ingestor import (
    TELEGRAM_BASE_URL,
    IngestResult,
    MediaItem,
    MediaType,
    PreparedPost,
    TelegramIngestorError,
    TelegramMessage,
    TelegramNotConfiguredError,
    build_post_url,
    group_albums,
    prepare_candidate,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "ChannelDiscovery",
    "DialogDiscovery",
    "MTProtoIngestor",
    "classify_peer",
    "describe_entity",
    "is_broadcast_channel",
    "kind_label",
    "message_to_value",
    "peer_identifier",
    "peer_invite_link",
]

#: Extra messages scanned on a freshness-gated pass so empty service
#: notifications do not eat the 50-post budget before today's keepers appear.
_FRESHNESS_SCAN_MULTIPLIER: Final[int] = 4
_FRESHNESS_SCAN_FLOOR: Final[int] = 50


def _media_from_message(message: Message) -> tuple[MediaItem, ...]:
    """Extract attachment metadata, without downloading anything.

    Ingestion stores metadata only; the bytes are fetched later by the media
    pipeline. A video is reported as ``video`` rather than ``document`` because the
    downstream watermark and cover logic branches on that distinction.
    """
    media = getattr(message, "media", None)
    if media is None:
        return ()

    message_id = int(message.id)
    if isinstance(media, MessageMediaPhoto):
        photo = media.photo
        if not isinstance(photo, Photo):
            return ()
        return (
            MediaItem(
                type="photo",
                original_post_id=message_id,
                media_id=str(photo.id),
                size=None,
            ),
        )

    if isinstance(media, MessageMediaDocument):
        document = media.document
        if not isinstance(document, Document):
            return ()
        kind: MediaType = "document"
        mime = (document.mime_type or "").lower()
        is_video = any(
            isinstance(attribute, DocumentAttributeVideo)
            for attribute in document.attributes
        )
        if is_video or mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("image/"):
            kind = "photo"
        return (
            MediaItem(
                type=kind,
                original_post_id=message_id,
                media_id=str(document.id),
                size=int(document.size) if document.size is not None else None,
            ),
        )

    # Polls, geo, contacts and web previews carry nothing this pipeline can
    # republish, so they are treated as text-only rather than as unknown media.
    return ()


@dataclass(frozen=True, slots=True)
class DialogDiscovery:
    """One dialog from an account's list, of any Telegram peer type.

    The dashboard shows every kind so the operator can open a link and read the
    about-text before marking the row for collection. Ingestion then reads
    whatever is marked, not only broadcast channels.
    """

    telegram_id: int
    identifier: str
    title: str
    username: str | None
    kind: TelegramPeerKind
    description: str
    invite_link: str | None
    participants: int | None
    avatar_data_uri: str | None


@dataclass(frozen=True, slots=True)
class ChannelDiscovery:
    """One broadcast channel found in the account's dialog list.

    Kept as a thin view of :class:`DialogDiscovery` so existing tests and the
    older import path keep compiling. New code should use :class:`DialogDiscovery`.
    """

    telegram_id: int
    identifier: str
    title: str
    username: str | None
    participants: int | None
    avatar_data_uri: str | None

    @classmethod
    def from_dialog(cls, dialog: DialogDiscovery) -> ChannelDiscovery:
        return cls(
            telegram_id=dialog.telegram_id,
            identifier=dialog.identifier,
            title=dialog.title,
            username=dialog.username,
            participants=dialog.participants,
            avatar_data_uri=dialog.avatar_data_uri,
        )


def kind_label(kind: TelegramPeerKind | str | None) -> str:
    """Operator-facing Russian name of a stored peer kind."""
    labels = {
        TelegramPeerKind.CHANNEL: "канал",
        TelegramPeerKind.MEGAGROUP: "супергруппа",
        TelegramPeerKind.GIGAGROUP: "broadcast-группа",
        TelegramPeerKind.GROUP: "группа",
        TelegramPeerKind.BOT: "бот",
        TelegramPeerKind.USER: "личный чат",
    }
    if isinstance(kind, str):
        try:
            kind = TelegramPeerKind(kind)
        except ValueError:
            return kind
    if kind is None:
        return "канал"
    return labels.get(kind, kind.value)


def classify_peer(entity: object) -> TelegramPeerKind:
    """Map a Telethon entity onto the stored dialog kind."""
    if isinstance(entity, User):
        return TelegramPeerKind.BOT if entity.bot else TelegramPeerKind.USER
    if isinstance(entity, Chat | ChatForbidden):
        return TelegramPeerKind.GROUP
    if isinstance(entity, Channel | ChannelForbidden):
        if getattr(entity, "gigagroup", False):
            return TelegramPeerKind.GIGAGROUP
        if getattr(entity, "megagroup", False):
            return TelegramPeerKind.MEGAGROUP
        return TelegramPeerKind.CHANNEL
    raise TelegramIngestorError(
        f"unrecognised Telegram peer {type(entity).__name__}"
    )


def peer_identifier(entity: object) -> str:
    """Stable identifier Telethon can resolve later.

    Public peers keep ``@username``. Private channels/megagroups use the
    ``-100``-prefixed id Telethon accepts. Small groups and users fall back to
    their numeric id, which ``get_entity`` still resolves for a signed-in
    account that already has the peer in its dialog list.
    """
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    telegram_id = int(getattr(entity, "id", 0) or 0)
    kind = classify_peer(entity)
    if kind in {
        TelegramPeerKind.CHANNEL,
        TelegramPeerKind.MEGAGROUP,
        TelegramPeerKind.GIGAGROUP,
    }:
        return f"-100{telegram_id}"
    return str(telegram_id)


def peer_invite_link(entity: object) -> str | None:
    """A URL the operator can open in Telegram.

    Public usernames become ``https://t.me/<name>``.

    A private channel or supergroup has no username, but Telegram resolves
    ``https://t.me/c/<internal id>`` for anyone already subscribed — which the
    collecting account is, or the dialog would not be in its list. That is a
    real link the owner can click, unlike minting an invite (admin-only, and a
    side effect an import must not cause).

    Small legacy groups have no ``/c/`` form, and a user without a username only
    resolves through ``tg://user?id=``.
    """
    username = getattr(entity, "username", None)
    if username:
        return f"{TELEGRAM_BASE_URL}/{username}"
    if isinstance(entity, User):
        return f"tg://user?id={int(entity.id)}"
    if isinstance(entity, Channel | ChannelForbidden):
        internal_id = int(getattr(entity, "id", 0) or 0)
        if internal_id:
            return f"{TELEGRAM_BASE_URL}/c/{internal_id}"
    return None


def peer_title(entity: object, identifier: str) -> str:
    """Human-readable name, never empty."""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or identifier


def describe_entity(entity: object) -> str:
    """Name the kind of Telegram peer ``entity`` is, for an error message."""
    labels = {
        TelegramPeerKind.BOT: "бот",
        TelegramPeerKind.USER: "личный чат",
        TelegramPeerKind.GROUP: "группа",
        TelegramPeerKind.GIGAGROUP: "broadcast-группа",
        TelegramPeerKind.MEGAGROUP: "супергруппа",
        TelegramPeerKind.CHANNEL: "канал",
    }
    try:
        return labels[classify_peer(entity)]
    except TelegramIngestorError:
        return type(entity).__name__


def is_broadcast_channel(entity: object) -> bool:
    """Whether ``entity`` is a broadcast channel.

    Kept for the tests and for any caller that still wants the old filter.
    Ingestion no longer uses this as a hard gate: the operator's checkbox is
    the filter. A gigagroup is still not a channel — it reports
    ``broadcast=True`` while carrying group traffic.
    """
    try:
        return classify_peer(entity) is TelegramPeerKind.CHANNEL
    except TelegramIngestorError:
        return False


def message_to_value(message: Message, channel: str) -> TelegramMessage | None:
    """Convert a Telethon message into the pipeline's value object.

    Returns ``None`` for a message with neither text nor media — a service
    notification such as "channel photo changed" — which has nothing to publish.
    """
    text = (message.message or "").strip()
    media = _media_from_message(message)
    if not text and not media:
        return None

    grouped = getattr(message, "grouped_id", None)
    return TelegramMessage(
        id=int(message.id),
        channel=channel,
        text=text,
        # Telethon models entities as objects; the pipeline only needs the offsets
        # and types, so they are flattened into the same mapping shape the bridge
        # produced.
        entities=tuple(
            {
                "type": type(entity).__name__,
                "offset": int(getattr(entity, "offset", 0)),
                "length": int(getattr(entity, "length", 0)),
                "url": getattr(entity, "url", None),
            }
            for entity in (message.entities or [])
        ),
        grouped_id=str(grouped) if grouped is not None else None,
        date=message.date,
        media=media,
        views=getattr(message, "views", None),
        forwards=getattr(message, "forwards", None),
        post_url=build_post_url(channel, int(message.id)),
    )


def _coerce_channel_target(channel: str) -> int | str:
    """Parse numeric channel IDs into integers so Telethon treats them as IDs."""
    clean = channel.strip()
    if (clean.startswith("-") and clean[1:].isdigit()) or clean.isdigit():
        try:
            return int(clean)
        except ValueError:
            return clean
    return clean


class MTProtoIngestor:
    """Reads Telegram sources through a pooled user account.

    One instance serves one ingestion pass: it claims an account, opens a client,
    reads every requested source through it, and reports the outcome back to the
    pool so rotation and flood-wait accounting stay accurate.

    Use as an async context manager; the client must be disconnected or the
    process leaves an MTProto connection open.
    """

    __slots__ = ("_about_exhausted", "_account", "_client", "_pool", "_session")

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._pool = TelegramAccountPool(session)
        self._account: AccountCredentials | None = None
        self._client: TelegramClient | None = None
        self._about_exhausted = False

    async def __aenter__(self) -> MTProtoIngestor:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def account(self) -> AccountCredentials | None:
        """The claimed account, or ``None`` before :meth:`start`."""
        return self._account

    async def start(self) -> None:
        """Claim an account and connect.

        Raises :class:`~app.services.telegram_ingestor.TelegramNotConfiguredError`
        when the pool is empty, which callers already treat as "skip Telegram
        sources" rather than as a failed run — a website-only deployment is valid.
        """
        try:
            account = await self._pool.acquire()
        except NoAccountAvailableError as error:
            raise TelegramNotConfiguredError(str(error)) from error
        await self.start_with(account)

    async def start_with(self, account: AccountCredentials) -> None:
        """Connect as a specific account, without claiming the LRU slot again.

        The sources-page import walks every healthy account so a channel that
        only one of them is subscribed to still appears. ``acquire`` would
        stamp ``last_used_at`` on the first and hide the rest.
        """
        client = TelegramClient(
            StringSession(account.session_string),
            account.api_id,
            account.api_hash,
            device_model=DEVICE_MODEL,
            system_version=SYSTEM_VERSION,
            app_version=APP_VERSION,
            connection_retries=2,
            timeout=30,
        )
        try:
            await client.connect()
            authorised = await client.is_user_authorized()
        except OSError as error:
            await self._pool.record_failure(
                account.account_id, error=f"connect failed: {type(error).__name__}"
            )
            raise TelegramIngestorError(
                f"could not connect to Telegram as {account.display_name}: "
                f"{type(error).__name__}"
            ) from error

        if not authorised:
            # The stored session was revoked — usually the account's owner ended
            # the session from another device. Counting it as a failure is right:
            # retrying cannot fix it, and three of these deactivate the account so
            # the pool stops handing out a dead credential.
            await client.disconnect()
            await self._pool.record_failure(
                account.account_id, error="session is no longer authorised"
            )
            raise TelegramIngestorError(
                f"the stored session for {account.display_name} is no longer "
                "authorised; re-run scripts/auth_telegram.py for it"
            )

        self._account = account
        self._client = client
        logger.info("MTProto ingestor connected as %s", account.display_name)

    async def close(self) -> None:
        """Disconnect the client, if one is open."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        # Teardown must not mask an error already propagating from the pass.
        except Exception:
            logger.debug("MTProto disconnect failed", exc_info=True)

    async def _resolve_peer(self, channel: str) -> object:
        """Resolve ``channel`` to any dialog this account can already see.

        Split out of :meth:`fetch_new_messages` so resolution failures and
        reading failures stay separately readable. The operator's checkbox is
        the filter: a marked group or bot is read the same way as a channel.
        """
        if self._client is None:  # pragma: no cover - guarded by the caller
            raise TelegramIngestorError("start() must be called before fetching")
        target = _coerce_channel_target(channel)
        try:
            entity = await self._client.get_entity(target)
        except (UsernameInvalidError, UsernameNotOccupiedError) as error:
            raise TelegramIngestorError(
                f"Telegram has no peer named {channel}"
            ) from error
        except ChannelPrivateError as error:
            raise TelegramIngestorError(
                f"{channel} is private or this account was banned from it"
            ) from error
        except FloodWaitError as error:
            await self._penalise_flood_wait(error)
            raise TelegramIngestorError(
                f"flood wait for {error.seconds}s while resolving {channel}"
            ) from error
        except ValueError as error:
            # If entity was not cached yet (e.g. numeric ID on fresh StringSession),
            # warm the dialogs cache once and retry resolution.
            try:
                await self._client.get_dialogs()
                entity = await self._client.get_entity(target)
            except Exception:  # noqa: BLE001
                # Telethon raises a bare ValueError for an unresolvable identifier.
                raise TelegramIngestorError(
                    f"could not resolve {channel}: {error}"
                ) from error
        return entity

    async def fetch_new_messages(
        self,
        channel: str,
        *,
        last_max_id: int | None,
        last_scraped_at: datetime | None = None,
        now: datetime | None = None,
    ) -> list[TelegramMessage]:
        """Read the new posts of ``channel``.

        Two window modes, plus a date gate the owner asked for:

        **First pass** (``last_max_id is None``) walks recent posts newest-first.
        **Later passes** set ``min_id`` from the stored watermark minus overlap.

        **Freshness.** On a first pass, and after downtime that crossed Moscow
        midnight, posts older than today 00:00 ``Europe/Moscow`` are dropped
        *before* the numeric cap. Yesterday is never used to fill the 50-post
        budget. Same-day later passes skip the date gate.

        Any marked peer is read — channel, group, chat or bot. The operator's
        checkbox is the filter; this method no longer rejects non-channels.
        """
        if self._client is None:
            raise TelegramIngestorError("start() must be called before fetching")

        moment = now or datetime.now(UTC)
        apply_cutoff = needs_freshness_cutoff(last_scraped_at, now=moment)
        keep_limit = settings.ingest_max_messages_per_source
        scan_limit = keep_limit
        if apply_cutoff:
            scan_limit = max(
                keep_limit * _FRESHNESS_SCAN_MULTIPLIER, _FRESHNESS_SCAN_FLOOR
            )

        if last_max_id is None:
            min_id = 0
        else:
            min_id = max(last_max_id - settings.ingest_overlap_messages, 0)

        entity = await self._resolve_peer(channel)

        collected: list[TelegramMessage] = []
        try:
            async for message in self._client.iter_messages(
                entity, limit=scan_limit, min_id=min_id
            ):
                if not isinstance(message, Message):
                    continue
                value = message_to_value(message, channel)
                if value is None:
                    continue
                if apply_cutoff and not is_on_or_after_moscow_today(
                    value.date, now=moment
                ):
                    continue
                collected.append(value)
                if len(collected) >= keep_limit:
                    break
        except FloodWaitError as error:
            await self._penalise_flood_wait(error)
            raise TelegramIngestorError(
                f"flood wait for {error.seconds}s while reading {channel}"
            ) from error
        except (AuthKeyUnregisteredError, SessionRevokedError) as error:
            if self._account is not None:
                await self._pool.record_failure(
                    self._account.account_id, error="session revoked by Telegram"
                )
            raise TelegramIngestorError(
                "Telegram revoked this session; re-authorise the account"
            ) from error

        # iter_messages yields newest first; the pipeline's watermark logic reads
        # more naturally oldest-first, and album grouping assumes ascending ids.
        collected.reverse()
        logger.info(
            "Read %d message(s) from %s as %s",
            len(collected),
            channel,
            self._account.display_name if self._account else "?",
        )
        return collected

    async def _penalise_flood_wait(self, error: FloodWaitError) -> None:
        """Park the current account for the duration Telegram demanded."""
        if self._account is None:
            return
        await self._pool.record_flood_wait(
            self._account.account_id, float(error.seconds)
        )

    async def ingest_source(
        self, source: Source, *, last_max_id: int | None = None
    ) -> IngestResult:
        """Collect one source, mirroring the bridge client's contract.

        Failures are returned inside :class:`IngestResult` rather than raised, so
        one dead or private channel does not halt a run over dozens of sources.
        """
        identifier = source.identifier.strip()
        try:
            messages = await self.fetch_new_messages(
                identifier,
                last_max_id=last_max_id,
                last_scraped_at=source.last_scraped_at,
            )
        except TelegramIngestorError as error:
            logger.warning("Telegram source %s failed: %s", identifier, error)
            return IngestResult(
                source_id=source.id,
                candidates=(),
                fetched_messages=0,
                albums_collapsed=0,
                max_message_id=None,
                errors=(str(error),),
            )

        candidates = group_albums(messages)
        albums_collapsed = sum(
            1 for candidate in candidates if len(candidate.message_ids) > 1
        )
        prepared: list[PreparedPost] = []
        errors: list[str] = []
        for candidate in candidates:
            try:
                prepared.append(
                    prepare_candidate(candidate, skip_media=source.skip_media)
                )
            # Deliberately broad: one unparsable post must not cost the rest
            # of the channel, and the filter can raise anything.
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "Could not prepare %s from %s", candidate.external_post_id, identifier
                )
                errors.append(f"{candidate.external_post_id}: {error}")

        max_message_id = max((message.id for message in messages), default=None)
        return IngestResult(
            source_id=source.id,
            candidates=tuple(prepared),
            fetched_messages=len(messages),
            albums_collapsed=albums_collapsed,
            max_message_id=max_message_id,
            errors=tuple(errors),
        )

    async def describe_peer(self, identifier: str) -> DialogDiscovery | None:
        """Resolve one peer by identifier and describe it like a dialog.

        A source can be perfectly collectable without appearing in
        ``iter_dialogs``: Telegram lets a user account read a **public** channel
        it never joined, and rows added by hand or seeded from configuration are
        exactly that case. Without this call they keep no avatar, no description
        and no peer kind, and the panel shows them as second-class rows even
        though ingestion reads them fine.

        Returns ``None`` when the peer cannot be resolved, so one dead
        identifier does not fail a refresh covering dozens of rows.
        """
        if self._client is None:
            raise TelegramIngestorError("start() must be called before fetching")
        try:
            entity = await self._resolve_peer(identifier)
            kind = classify_peer(entity)
        except TelegramIngestorError as error:
            logger.info("Could not describe %s: %s", identifier, error)
            return None

        resolved = peer_identifier(entity)
        return DialogDiscovery(
            telegram_id=int(getattr(entity, "id", 0) or 0),
            identifier=resolved,
            title=peer_title(entity, resolved),
            username=getattr(entity, "username", None),
            kind=kind,
            description=await self._about_text(entity),
            invite_link=peer_invite_link(entity),
            participants=getattr(entity, "participants_count", None),
            avatar_data_uri=await self._avatar(entity),
        )

    async def list_dialogs(self) -> list[DialogDiscovery]:
        """Enumerate every dialog the signed-in account can see.

        Backs the sources-page import. Groups, chats and bots are included so
        the operator can inspect them and mark collection explicitly. Avatars
        come back as small data URIs: Telegram CDN links expire.
        """
        if self._client is None:
            raise TelegramIngestorError("start() must be called before fetching")

        discovered: list[DialogDiscovery] = []
        seen_ids: set[int] = set()

        async for dialog in self._client.iter_dialogs():
            entity = dialog.entity
            try:
                kind = classify_peer(entity)
            except TelegramIngestorError:
                continue
            telegram_id = int(getattr(entity, "id", 0) or 0)
            if not telegram_id or telegram_id in seen_ids:
                continue
            seen_ids.add(telegram_id)
            identifier = peer_identifier(entity)
            discovered.append(
                DialogDiscovery(
                    telegram_id=telegram_id,
                    identifier=identifier,
                    title=peer_title(entity, identifier),
                    username=getattr(entity, "username", None),
                    kind=kind,
                    description=await self._about_text(entity),
                    invite_link=peer_invite_link(entity),
                    participants=getattr(entity, "participants_count", None),
                    avatar_data_uri=await self._avatar(entity),
                )
            )

        discovered.sort(key=lambda item: (item.kind.value, item.title.lower()))
        logger.info("Discovered %d dialog(s)", len(discovered))
        return discovered

    async def list_channels(self) -> list[ChannelDiscovery]:
        """Broadcast-only view of :meth:`list_dialogs`, kept for older callers."""
        dialogs = await self.list_dialogs()
        channels = [
            ChannelDiscovery.from_dialog(dialog)
            for dialog in dialogs
            if dialog.kind is TelegramPeerKind.CHANNEL
        ]
        logger.info("Discovered %d broadcast channels", len(channels))
        return channels

    async def _about_text(self, entity: object) -> str:
        """Best-effort about/bio. Empty on any failure — never aborts import.

        ``GetFull*`` is one extra RPC per dialog. After the first flood wait we
        stop asking, so a hundred-dialog import does not park the account; rows
        still import, just without the about-text.
        """
        if self._client is None or self._about_exhausted:
            return ""
        try:
            if isinstance(entity, Channel | ChannelForbidden):
                full = await self._client(GetFullChannelRequest(entity))
                return str(getattr(full.full_chat, "about", "") or "")
            if isinstance(entity, Chat | ChatForbidden):
                full = await self._client(GetFullChatRequest(int(entity.id)))
                return str(getattr(full.full_chat, "about", "") or "")
            if isinstance(entity, User):
                full = await self._client(GetFullUserRequest(entity))
                return str(getattr(full.full_user, "about", "") or "")
        except FloodWaitError:
            self._about_exhausted = True
            logger.info("Stopped fetching about-texts after a flood wait")
        except Exception:
            logger.debug("About-text fetch failed", exc_info=True)
        return ""

    async def _avatar(self, entity: object) -> str | None:
        """Download the small profile photo as a data URI.

        ``download_profile_photo`` is the correct call for a *peer*:
        ``download_media`` expects a message and quietly returns ``None`` for an
        entity, which is why every avatar came back empty. ``download_big=False``
        asks for the small variant, so a hundred-dialog import stays quick.

        Returns ``None`` on any failure: a missing avatar degrades to the initial
        letter placeholder, and a failure here must not abort the whole import.
        """
        if self._client is None:
            return None
        try:
            if getattr(entity, "photo", None) is None:
                return None
            data = await self._client.download_profile_photo(
                entity, file=bytes, download_big=False
            )
            if not isinstance(data, bytes) or not data:
                return None
            encoded = base64.b64encode(data).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
        except Exception:
            logger.debug("Avatar fetch failed", exc_info=True)
            return None

    async def download_photo(
        self, channel: str, message_id: int, *, limit_bytes: int
    ) -> bytes:
        """Download one message's photo, for the watermark check.

        Ingestion stores media *metadata* only, because the bytes are large and
        most posts never become an article. The watermark verdict, however, cannot
        be reached from metadata: it needs the pixels. This is the one place that
        fetches them for inspection, and it is deliberately per-message rather than
        per-channel so the caller can bound how many images an inspection run pulls.

        Returns ``b""`` for every failure and for anything larger than
        ``limit_bytes``: an unreadable image is a media verdict ("cannot be
        checked"), not a failed run. A size ceiling is applied because a channel
        can attach a 40 MB photo and the container's memory is shared with
        Chromium.
        """
        if self._client is None:
            raise TelegramIngestorError("start() must be called before fetching")

        try:
            entity = await self._resolve_peer(channel)
            message = await self._client.get_messages(entity, ids=message_id)
        except TelegramIngestorError:
            raise
        except FloodWaitError as error:
            await self._penalise_flood_wait(error)
            raise TelegramIngestorError(
                f"flood wait for {error.seconds}s while fetching media from {channel}"
            ) from error
        # Deliberately broad: one unreadable attachment must not abort an
        # inspection run over dozens of channels.
        except Exception:
            logger.info(
                "Could not fetch message %d from %s", message_id, channel, exc_info=True
            )
            return b""

        if not isinstance(message, Message) or message.media is None:
            return b""

        media = message.media
        if isinstance(media, MessageMediaDocument):
            document = media.document
            size = getattr(document, "size", None)
            if isinstance(size, int) and size > limit_bytes:
                logger.info(
                    "Skipping %d-byte attachment from %s (ceiling %d)",
                    size,
                    channel,
                    limit_bytes,
                )
                return b""

        try:
            data = await self._client.download_media(message, file=bytes)
        except FloodWaitError as error:
            await self._penalise_flood_wait(error)
            raise TelegramIngestorError(
                f"flood wait for {error.seconds}s while downloading from {channel}"
            ) from error
        except Exception:
            logger.info("Media download failed for %s", channel, exc_info=True)
            return b""

        if not isinstance(data, bytes) or len(data) > limit_bytes:
            return b""
        return data

    async def download_video(
        self, channel: str, message_id: int, *, limit_bytes: int
    ) -> bytes:
        """Download one message's video document for VK upload.

        Identical flow to :meth:`download_photo` but targeted at
        ``MessageMediaDocument`` (mp4/video).  Photo attachments are skipped:
        if an operator clicks "Upload to VK" on a video card that turned out to
        be a photo the call returns ``b""`` rather than silently uploading the
        wrong thing.

        The ``limit_bytes`` ceiling is enforced both before the transfer (via
        the ``document.size`` attribute) and after (on the downloaded bytes),
        so a server-side size mismatch still cannot push an oversized payload
        into the VK upload pipeline.

        Returns ``b""`` for every failure including FloodWait (which is
        re-raised as :class:`TelegramIngestorError` so the caller can surface
        it to the operator without crashing the dashboard).
        """
        if self._client is None:
            raise TelegramIngestorError("start() must be called before fetching")

        try:
            entity = await self._resolve_peer(channel)
            message = await self._client.get_messages(entity, ids=message_id)
        except TelegramIngestorError:
            raise
        except FloodWaitError as error:
            await self._penalise_flood_wait(error)
            raise TelegramIngestorError(
                f"flood wait for {error.seconds}s while fetching video from {channel}"
            ) from error
        except Exception:
            logger.info(
                "Could not fetch message %d from %s for video download",
                message_id,
                channel,
                exc_info=True,
            )
            return b""

        # Only proceed for document-type media (video, mp4).
        if not isinstance(message, Message) or not isinstance(
            getattr(message, "media", None), MessageMediaDocument
        ):
            return b""

        document = message.media.document
        size = getattr(document, "size", None)
        if isinstance(size, int) and size > limit_bytes:
            logger.info(
                "Skipping %d-byte video from %s (ceiling %d)",
                size,
                channel,
                limit_bytes,
            )
            return b""

        try:
            data = await self._client.download_media(message, file=bytes)
        except FloodWaitError as error:
            await self._penalise_flood_wait(error)
            raise TelegramIngestorError(
                f"flood wait for {error.seconds}s while downloading video from {channel}"
            ) from error
        except Exception:
            logger.info(
                "Video download failed for %s message %d", channel, message_id,
                exc_info=True,
            )
            return b""

        return data if isinstance(data, bytes) and len(data) <= limit_bytes else b""

    async def mark_read(self, channel: str, max_id: int) -> bool:
        """Mark ``channel`` read up to ``max_id``. Fire-and-forget by contract.

        Called by the worker **after** the batch is committed, never before. The
        order matters: this is the only signal that says "these posts are handled",
        so marking read first and then failing the commit would leave posts read in
        Telegram but absent from the database — invisible to an operator scanning
        the channel for what the pipeline missed.

        Every failure is swallowed into ``False`` plus a warning. Marking read is
        cosmetic for the reader account and must never fail a collection pass whose
        rows are already safely committed. Skipped entirely when
        ``INGEST_MARK_READ`` is off.
        """
        if not settings.ingest_mark_read:
            logger.debug("INGEST_MARK_READ is off; leaving %s unread", channel)
            return False
        if self._client is None:
            logger.warning("Cannot mark %s read: no client is connected", channel)
            return False

        target = _coerce_channel_target(channel)
        try:
            entity = await self._client.get_entity(target)
            await self._client.send_read_acknowledge(entity, max_id=max_id)
        except FloodWaitError as error:
            # Not penalised in the pool: the reading itself succeeded, and taking
            # the account out of rotation over a cosmetic call would cost a whole
            # collection slot for no benefit.
            logger.warning(
                "Flood wait (%ds) while marking %s read up to %d",
                error.seconds,
                channel,
                max_id,
            )
            return False
        # Deliberately broad: cosmetic, and must never propagate into a pass whose
        # rows are already committed.
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Could not mark %s read up to %d: %s",
                channel,
                max_id,
                type(error).__name__,
            )
            return False

        logger.info("Marked %s read up to message %d", channel, max_id)
        return True

    async def report_success(self) -> None:
        """Clear the account's failure counter after a clean pass."""
        if self._account is not None:
            await self._pool.record_success(self._account.account_id)

