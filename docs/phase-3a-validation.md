# Phase 3A Validation: Existing Local Codex Presentation

Date: 2026-07-16

## Status and boundary

Phase 3A implements one complete local action slice: opening an existing Codex
session through a validated tmux surface and a versioned presentation plan.
The core now owns:

- exact tmux socket/session/window/pane locators and shell-free commands;
- systemd-scoped creation of waiting tmux servers, identity metadata, bounded
  inspection, client selection, attachment, and rollback;
- atomic adoption of a trustworthy live tmux pane;
- one active leased resume intent per session, idempotent request retries, and
  a waiting bootstrap that starts only after attachment;
- a final full reconciliation and duplicate-runtime check immediately before
  `exec codex resume <uuid>`;
- atomic missed-hook recovery when one live process, its durable Codex session
  ID, and the complete launch-owned tmux locator all agree;
- versioned `focus`, `switch`, `attach`, and `blocked` presentation plans;
- revalidated `select-surface` and `attach-surface` actions that never accept a
  raw frontend tmux target.

The separate `agent-switchboard-dms` repository validates the public plan
again, focuses niri, and launches Ghostty. It hashes the opaque desktop token
into a valid Wayland application ID, retains a title-plus-host fallback for an
adopted pre-Switchboard pane, and retries failed focus with the same request ID
and `can_focus_desktop=false` before attaching. DMS owns desktop presentation;
the core remains the only owner of tmux and provider argv.

This record does not claim new-session or project preparation, Claude
workspaces, remote actions, or a TUI.

## Command surface

```sh
swbctl prepare-open <session-key> --request-id <uuid> \
  [--has-current-terminal] \
  [--current-tmux-client <id>] \
  [--can-focus-desktop] \
  [--can-launch-terminal] \
  --json
swbctl select-surface <surface-id> --client <tmux-client-id>
swbctl attach-surface <surface-id>
```

`prepare-open` always performs bounded full reconciliation first. A confirmed
managed surface is revalidated against both the registry and tmux metadata. A
live session without a trustworthy tmux pane returns the non-retryable
`unmanaged_surface` block instead of starting another Codex process. A parked
session must remain provider-resumable and have an available absolute working
directory before a lease can create its waiting surface.

New tmux servers are started with
`systemd-run --user --scope --collect --quiet --`; terminal attachment uses
`tmux -u`. The frontend sees a stable surface ID but cannot provide a locator
to either action command.

## Verification record

| Gate | Environment | Recorded result |
| --- | --- | --- |
| Core tmux and presentation tests | Isolated registries and fake exact tmux/process boundaries | Live adoption, unmanaged-live blocking, waiting bootstrap, lease conflict/idempotency, rollback, stale locator/client rejection, final duplicate check, and exact attach/select argv passed |
| Full core source suite and static checks | Current development environment | 431 tests passed; compileall, Ruff format, Ruff lint, and diff checks passed |
| DMS action implementation | Separate local `agent-switchboard-dms` checkout | 109 Python tests and 18 JavaScript behavior groups passed; QML formatting, Ruff, Pyright package checks, and diff checks passed |
| Live current-session adoption | Codex 0.144.4, niri, Ghostty, and existing attached tmux pane | The live pane gained one confirmed surface binding, the existing Ghostty window was focused through the title/host fallback, niri stayed at seven windows, and the tmux server PID was unchanged |
| DMS reload | Existing user DMS service and development-plugin symlink | Plugin reload and `sb:` query succeeded; DMS service PID and tmux server PID were unchanged; the separate legacy `agentSessions` plugin path was untouched |
| Codex hook installation | User Codex home | Five definitions installed with the supported ownership-safe merger; config remained unchanged and tmux remained running |
| Codex hook trust and doctor | Codex 0.144.4 with the five installed handlers trusted interactively | Doctor reported healthy; the isolated handler probe measured a 90.8 ms cold start and 80.3 ms warm p95 |
| Parked-session resume | One retained resumable Codex session opened through DMS into a new managed Ghostty/tmux surface | Exactly one `codex resume <uuid>` process, one managed Ghostty window, and one attached tmux client were observed; the original tmux server PID was unchanged |
| Missed-`SessionStart` recovery | The live parked-session resume did not emit a retained `SessionStart` event | Bounded reconciliation correlated the exact durable session ID, process birth, and full launch-owned tmux locator, then atomically marked the launch `bound` and established the symmetric confirmed surface binding |
| Reopen idempotency | The same live session was opened through DMS a second time | DMS returned `focused` for the existing surface; the managed window and Codex process counts both remained one |

## Live acceptance result

The five Agent Switchboard handlers were reviewed and trusted through Codex's
interactive `/hooks` flow. `swbctl doctor` is healthy, and ordinary lifecycle
events are reaching the registry.

The parked-session acceptance also exercised the documented missed-hook path:
this Codex resume did not produce a retained `SessionStart` event. Switchboard
did not assume the launch succeeded. Live reconciliation required one exact
durable provider session ID, a confirmed process birth, and a complete match to
the launch-owned tmux socket/session/window/pane locator before consuming the
pending intent. A mismatched locator is rejected atomically. The second open
then focused the bound surface without creating a duplicate runtime.

This completes the Phase 3A trusted parked-session acceptance. It does not
claim that every Codex resume will emit `SessionStart`; correctness no longer
depends on that single event when exact live evidence is available.

## Remaining implementation scope

- Phase 2B Claude discovery, hooks, supervisor/process liveness, and normalized
  runtime truth.
- Phase 3C Claude manager/session workspace and surface policy.
- Remote snapshot and action transport.
- Searchable TUI, curation, handoffs, and current-session agent tools.
