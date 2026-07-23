from __future__ import annotations

import os
import pty
import shutil
import signal
import struct
import subprocess
import termios
import time
from fcntl import ioctl
from pathlib import Path

import pytest

from agent_switchboard._v3.domain import GenerationId, HostId, ViewId, ViewMode
from agent_switchboard._v3.tmux_view import TmuxExecutor, TmuxViewError

GENERATION = GenerationId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
HOST = HostId("11111111-1111-4111-8111-111111111111")
VIEW = ViewId("22222222-2222-4222-8222-222222222222")
VIEW_TWO = ViewId("22222222-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FRAME = "33333333-3333-4333-8333-333333333333"
FRAME_TWO = "33333333-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SURFACE = "44444444-4444-4444-8444-444444444444"

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


def wait_for(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_isolated_view_shell_preserves_surface_across_modes_and_detach(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "tmux.sock"
    tmux = TmuxExecutor(socket)
    clients: list[tuple[subprocess.Popen[bytes], int]] = []
    try:
        evidence = tmux.server_evidence(HOST, observed_at=10)
        assert evidence.socket_path == str(socket)
        shell = tmux.create_shell(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            frame_id=FRAME,
            mode=ViewMode.NAVIGATOR,
            sidebar_command=("sleep", "60"),
        )
        assert shell.sidebar is not None
        assert len(shell.holding_panes) == 1
        surface = tmux.spawn_surface(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            frame_id=FRAME,
            surface_id=SURFACE,
            command=("sleep", "60"),
        )
        assert surface.input_off
        process_id = surface.process_id
        presented = tmux.present_surface(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            mode=ViewMode.NAVIGATOR,
            surface_id=SURFACE,
        )
        assert presented.active.pane_id == surface.pane_id
        assert presented.active.process_id == process_id

        master, slave = pty.openpty()
        ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        environment = dict(os.environ)
        environment["TERM"] = "xterm-256color"
        environment.pop("TMUX", None)
        client = subprocess.Popen(
            tmux.attach_argv("test", VIEW),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env=environment,
        )
        os.close(slave)
        clients.append((client, master))
        assert wait_for(lambda: tmux.run("list-clients", check=False).returncode == 0)
        geometry = tmux._pane(surface.pane_id).geometry
        ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 180, 0, 0))
        os.kill(client.pid, signal.SIGWINCH)
        assert wait_for(lambda: tmux._pane(surface.pane_id).geometry != geometry)
        assert tmux._pane(surface.pane_id).process_id == process_id
        client.terminate()
        client.wait(timeout=3)
        os.close(master)
        clients.clear()
        assert wait_for(
            lambda: (
                tmux.run("list-clients", check=False).returncode != 0
                or not tmux.run("list-clients", check=False).stdout.strip()
            )
        )
        assert tmux._pane(surface.pane_id).process_id == process_id

        master, slave = pty.openpty()
        independent = subprocess.Popen(
            [
                "tmux",
                "-S",
                str(socket),
                "attach-session",
                "-f",
                "active-pane,ignore-size",
                "-t",
                f"{shell.names.view_session}:main",
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env=environment,
        )
        os.close(slave)
        clients.append((independent, master))
        assert wait_for(
            lambda: (
                "active-pane"
                in tmux.run("list-clients", "-F", "#{client_flags}", check=False).stdout
            )
        )
        with pytest.raises(TmuxViewError) as caught:
            tmux.present_surface(
                prefix="test",
                generation_id=GENERATION,
                view_id=VIEW,
                mode=ViewMode.NAVIGATOR,
                surface_id=SURFACE,
            )
        assert caught.value.code == "independent_client_unsupported"
        independent.terminate()
        independent.wait(timeout=3)
        os.close(master)
        clients.clear()

        tmux.run("resize-pane", "-Z", "-t", surface.pane_id)
        assert tmux.inspect_shell("test", GENERATION, VIEW, ViewMode.NAVIGATOR).zoomed

        direct = tmux.set_mode(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            current_mode=ViewMode.NAVIGATOR,
            target_mode=ViewMode.DIRECT,
            sidebar_command=("sleep", "60"),
        )
        assert direct.sidebar is None
        assert direct.active.process_id == process_id
        navigator = tmux.set_mode(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            current_mode=ViewMode.DIRECT,
            target_mode=ViewMode.NAVIGATOR,
            sidebar_command=("sleep", "60"),
        )
        assert navigator.sidebar is not None
        assert navigator.active.process_id == process_id
        assert navigator.zoomed

        tmux.run(
            "respawn-pane", "-k", "-t", navigator.sidebar.pane_id, "/usr/bin/false"
        )
        assert wait_for(lambda: tmux._pane(navigator.sidebar.pane_id).dead)
        restarted = tmux.restart_sidebar(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            sidebar_command=("sleep", "60"),
        )
        assert restarted.sidebar is not None
        assert not restarted.sidebar.dead
        assert restarted.active.process_id == process_id
    finally:
        for client, master in clients:
            client.terminate()
            client.wait(timeout=3)
            os.close(master)
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            check=False,
            capture_output=True,
        )


def test_server_restart_changes_generation_and_invalidates_old_shell(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "tmux.sock"
    tmux = TmuxExecutor(socket)
    first = tmux.server_evidence(HOST, observed_at=10)
    tmux.create_shell(
        prefix="test",
        generation_id=GENERATION,
        view_id=VIEW,
        frame_id=FRAME,
        mode=ViewMode.DIRECT,
        sidebar_command=("sleep", "60"),
    )
    subprocess.run(
        ["tmux", "-S", str(socket), "kill-server"],
        check=True,
        capture_output=True,
    )
    second = tmux.server_evidence(HOST, observed_at=20)
    try:
        assert second.tmux_server_id != first.tmux_server_id
        with pytest.raises(TmuxViewError):
            tmux.inspect_shell("test", GENERATION, VIEW, ViewMode.DIRECT)
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            check=False,
            capture_output=True,
        )


def test_observing_missing_server_does_not_create_it(tmp_path: Path) -> None:
    socket = tmp_path / "tmux.sock"
    tmux = TmuxExecutor(socket)

    with pytest.raises(TmuxViewError) as caught:
        tmux.observe_server_evidence(HOST, observed_at=10)

    assert caught.value.code == "tmux_unavailable"
    assert not socket.exists()


def test_exact_client_switch_and_replacement_are_single_client_scoped(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "tmux.sock"
    tmux = TmuxExecutor(socket)
    client: subprocess.Popen[bytes] | None = None
    master: int | None = None
    try:
        tmux.server_evidence(HOST, observed_at=10)
        first = tmux.create_shell(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW,
            frame_id=FRAME,
            mode=ViewMode.DIRECT,
            sidebar_command=("sleep", "60"),
        )
        second = tmux.create_shell(
            prefix="test",
            generation_id=GENERATION,
            view_id=VIEW_TWO,
            frame_id=FRAME_TWO,
            mode=ViewMode.DIRECT,
            sidebar_command=("sleep", "60"),
        )
        master, slave = pty.openpty()
        environment = dict(os.environ)
        environment["TERM"] = "xterm-256color"
        environment.pop("TMUX", None)
        client = subprocess.Popen(
            tmux.attach_argv("test", VIEW),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            env=environment,
        )
        os.close(slave)
        assert wait_for(lambda: tmux.run("list-clients", check=False).returncode == 0)
        client_name = tmux.exact_client_for_pane(first.active.pane_id)
        tmux.switch_exact_client(
            client_name=client_name,
            source_pane_id=first.active.pane_id,
            target_session=tmux.names("test", VIEW_TWO).view_session,
        )
        assert tmux.exact_client_for_pane(second.active.pane_id) == client_name
        tmux.replace_exact_client(
            client_name=client_name,
            source_pane_id=second.active.pane_id,
            command=("/usr/bin/true",),
        )
        assert client.wait(timeout=3) == 0
        client = None
    finally:
        if client is not None:
            client.terminate()
            client.wait(timeout=3)
        if master is not None:
            os.close(master)
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"],
            check=False,
            capture_output=True,
        )
