"""Small, replaceable terminal rules; no device business commands."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .models import Identity, IdentityConflict, Prompt

Pattern = str | re.Pattern[str]

# Bootstrap syntax is deliberately restricted. Syntax alone identifies no vendor.
BOOTSTRAP_PROMPT = (
    r"(?m)^(?:<[^<>\r\n]+>|\[[~*]?[^\[\]\r\n]+\]|"
    r"[\w.:/-]+(?:\([\w.:/-]+\))?[>#])[ \t]*\Z"
)


@dataclass(frozen=True, slots=True)
class Profile:
    encoding: str = "utf-8"
    newline: str = "\n"
    echo: bool = True
    prompt_pattern: Pattern = BOOTSTRAP_PROMPT
    page_pattern: Pattern | None = r"(?:----[ \t]*More[ \t]*----|--More--)[ \t]*\Z"
    continue_key: str = " "
    quit_key: str = "q"
    term_type: str = "vt100"
    term_size: tuple[int, int] = (80, 24)

    def parse_prompt(self, text: str, device: str | None = None) -> Prompt:
        """Named device/permission/mode groups override the built-in extraction."""
        match = re.fullmatch(self.prompt_pattern, text)
        if match is None:
            raise ValueError("Text does not match the configured prompt rule")
        fields = match.groupdict()
        value = text.rstrip(" \t")
        if value.startswith("<"):
            name, permission, mode = value[1:-1], "user", "exec"
        elif value.startswith("["):
            inner = value[1:-1].lstrip("~*")
            name = (
                device
                if device and (inner == device or inner.startswith(device + "-"))
                else inner
            )
            mode = inner[len(name) :].lstrip("-") or "config"
            permission = "configuration"
        else:
            parts = re.fullmatch(r"(.+?)(?:\(([^)]+)\))?([>#])", value)
            if parts is None:
                name, permission, mode = value, None, None
            else:
                name, mode, suffix = parts.groups()
                mode = mode or "exec"
                permission = "privileged" if suffix == "#" else "user"
        return Prompt(
            value,
            fields.get("device") or name,
            fields.get("permission") or permission,
            fields.get("mode") or mode,
        )


HUAWEI = Profile(
    prompt_pattern=r"(?m)^(?:<[^<>\r\n]+>|\[[~*]?[^\[\]\r\n]+\])[ \t]*\Z",
    page_pattern=r"(?m)^[ \t]*----[ \t]*More[ \t]*----[ \t]*\Z",
)
CISCO = Profile(prompt_pattern=r"(?m)^[\w.:/-]+(?:\([\w.:/-]+\))?[>#][ \t]*\Z")
DEFAULT_PROFILES = {"huawei": HUAWEI, "cisco": CISCO}


def identify_login(login_output: str, prompt: str) -> Identity:
    """Identify network vendor/model or Linux distribution/release from text.

    A network software version is not a hardware model. Linux kernel versions
    are not distribution releases. Absent evidence therefore stays unknown.
    This pure function also accepts explicit probe output supplied by callers.
    """
    del prompt
    rules = (
        ("huawei", r"(?i)\bHuawei\b|\bVRP\b"),
        ("cisco", r"(?i)\bCisco\b"),
        ("zte", r"(?i)\bZTE\b"),
        ("nokia", r"(?i)\bNokia\b"),
    )
    found = []
    for vendor, evidence in rules:
        if re.search(evidence, login_output):
            found.append(Identity(vendor, _network_platform(login_output, vendor)))
    linux = _linux_identity(login_output)
    if linux is not None:
        found.append(linux)
    if len(found) > 1:
        raise IdentityConflict("Identification text contains multiple identities")
    return found[0] if found else Identity()


def _network_platform(text: str, vendor: str) -> str | None:
    labelled = (
        r"(?im)^[ \t]*(?:Device[ \t]+model|Product[ \t]+(?:name|model)|"
        r"Model(?:[ \t]+number)?|Chassis[ \t]+type)[ \t]*[:=][ \t]*"
        r"([A-Za-z0-9][A-Za-z0-9._/-]*)[ \t\r]*$"
    )
    huawei_model = r"(?:S\d|CE\d|NE\d|AR\d|USG\d)[A-Za-z0-9._/-]*"
    patterns = [labelled]
    if vendor == "huawei":
        patterns.extend(
            (
                rf"(?im)^[ \t]*(?:HUAWEI[ \t]+)?({huawei_model})[ \t]+uptime[ \t]+is\b",
                rf"(?i)\bHUAWEI[ \t]+({huawei_model})\b",
                # A software banner may expose only a family, e.g. (S9300 V200...).
                rf"(?i)\(({huawei_model})[ \t]+V\d",
            )
        )
    elif vendor == "cisco":
        patterns.append(
            r"(?im)^[ \t]*cisco[ \t]+([A-Za-z][A-Za-z0-9._/-]*)"
            r"(?:[ \t]+\([^\r\n]*\))?[ \t]+processor\b"
        )
    # Prefer explicit model labels over a less specific product-family banner.
    for pattern in patterns:
        values = {match.group(1) for match in re.finditer(pattern, text)}
        if len(values) > 1:
            raise IdentityConflict(
                "Identification text contains multiple device models"
            )
        if values:
            return values.pop()
    return None


def _linux_identity(text: str) -> Identity | None:
    # Parse only ID/VERSION_ID, never ID_LIKE, BUILD_ID or uname kernel versions.
    # shlex handles quotes without executing or expanding shell expressions.
    fields: dict[str, str] = {}
    for match in re.finditer(r"(?m)^(ID|VERSION_ID)=(.*)$", text):
        name, raw_value = match.groups()
        values = shlex.split(raw_value.strip(), comments=True)
        if len(values) > 1:
            raise ValueError(f"Malformed os-release {name} assignment")
        value = values[0] if values else ""
        if name in fields and fields[name] != value:
            raise IdentityConflict(f"Conflicting os-release {name} assignments")
        fields[name] = value
    if "ID" in fields:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", fields["ID"]):
            raise ValueError("Malformed os-release ID")
        return Identity(fields["ID"], fields.get("VERSION_ID") or None)

    banners = (
        ("debian", r"Debian GNU/Linux(?:[ \t]+(?P<release>\d[\w.+-]*))?"),
        ("ubuntu", r"Ubuntu(?:[ \t]+(?P<release>\d[\w.+-]*))?"),
        ("fedora", r"Fedora(?: Linux)?(?: release)?(?:[ \t]+(?P<release>\d[\w.+-]*))?"),
        ("arch", r"Arch Linux"),
    )
    found = []
    for vendor, banner in banners:
        pattern = rf"(?im)^(?:Welcome to[ \t]+)?{banner}(?=[ \t\r\n(]|$)"
        match = re.search(pattern, text)
        if match:
            found.append(Identity(vendor, match.groupdict().get("release")))
    if len(found) > 1:
        raise IdentityConflict(
            "Identification text contains multiple Linux distributions"
        )
    return found[0] if found else None
