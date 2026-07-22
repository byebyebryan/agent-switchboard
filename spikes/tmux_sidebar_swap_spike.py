#!/usr/bin/env python3
"""Prove a fenced persistent tmux view without proxying provider panes."""

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
CONTROL_PROMPT = (
    "Call transition_claim() and follow the returned transition instructions."
)
PANE_IDENTITY = "@agent_switchboard_spike_identity"
PANE_TRANSITION = "@agent_switchboard_spike_transition"


def run(
    socket: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    return subprocess.run(
        ["tmux", "-L", socket, *arguments],
        check=check,
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        env=environment,
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


def attach_client(
    socket: str,
    target: str,
    *,
    rows: int,
    columns: int,
    flags: str | None = None,
) -> AttachedClient:
    master, slave = pty.openpty()
    fcntl.ioctl(
        slave,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", rows, columns, 0, 0),
    )
    client_name = os.ttyname(slave)
    environment = dict(os.environ)
    environment["TERM"] = "xterm-256color"
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    command = ["tmux", "-L", socket, "attach-session"]
    if flags is not None:
        command.extend(("-f", flags))
    command.extend(("-t", target))
    process = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=environment,
    )
    os.close(slave)
    return AttachedClient(process, master, client_name)


def client_rows(socket: str) -> tuple[tuple[str, str, str, str, str], ...]:
    result = run(
        socket,
        "list-clients",
        "-F",
        "#{client_name}\t#{session_name}\t#{window_name}\t#{pane_id}\t#{client_flags}",
        check=False,
    )
    if result.returncode != 0:
        return ()
    rows: list[tuple[str, str, str, str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 5:
            rows.append((fields[0], fields[1], fields[2], fields[3], fields[4]))
    return tuple(rows)


def client_names(socket: str) -> tuple[str, ...]:
    return tuple(row[0] for row in client_rows(socket))


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


def pane_value(socket: str, pane: str, expression: str) -> str:
    return run(
        socket,
        "display-message",
        "-p",
        "-t",
        pane,
        expression,
    ).stdout.strip()


def pane_location(socket: str, pane: str) -> tuple[str, str, str, str]:
    value = pane_value(
        socket,
        pane,
        "#{session_name}\t#{window_name}\t#{window_id}\t#{pane_id}",
    )
    session, window_name, window_id, observed_pane = value.split("\t")
    return session, window_name, window_id, observed_pane


def pane_geometry(socket: str, pane: str) -> str:
    return pane_value(
        socket,
        pane,
        "#{pane_left}:#{pane_top}:#{pane_width}:#{pane_height}",
    )


def pane_alive(socket: str, pane: str) -> bool:
    return pane_value(socket, pane, "#{pane_dead}") == "0"


def pane_option(socket: str, pane: str, option: str) -> str:
    result = run(
        socket,
        "show-options",
        "-p",
        "-v",
        "-t",
        pane,
        option,
        check=False,
    )
    return "" if result.returncode != 0 else result.stdout.strip()


def pane_is_authorized_main(
    socket: str,
    pane: str,
    authorization: Path,
) -> bool:
    if not authorization.exists():
        return False
    try:
        session, window_name, _window_id, observed_pane = pane_location(socket, pane)
    except (subprocess.CalledProcessError, ValueError):
        return False
    if (
        session != "view"
        or window_name != "main"
        or observed_pane != pane
        or pane_option(socket, pane, PANE_TRANSITION) != "authorized"
    ):
        return False
    return any(
        row[1] == "view" and row[2] == "main" and row[3] == pane
        for row in client_rows(socket)
    )


def tmux_generation(socket: str) -> tuple[str, str, str]:
    value = run(
        socket,
        "display-message",
        "-p",
        "#{socket_path}\t#{pid}\t#{start_time}",
    ).stdout.strip()
    socket_path, server_pid, start_time = value.split("\t")
    return socket_path, server_pid, start_time


def dummy_command(
    identity: str,
    marker: Path,
    *,
    socket: str | None = None,
    pane: str | None = None,
    authorization: Path | None = None,
    input_log: Path | None = None,
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
    if socket is not None or pane is not None or authorization is not None:
        if socket is None or pane is None or authorization is None:
            raise ValueError("gate requires socket, pane, and authorization")
        arguments.extend(
            (
                "--socket",
                socket,
                "--pane",
                pane,
                "--authorization",
                str(authorization),
            )
        )
    if input_log is not None:
        arguments.extend(("--input-log", str(input_log)))
    return "exec " + shlex.join(arguments)


def run_dummy_runtime(arguments: argparse.Namespace) -> int:
    if arguments.identity is None or arguments.marker is None:
        raise SystemExit("dummy runtime requires --identity and --marker")
    gated = any(
        value is not None
        for value in (arguments.socket, arguments.pane, arguments.authorization)
    )
    if gated and not all(
        value is not None
        for value in (arguments.socket, arguments.pane, arguments.authorization)
    ):
        raise SystemExit("gate requires socket, pane, and authorization")
    if gated and not wait_for(
        lambda: pane_is_authorized_main(
            arguments.socket,
            arguments.pane,
            Path(arguments.authorization),
        ),
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
    if arguments.input_log is not None:
        line = sys.stdin.readline()
        Path(arguments.input_log).write_text(line, encoding="utf-8")
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
        "controlPrompt": "fixed-template-only",
    }

    with tempfile.TemporaryDirectory(prefix="asb-fenced-view-") as temporary:
        root = Path(temporary)
        sidebar_marker = root / "sidebar-started"
        recreated_sidebar_marker = root / "sidebar-recreated"
        project_marker = root / "project-started"
        project_input = root / "project-input"
        child_marker = root / "child-started"
        authorization = root / "child-authorized"
        transport_commit = root / "transport-committed"
        server_killed = False

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
                "main",
                "/bin/sh",
            )
            run(socket, "set-option", "-t", "view", "status", "off")
            run(socket, "set-option", "-t", "view", "destroy-unattached", "off")
            run(socket, "set-window-option", "-t", "view:main", "remain-on-exit", "on")
            main_placeholder = pane_value(socket, "view:main", "#{pane_id}")
            run(socket, "send-keys", "-t", main_placeholder, "exit", "Enter")
            checks["main_placeholder_retained_dead"] = wait_for(
                lambda: pane_value(socket, main_placeholder, "#{pane_dead}") == "1"
            )

            run(
                socket,
                "new-session",
                "-d",
                "-x",
                "140",
                "-y",
                "40",
                "-s",
                "hold",
                "-n",
                "placeholder",
                "/bin/sh",
            )
            run(socket, "set-option", "-t", "hold", "status", "off")
            run(socket, "set-option", "-t", "hold", "destroy-unattached", "off")
            run(
                socket,
                "set-window-option",
                "-t",
                "hold:placeholder",
                "remain-on-exit",
                "on",
            )
            hold_placeholder = pane_value(socket, "hold:placeholder", "#{pane_id}")
            run(socket, "send-keys", "-t", hold_placeholder, "exit", "Enter")
            checks["holding_placeholder_retained_dead"] = wait_for(
                lambda: pane_value(socket, hold_placeholder, "#{pane_dead}") == "1"
            )

            # A hostile global default must not destroy Switchboard-owned sessions.
            run(socket, "set-option", "-g", "destroy-unattached", "on")
            generation_before = tmux_generation(socket)

            run(
                socket,
                "respawn-pane",
                "-k",
                "-t",
                main_placeholder,
                dummy_command("sidebar", sidebar_marker),
            )
            sidebar_pane = main_placeholder
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
                dummy_command(
                    "project",
                    project_marker,
                    input_log=project_input,
                ),
            ).stdout.strip()
            child_pane = run(
                socket,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                "hold:",
                "-n",
                "staged-child",
                "sleep 30",
            ).stdout.strip()

            for pane, identity in (
                (sidebar_pane, "view-sidebar"),
                (project_pane, "project-surface"),
                (child_pane, "child-surface"),
            ):
                run(socket, "set-option", "-p", "-t", pane, PANE_IDENTITY, identity)
            run(
                socket,
                "set-option",
                "-p",
                "-t",
                child_pane,
                PANE_TRANSITION,
                "unauthorized",
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
                    authorization=authorization,
                ),
            )
            run(socket, "select-window", "-t", "view:main")
            run(socket, "select-pane", "-t", project_pane)

            checks["sidebar_started"] = wait_for(sidebar_marker.exists)
            checks["project_started"] = wait_for(project_marker.exists)
            checks["child_blocked_without_authorization"] = not child_marker.exists()
            checks["view_exposes_only_main_window"] = run(
                socket,
                "list-windows",
                "-t",
                "view",
                "-F",
                "#{window_name}",
            ).stdout.splitlines() == ["main"]

            sidebar_pid = pane_value(socket, sidebar_pane, "#{pane_pid}")
            project_pid = pane_value(socket, project_pane, "#{pane_pid}")
            sidebar_width = pane_value(socket, sidebar_pane, "#{pane_width}")
            right_slot_geometry = pane_geometry(socket, project_pane)

            first_client = attach_client(socket, "view:main", rows=40, columns=140)
            clients.append(first_client)
            checks["first_client_attached"] = wait_for(
                lambda: first_client.name in client_names(socket)
            )
            checks["project_initially_active"] = wait_for(
                lambda: client_pane(socket, first_client.name) == project_pane
            )

            # Even an explicitly attached holding client cannot satisfy the gate.
            holding_client = attach_client(
                socket,
                "hold:staged-child",
                rows=32,
                columns=100,
            )
            clients.append(holding_client)
            checks["holding_client_attached"] = wait_for(
                lambda: holding_client.name in client_names(socket)
            )
            time.sleep(0.25)
            checks["holding_selection_did_not_start_child"] = not child_marker.exists()
            holding_client.close()
            clients.remove(holding_client)

            second_client = attach_client(
                socket,
                "view:main",
                rows=24,
                columns=80,
                flags="read-only,ignore-size",
            )
            clients.append(second_client)
            checks["unequal_read_only_client_attached"] = wait_for(
                lambda: second_client.name in client_names(socket)
            )
            second_row = next(
                row for row in client_rows(socket) if row[0] == second_client.name
            )
            checks["read_only_client_does_not_drive_size"] = (
                "read-only" in second_row[4] and "ignore-size" in second_row[4]
            )

            active_client = attach_client(
                socket,
                "view:main",
                rows=30,
                columns=100,
                flags="active-pane,ignore-size",
            )
            clients.append(active_client)
            checks["active_pane_client_detected_as_unsupported"] = wait_for(
                lambda: any(
                    row[0] == active_client.name and "active-pane" in row[4]
                    for row in client_rows(socket)
                )
            )
            active_client.close()
            clients.remove(active_client)

            # Durable intent exists, but location and metadata still block exec.
            authorization.write_text("authorized\n", encoding="ascii")
            time.sleep(0.25)
            checks["authorization_without_main_placement_still_blocked"] = (
                not child_marker.exists()
            )
            run(
                socket,
                "set-option",
                "-p",
                "-t",
                child_pane,
                PANE_TRANSITION,
                "authorized",
            )

            run(
                socket,
                "swap-pane",
                "-d",
                "-s",
                child_pane,
                "-t",
                project_pane,
                ";",
                "select-pane",
                "-t",
                child_pane,
                ";",
                "select-pane",
                "-d",
                "-t",
                project_pane,
            )
            checks["child_started_only_after_authorized_main_placement"] = wait_for(
                child_marker.exists
            )
            checks["swap_before_locator_commit_is_observable"] = (
                pane_location(socket, child_pane)[:2] == ("view", "main")
                and pane_location(socket, project_pane)[:2]
                == ("hold", "staged-child")
                and pane_option(socket, child_pane, PANE_IDENTITY) == "child-surface"
                and pane_option(socket, project_pane, PANE_IDENTITY)
                == "project-surface"
            )
            checks["right_slot_geometry_preserved"] = (
                pane_geometry(socket, child_pane) == right_slot_geometry
            )
            transport_commit.write_text("committed\n", encoding="ascii")
            checks["locator_commit_followed_inspection"] = transport_commit.exists()

            # Complete-and-return: parent first, fixed control prompt, input fenced.
            run(
                socket,
                "swap-pane",
                "-d",
                "-s",
                project_pane,
                "-t",
                child_pane,
                ";",
                "select-pane",
                "-t",
                project_pane,
                ";",
                "select-pane",
                "-e",
                "-t",
                project_pane,
                ";",
                "send-keys",
                "-c",
                first_client.name,
                "-t",
                project_pane,
                "-l",
                "--",
                CONTROL_PROMPT,
                ";",
                "send-keys",
                "-c",
                first_client.name,
                "-t",
                project_pane,
                "Enter",
                ";",
                "select-pane",
                "-d",
                "-t",
                project_pane,
            )
            checks["parent_presented_before_child_cleanup"] = wait_for(
                lambda: all(
                    client_pane(socket, client.name) == project_pane
                    for client in (first_client, second_client)
                )
            )
            checks["control_prompt_delivered_exactly_once"] = wait_for(
                lambda: project_input.exists()
                and project_input.read_text(encoding="utf-8") == CONTROL_PROMPT + "\n"
            )
            checks["parent_input_remained_fenced_after_submit"] = (
                pane_value(socket, project_pane, "#{pane_input_off}") == "1"
            )
            run(socket, "select-pane", "-e", "-t", project_pane)
            checks["prompt_observation_reenabled_input"] = (
                pane_value(socket, project_pane, "#{pane_input_off}") == "0"
            )

            # Safe Human-close ordering can now remove only the parked child.
            run(socket, "kill-pane", "-t", child_pane)
            checks["parked_child_cleanup_preserved_main"] = (
                pane_alive(socket, project_pane)
                and pane_location(socket, project_pane)[:2] == ("view", "main")
            )

            # A mode change requested while zoomed restores that display choice.
            run(socket, "resize-pane", "-Z", "-t", project_pane)
            checks["provider_zoomed_before_mode_toggle"] = (
                pane_value(socket, project_pane, "#{window_zoomed_flag}") == "1"
            )
            run(socket, "resize-pane", "-Z", "-t", project_pane)
            run(socket, "kill-pane", "-t", sidebar_pane)
            checks["direct_mode_removed_sidebar_externally"] = set(
                run(
                    socket,
                    "list-panes",
                    "-t",
                    "view:main",
                    "-F",
                    "#{pane_id}",
                ).stdout.splitlines()
            ) == {project_pane}
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
                project_pane,
                dummy_command("sidebar-recreated", recreated_sidebar_marker),
            ).stdout.strip()
            run(
                socket,
                "set-option",
                "-p",
                "-t",
                sidebar_pane,
                PANE_IDENTITY,
                "view-sidebar",
            )
            checks["navigator_sidebar_recreated"] = wait_for(
                recreated_sidebar_marker.exists
            )
            run(socket, "resize-pane", "-Z", "-t", project_pane)
            checks["zoom_restored_after_mode_toggle"] = (
                pane_value(socket, project_pane, "#{window_zoomed_flag}") == "1"
                and pane_value(socket, project_pane, "#{pane_pid}") == project_pid
            )
            run(socket, "resize-pane", "-Z", "-t", project_pane)

            checks["all_surviving_processes_preserved"] = (
                pane_alive(socket, project_pane)
                and pane_alive(socket, sidebar_pane)
                and pane_value(socket, project_pane, "#{pane_pid}") == project_pid
                and pane_option(socket, sidebar_pane, PANE_IDENTITY) == "view-sidebar"
            )
            checks["original_sidebar_was_distinct_executor_target"] = (
                sidebar_pid != project_pid
            )

            # Detach every client: per-session option must beat hostile global default.
            for client in reversed(clients):
                client.close()
            clients.clear()
            checks["all_clients_detached"] = wait_for(
                lambda: not client_names(socket)
            )
            checks["view_survived_global_destroy_unattached"] = (
                run(socket, "has-session", "-t", "=view", check=False).returncode == 0
            )
            checks["holding_survived_global_destroy_unattached"] = (
                run(socket, "has-session", "-t", "=hold", check=False).returncode == 0
            )

            observations["dummyAgentProcessesStarted"] = 2
            observations["viewWindowCount"] = 1
            observations["viewClientCountBeforeDetach"] = 2
            observations["viewServerSocket"] = generation_before[0]
            observations["viewServerStartTime"] = generation_before[2]
            observations["mainPane"] = project_pane
            observations["holdingPlaceholder"] = hold_placeholder

            # Same named socket after server loss must have a different generation.
            run(socket, "kill-server")
            server_killed = True
            if not wait_for(
                lambda: not Path(f"/proc/{generation_before[1]}").exists()
            ):
                raise RuntimeError("isolated tmux server survived kill-server")
            run(
                socket,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "replacement",
                "sleep 30",
            )
            server_killed = False
            generation_after = tmux_generation(socket)
            checks["same_socket_restart_changed_server_generation"] = (
                generation_after[0] == generation_before[0]
                and generation_after[1:] != generation_before[1:]
            )
        finally:
            for client in reversed(clients):
                client.close()
            if not server_killed:
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
        description="Probe a fenced persistent Switchboard tmux view"
    )
    parser.add_argument("--dummy-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--identity", help=argparse.SUPPRESS)
    parser.add_argument("--marker", help=argparse.SUPPRESS)
    parser.add_argument("--socket", help=argparse.SUPPRESS)
    parser.add_argument("--pane", help=argparse.SUPPRESS)
    parser.add_argument("--authorization", help=argparse.SUPPRESS)
    parser.add_argument("--input-log", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.dummy_runtime:
        return run_dummy_runtime(arguments)
    return run_probe()


if __name__ == "__main__":
    raise SystemExit(main())
