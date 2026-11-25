"""Public values shared by sessions and command contexts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class Identity:
    """Network vendor/model or Linux distribution/release; never a kernel version."""

    vendor: str | None = None
    platform: str | None = None


@dataclass(frozen=True, slots=True)
class Prompt:
    text: str
    device: str
    permission: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    kind: Literal["match", "page", "prompt"]
    text: str
    name: str | None = None
    groups: tuple[str | None, ...] = ()
    fields: dict[str, str | None] = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return self.kind == "prompt"


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    command: str
    output: str
    stderr: str
    prompt_before: str
    prompt_after: str | None
    elapsed_seconds: float
    status: Literal["completed", "interrupted", "unknown"]
    output_complete: bool


class TinyMikoError(Exception):
    """Base exception for terminal and configuration failures."""


class BoundaryError(TinyMikoError):
    """The terminal cannot be confirmed ready for a command."""


class IdentityConflict(TinyMikoError):
    """Explicit identity contradicts observed login information."""


class CommandError(TinyMikoError):
    def __init__(self, message: str, result: CommandResult):
        super().__init__(message)
        self.result = result


class CommandTimeout(CommandError, TimeoutError):
    """The total command deadline expired; remote outcome may be unknown."""


class OutputLimitExceeded(CommandError):
    """A command exceeded its output or pagination budget."""
