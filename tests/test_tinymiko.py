"""Exercise real SSH/PTY streams with a deterministic network-device simulator."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import asyncssh
import pytest

from tinymiko import (
    CISCO,
    HUAWEI,
    BoundaryError,
    CommandError,
    CommandTimeout,
    Identity,
    IdentityConflict,
    OutputLimitExceeded,
    Profile,
    Session,
    identify_login,
)
from tinymiko.channel import Terminal


class NoAuth(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return False


class Device:
    def __init__(self, *, chunk_size: int = 1000, banner: bytes | None = None):
        self.chunk_size = chunk_size
        self.banner = (
            banner
            if banner is not None
            else b"Huawei VRP Version 8.220 (S9300 V200R022C00SPC500)\r\n"
        )
        self.commands: list[str] = []
        self.keys: list[bytes] = []
        self.prompt = b"<R1>"
        self.processes: list[asyncssh.SSHServerProcess[bytes]] = []
        self.tasks: set[asyncio.Task] = set()
        self.errors: list[Exception] = []

    async def send(
        self, process: asyncssh.SSHServerProcess[bytes], text: bytes
    ) -> None:
        for index in range(0, len(text), self.chunk_size):
            process.stdout.write(text[index : index + self.chunk_size])
            await process.stdout.drain()
            await asyncio.sleep(0)

    async def run(self, process: asyncssh.SSHServerProcess[bytes]) -> None:
        self.processes.append(process)
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            await self.send(process, self.banner + self.prompt)
            while raw := await process.stdin.readline():
                command = raw.strip().decode()
                self.commands.append(command)
                await self.send(process, raw.rstrip(b"\r\n") + b"\r\n")
                if command == "pages":
                    for index, body in enumerate(
                        ("第一页\r\n", "target 第二页\r\n", "最后一页\r\n")
                    ):
                        await self.send(process, body.encode())
                        if index < 2:
                            marker = b"---- More ----"
                            await self.send(process, marker)
                            key = await process.stdin.readexactly(1)
                            self.keys.append(key)
                            erase = (
                                b"\b" * len(marker)
                                + b" " * len(marker)
                                + b"\b" * len(marker)
                            )
                            await self.send(process, erase)
                            if key == b"q":
                                break
                elif command == "save":
                    await self.send(process, b"Continue? [y/n]:")
                    answer = await process.stdin.readline()
                    self.keys.append(answer)
                    await self.send(process, answer.rstrip() + b"\r\nFile name:")
                    filename = await process.stdin.readline()
                    self.keys.append(filename)
                    await self.send(process, filename.rstrip() + b"\r\nSaved\r\n")
                elif command == "secret":
                    await self.send(process, b"Password:")
                    answer = await process.stdin.readline()
                    self.keys.append(answer)
                    await self.send(process, answer.rstrip() + b"\r\nAccepted\r\n")
                elif command == "repeat-secret":
                    await self.send(process, b"Password:")
                    answer = (await process.stdin.readline()).rstrip()
                    await self.send(process, b"received=" + answer + b";done\r\n")
                elif command == "slow":
                    await asyncio.sleep(0.08)
                    await self.send(process, b"finished\r\n")
                elif command == "hang":
                    await self.send(process, b"working\r\n")
                    await process.stdin.read()
                    return
                elif command == "disconnect":
                    await self.send(process, b"partial\r\n")
                    process.exit(0)
                    return
                elif command == "large":
                    await self.send(process, b"x" * 2000)
                elif command == "mode":
                    self.prompt = b"[~R1-GigabitEthernet0/0/1]"
                elif command == "stderr":
                    process.stderr.write(b"diagnostic\n")
                    await self.send(process, b"body\r\n")
                else:
                    await self.send(
                        process, b"  body <R1> in text\r\n\x1b[32mok\x1b[0m\r\n"
                    )
                await self.send(process, self.prompt)
        except (asyncssh.Error, EOFError, ConnectionError):
            pass  # Expected when a test closes an unfinished operation.
        except Exception as exc:
            self.errors.append(exc)
        finally:
            self.tasks.discard(task)


@asynccontextmanager
async def device_server(device: Device) -> AsyncIterator[dict]:
    key = asyncssh.generate_private_key("ssh-ed25519")
    async with await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=NoAuth,
        server_host_keys=[key],
        process_factory=device.run,
        encoding=None,
        line_editor=False,
    ) as listener:
        try:
            yield {
                "host": "127.0.0.1",
                "port": listener.get_port(),
                "username": "test",
                "known_hosts": None,
            }
        finally:
            for process in device.processes:
                process.close()
            for task in tuple(device.tasks):
                task.cancel()
            if device.tasks:
                await asyncio.gather(*device.tasks, return_exceptions=True)
            assert not device.errors


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1000])
def test_execute_and_automatic_pagination(chunk_size):
    async def scenario():
        device = Device(chunk_size=chunk_size)
        deltas = []
        async with device_server(device) as options:
            async with Session.connect(options, on_output=deltas.append) as session:
                assert session.identity == Identity("huawei", "S9300")
                assert session.device_name == "R1"
                assert session.permission == "user"
                result = await session.execute("show")
                assert result.output == "  body <R1> in text\nok\n"
                assert result.output_complete
                deltas.clear()
                result = await session.execute_paged("pages")
                assert result.output == "第一页\ntarget 第二页\n最后一页\n"
                assert "".join(deltas) == result.output
                assert result.status == "completed"
                assert result.prompt_before == result.prompt_after == "<R1>"
                assert result.output_complete
                assert device.keys == [b" ", b" "]

    asyncio.run(scenario())


def test_target_pause_quit_and_single_steps():
    async def scenario():
        device = Device(chunk_size=1)
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                async with session.command("pages") as command:
                    event = await command.read_pages(expect=r"target")
                    assert event.kind == "match"
                    assert not event.done
                    assert device.keys == [b" "]
                    with pytest.raises(RuntimeError, match="not completed"):
                        _ = command.result
                    result = await command.quit_paging()
                    assert result.status == "completed"
                    assert not result.output_complete
                    assert result.output == "第一页\ntarget 第二页\n"
                assert device.keys == [b" ", b"q"]
                async with session.command("pages") as command:
                    events = [await command.next_page()]
                    assert events[0].kind == "page"
                    while not events[-1].done:
                        events.append(await command.next_page())
                    assert [event.kind for event in events] == [
                        "page",
                        "page",
                        "prompt",
                    ]
                    assert (
                        "".join(event.text for event in events) == command.result.output
                    )
                async with session.command("pages") as command:
                    event = await command.read_pages(expect="absent")
                    assert event.done
                    assert event.name is None
                    assert command.result.output_complete

    asyncio.run(scenario())


def test_native_interaction_and_sensitive_echo(caplog):
    async def scenario():
        device = Device(chunk_size=1)
        logger = logging.LoggerAdapter(
            logging.getLogger("test.tinymiko"), {"app": "tests"}
        )
        deltas = []
        async with device_server(device) as options:
            async with Session.connect(
                options,
                logger=logger,
                log_output=True,
                on_output=deltas.append,
            ) as session:
                async with session.command("save") as command:
                    event = await command.expect({"confirm": r"Continue\? \[y/n\]:"})
                    assert event.name == "confirm"
                    await command.reply("y", newline=True)
                    event = await command.expect({"filename": r"File (?P<label>name):"})
                    assert event.fields == {"label": "name"}
                    await command.reply("backup.cfg", newline=True)
                    assert (await command.expect({})).done
                    assert "backup.cfg" not in command.result.output
                    assert "Saved" in command.result.output
                async with session.command("secret") as command:
                    await command.expect({"password": "Password:"})
                    await command.reply("s3cr3t", newline=True, sensitive=True)
                    assert (await command.expect({})).done
                    assert "s3cr3t" not in command.result.output
                assert "s3cr3t" not in "".join(deltas)
                records = [r for r in caplog.records if r.name == "test.tinymiko"]
                assert records
                assert all(r.app == "tests" and r.session_id for r in records)
                assert "s3cr3t" not in "".join(r.getMessage() for r in records)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())


def test_simple_execute_does_not_send_page_keys():
    async def scenario():
        device = Device()
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                with pytest.raises(CommandTimeout) as error:
                    await session.execute("pages", timeout=0.1)
                assert error.value.result.status == "unknown"
                assert not error.value.result.output_complete
                assert "第一页" in error.value.result.output
                assert device.keys == []
                assert session.closed
                with pytest.raises(BoundaryError, match="closed"):
                    await session.execute("show")

    asyncio.run(scenario())


def test_deadline_covers_user_code_and_cancellation_propagates():
    async def scenario():
        device = Device()
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                command = session.command("save", timeout=0.1)
                with pytest.raises(CommandTimeout):
                    async with command:
                        await command.expect({"confirm": r"\[y/n\]:"})
                        await asyncio.sleep(10)
                assert session.closed
                assert command.partial_result.status == "unknown"
            async with Session.connect(options) as session:
                command = session.command("hang")

                async def work():
                    async with command:
                        await command.expect({})

                task = asyncio.create_task(work())
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert session.closed
                assert command.partial_result.status == "unknown"

    asyncio.run(scenario())


def test_serialization_nesting_and_early_context_exit():
    async def scenario():
        device = Device()
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                results = await asyncio.gather(
                    session.execute("slow"), session.execute("show")
                )
                assert all(r.status == "completed" for r in results)
                assert device.commands == ["slow", "show"]
                async with session.command("show") as command:
                    with pytest.raises(RuntimeError, match="Nested"):
                        await session.execute("nested")
                    with pytest.raises(RuntimeError, match="Nested"):
                        await asyncio.create_task(session.execute("nested-child"))
                    await command.expect({})
                async with session.command("hang") as command:
                    pass
                assert session.closed
                assert command.partial_result.status == "unknown"

    asyncio.run(scenario())


def test_disconnect_limits_and_prompt_mode():
    async def scenario():
        device = Device()
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                result = await session.execute("mode")
                assert result.prompt_after == "[~R1-GigabitEthernet0/0/1]"
                assert session.configuration_mode == "GigabitEthernet0/0/1"
                assert session.device_name == "R1"
                with pytest.raises(CommandError) as error:
                    await session.execute("disconnect")
                assert "partial" in error.value.result.output
                assert session.closed
            device.prompt = b"<R1>"
            async with Session.connect(options, max_pages=1) as session:
                with pytest.raises(OutputLimitExceeded):
                    await session.execute_paged("pages")
                assert session.closed
            async with Session.connect(options, max_output=512) as session:
                with pytest.raises(CommandError):
                    await session.execute("large")
                assert session.closed

    asyncio.run(scenario())


def test_unknown_device_custom_identifier_and_conflict():
    async def scenario():
        device = Device(banner=b"Welcome\r\n")
        async with device_server(device) as options:
            with pytest.raises(BoundaryError, match="No default profile"):
                async with Session.connect(options):
                    pass
            async with Session.connect(options, profile=Profile()) as session:
                assert session.identity == Identity()

            async def identify(session):
                result = await session.execute("probe")
                assert "body" in result.output
                return Identity("huawei", "S9300")

            async with Session.connect(options, identify=identify) as session:
                assert session.identity.platform == "S9300"
            with pytest.raises(IdentityConflict):
                async with Session.connect(
                    options,
                    identify=identify,
                    identity=Identity("cisco"),
                ):
                    pass
            with pytest.raises(IdentityConflict, match="platform"):
                async with Session.connect(
                    options,
                    identify=identify,
                    identity=Identity("huawei", "S9312"),
                ):
                    pass
            async with Session.connect(
                options,
                profile=HUAWEI,
                identity=Identity("huawei", "S9300"),
            ) as session:
                assert session.identity == Identity(vendor="huawei", platform="S9300")

    asyncio.run(scenario())


def test_stderr_and_idle_output_boundary():
    async def scenario():
        device = Device()
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                result = await session.execute("stderr")
                assert result.stderr == "diagnostic\n"
                assert result.output == "body\n"
                process = device.processes[-1]
                process.stdout.write(b"notice\r\n<R1>")
                await asyncio.sleep(0.02)
                result = await session.execute("show")
                assert "notice" not in result.output
                assert "notice" in "".join(session.idle_output)
                process.stdout.write(b"unconfirmed notice")
                await asyncio.sleep(0.02)
                with pytest.raises(BoundaryError):
                    await session.execute("show")
                assert session.closed

    asyncio.run(scenario())


def test_terminal_rules_and_vendor_evidence():
    assert identify_login("", "<R1>") == Identity()
    with pytest.raises(IdentityConflict):
        identify_login("Huawei Cisco", "R1#")
    assert CISCO.parse_prompt("R1(config-if)#").mode == "config-if"
    assert HUAWEI.parse_prompt("[~R1-interface]", "R1").device == "R1"
    custom = replace(Profile(), prompt_pattern=r"(?P<device>box):(?P<mode>config)>")
    assert custom.parse_prompt("box:config>").device == "box"
    terminal = Terminal()
    for char in "  first\r\n\x1b[32msecond\x1b[0m":
        terminal.feed(char)
    assert terminal.text == "  first\nsecond"


def test_short_secret_does_not_rewrite_previous_output():
    async def scenario():
        device = Device(chunk_size=1)
        deltas = []
        async with device_server(device) as options:
            async with Session.connect(options, on_output=deltas.append) as session:
                async with session.command("secret") as command:
                    await command.expect({"password": "Password:"})
                    await command.reply("a", newline=True, sensitive=True)
                    await command.expect({})
                    assert command.result.output == "Password:Accepted\n"
                    assert "".join(deltas) == command.result.output
            deltas.clear()
            async with Session.connect(
                options,
                profile=replace(HUAWEI, echo=False),
                on_output=deltas.append,
            ) as session:
                async with session.command("repeat-secret") as command:
                    await command.expect({"password": "Password:"})
                    await command.reply("s3cr3t", newline=True, sensitive=True)
                    # An event inside a secret must not publish its prefix.
                    await command.expect({"prefix": "received=s3"})
                    assert "s3" not in "".join(deltas)
                    await command.expect({})
                    assert "s3cr3t" not in command.result.output
                    assert "[REDACTED]" in command.result.output
                    assert "".join(deltas) == command.result.output

    asyncio.run(scenario())


def test_caller_exception_and_pending_page_continue():
    async def scenario():
        device = Device(chunk_size=1000)
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                async with session.command("pages") as command:
                    await command.next_page()
                    event = await command.read_pages(expect="target")
                    assert event.name == "expect"
                    event = await command.read_pages()
                    assert event.done
                    assert command.result.output_complete
                command = session.command("save")
                with pytest.raises(ValueError, match="parse failed"):
                    async with command:
                        await command.expect({"confirm": r"\[y/n\]:"})
                        raise ValueError("parse failed")
                assert session.closed
                assert command.partial_result.status == "unknown"

    asyncio.run(scenario())


def test_cisco_default_profile_and_explicit_overrides():
    async def scenario():
        device = Device(banner=b"Cisco IOS Software, Version 15.2\r\n")
        device.prompt = b"R1#"
        async with device_server(device) as options:
            async with Session.connect(options) as session:
                assert session.identity.vendor == "cisco"
                assert session.permission == "privileged"
                assert session.configuration_mode == "exec"
                result = await session.execute("show")
                assert result.status == "completed"
                device.prompt = b"R1(config-if)#"
                await session.execute("show")
                assert session.configuration_mode == "config-if"
                assert session.device_name == "R1"
            async with Session.connect(
                options,
                profile=replace(CISCO, continue_key="c"),
            ) as session:
                result = await session.execute_paged("pages")
                assert result.output_complete
                assert device.keys == [b"c", b"c"]

    asyncio.run(scenario())


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1000])
def test_huawei_wide_pager_erasure_preserves_body_indentation(chunk_size):
    terminal = Terminal()
    terminal.feed("first\r\n  ---- More ----")
    match = re.search(HUAWEI.page_pattern, terminal.text)
    assert match is not None
    prefix = terminal.take(match.end(), erase=True)[: match.start()]
    raw = "\x1b[42D" + " " * 42 + "\x1b[42D" + " description link \r\n#\r\n"
    for offset in range(0, len(raw), chunk_size):
        terminal.feed(raw[offset : offset + chunk_size])
    assert prefix + terminal.text == "first\n description link \n#\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Huawei Versatile Routing Platform Software\r\n"
            "VRP (R) software, Version 5.170 (S9300 V200R022C00SPC500)\r\n",
            Identity("huawei", "S9300"),
        ),
        (
            "Huawei VRP (S9300 V200R022C00)\r\nModel: S9312\r\n",
            Identity("huawei", "S9312"),
        ),
        (
            "HUAWEI S5735-L48T4X-A1 uptime is 10 days",
            Identity("huawei", "S5735-L48T4X-A1"),
        ),
        ("Huawei VRP Version 8.220", Identity("huawei", None)),
        ("Cisco IOS Software, Version 15.2", Identity("cisco", None)),
        (
            "Cisco IOS XE Software\r\nModel Number : C9300-48P\r\n",
            Identity("cisco", "C9300-48P"),
        ),
        (
            "cisco ISR4331/K9 (1RU) processor with memory",
            Identity("cisco", "ISR4331/K9"),
        ),
        (
            'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n',
            Identity("debian", "12"),
        ),
        ("ID=fedora\nVERSION_ID=40\n", Identity("fedora", "40")),
        (
            'ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n',
            Identity("ubuntu", "24.04"),
        ),
        ("ID=arch\nBUILD_ID=20261124\n", Identity("arch", None)),
        ("ID=debian\nVERSION_ID=\n", Identity("debian", None)),
        ('ID="custom-linux"\nVERSION_ID="1.2"\n', Identity("custom-linux", "1.2")),
        ("Debian GNU/Linux 12 host tty1", Identity("debian", "12")),
        (
            "Welcome to Ubuntu 24.04.1 LTS (GNU/Linux 6.8.0 x86_64)",
            Identity("ubuntu", "24.04.1"),
        ),
        ("Fedora Linux 40 (Server Edition)", Identity("fedora", "40")),
        ("Arch Linux", Identity("arch", None)),
        ("Linux host 6.1.0 #1 SMP Debian 6.1.140 x86_64", Identity()),
        ("Model: S9300\n", Identity()),
    ],
)
def test_network_model_and_linux_release_identification(text, expected):
    assert identify_login(text, "") == expected


def test_ambiguous_platform_evidence_is_not_silently_selected():
    with pytest.raises(IdentityConflict, match="models"):
        identify_login("Huawei\nModel: S9312\nModel: S9306\n", "")
    with pytest.raises(IdentityConflict, match="ID"):
        identify_login("ID=debian\nID=fedora\n", "")
    with pytest.raises(IdentityConflict, match="identities"):
        identify_login("Huawei\nID=debian\nVERSION_ID=12\n", "")


def test_linux_identity_uses_callers_shell_profile():
    async def scenario():
        device = Device(banner=b'ID=debian\r\nVERSION_ID="12"\r\n')
        device.prompt = b"host#"
        profile = Profile(prompt_pattern=r"(?m)^(?P<device>host)[#$][ \t]*\Z")
        async with device_server(device) as options:
            async with Session.connect(options, profile=profile) as session:
                assert session.identity == Identity(vendor="debian", platform="12")
                assert (await session.execute("show")).status == "completed"
            with pytest.raises(IdentityConflict, match="platform"):
                async with Session.connect(
                    options,
                    profile=profile,
                    identity=Identity("debian", "11"),
                ):
                    pass

    asyncio.run(scenario())
