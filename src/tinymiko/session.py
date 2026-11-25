"""Single-shell ownership, command boundaries, paging and native Python interaction."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import replace
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4

import asyncssh

from .channel import Channel, Terminal
from .models import (
    BoundaryError,
    CommandError,
    CommandResult,
    CommandTimeout,
    Event,
    Identity,
    IdentityConflict,
    OutputLimitExceeded,
    Prompt,
)
from .profiles import DEFAULT_PROFILES, Pattern, Profile, identify_login

Logger = logging.Logger | logging.LoggerAdapter
Identifier = Callable[["Session"], Identity | Awaitable[Identity]]
_held_sessions: ContextVar[frozenset[str]] = ContextVar(
    "tinymiko_sessions", default=frozenset()
)
_logger = logging.getLogger("tinymiko")
_logger.addHandler(logging.NullHandler())


class Session:
    """One SSH connection and one persistent shell. Use Session.connect()."""

    def __init__(
        self,
        profile: Profile,
        logger: Logger,
        *,
        max_output: int,
        max_pages: int,
        cleanup_timeout: float,
        log_output: bool,
        on_output: Callable[[str], None] | None,
    ):
        self.profile = profile
        self.logger = logger
        self.session_id = uuid4().hex
        self.identity = Identity()
        self.login_output = ""
        self.idle_output: list[str] = []
        self.prompt_info = Prompt("", "")
        self._max_output = max_output
        self._max_pages = max_pages
        self._cleanup_timeout = cleanup_timeout
        self._log_output = log_output
        self._on_output = on_output
        self._lock = asyncio.Lock()
        self._closed = False
        self._connection: asyncssh.SSHClientConnection | None = None
        self._channel = Channel(profile.encoding, max_output)

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        options: Mapping[str, Any] | asyncssh.SSHClientConnectionOptions,
        *,
        profile: Profile | None = None,
        identify: Identifier | None = None,
        identity: Identity | None = None,
        logger: Logger | None = None,
        login_timeout: float = 30,
        cleanup_timeout: float = 3,
        max_output: int = 8 * 1024 * 1024,
        max_pages: int = 10000,
        log_output: bool = False,
        on_output: Callable[[str], None] | None = None,
    ) -> AsyncGenerator[Session]:
        """Connection options are passed directly to AsyncSSH, including host keys.

        An explicit profile supplies the bootstrap rules and overrides defaults.
        Custom identifiers receive this usable bootstrap session for probing.
        """
        if min(login_timeout, cleanup_timeout, max_output, max_pages) <= 0:
            raise ValueError("Timeouts and resource limits must be positive")
        session = cls(
            profile or Profile(),
            logger if logger is not None else _logger,
            max_output=max_output,
            max_pages=max_pages,
            cleanup_timeout=cleanup_timeout,
            log_output=log_output,
            on_output=on_output,
        )
        try:
            async with asyncio.timeout(login_timeout):
                if isinstance(options, asyncssh.SSHClientConnectionOptions):
                    session._connection = await asyncssh.connect(options=options)
                else:
                    session._connection = await asyncssh.connect(**options)
                await session._connection.create_session(
                    lambda: session._channel,
                    encoding=None,
                    request_pty=True,
                    term_type=session.profile.term_type,
                    term_size=session.profile.term_size,
                )
                await session._login()
                if identify is None:
                    observed = identify_login(session.login_output, session.prompt)
                else:
                    value = identify(session)
                    observed = await value if inspect.isawaitable(value) else value
                if identity is not None:
                    for field in ("vendor", "platform"):
                        explicit, actual = (
                            getattr(identity, field),
                            getattr(observed, field),
                        )
                        if (
                            explicit is not None
                            and actual is not None
                            and explicit != actual
                        ):
                            raise IdentityConflict(
                                f"Explicit {field} conflicts with identification"
                            )
                    observed = Identity(
                        identity.vendor or observed.vendor,
                        identity.platform or observed.platform,
                    )
                session.identity = observed
                selected = profile or DEFAULT_PROFILES.get(observed.vendor or "")
                if selected is None:
                    raise BoundaryError(
                        "No default profile; supply an explicit Profile"
                    )
                session.profile = selected
                session.prompt_info = selected.parse_prompt(
                    session.prompt, session.device_name
                )
            session._log(logging.INFO, "session_open", "SSH shell ready")
            yield session
        finally:
            await session.aclose()

    @property
    def prompt(self) -> str:
        return self.prompt_info.text

    @property
    def device_name(self) -> str:
        return self.prompt_info.device

    @property
    def permission(self) -> str | None:
        return self.prompt_info.permission

    @property
    def configuration_mode(self) -> str | None:
        return self.prompt_info.mode

    @property
    def closed(self) -> bool:
        return self._closed

    def _log(
        self,
        level: int,
        event_type: str,
        message: str,
        command_id: str | None = None,
        **fields: object,
    ) -> None:
        context = dict(
            fields,
            session_id=self.session_id,
            command_id=command_id,
            event_type=event_type,
        )
        if isinstance(self.logger, logging.LoggerAdapter):
            message, kwargs = self.logger.process(message, {"extra": context})
            kwargs["extra"] = {**kwargs.get("extra", {}), **context}
            self.logger.logger.log(level, message, **kwargs)
        else:
            self.logger.log(level, message, extra=context)

    async def _login(self) -> None:
        terminal = Terminal()
        size = 0
        while True:
            text, stderr = await self._channel.read()
            size += len(text.encode(self.profile.encoding))
            if size > self._max_output:
                raise BoundaryError("Login output limit exceeded")
            self.login_output += text
            if stderr:
                continue
            terminal.feed(text)
            match = re.search(self.profile.prompt_pattern, terminal.text)
            if match:
                self.prompt_info = self.profile.parse_prompt(match.group())
                return

    def command(self, command: str, *, timeout: float = 30) -> Command:
        return Command(self, command, timeout)

    async def execute(self, command: str, *, timeout: float = 30) -> CommandResult:
        """Wait only for prompt; do not send pagination or confirmation replies."""
        async with self.command(command, timeout=timeout) as operation:
            while not (await operation._expect({}, pages=False)).done:
                pass
            return operation.result

    async def execute_paged(
        self, command: str, *, timeout: float = 30
    ) -> CommandResult:
        async with self.command(command, timeout=timeout) as operation:
            await operation.read_pages()
            return operation.result

    def _prepare(self) -> None:
        if self._closed:
            raise BoundaryError("Session is closed")
        terminal = Terminal()
        while (item := self._channel.pop()) is not None:
            text, stderr = item
            if stderr:
                self.idle_output.append(text)
            else:
                terminal.feed(text)
        if terminal.text:
            self.idle_output.append(terminal.text)
            match = re.search(self.profile.prompt_pattern, terminal.text)
            if match is None:
                raise BoundaryError(
                    "Idle output left the terminal boundary unconfirmed"
                )
            prompt = self.profile.parse_prompt(match.group(), self.device_name)
            if prompt.device != self.device_name:
                raise BoundaryError("Idle prompt changed device identity")
            self.prompt_info = prompt

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._connection is not None:
            self._connection.close()
            try:
                await asyncio.wait_for(
                    self._connection.wait_closed(), self._cleanup_timeout
                )
            except TimeoutError:
                self._connection.abort()
        self._log(logging.INFO, "session_close", "SSH shell closed")


class Command:
    """Own a session until a prompt is received or the uncertain shell is closed."""

    def __init__(self, session: Session, command: str, timeout: float):
        if not command or any(c in command for c in "\r\n\x00"):
            raise ValueError("A top-level command must be one nonempty line")
        if timeout <= 0:
            raise ValueError("Command timeout must be positive")
        self.session = session
        self.command_id = uuid4().hex
        self.command = command
        self._timeout = timeout
        self._timer: asyncio.Timeout | None = None
        self._token: Token[frozenset[str]] | None = None
        self._owner: asyncio.Task | None = None
        self._terminal = Terminal()
        self._body = ""
        self._stderr = ""
        self._secrets: list[str] = []
        self._secret_offsets: list[int] = []
        self._echo: str | None = None
        self._published = ""
        self._page = False
        self._pages = 0
        self._bytes = 0
        self._started = 0.0
        self._sent = False
        self._finished = False
        self._abandoned = False
        self._prompt_before = ""
        self._result: CommandResult | None = None

    def _redact(
        self,
        text: str,
        *,
        final: bool = False,
        secrets: list[str] | None = None,
    ) -> str:
        secrets = self._secrets if secrets is None else secrets
        if not secrets:
            return text
        pattern = re.compile(
            "|".join(re.escape(s) for s in sorted(secrets, key=len, reverse=True))
        )
        matches = list(pattern.finditer(text))
        # Hold a possible split secret until the next event. On failure, mask it.
        suffix = max(
            (n for s in secrets for n in range(1, len(s)) if text.endswith(s[:n])),
            default=0,
        )
        if matches and matches[-1].end() == len(text):
            suffix = 0
        if suffix:
            return pattern.sub("[REDACTED]", text[:-suffix]) + (
                "[REDACTED]" if final else ""
            )
        return pattern.sub("[REDACTED]", text)

    def _redact_body(self, text: str, *, final: bool = False) -> str:
        # New secrets affect only subsequent output. Rewriting already-published
        # questions (e.g. password "a" in "Password:") would break append-only logs.
        parts = []
        active: list[str] = []
        start = 0
        for offset, secret in zip(self._secret_offsets, self._secrets, strict=True):
            parts.append(self._redact(text[start:offset], final=True, secrets=active))
            active.append(secret)
            start = offset
        parts.append(self._redact(text[start:], final=final, secrets=active))
        return "".join(parts)

    def _pending(self) -> str:
        if self._echo is not None:
            return ""
        text = self._terminal.text
        for pattern in (
            self.session.profile.page_pattern,
            self.session.profile.prompt_pattern,
        ):
            if pattern is not None and (match := re.search(pattern, text)):
                text = text[: match.start()]
        return text

    @property
    def output(self) -> str:
        return self._redact_body(self._body + self._pending(), final=self._finished)

    @property
    def partial_result(self) -> CommandResult:
        if self._result:
            return self._result
        return CommandResult(
            self.command_id,
            self.command,
            self.output,
            self._redact(self._stderr, final=True),
            self._prompt_before,
            None,
            max(0, time.monotonic() - self._started) if self._started else 0,
            "unknown" if self._sent else "interrupted",
            False,
        )

    @property
    def result(self) -> CommandResult:
        if self._result is None or self._result.status != "completed":
            raise RuntimeError("Command has not completed; inspect partial_result")
        return self._result

    async def __aenter__(self) -> Command:
        if self._owner is not None:
            raise RuntimeError("Command contexts cannot be reused")
        if self.session.session_id in _held_sessions.get():
            raise RuntimeError("Nested commands on the same session are forbidden")
        await self.session._lock.acquire()
        self._owner = asyncio.current_task()
        self._token = _held_sessions.set(
            _held_sessions.get() | {self.session.session_id}
        )
        try:
            self.session._prepare()
            self._prompt_before = self.session.prompt
            self._started = time.monotonic()
            self._timer = asyncio.timeout(self._timeout)
            await self._timer.__aenter__()
            self.session._log(
                logging.INFO, "command_start", "Command started", self.command_id
            )
            await self._write(self.command, newline=True)
            return self
        except BaseException as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        timed_out = False
        try:
            if self._timer:
                try:
                    await self._timer.__aexit__(exc_type, exc, tb)
                except TimeoutError:
                    timed_out = True
            if not self._finished:
                # A partial-result snapshot survives cancellation and caller errors.
                self._body += self._pending()
                self._terminal.take(len(self._terminal.text))
                self._finished = True
                self._result = self.partial_result
                await self.session.aclose()
                try:
                    self._publish(final=True)
                except Exception:
                    if exc is None:
                        raise
                    exc.add_note("Output observer failed during command cleanup")
                self.session._log(
                    logging.WARNING,
                    "command_abort",
                    "Command ended without a prompt",
                    self.command_id,
                    status=self._result.status,
                )
        finally:
            if self._token is not None:
                _held_sessions.reset(self._token)
                self._token = None
            self.session._lock.release()
        if timed_out:
            raise CommandTimeout(
                "Command deadline expired", self.partial_result
            ) from None
        return False

    def _check_owner(self) -> None:
        if self._owner is None or asyncio.current_task() is not self._owner:
            raise RuntimeError("Command IO must run in its owning task and context")
        if self._finished:
            raise RuntimeError("Command is already finished")
        if self.session.closed:
            raise BoundaryError("Session is closed")

    async def _write(self, text: str, *, newline: bool) -> None:
        self._sent = True  # From here a remote side effect cannot be ruled out.
        self._echo = text if self.session.profile.echo and text else None
        try:
            await self.session._channel.write(
                text + (self.session.profile.newline if newline else ""),
                self.session.profile.encoding,
            )
        except (OSError, asyncssh.Error, EOFError) as exc:
            await self.session.aclose()
            raise CommandError("SSH shell write failed", self.partial_result) from exc

    async def reply(self, text: str, *, newline: bool, sensitive: bool = False) -> None:
        self._check_owner()
        if sensitive and text:
            self._secrets.append(text)
            self._secret_offsets.append(len(self._body))
        self._page = False
        await self._write(text, newline=newline)
        self.session._log(
            logging.DEBUG, "reply", "Interaction reply sent", self.command_id
        )

    def _strip_echo(self) -> bool:
        if self._echo is None:
            return True
        value = self._terminal.text
        expected = self._echo
        if expected.startswith(value):
            return False
        if value.startswith(expected + "\n"):
            self._terminal.take(len(expected) + 1)
        elif value.startswith(expected) and expected.strip() == "":
            self._terminal.take(len(expected))
        self._echo = None
        return True

    async def _read(self) -> None:
        try:
            text, stderr = await self.session._channel.read()
            self._bytes += len(text.encode(self.session.profile.encoding))
            if self._bytes > self.session._max_output:
                raise OutputLimitExceeded(
                    "Command output limit exceeded", self.partial_result
                )
            if stderr:
                self._stderr += text
            else:
                self._terminal.feed(text)
        except CommandError:
            await self.session.aclose()
            raise
        except (
            OSError,
            asyncssh.Error,
            EOFError,
            UnicodeError,
            BufferError,
            ValueError,
        ) as exc:
            await self.session.aclose()
            raise CommandError("SSH shell read failed", self.partial_result) from exc

    def _publish(self, *, final: bool = False) -> str:
        safe = self._redact_body(self._body, final=final)
        delta = safe[len(self._published) :]
        self._published = safe
        if delta:
            if self.session._log_output:
                self.session._log(logging.DEBUG, "output", delta, self.command_id)
            if self.session._on_output:
                self.session._on_output(delta)
        return delta

    async def expect(self, patterns: Mapping[str, Pattern]) -> Event:
        return await self._expect(patterns, pages=True)

    async def _expect(self, patterns: Mapping[str, Pattern], *, pages: bool) -> Event:
        self._check_owner()
        compiled = [(name, re.compile(pattern)) for name, pattern in patterns.items()]
        while True:
            if self._strip_echo():
                value = self._terminal.text
                candidates: list[
                    tuple[
                        int,
                        int,
                        Literal["match", "page", "prompt"],
                        str | None,
                        re.Match[str],
                    ]
                ] = []
                prompt = re.search(self.session.profile.prompt_pattern, value)
                if prompt:
                    info = self.session.profile.parse_prompt(
                        prompt.group(), self.session.device_name
                    )
                    if info.device == self.session.device_name:
                        candidates.append((prompt.start(), -2, "prompt", None, prompt))
                page_pattern = self.session.profile.page_pattern
                page = (
                    re.search(page_pattern, value) if page_pattern is not None else None
                )
                if pages and page:
                    candidates.append((page.start(), -1, "page", None, page))
                for order, (name, pattern) in enumerate(compiled):
                    match = pattern.search(value)
                    if match:
                        if match.start() == match.end():
                            raise ValueError(
                                "Interaction patterns must not match empty text"
                            )
                        if (
                            prompt
                            and match.start() < prompt.end()
                            and prompt.start() < match.end()
                        ):
                            raise BoundaryError(
                                "Interaction rule overlaps the terminal prompt"
                            )
                        if (
                            page
                            and match.start() < page.end()
                            and page.start() < match.end()
                        ):
                            continue
                        candidates.append((match.start(), order, "match", name, match))
                if candidates:
                    _, _, kind, name, match = min(candidates, key=lambda item: item[:2])
                    consumed = self._terminal.take(match.end(), erase=kind == "page")
                    self._body += (
                        consumed if kind == "match" else consumed[: match.start()]
                    )
                    if kind == "page":
                        self._page = True
                    elif kind == "prompt":
                        self.session.prompt_info = self.session.profile.parse_prompt(
                            match.group(),
                            self.session.device_name,
                        )
                        self._finished = True
                        if self._timer:
                            self._timer.reschedule(None)
                    if self._finished:
                        self._result = replace(
                            self.partial_result,
                            status="completed",
                            prompt_after=self.session.prompt,
                            output_complete=not self._abandoned,
                        )
                    text = self._publish(final=self._finished)
                    if self._result is not None:
                        self.session._log(
                            logging.INFO,
                            "command_end",
                            "Command completed",
                            self.command_id,
                            status="completed",
                            output_complete=not self._abandoned,
                            elapsed_seconds=self._result.elapsed_seconds,
                        )
                    else:
                        self.session._log(
                            logging.DEBUG, kind, "Terminal event", self.command_id
                        )
                    return Event(
                        kind,
                        text,
                        name,
                        tuple(
                            self._redact(v, final=True) if v else v
                            for v in match.groups()
                        )
                        if kind == "match"
                        else (),
                        {
                            k: self._redact(v, final=True) if v else v
                            for k, v in match.groupdict().items()
                        }
                        if kind == "match"
                        else {},
                    )
            await self._read()

    async def _continue_page(self, *, quit_page: bool = False) -> None:
        self._check_owner()
        if not self._page:
            raise RuntimeError("No confirmed pagination boundary")
        if not quit_page:
            self._pages += 1
            if self._pages > self.session._max_pages:
                await self.session.aclose()
                raise OutputLimitExceeded(
                    "Pagination limit exceeded", self.partial_result
                )
        else:
            self._abandoned = True
        # Pager keys generally have no echo and no trailing newline.
        self._page = False
        key = (
            self.session.profile.quit_key
            if quit_page
            else self.session.profile.continue_key
        )
        await self._write(key, newline=False)
        self._echo = None

    async def read_pages(self, expect: Pattern | None = None) -> Event:
        self._check_owner()
        patterns = {"expect": expect} if expect is not None else {}
        texts = []
        while True:
            if self._page:
                await self._continue_page()
            event = await self._expect(patterns, pages=True)
            texts.append(event.text)
            if event.kind != "page":
                return replace(event, text="".join(texts))

    async def next_page(self) -> Event:
        self._check_owner()
        if self._page:
            await self._continue_page()
        return await self._expect({}, pages=True)

    async def quit_paging(self) -> CommandResult:
        self._check_owner()
        if not self._page:
            event = await self._expect({}, pages=True)
            if event.done:
                return self.result
        await self._continue_page(quit_page=True)
        await self._expect({}, pages=False)
        return self.result
