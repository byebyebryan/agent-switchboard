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
| Full core source suite and static checks | Current development environment | 429 tests passed; compileall, Ruff format, Ruff lint, and diff checks passed |
| DMS action implementation | Separate local `agent-switchboard-dms` checkout | 109 Python tests and 18 JavaScript behavior groups passed; QML formatting, Ruff, Pyright package checks, and diff checks passed |
| Live current-session adoption | Codex 0.144.4, niri, Ghostty, and existing attached tmux pane | The live pane gained one confirmed surface binding, the existing Ghostty window was focused through the title/host fallback, niri stayed at seven windows, and the tmux server PID was unchanged |
| DMS reload | Existing user DMS service and development-plugin symlink | Plugin reload and `sb:` query succeeded; DMS service PID and tmux server PID were unchanged; the separate legacy `agentSessions` plugin path was untouched |
| Codex hook installation | User Codex home | Five definitions installed with the supported ownership-safe merger; config remained unchanged and tmux remained running |

## Remaining live gate

Codex requires hook definitions to be reviewed and trusted interactively. The
installation completed, but `swbctl doctor` currently reports exactly five
`hook_untrusted` errors: `SessionStart`, `UserPromptSubmit`,
`PermissionRequest`, `PostToolUse`, and `Stop`. The isolated probe measured an
86.7 ms cold start and 77.1 ms warm p95. Switchboard intentionally cannot edit
trust state.

Run `/hooks` in Codex, review and trust those five Agent Switchboard handlers,
then rerun:

```sh
swbctl doctor
```

A healthy doctor result is required before claiming live hook coverage and the
parked-session end-to-end acceptance. The implementation and deterministic
tests are complete; this user-owned trust decision remains open.

## Remaining implementation scope

- Project-aware `prepare-new` and new local Codex session creation.
- Claude discovery/liveness from Phase 2 and Claude manager/session surface
  policy from Phase 3.
- Remote snapshot and action transport.
- Searchable TUI, curation, handoffs, and current-session agent tools.
