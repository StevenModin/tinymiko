"""Bounded SSH receive queue and incremental terminal text normalization."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque

import asyncssh


class Channel(asyncssh.SSHClientSession[bytes]):
    """Callbacks only enqueue; the active operation is the only consumer."""

    def __init__(self, encoding: str, max_pending_bytes: int):
        self._decoders = {
            False: codecs.getincrementaldecoder(encoding)(),
            True: codecs.getincrementaldecoder(encoding)(),
        }
        self._queue: deque[tuple[bytes, bool]] = deque()
        self._available = asyncio.Event()
        self._writable = asyncio.Event()
        self._writable.set()
        self._pending = 0
        self._limit = max_pending_bytes
        self._error: Exception | None = None
        self._eof = False
        self._finalized = False
        self._tails: deque[tuple[str, bool]] = deque()
        self.transport: asyncssh.SSHClientChannel[bytes] | None = None

    def connection_made(self, chan: asyncssh.SSHClientChannel[bytes]) -> None:
        self.transport = chan

    def data_received(self, data: bytes, datatype: int | None) -> None:
        if self._error:
            return
        if self._pending + len(data) > self._limit:
            self._error = BufferError("SSH receive buffer limit exceeded")
            if self.transport:
                self.transport.close()
        else:
            self._queue.append((data, datatype is not None))
            self._pending += len(data)
        self._available.set()

    def eof_received(self) -> bool:
        self._eof = True
        self._available.set()
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        self._error = self._error or exc
        self._eof = True
        self._available.set()
        self._writable.set()

    def pause_writing(self) -> None:
        self._writable.clear()

    def resume_writing(self) -> None:
        self._writable.set()

    def pop(self) -> tuple[str, bool] | None:
        if self._queue:
            data, stderr = self._queue.popleft()
            self._pending -= len(data)
            return self._decoders[stderr].decode(data), stderr
        if self._error:
            raise self._error
        if self._tails:
            return self._tails.popleft()
        if self._eof:
            if not self._finalized:
                self._finalized = True
                # Flush both decoders so an incomplete multibyte sequence fails.
                for stderr, decoder in self._decoders.items():
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        self._tails.append((tail, stderr))
                if self._tails:
                    return self._tails.popleft()
            raise EOFError("SSH shell closed before a terminal boundary")
        return None

    async def read(self) -> tuple[str, bool]:
        while True:
            item = self.pop()
            if item is not None:
                return item
            self._available.clear()
            await self._available.wait()

    async def write(self, data: str, encoding: str) -> None:
        if self.transport is None or self.transport.is_closing():
            raise EOFError("SSH shell is closed")
        self.transport.write(data.encode(encoding))
        await self._writable.wait()
        if self.transport.is_closing():
            raise EOFError("SSH shell closed during write")


class Terminal:
    """Keep cursor cells after consumption so pager erasure cannot eat body text.

    Completed lines are stable. The current line remains editable until an event
    consumes it; consumed cells stay hidden but retain their terminal columns.
    This handles CR/LF, backspace, SGR, erase-line and horizontal cursor movement.
    Full-screen terminal applications are deliberately outside this core.
    """

    def __init__(self) -> None:
        self._ready = ""
        self._lines: list[str] = []
        self._line: list[tuple[str, bool]] = []
        self._cursor = 0
        self._escape = ""
        self._pager_cleanup = False
        self._pager_erasing = False
        self._erase_spaces = False

    @property
    def text(self) -> str:
        if self._lines:
            self._ready += "".join(self._lines)
            self._lines.clear()
        return self._ready + "".join(char for char, visible in self._line if visible)

    def take(self, count: int, *, erase: bool = False) -> str:
        value = self.text[:count]
        stable = min(count, len(self._ready))
        self._ready = self._ready[stable:]
        remaining = count - stable
        if remaining and erase:
            self._pager_cleanup = True
            self._pager_erasing = False
            self._erase_spaces = False
        for index, (char, visible) in enumerate(self._line):
            if remaining == 0:
                break
            if visible:
                self._line[index] = char, False
                remaining -= 1
        return value

    def feed(self, text: str) -> None:
        for char in text:
            if self._escape:
                self._escape += char
                if self._escape.startswith("\x1b]"):
                    if char == "\a" or self._escape.endswith("\x1b\\"):
                        self._escape = ""
                elif self._escape == "\x1b[":
                    continue
                elif len(self._escape) > 2 and "@" <= char <= "~":
                    self._control(self._escape[2:-1], char)
                    self._escape = ""
                elif len(self._escape) == 2 and char not in "[]":
                    self._escape = ""
                if len(self._escape) > 4096:
                    raise ValueError("Terminal escape sequence exceeds limit")
            elif char == "\x1b":
                self._escape = char
            elif char == "\r":
                self._cursor_moved()
                self._cursor = 0
            elif char == "\n":
                visible = "".join(c for c, show in self._line if show)
                if visible or not self._line:
                    self._lines.append(visible + "\n")
                self._line.clear()
                self._cursor = 0
                self._pager_cleanup = False
                self._pager_erasing = False
                self._erase_spaces = False
            elif char == "\b":
                self._cursor_moved()
                self._cursor = max(0, self._cursor - 1)
            elif char == "\t" or char >= " ":
                if char != " " and self._pager_cleanup:
                    if self._erase_spaces:
                        self._finish_pager_cleanup()
                    self._pager_cleanup = False
                erasing = char == " " and self._pager_cleanup and self._pager_erasing
                while len(self._line) <= self._cursor:
                    self._line.append((" ", False))
                # Huawei clears a wider span than its visible pager (e.g. 42
                # columns for a 16-column marker). None of this padding is body.
                self._line[self._cursor] = char, not erasing
                self._erase_spaces = self._erase_spaces or erasing
                self._cursor += 1

    def _finish_pager_cleanup(self) -> None:
        self._line.clear()
        self._cursor = 0
        self._pager_cleanup = False
        self._pager_erasing = False
        self._erase_spaces = False

    def _cursor_moved(self) -> None:
        if self._pager_cleanup:
            if self._erase_spaces:
                self._finish_pager_cleanup()
            else:
                self._pager_erasing = True

    def _control(self, params: str, final: str) -> None:
        number = int(params) if params.isdigit() else 0
        if final in ("D", "G"):
            self._cursor_moved()
        if final == "K" and self._pager_cleanup:
            self._finish_pager_cleanup()
        if final == "K":
            if number == 2:
                self._line.clear()
            elif number == 1:
                for index in range(min(self._cursor + 1, len(self._line))):
                    self._line[index] = " ", False
            else:
                del self._line[self._cursor :]
        elif final == "D":
            self._cursor = max(0, self._cursor - (number or 1))
        elif final == "C":
            self._cursor += number or 1
        elif final == "G":
            self._cursor = max(0, (number or 1) - 1)
        if self._cursor > 65536:
            raise ValueError("Terminal cursor exceeds supported line width")
