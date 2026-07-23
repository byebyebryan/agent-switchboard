#!/usr/bin/env python3
"""Render sanitized navigator state in a spike-only pane."""

from __future__ import annotations

import argparse
import time

from navigator import (
    NavigatorState,
    TransitionVisibility,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument(
        "--transition",
        choices=[item.value for item in TransitionVisibility],
        required=True,
    )
    arguments = parser.parse_args()
    state = NavigatorState(
        previous=arguments.previous,
        current=arguments.current,
        transition=TransitionVisibility(arguments.transition),
        active_tip=arguments.current,
    )
    print(state.render(), flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
