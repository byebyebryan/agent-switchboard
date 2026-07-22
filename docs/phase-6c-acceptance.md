# Phase 6C Acceptance

Date: 2026-07-22

Status: accepted behind the private replacement boundary

Phase 6C replaces the retained feasibility spike with registry-backed view
lifecycle code, a bounded tmux executor, a compact Textual navigator, and one
private CLI used only for isolated development. The installed `swbctl 0.2.0`
and live user state were not changed.

## Accepted behavior

- A view exposes one `main` window. Its holding panes live in a separate,
  unattached session and cannot become visible through normal view navigation.
- Dead placeholders keep both containers durable without an anchor process.
- Generation, view, frame, surface, and role metadata fence every inspected or
  moved pane. Server identity is derived from socket path, PID, and start time.
- Project open single-flights the lazy workspace, preserves the current project
  descendant, and focuses an existing owner rather than stealing affinity.
- Focus and mode changes use durable request IDs, revision CAS, execution
  leases, inspected transport phases, and atomic presentation commit.
- Navigator/direct toggles preserve provider pane and process identity and
  restore native zoom. A dead sidebar is restarted independently.
- Managed mutation rejects a tmux client using independent `active-pane`.
  The view window declares `window-size=latest`; read-only observers can use
  tmux `ignore-size` without taking geometry authority.
- Attach revalidates the exact shell before recording the attach revision.
  Presentation returns only `focus`, one leased `attach`, or bounded `blocked`.
- Retirement rejects live surfaces, retires the registry view first, removes
  only its exact two tmux sessions, and orphans placements so a later view can
  own the frames.
- Server-generation loss clears stale pane/process locators, revokes surface
  capabilities, recreates a placeholder shell, and leaves provider-backed
  views degraded with an explicit exact-resume recovery target.
- The resident navigator projects only NavigatorState v1 and provides compact
  Projects, History/open-frame, Recovery, and Settings panels. Switching to
  direct mode runs through an external short-lived executor, so killing the
  sidebar cannot interrupt the durable mode commit.

## Automated evidence

The private Phase 6 suite passes 60 tests. Its integrated path imports a real
CutoverBundle into a staged generation, proves mutation is blocked, commits
with paired evidence, opens a project through the private CLI on an isolated
tmux socket, starts a live Textual sidebar, changes mode, and returns a bounded
attach command. Separate PTY tests cover detach survival, zoom, dead-sidebar
restart, independent-client rejection, server restart, placement focus,
retirement/reopen, desktop leases, and projection.

```sh
.venv/bin/pytest -q tests/test_v3_*.py
.venv/bin/ruff format --check src/agent_switchboard/_v3 tests/test_v3_*.py
.venv/bin/ruff check src/agent_switchboard/_v3 tests/test_v3_*.py
```

## Installed-provider no-model evidence

Both provider probes used a unique tmux socket and disposable `HOME`,
`XDG_CONFIG_HOME`, and `XDG_STATE_HOME`. They started the native interactive
TUI with no prompt, performed no model turn, verified a live pane, moved it
through holding, reconstructed direct/navigator composition, checked stable
pane/process identity, killed the private tmux server, and removed the
disposable home.

| Provider | Version | Host | Result |
| --- | --- | --- | --- |
| Codex | `0.144.6` | local | pane alive; process stable; navigator restored |
| Claude Code | `2.1.216` | `snap.lan` | pane alive; process stable; navigator restored |

Claude Code was not installed locally, so its isolated probe ran on `snap.lan`.
It did not modify either repository or the host's persistent tmux state.

## Boundary to Phase 6D

Phase 6C detects provider loss and constructs the exact recovery target; it
does not resume or bind provider UUIDs. Exact Codex/Claude resume, guarded
launch, Surface binding, frame-scoped capability issue, and post-turn control
belong to Phase 6D. DMS replacement and real compositor focus remain Phase 6E.
