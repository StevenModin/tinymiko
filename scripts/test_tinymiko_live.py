"""Validate tinymiko on an authorized Huawei SSH device using read-only queries.

Requires project dependencies and TINYMIKO_PASSWORD (or --password-env). No
credentials or device configuration are persisted; the JSON report holds only
test outcomes, output sizes, hashes and prompt categories. The initial host key
is obtained with TOFU and pinned for all connections during this invocation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import asyncssh

from tinymiko import HUAWEI, CommandResult, Session, identify_login


class Events(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.counts: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        self.counts[getattr(record, "event_type", "other")] += 1


def validate(result: CommandResult) -> None:
    if result.status != "completed":
        raise AssertionError("Command did not return to a prompt")
    error = re.search(
        r"(?im)^\s*(?:Error:|%\s*Error|Unrecognized command)[^\r\n]*", result.output
    )
    if error:
        raise AssertionError("Device rejected the command: " + error.group().strip())
    if re.search(r"----\s*More\s*----|--More--|\x1b|\x08", result.output):
        raise AssertionError("Public output contains pagination or terminal artifacts")


async def run(args: argparse.Namespace, report: dict[str, Any]) -> dict[str, Any]:
    secret = os.environ[args.password_env]
    report["tests"] = []
    events = Events()
    logger = logging.Logger("tinymiko.live", level=logging.DEBUG)
    logger.addHandler(events)
    deltas: list[str] = []
    transport: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "connect_timeout": 15,
    }
    if args.legacy_algorithms:
        transport.update(
            kex_algs="+diffie-hellman-group-exchange-sha1,diffie-hellman-group1-sha1",
            encryption_algs="+aes256-cbc,aes128-cbc,3des-cbc",
        )
    # Fetching is not independent identity verification; pin it to avoid accepting
    # a different key during subsequent test connections in this invocation.
    key = await asyncssh.get_server_host_key(
        args.host,
        args.port,
        kex_algs=transport.get("kex_algs", ()),
        options=asyncssh.SSHClientConnectionOptions(**transport),
    )
    if key is None:
        raise RuntimeError("Server did not provide a host key")
    report["host_key_fingerprint"] = key.get_fingerprint()
    report["host_key_policy"] = "TOFU; pinned within this test invocation"
    options = dict(
        transport,
        username=args.username,
        password=secret,
        client_keys=[],
        known_hosts=([key], [], []),
    )
    profile = replace(HUAWEI, term_size=(160, 24))

    def record(name: str, result: CommandResult, **extra: object) -> None:
        validate(result)
        if "".join(deltas) != result.output:
            raise AssertionError("Output callback and accumulated output differ")
        item = {
            "name": name,
            "passed": True,
            "status": result.status,
            "output_complete": result.output_complete,
            "output_chars": len(result.output),
            "output_lines": len(result.output.splitlines()),
            "sha256": hashlib.sha256(result.output.encode()).hexdigest(),
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            **extra,
        }
        report["tests"].append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
        deltas.clear()

    async with Session.connect(
        options,
        profile=profile,
        logger=logger,
        login_timeout=30,
        on_output=deltas.append,
    ) as session:
        report["login"] = {
            "vendor": session.identity.vendor,
            "platform": session.identity.platform,
            "mode": session.configuration_mode,
            "permission": session.permission,
            "prompt_style": "<>" if session.prompt.startswith("<") else "[]",
        }
        print(json.dumps({"login": report["login"]}, ensure_ascii=False), flush=True)

        before = events.counts["page"]
        full = await session.execute_paged("display interface brief main", timeout=90)
        pages = events.counts["page"] - before
        record("automatic_interface_pages", full, page_events=pages)
        if pages == 0:
            raise AssertionError("Interface query did not exercise pagination")
        target = re.compile(r"(?m)^\s*GigabitEthernet1/0/10(?=[ \t\r\n]|$)")
        if target.search(full.output) is None:
            raise AssertionError(
                "Full interface output does not contain the requested interface"
            )

        before = events.counts["page"]
        async with session.command(
            "display interface brief main", timeout=90
        ) as command:
            event = await command.read_pages(expect=target)
            if event.done:
                raise AssertionError(
                    "Target was not matched before the terminal prompt"
                )
            result = await command.quit_paging()
        if target.search(result.output) is None or result.output_complete:
            raise AssertionError(
                "Conditional pagination did not stop with partial output"
            )
        record(
            "conditional_interface_stop",
            result,
            page_events=events.counts["page"] - before,
            matched_interface="GigabitEthernet1/0/10",
        )

        before = events.counts["page"]
        async with session.command(
            "display interface brief main", timeout=60
        ) as command:
            first = await command.next_page()
            if first.kind != "page":
                raise AssertionError("Single-step query did not stop at its first page")
            second = await command.next_page()
            if second.done:
                result = command.result
            else:
                result = await command.quit_paging()
        record("single_step_pages", result, page_events=events.counts["page"] - before)

        # Capture only in memory to distinguish device whitespace from IO bugs.
        before = events.counts["page"]
        paged_config, raw_paged_config = await capture_configuration(
            session, paged=True
        )
        record(
            "automatic_configuration_pages",
            paged_config,
            page_events=events.counts["page"] - before,
        )

        for text in ("screen-length 0 temp", "screen-length 0 temporary", "scr 0 t"):
            disabled = await session.execute(text)
            errors = re.findall(
                r"(?im)^\s*(?:Error:|%\s*Error|Unrecognized command)[^\r\n]*",
                disabled.output,
            )
            if errors:
                item = {
                    "name": "disable_session_pagination_attempt",
                    "command": text,
                    "passed": False,
                    "device_messages": errors,
                }
                report["tests"].append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                deltas.clear()
                continue
            record("disable_session_pagination", disabled, command=text)
            break
        else:
            raise AssertionError(
                "Device rejected all temporary pagination-disable spellings"
            )
        before = events.counts["page"]
        config, raw_simple_config = await capture_configuration(session, paged=False)
        if not re.search(r"(?im)^\s*interface\s+", config.output):
            raise AssertionError(
                "Configuration query contains no interface definitions"
            )
        record(
            "simple_interface_configuration",
            config,
            page_events=events.counts["page"] - before,
            interface_blocks=len(re.findall(r"(?im)^\s*interface\s+", config.output)),
        )

        if config.output != paged_config.output:
            differences = []
            for index, (paged, simple) in enumerate(
                zip(
                    paged_config.output.splitlines(),
                    config.output.splitlines(),
                    strict=True,
                ),
                1,
            ):
                if paged != simple:
                    differences.append(
                        {
                            "line": index,
                            "paged_length": len(paged),
                            "simple_length": len(simple),
                            "same_after_strip": paged.strip() == simple.strip(),
                            "paged_indent": len(paged) - len(paged.lstrip()),
                            "simple_indent": len(simple) - len(simple.lstrip()),
                            "same_after_rstrip": paged.rstrip() == simple.rstrip(),
                            "raw_paged_trailing_lengths": sorted(
                                {
                                    len(value)
                                    for value in re.findall(
                                        re.escape(paged.rstrip()) + r"([ \t]*)\r*\n",
                                        raw_paged_config,
                                    )
                                }
                            ),
                            "raw_simple_trailing_lengths": sorted(
                                {
                                    len(value)
                                    for value in re.findall(
                                        re.escape(simple.rstrip()) + r"([ \t]*)\r*\n",
                                        raw_simple_config,
                                    )
                                }
                            ),
                            "paged_trailing_length": len(paged) - len(paged.rstrip()),
                            "simple_trailing_length": len(simple)
                            - len(simple.rstrip()),
                        }
                    )
            report["configuration_differences"] = differences
            contexts = []
            for marker in re.finditer(r"---- More ----", raw_paged_config):
                start = raw_paged_config.rfind("\n", 0, marker.start()) + 1
                fragment = raw_paged_config[start : marker.end() + 120]
                tokens = re.findall(
                    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x08+|\r|\n| +|[^\x1b\x08\r\n ]+",
                    fragment,
                )
                contexts.append(
                    [
                        token
                        if token.isspace() or token.startswith(("\x1b", "\x08"))
                        else f"<text:{len(token)}>"
                        for token in tokens
                    ]
                )
            report["pager_control_samples"] = contexts[:3]
            raw_whitespace_only = all(
                difference["same_after_rstrip"]
                and difference["raw_paged_trailing_lengths"]
                == [difference["paged_trailing_length"]]
                and difference["raw_simple_trailing_lengths"]
                == [difference["simple_trailing_length"]]
                for difference in differences
            )
            if not raw_whitespace_only:
                raise AssertionError("Paged and non-paged configuration output differ")
            report["device_trailing_whitespace_differs"] = True
        report["paged_and_unpaged_configuration_equal"] = (
            config.output == paged_config.output
        )
        report["paged_and_unpaged_equal_ignoring_trailing_spaces"] = [
            line.rstrip() for line in config.output.splitlines()
        ] == [line.rstrip() for line in paged_config.output.splitlines()]

        # A repeated query checks for stale bytes or dropped/duplicated body text.
        repeated = await session.execute(
            "display current-configuration interface", timeout=120
        )
        if repeated.output != config.output:
            raise AssertionError("Repeated interface configuration output differs")
        record("repeat_configuration_consistency", repeated)
        entered = await session.execute("system-view")
        record("enter_configuration_view", entered)
        if not session.prompt.startswith("[") or session.configuration_mode != "config":
            raise AssertionError("Configuration prompt was not recognized")
        record("return_to_user_view", await session.execute("quit"))
        if not session.prompt.startswith("<"):
            raise AssertionError("Session unexpectedly left user view")
        probe = await session.execute("display version")
        record("explicit_platform_probe", probe)
        observed = identify_login(probe.output, session.prompt)
        report["explicit_probe"] = {
            "vendor": observed.vendor,
            "platform": observed.platform,
        }
        report["final_mode"] = session.configuration_mode
    return report


async def capture_configuration(
    session: Session, *, paged: bool
) -> tuple[CommandResult, str]:
    raw_reads: list[str] = []
    original_read = session._channel.read

    async def tracked_read() -> tuple[str, bool]:
        item = await original_read()
        if not item[1]:
            raw_reads.append(item[0])
        return item

    # This is test instrumentation, not a second terminal consumer.
    with patch.object(session._channel, "read", tracked_read):
        execute = session.execute_paged if paged else session.execute
        result = await execute("display current-configuration interface", timeout=120)
    return result, "".join(raw_reads)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-env", default="TINYMIKO_PASSWORD")
    parser.add_argument("--legacy-algorithms", action="store_true")
    parser.add_argument(
        "--report", type=Path, default=Path("output/tinymiko-live.json")
    )
    args = parser.parse_args()
    started = time.monotonic()
    report: dict[str, Any] = {"host": args.host}
    try:
        report.update(asyncio.run(run(args, report)))
        report["passed"] = True
    except Exception as exc:
        # Do not serialize traceback, credentials or raw device configuration.
        report["passed"] = False
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc).replace(
            os.environ.get(args.password_env, "\x00"), "[REDACTED]"
        )
        print(json.dumps(report, ensure_ascii=False), flush=True)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
