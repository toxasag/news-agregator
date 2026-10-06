"""Remote command executor for the target WordPress host.

The WordPress site this pipeline publishes to runs on a different machine from the
pipeline itself, so a few operations — flushing a cache, reloading nginx, checking
disk before a media upload, dropping in a theme snippet — have to happen over SSH.
This module is that channel, and it is deliberately narrow.

**Public-key authentication only.** No password prompt and no ``ssh-agent``
fallback: a deployment script that can be made to ask for a password is a
deployment script that hangs in CI, and an agent connection would let this process
use credentials it was never given.

**The host key is verified.** ``known_hosts`` is honoured by default, and disabling
it requires an explicit flag whose name says what it costs
(``insecure_skip_host_key_check``). Accepting any host key silently is how a
deployment script ends up handing its commands to whoever answers on port 22.

**Commands are argument lists, not strings.** :meth:`RemoteExecutor.run` accepts a
sequence and quotes each element with :func:`shlex.quote`, so a path containing a
space or a semicolon is data rather than a second command. A raw string form exists
for the cases that genuinely need shell features, and it is named
:meth:`run_shell` so choosing it is a visible decision.

Every method returns a :class:`RemoteResult` rather than raising on a non-zero exit,
because "the command failed" is usually information the caller wants to act on
rather than an exception it wants to propagate. Connection failures do raise: those
mean the channel itself is unusable.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import asyncssh

from app.config import settings

logger: Final[logging.Logger] = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RemoteConnectionError",
    "RemoteExecutor",
    "RemoteResult",
    "RemoteTarget",
    "build_command",
]

#: Per-command wall-clock budget. Long enough for a cache flush or a package
#: install, short enough that a wedged command does not hold a worker forever.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0

#: Connection establishment budget, separate from the command budget: an
#: unreachable host should fail in seconds, not after the command timeout.
CONNECT_TIMEOUT_SECONDS: Final[float] = 20.0

#: Captured output is truncated at this length. A command that prints a whole log
#: file must not be able to exhaust memory on a 4 GB VPS through this channel.
MAX_OUTPUT_CHARS: Final[int] = 64 * 1024


class RemoteConnectionError(RuntimeError):
    """The SSH channel could not be established or authenticated."""


@dataclass(frozen=True, slots=True)
class RemoteTarget:
    """Validated non-secret SSH connection metadata for a future site binding.

    Private key material never enters this value object: the key remains a local
    filesystem reference managed by the deployment/runtime, and database-backed
    encrypted credentials are intentionally deferred to the connections phase.
    """

    host: str
    username: str
    port: int
    private_key_path: Path
    known_hosts_path: Path | None = None
    wordpress_path: str = "/"

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("SSH host must not be empty")
        if not self.username.strip():
            raise ValueError("SSH username must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        path = self.wordpress_path.strip()
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("WordPress path must be an absolute safe path")


@dataclass(frozen=True, slots=True)
class RemoteResult:
    """Outcome of one remote command."""

    command: str
    exit_status: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def raise_for_status(self) -> None:
        """Raise when the command failed.

        Opt-in, for the callers that genuinely cannot continue: the default is to
        return the result so a caller can inspect a non-zero exit that it expects.
        """
        if not self.ok:
            detail = (self.stderr or self.stdout).strip()[:400]
            raise RuntimeError(
                f"remote command failed with exit {self.exit_status}: "
                f"{self.command} :: {detail}"
            )


def build_command(argv: Sequence[str]) -> str:
    """Quote an argument list into a single safe shell command.

    Each element is quoted individually, so a filename containing a space, a
    semicolon or a backtick is passed as one argument rather than being reparsed by
    the remote shell as extra commands. This is the same reasoning that makes
    parameterised SQL the only acceptable way to build a query.
    """
    if not argv:
        raise ValueError("refusing to run an empty command")
    return " ".join(shlex.quote(str(part)) for part in argv)


class RemoteExecutor:
    """Runs commands on the configured remote host over SSH.

    The connection is opened lazily and reused for the lifetime of the instance, so
    a deployment that runs six commands pays for one handshake. Use it as an async
    context manager to guarantee the connection is closed.
    """

    __slots__ = (
        "_connection",
        "_host",
        "_insecure_skip_host_key_check",
        "_key_path",
        "_known_hosts",
        "_lock",
        "_port",
        "_username",
    )

    def __init__(
        self,
        *,
        host: str | None = None,
        username: str | None = None,
        port: int | None = None,
        key_path: Path | None = None,
        known_hosts: Path | None = None,
        insecure_skip_host_key_check: bool = False,
    ) -> None:
        self._host = (settings.ssh_remote_host if host is None else host).strip()
        self._username = (
            settings.ssh_remote_user if username is None else username
        ).strip()
        self._port = settings.ssh_remote_port if port is None else port
        self._key_path = (
            settings.ssh_private_key_path if key_path is None else key_path
        )
        self._known_hosts = known_hosts
        self._insecure_skip_host_key_check = insecure_skip_host_key_check
        self._connection: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_target(cls, target: RemoteTarget) -> Self:
        """Build a transport from validated metadata without opening a connection."""
        return cls(
            host=target.host,
            username=target.username,
            port=target.port,
            key_path=target.private_key_path,
            known_hosts=target.known_hosts_path,
        )

    @property
    def is_configured(self) -> bool:
        """Whether a host, user and readable private key are all available."""
        return bool(self._host and self._username and self._key_path.is_file())

    @property
    def target(self) -> str:
        """A human-readable ``user@host:port`` for logs and error messages."""
        return f"{self._username}@{self._host}:{self._port}"

    @property
    def key_path(self) -> Path:
        """The private key this executor authenticates with.

        Exposed so the CLI can report *why* the executor is unconfigured — a
        missing key and a missing host produce the same "not configured" otherwise.
        """
        return self._key_path

    async def __aenter__(self) -> Self:
        await self._connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the SSH connection, if one is open."""
        async with self._lock:
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.close()
            await connection.wait_closed()

    async def _connect(self) -> asyncssh.SSHClientConnection:
        """Open the connection, or return the existing one."""
        async with self._lock:
            if self._connection is not None:
                return self._connection

            if not self._host:
                raise RemoteConnectionError(
                    "SSH_REMOTE_HOST is not set: remote execution is unavailable"
                )
            if not self._key_path.is_file():
                raise RemoteConnectionError(
                    f"the private key {self._key_path} does not exist. Only "
                    "public-key authentication is supported."
                )

            # `known_hosts=None` disables verification in asyncssh, which is why it
            # is reachable only through the explicitly-named flag.
            known_hosts: object = ()
            if self._insecure_skip_host_key_check:
                logger.warning(
                    "Host key verification is DISABLED for %s; the connection is "
                    "vulnerable to interception",
                    self.target,
                )
                known_hosts = None
            elif self._known_hosts is not None:
                known_hosts = str(self._known_hosts)
            else:
                # An empty tuple means "use the default known_hosts files".
                known_hosts = ()

            try:
                connection = await asyncio.wait_for(
                    asyncssh.connect(
                        self._host,
                        port=self._port,
                        username=self._username,
                        client_keys=[str(self._key_path)],
                        known_hosts=known_hosts,
                    ),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except asyncssh.HostKeyNotVerifiable as exc:
                raise RemoteConnectionError(
                    f"the host key for {self.target} is not in known_hosts. Add it "
                    "with ssh-keyscan, or pass insecure_skip_host_key_check=True if "
                    "you accept the interception risk."
                ) from exc
            except asyncssh.PermissionDenied as exc:
                raise RemoteConnectionError(
                    f"{self.target} rejected the key {self._key_path}"
                ) from exc
            except (TimeoutError, OSError, asyncssh.Error) as exc:
                raise RemoteConnectionError(
                    f"could not connect to {self.target}: {type(exc).__name__}"
                ) from exc

            self._connection = connection
            logger.info("SSH connection established to %s", self.target)
            return connection

    # ASYNC109: `timeout` here is a per-command budget forwarded to
    # `asyncio.wait_for`, not a cancellation scope the caller should own. Callers
    # need to set it per command — a package install and a `df` want very
    # different budgets — so it stays a parameter.
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109
    ) -> RemoteResult:
        """Run one command from an argument list.

        The preferred entry point: each argument is quoted, so a value containing
        shell metacharacters cannot become a second command.
        """
        return await self.run_shell(build_command(argv), timeout=timeout)

    async def run_shell(
        self,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,  # noqa: ASYNC109 - see `run`
    ) -> RemoteResult:
        """Run a raw shell command.

        Named separately from :meth:`run` so that choosing the unquoted form — for a
        pipeline or a redirect — is a deliberate, visible decision rather than the
        default. Never pass interpolated user input here.
        """
        if not command.strip():
            raise ValueError("refusing to run an empty command")
        connection = await self._connect()
        logger.info("Running on %s: %s", self.target, command[:200])
        try:
            completed = await asyncio.wait_for(
                connection.run(command, check=False), timeout=timeout
            )
        except TimeoutError as exc:
            raise RemoteConnectionError(
                f"the remote command timed out after {timeout:.0f}s: {command[:120]}"
            ) from exc
        except asyncssh.Error as exc:
            raise RemoteConnectionError(
                f"the remote command failed to execute: {type(exc).__name__}"
            ) from exc

        return RemoteResult(
            command=command,
            exit_status=int(completed.exit_status or 0),
            stdout=_decode(completed.stdout),
            stderr=_decode(completed.stderr),
        )

    async def upload(self, local: Path, remote: str) -> None:
        """Copy one file to the remote host over SFTP.

        Used for the theme snippet and for configuration fragments. SFTP rather than
        a piped ``cat``: a redirect would need the remote path interpolated into a
        shell command, which is exactly what :meth:`run` exists to avoid.
        """
        # ASYNC240: a single stat on a local path, before any network work. Pulling
        # in an async filesystem layer for it would add a dependency to avoid a
        # microsecond of blocking.
        if not local.is_file():  # noqa: ASYNC240
            raise ValueError(f"{local} is not a readable file")
        connection = await self._connect()
        try:
            async with connection.start_sftp_client() as sftp:
                await sftp.put(str(local), remote)
        except (asyncssh.Error, OSError) as exc:
            raise RemoteConnectionError(
                f"could not upload {local.name} to {self.target}: "
                f"{type(exc).__name__}"
            ) from exc
        logger.info("Uploaded %s to %s:%s", local.name, self.target, remote)

    async def check_disk_space(self, path: str = "/") -> RemoteResult:
        """Report disk usage for ``path``.

        Worth a named method because it is the check that precedes a media upload:
        a WordPress host with a full disk accepts the REST call and then fails to
        write the file, which surfaces much later as a broken image.
        """
        return await self.run(["df", "-h", path])

    async def reload_nginx(self) -> RemoteResult:
        """Test and reload the nginx configuration.

        ``nginx -t`` first: reloading a broken configuration takes the site down,
        and the test is what makes this safe to run unattended. The two are chained
        with ``&&`` in one shell command so the reload cannot run after a failed
        test.
        """
        return await self.run_shell("nginx -t && systemctl reload nginx")


def _decode(stream: object) -> str:
    """Normalise asyncssh output to a bounded string."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        text = stream.decode("utf-8", errors="replace")
    else:
        text = str(stream)
    return text[:MAX_OUTPUT_CHARS]


async def _main(argv: Sequence[str]) -> int:
    """Run one command on the configured host and print its output.

    A thin operator entry point:

        python -m scripts.remote_ssh df -h /
    """
    if not argv:
        sys.stderr.write(
            "usage: python -m scripts.remote_ssh <command> [args...]\n"
            "       Reads SSH_REMOTE_HOST, SSH_REMOTE_USER, SSH_REMOTE_PORT and\n"
            "       SSH_PRIVATE_KEY_PATH from the environment.\n"
        )
        return 2

    executor = RemoteExecutor()
    if not executor.is_configured:
        sys.stderr.write(
            f"SSH is not configured: host={executor.target}, "
            f"key readable={executor.key_path.is_file()}\n"
        )
        return 2

    try:
        async with executor as session:
            result = await session.run(list(argv))
    except RemoteConnectionError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.exit_status


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
