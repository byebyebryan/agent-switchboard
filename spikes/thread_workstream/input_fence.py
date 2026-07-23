#!/usr/bin/env python3
"""Present a native provider PTY while dropping every input byte."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
from collections.abc import Sequence


class FenceError(RuntimeError):
    pass


def _write_status(path: Path, *, child_started: bool, dropped_bytes: int) -> None:
    payload = json.dumps(
        {
            "childStarted": child_started,
            "droppedBytes": dropped_bytes,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    path.chmod(0o600)


def _copy_window_size(destination: int) -> None:
    try:
        size = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, xpixel, ypixel = struct.unpack("HHHH", size)
        fcntl.ioctl(
            destination,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, xpixel, ypixel),
        )
    except OSError:
        pass


def run_fence(status: Path, command: Sequence[str]) -> int:
    expected_root = os.environ.get("ASB_SPIKE_DISPOSABLE_ROOT")
    if (
        not expected_root
        or not status.is_absolute()
        or not status.resolve().is_relative_to(Path(expected_root).resolve())
        or not command
    ):
        raise FenceError("input fence is outside the disposable study")
    master, slave = pty.openpty()
    _copy_window_size(slave)
    child = subprocess.Popen(
        list(command),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    dropped = 0
    _write_status(status, child_started=True, dropped_bytes=dropped)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            readable, _, _ = select.select(
                [master, sys.stdin.buffer],
                [],
                [],
                0.2,
            )
            if master in readable:
                try:
                    output = os.read(master, 65536)
                except OSError:
                    output = b""
                if not output:
                    break
                os.write(sys.stdout.fileno(), output)
            if sys.stdin.buffer in readable:
                incoming = os.read(sys.stdin.fileno(), 65536)
                if not incoming:
                    break
                dropped += len(incoming)
                _write_status(status, child_started=True, dropped_bytes=dropped)
            if child.poll() is not None:
                break
    finally:
        if child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        os.close(master)
        _write_status(status, child_started=False, dropped_bytes=dropped)
    return child.returncode or 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = arguments.command
    if command and command[0] == "--":
        command = command[1:]
    try:
        return run_fence(arguments.status, command)
    except (FenceError, OSError, subprocess.SubprocessError):
        print("input fence failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
