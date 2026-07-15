#!/usr/bin/env python3
"""Verify client-gated startup and exact-client routing on an isolated tmux server."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import pty
import struct
import subprocess
import tempfile
import time
import termios
import uuid


def run(
    socket: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        check=check,
        text=True,
        capture_output=True,
    )


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def attach_client(socket: str, target: str) -> tuple[subprocess.Popen[bytes], int, str]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    client_name = os.ttyname(slave)
    environment = os.environ.copy()
    environment["TERM"] = "xterm-256color"
    process = subprocess.Popen(
        ["tmux", "-L", socket, "attach-session", "-t", target],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=environment,
    )
    os.close(slave)
    return process, master, client_name


def bootstrap_command(socket: str, target: str, marker: Path) -> str:
    provider = f"printf started > {marker}; exec sleep 30"
    return (
        'while [ "$(tmux -L '
        + socket
        + " display-message -p -t "
        + target
        + " '#{window_active_clients}')\" = 0 ]; do sleep 0.05; done; "
        + "exec sh -c '"
        + provider
        + "'"
    )


def session_attached_command(socket: str, target: str, provider: str) -> str:
    return (
        'while [ "$(tmux -L '
        + socket
        + " display-message -p -t "
        + target
        + " '#{session_attached}')\" = 0 ]; do sleep 0.05; done; exec "
        + provider
    )


def main() -> int:
    socket = "asb-spike-" + uuid.uuid4().hex[:10]
    client_process: subprocess.Popen[bytes] | None = None
    client_master: int | None = None
    results: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="asb-tmux-") as tmp:
        tmp_path = Path(tmp)
        marker = tmp_path / "provider-started"
        try:
            run(
                socket,
                "new-session",
                "-d",
                "-s",
                "workspace",
                "-n",
                "manager",
                "sleep",
                "30",
            )
            run(
                socket,
                "new-window",
                "-d",
                "-t",
                "workspace",
                "-n",
                "target",
                "sleep",
                "30",
            )
            run(socket, "select-window", "-t", "workspace:manager")
            window_id = run(
                socket,
                "display-message",
                "-p",
                "-t",
                "workspace:target",
                "#{window_id}",
            ).stdout.strip()
            run(
                socket,
                "respawn-pane",
                "-k",
                "-t",
                window_id,
                bootstrap_command(socket, window_id, marker),
            )
            pane_pid_before = run(
                socket, "display-message", "-p", "-t", window_id, "#{pane_pid}"
            ).stdout.strip()

            time.sleep(0.25)
            results["blocked_without_client"] = not marker.exists()

            client_process, client_master, client_name = attach_client(
                socket, "workspace"
            )
            attached = wait_for(
                lambda: (
                    client_name
                    in run(
                        socket, "list-clients", "-F", "#{client_name}"
                    ).stdout.splitlines()
                )
            )
            results["client_attached"] = attached
            time.sleep(0.25)
            results["blocked_on_other_window"] = not marker.exists()

            switch = run(
                socket,
                "switch-client",
                "-c",
                client_name,
                "-t",
                "workspace:target",
                check=False,
            )
            results["target_switch_succeeded"] = switch.returncode == 0
            results["started_on_target_view"] = wait_for(marker.exists)
            pane_pid_after = run(
                socket, "display-message", "-p", "-t", window_id, "#{pane_pid}"
            ).stdout.strip()
            results["exec_preserved_pane_pid"] = pane_pid_before == pane_pid_after

            client_process.terminate()
            client_process.wait(timeout=3)
            os.close(client_master)
            client_process = None
            client_master = None
            wait_for(
                lambda: (
                    client_name
                    not in run(
                        socket, "list-clients", "-F", "#{client_name}"
                    ).stdout.splitlines()
                )
            )
            stale = run(
                socket,
                "switch-client",
                "-c",
                client_name,
                "-t",
                "workspace:manager",
                check=False,
            )
            results["stale_client_rejected"] = stale.returncode != 0

            expiry_marker = tmp_path / "expired"
            run(
                socket,
                "new-session",
                "-d",
                "-s",
                "expiry",
                "i=0; while [ $i -lt 10 ]; do i=$((i + 1)); sleep 0.05; done; "
                f"printf expired > {expiry_marker}; exit 124",
            )
            run(socket, "set-option", "-t", "expiry", "remain-on-exit", "on")
            results["unpresented_launch_expired"] = wait_for(expiry_marker.exists)
            expiry_status = run(
                socket,
                "display-message",
                "-p",
                "-t",
                "expiry:0.0",
                "#{pane_dead}:#{pane_dead_status}",
            ).stdout.strip()
            results["expiry_status_retained"] = expiry_status == "1:124"
            run(socket, "kill-session", "-t", "expiry")
            results["expired_surface_cleaned"] = (
                run(socket, "has-session", "-t", "expiry", check=False).returncode != 0
            )

            run(
                socket,
                "new-session",
                "-d",
                "-s",
                "provider-failure",
                session_attached_command(
                    socket, "provider-failure:0.0", "sh -c 'exit 42'"
                ),
            )
            run(
                socket,
                "set-option",
                "-t",
                "provider-failure",
                "remain-on-exit",
                "on",
            )
            failure_process, failure_master, _ = attach_client(
                socket, "provider-failure"
            )
            provider_failed = wait_for(
                lambda: (
                    run(
                        socket,
                        "display-message",
                        "-p",
                        "-t",
                        "provider-failure:0.0",
                        "#{pane_dead}:#{pane_dead_status}",
                    ).stdout.strip()
                    == "1:42"
                )
            )
            results["provider_start_failure_retained"] = provider_failed
            failure_process.terminate()
            failure_process.wait(timeout=3)
            os.close(failure_master)
            run(socket, "kill-session", "-t", "provider-failure")
            results["failed_surface_cleaned"] = (
                run(
                    socket, "has-session", "-t", "provider-failure", check=False
                ).returncode
                != 0
            )
        finally:
            if client_process is not None:
                client_process.terminate()
                try:
                    client_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    client_process.kill()
            if client_master is not None:
                os.close(client_master)
            run(socket, "kill-server", check=False)

    passed = all(value is True for value in results.values())
    print(json.dumps({"passed": passed, **results}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
