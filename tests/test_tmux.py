from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import agent_switchboard.tmux as tmux_module
from agent_switchboard.tmux import (
    TmuxController,
    TmuxError,
    TmuxLocator,
    TmuxMetadata,
    TmuxTargetMissing,
)

SOCKET = "/tmp/tmux-1000/default"
LOCATOR = TmuxLocator(SOCKET, "as-codex-test", "@4", "%7")


class FakeTmux:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.metadata = {
            "@agent_switchboard_surface_id": None,
            "@agent_switchboard_session_key": None,
            "@agent_switchboard_provider": None,
            "@agent_switchboard_launch_id": None,
            "@agent_switchboard_surface_role": None,
        }
        self.fail_option: str | None = None

    def __call__(
        self, argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        command = list(argv)
        self.calls.append((command, timeout))
        tmux = command[command.index("--") + 1 :] if "--" in command else command
        if "new-session" in tmux:
            return subprocess.CompletedProcess(
                command, 0, f"{SOCKET}\tas-codex-test\t@4\t%7\n".encode(), b""
            )
        if "display-message" in tmux:
            values = [
                SOCKET,
                "as-codex-test",
                "@4",
                "%7",
                "0",
                *(value or "" for value in self.metadata.values()),
            ]
            return subprocess.CompletedProcess(
                command, 0, "\t".join(values).encode() + b"\n", b""
            )
        if "set-option" in tmux:
            unset = "-u" in tmux
            option = tmux[-1] if unset else tmux[-2]
            if option == self.fail_option:
                self.fail_option = None
                return subprocess.CompletedProcess(command, 1, b"", b"write failed")
            self.metadata[option] = None if unset else tmux[-1]
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if "list-clients" in tmux:
            stdout = b"/dev/pts/8\tas-codex-test\t@4\t%7\n"
            if tmux[-1] == "#{client_tty}":
                stdout = b"/dev/pts/8\n"
            return subprocess.CompletedProcess(command, 0, stdout, b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")


def test_locator_storage_is_canonical_and_strict() -> None:
    stored = LOCATOR.to_storage()

    assert stored == (
        '{"pane":"%7","session":"as-codex-test",'
        '"socket":"/tmp/tmux-1000/default","window":"@4"}'
    )
    assert TmuxLocator.from_storage(stored) == LOCATOR
    assert LOCATOR.target == "%7"

    invalid = (
        "not-json",
        '{"pane":"%7","session":"work","socket":"relative","window":"@4"}',
        '{"pane":7,"session":"work","socket":"/tmp/tmux","window":"@4"}',
        '{"extra":1,"pane":"%7","session":"work","socket":"/tmp/tmux","window":"@4"}',
    )
    for value in invalid:
        with pytest.raises(TmuxError):
            TmuxLocator.from_storage(value)


def test_create_surface_uses_systemd_scope_argv_and_sets_metadata(
    tmp_path: Path,
) -> None:
    fake = FakeTmux()
    controller = TmuxController(
        runner=fake,
        systemd_run="/usr/bin/systemd-run",
    )

    observed = controller.create_surface(
        name="as-codex-test",
        cwd=tmp_path,
        command=("/opt/swbctl", "bootstrap", "launch-id"),
        environment={"SWB_TOKEN": "opaque"},
        surface_id="surface-id",
        session_key="host:codex:session",
        provider="codex",
        launch_id="launch-id",
        role="session",
    )

    assert observed.locator == LOCATOR
    assert observed.metadata == TmuxMetadata(
        "surface-id",
        "host:codex:session",
        "codex",
        "launch-id",
        "session",
    )
    creation, timeout = fake.calls[0]
    assert creation[:7] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--",
        "tmux",
    ]
    assert creation[-5:] == [
        "-e",
        "SWB_TOKEN=opaque",
        "/opt/swbctl",
        "bootstrap",
        "launch-id",
    ]
    assert timeout == tmux_module.TMUX_CREATE_TIMEOUT_SECONDS


def test_metadata_failure_restores_prior_values() -> None:
    fake = FakeTmux()
    fake.metadata.update(
        {
            "@agent_switchboard_surface_id": "prior-surface",
            "@agent_switchboard_session_key": "prior-session",
            "@agent_switchboard_provider": "codex",
            "@agent_switchboard_launch_id": None,
            "@agent_switchboard_surface_role": "session",
        }
    )
    fake.fail_option = "@agent_switchboard_surface_role"
    controller = TmuxController(runner=fake, systemd_run=None)

    with pytest.raises(TmuxError, match="write failed"):
        controller.set_metadata(
            LOCATOR,
            surface_id="new-surface",
            session_key="new-session",
            provider="codex",
            launch_id="new-launch",
            role="session",
        )

    assert fake.metadata == {
        "@agent_switchboard_surface_id": "prior-surface",
        "@agent_switchboard_session_key": "prior-session",
        "@agent_switchboard_provider": "codex",
        "@agent_switchboard_launch_id": None,
        "@agent_switchboard_surface_role": "session",
    }


def test_select_surface_revalidates_client_and_locator() -> None:
    fake = FakeTmux()
    controller = TmuxController(runner=fake, systemd_run=None)

    controller.select_surface(LOCATOR, client="/dev/pts/8")

    commands = [call for call, _timeout in fake.calls]
    assert commands[-3:] == [
        [
            "tmux",
            "-S",
            SOCKET,
            "switch-client",
            "-c",
            "/dev/pts/8",
            "-t",
            "=as-codex-test",
        ],
        ["tmux", "-S", SOCKET, "select-window", "-t", "@4"],
        ["tmux", "-S", SOCKET, "select-pane", "-t", "%7"],
    ]
    assert TmuxController.attach_argv(LOCATOR) == [
        "tmux",
        "-S",
        SOCKET,
        "-u",
        "attach-session",
        "-t",
        "=as-codex-test",
    ]
    assert controller.client_exists(LOCATOR, "/dev/pts/8")


def test_provider_exit_is_sent_only_to_the_revalidated_exact_pane() -> None:
    fake = FakeTmux()
    controller = TmuxController(runner=fake, systemd_run=None)

    controller.request_provider_exit(LOCATOR)

    commands = [call for call, _timeout in fake.calls]
    assert commands[-3:] == [
        ["tmux", "-S", SOCKET, "send-keys", "-t", "%7", "C-c"],
        [
            "tmux",
            "-S",
            SOCKET,
            "send-keys",
            "-t",
            "%7",
            "-l",
            "--",
            "/exit",
        ],
        ["tmux", "-S", SOCKET, "send-keys", "-t", "%7", "Enter"],
    ]


def test_missing_target_and_missing_systemd_run_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(
        argv: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"", b"can't find pane: %7\n")

    with pytest.raises(TmuxTargetMissing, match="can't find pane"):
        TmuxController(runner=missing, systemd_run=None).inspect_locator(LOCATOR)

    monkeypatch.setattr(tmux_module.shutil, "which", lambda _name: None)
    controller = TmuxController(runner=FakeTmux())
    with pytest.raises(TmuxError, match="systemd-run is required"):
        controller.create_surface(
            name="as-codex-test",
            cwd=tmp_path,
            command=("true",),
            environment={},
            surface_id="surface-id",
            session_key=None,
            provider="codex",
            launch_id=None,
            role="session",
        )
