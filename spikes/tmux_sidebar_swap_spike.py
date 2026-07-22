#!/usr/bin/env python3
"""Prove a persistent sidebar can swap native agent panes without proxying them."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
import pty
import shlex
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid


COMMAND_TIMEOUT_SECONDS = 5.0
WAIT_TIMEOUT_SECONDS = 4.0
DUMMY_RUNTIME_TIMEOUT_SECONDS = 20.0


def run(
    socket: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", socket, *arguments],
        check=check,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def wait_for(predicate, timeout: float = WAIT_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@dataclass(slots=True)
class AttachedClient:
    process: subprocess.Popen[bytes]
    master: int
    name: str

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)


def attach_client(socket: str, target: str) -> AttachedClient:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    client_name = os.ttyname(slave)
    environment = dict(os.environ)
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
    return AttachedClient(process, master, client_name)


def client_names(socket: str) -> tuple[str, ...]:
    result = run(socket, "list-clients", "-F", "#{client_name}", check=False)
    if result.returncode != 0:
        return ()
    return tuple(result.stdout.splitlines())


def client_pane(socket: str, client: str) -> str | None:
    result = run(
        socket,
        "display-message",
        "-p",
        "-c",
        client,
        "#{pane_id}",
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def pane_is_viewed(socket: str, pane: str) -> bool:
    result = run(
        socket,
        "list-clients",
        "-F",
        "#{session_name}\t#{window_id}\t#{pane_id}",
        check=False,
    )
    if result.returncode != 0:
        return False
    return any(line.rsplit("\t", 1)[-1] == pane for line in result.stdout.splitlines())


def pane_value(socket: str, pane: str, expression: str) -> str:
    return run(
        socket,
        "display-message",
        "-p",
        "-t",
        pane,
        expression,
    ).stdout.strip()


def pane_location(socket: str, pane: str) -> tuple[str, str, str]:
    value = pane_value(
        socket,
        pane,
        "#{session_name}\t#{window_id}\t#{pane_id}",
    )
    session, window, observed_pane = value.split("\t")
    return session, window, observed_pane


def pane_geometry(socket: str, pane: str) -> str:
    return pane_value(
        socket,
        pane,
        "#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}",
    )


def pane_alive(socket: str, pane: str) -> bool:
    return pane_value(socket, pane, "#{pane_dead}") == "0"


def pane_option(socket: str, pane: str, option: str) -> str:
    return run(socket, "show-options", "-p", "-v", "-t", pane, option).stdout.strip()


def dummy_command(
    identity: str,
    marker: Path,
    *,
    socket: str | None = None,
    pane: str | None = None,
) -> str:
    arguments = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--dummy-runtime",
        "--identity",
        identity,
        "--marker",
        str(marker),
    ]
    if socket is not None and pane is not None:
        arguments.extend(("--socket", socket, "--pane", pane))
    return "exec " + shlex.join(arguments)


def run_dummy_runtime(arguments: argparse.Namespace) -> int:
    if arguments.identity is None or arguments.marker is None:
        raise SystemExit("dummy runtime requires --identity and --marker")
    if (arguments.socket is None) != (arguments.pane is None):
        raise SystemExit("dummy runtime requires both --socket and --pane")
    if arguments.socket is not None and not wait_for(
        lambda: pane_is_viewed(arguments.socket, arguments.pane),
        timeout=DUMMY_RUNTIME_TIMEOUT_SECONDS,
    ):
        return 124

    Path(arguments.marker).write_text(
        json.dumps(
            {"identity": arguments.identity, "pid": os.getpid()},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    while True:
        time.sleep(60)


def run_probe() -> int:
    socket = "asb-sidebar-spike-" + uuid.uuid4().hex[:10]
    clients: list[AttachedClient] = []
    checks: dict[str, bool] = {}
    observations: dict[str, object] = {
        "tmuxVersion": subprocess.run(
            ["tmux", "-V"],
            check=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        ).stdout.strip(),
        "dummyAgentProcessesStarted": 0,
        "modelTurnsStarted": 0,
        "isolatedTmuxServer": True,
    }

    with tempfile.TemporaryDirectory(prefix="asb-sidebar-view-") as temporary:
        root = Path(temporary)
        sidebar_marker = root / "sidebar-started"
        recreated_sidebar_marker = root / "sidebar-recreated"
        project_marker = root / "project-started"
        child_marker = root / "child-started"

        try:
            run(
                socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-x",
                "140",
                "-y",
                "40",
                "-s",
                "view",
                "-n",
                "anchor",
                "/bin/sh",
            )
            run(socket, "set-option", "-t", "view", "status", "off")
            run(
                socket,
                "set-window-option",
                "-t",
                "view:anchor",
                "remain-on-exit",
                "on",
            )
            anchor_pane = pane_value(socket, "view:anchor", "#{pane_id}")
            run(socket, "send-keys", "-t", anchor_pane, "exit", "Enter")
            checks["dead_anchor_retained_without_process"] = wait_for(
                lambda: pane_value(socket, anchor_pane, "#{pane_dead}") == "1"
            )

            sidebar_pane = run(
                socket,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                "view:",
                "-n",
                "main",
                dummy_command("sidebar", sidebar_marker),
            ).stdout.strip()
            project_pane = run(
                socket,
                "split-window",
                "-d",
                "-h",
                "-l",
                "104",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                sidebar_pane,
                dummy_command("project", project_marker),
            ).stdout.strip()
            child_pane = run(
                socket,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                "view:",
                "-n",
                "staged-child",
                "sleep 30",
            ).stdout.strip()
            main_window = pane_location(socket, sidebar_pane)[1]
            staged_window = pane_location(socket, child_pane)[1]

            for pane, identity in (
                (sidebar_pane, "view-sidebar"),
                (project_pane, "project-surface"),
                (child_pane, "child-surface"),
            ):
                run(
                    socket,
                    "set-option",
                    "-p",
                    "-t",
                    pane,
                    "@agent_switchboard_spike_identity",
                    identity,
                )

            run(
                socket,
                "respawn-pane",
                "-k",
                "-t",
                child_pane,
                dummy_command(
                    "child",
                    child_marker,
                    socket=socket,
                    pane=child_pane,
                ),
            )
            run(socket, "select-window", "-t", "view:main")
            run(socket, "select-pane", "-t", project_pane)

            checks["sidebar_started"] = wait_for(sidebar_marker.exists)
            checks["project_started"] = wait_for(project_marker.exists)
            checks["child_blocked_without_viewer"] = not child_marker.exists()

            sidebar_pid = pane_value(socket, sidebar_pane, "#{pane_pid}")
            project_pid = pane_value(socket, project_pane, "#{pane_pid}")
            child_pid = pane_value(socket, child_pane, "#{pane_pid}")
            sidebar_geometry = pane_geometry(socket, sidebar_pane)
            sidebar_width = pane_value(socket, sidebar_pane, "#{pane_width}")
            right_slot_geometry = pane_geometry(socket, project_pane)

            first_client = attach_client(socket, "view:main")
            clients.append(first_client)
            checks["first_client_attached"] = wait_for(
                lambda: first_client.name in client_names(socket)
            )
            checks["project_initially_active"] = wait_for(
                lambda: client_pane(socket, first_client.name) == project_pane
            )
            time.sleep(0.25)
            checks["child_blocked_in_hidden_window"] = not child_marker.exists()
            checks["session_attached_while_child_hidden"] = (
                pane_value(socket, child_pane, "#{session_attached}") == "1"
                and pane_value(socket, child_pane, "#{window_active_clients}") == "0"
            )

            run(
                socket,
                "swap-pane",
                "-d",
                "-s",
                child_pane,
                "-t",
                project_pane,
            )
            run(socket, "select-pane", "-t", child_pane)
            checks["child_swapped_into_right_slot"] = wait_for(
                lambda: client_pane(socket, first_client.name) == child_pane
            )
            checks["child_started_only_when_visible"] = wait_for(child_marker.exists)
            checks["source_and_target_locations_swapped"] = (
                pane_location(socket, child_pane)[1] == main_window
                and pane_location(socket, project_pane)[1] == staged_window
            )
            checks["right_slot_geometry_preserved"] = (
                pane_geometry(socket, child_pane) == right_slot_geometry
            )
            checks["sidebar_geometry_preserved"] = (
                pane_geometry(socket, sidebar_pane) == sidebar_geometry
            )
            checks["all_processes_survived_first_swap"] = (
                pane_value(socket, sidebar_pane, "#{pane_pid}") == sidebar_pid
                and pane_value(socket, project_pane, "#{pane_pid}") == project_pid
                and pane_value(socket, child_pane, "#{pane_pid}") == child_pid
                and pane_alive(socket, sidebar_pane)
                and pane_alive(socket, project_pane)
                and pane_alive(socket, child_pane)
            )
            checks["pane_metadata_followed_processes"] = (
                pane_option(socket, sidebar_pane, "@agent_switchboard_spike_identity")
                == "view-sidebar"
                and pane_option(
                    socket, project_pane, "@agent_switchboard_spike_identity"
                )
                == "project-surface"
                and pane_option(socket, child_pane, "@agent_switchboard_spike_identity")
                == "child-surface"
            )

            run(socket, "kill-pane", "-t", sidebar_pane)
            checks["direct_mode_removed_sidebar"] = set(
                run(
                    socket,
                    "list-panes",
                    "-t",
                    "view:main",
                    "-F",
                    "#{pane_id}",
                ).stdout.splitlines()
            ) == {child_pane}
            checks["direct_mode_preserved_agent"] = (
                pane_alive(socket, child_pane)
                and pane_value(socket, child_pane, "#{pane_pid}") == child_pid
            )
            checks["anchor_kept_view_session_alive"] = (
                pane_value(socket, anchor_pane, "#{pane_dead}") == "1"
                and pane_location(socket, anchor_pane)[0] == "view"
            )

            sidebar_pane = run(
                socket,
                "split-window",
                "-d",
                "-b",
                "-h",
                "-l",
                sidebar_width,
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                child_pane,
                dummy_command("sidebar-recreated", recreated_sidebar_marker),
            ).stdout.strip()
            run(
                socket,
                "set-option",
                "-p",
                "-t",
                sidebar_pane,
                "@agent_switchboard_spike_identity",
                "view-sidebar",
            )
            checks["navigator_mode_recreated_sidebar"] = wait_for(
                recreated_sidebar_marker.exists
            )
            sidebar_pid = pane_value(socket, sidebar_pane, "#{pane_pid}")
            checks["navigator_mode_restored_geometry"] = (
                pane_geometry(socket, sidebar_pane) == sidebar_geometry
                and pane_geometry(socket, child_pane) == right_slot_geometry
            )
            checks["mode_toggle_preserved_agent_identity"] = (
                pane_alive(socket, child_pane)
                and pane_value(socket, child_pane, "#{pane_pid}") == child_pid
                and pane_option(
                    socket, child_pane, "@agent_switchboard_spike_identity"
                )
                == "child-surface"
            )

            run(socket, "resize-pane", "-Z", "-t", child_pane)
            checks["agent_pane_zoomed"] = (
                pane_value(socket, child_pane, "#{window_zoomed_flag}") == "1"
            )
            run(socket, "resize-pane", "-Z", "-t", child_pane)
            checks["split_restored_after_zoom"] = (
                pane_value(socket, child_pane, "#{window_zoomed_flag}") == "0"
                and pane_geometry(socket, sidebar_pane) == sidebar_geometry
                and pane_geometry(socket, child_pane) == right_slot_geometry
            )

            run(
                socket,
                "swap-pane",
                "-d",
                "-s",
                project_pane,
                "-t",
                child_pane,
            )
            run(socket, "select-pane", "-t", project_pane)
            checks["rollback_restored_project"] = wait_for(
                lambda: client_pane(socket, first_client.name) == project_pane
            )
            checks["rollback_preserved_child"] = (
                pane_alive(socket, child_pane)
                and pane_value(socket, child_pane, "#{pane_pid}") == child_pid
                and pane_location(socket, child_pane)[1] == staged_window
            )

            second_client = attach_client(socket, "view:main")
            clients.append(second_client)
            checks["second_client_attached"] = wait_for(
                lambda: second_client.name in client_names(socket)
            )
            run(
                socket,
                "swap-pane",
                "-d",
                "-s",
                child_pane,
                "-t",
                project_pane,
            )
            run(socket, "select-pane", "-t", child_pane)
            checks["shared_view_clients_follow_same_active_slot"] = wait_for(
                lambda: (
                    client_pane(socket, first_client.name) == child_pane
                    and client_pane(socket, second_client.name) == child_pane
                )
            )
            checks["sidebar_remained_alongside_child"] = set(
                run(
                    socket,
                    "list-panes",
                    "-t",
                    "view:main",
                    "-F",
                    "#{pane_id}",
                ).stdout.splitlines()
            ) == {sidebar_pane, child_pane}
            checks["all_processes_survived_complete_cycle"] = (
                pane_value(socket, sidebar_pane, "#{pane_pid}") == sidebar_pid
                and pane_value(socket, project_pane, "#{pane_pid}") == project_pid
                and pane_value(socket, child_pane, "#{pane_pid}") == child_pid
                and all(
                    pane_alive(socket, pane)
                    for pane in (sidebar_pane, project_pane, child_pane)
                )
            )

            observations["dummyAgentProcessesStarted"] = 2
            observations["viewClientCount"] = len(client_names(socket))
            observations["sidebarPaneStable"] = sidebar_pane
            observations["activePaneStable"] = child_pane
            observations["stagedPaneStable"] = project_pane
            observations["deadAnchorPane"] = anchor_pane
        finally:
            for client in reversed(clients):
                client.close()
            run(socket, "kill-server", check=False)

    passed = all(checks.values())
    print(
        json.dumps(
            {"passed": passed, "checks": checks, "observations": observations},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe a persistent Switchboard sidebar and swappable agent pane"
    )
    parser.add_argument("--dummy-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--identity", help=argparse.SUPPRESS)
    parser.add_argument("--marker", help=argparse.SUPPRESS)
    parser.add_argument("--socket", help=argparse.SUPPRESS)
    parser.add_argument("--pane", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.dummy_runtime:
        return run_dummy_runtime(arguments)
    return run_probe()


if __name__ == "__main__":
    raise SystemExit(main())
