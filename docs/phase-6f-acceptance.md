# Phase 6F Terminal-Native Acceptance

Date: 2026-07-22

Status: implementation closure complete; isolated managed-session acceptance pending

Phase 6F makes the resident navigator and terminal-native managed-session path
complete enough for an isolated acceptance trial. Workflow adoption remains a
later explicit user decision. Acceptance does not restart DMS, replace the
user's tmux server, or claim authority over existing Codex or Claude Code
sessions.

## Implemented behavior

- `view enter` resolves a project, exact view/frame, or recovery on its owner
  host and selects navigator or direct mode in one command.
- A plain shell execs the exact tmux or configured SSH attachment. A managed
  client stays in place for its current view, switches exactly once for another
  local view, or is replaced only after remote owner preflight.
- Multiple/no exact invoking-client matches block. Cross-host replacement
  carries bounded pane-local hop evidence and the fifth nested hop blocks with
  an exact detach-and-enter-directly instruction.
- Manual focus fences the source before movement, requires an input-fenced live
  target, and atomically commits placements, view cursor, and same-WorkContext
  foreground authority. Known/uncertain background activity requires explicit
  confirmation.
- A provably restored pre-commit failure returns the view to ready with source
  authority unchanged. Uncertain presentation opens recovery and leaves both
  panes input-fenced.
- The resident Textual app has `Views`, `Projects`, `Tasks`, `History`,
  `Recovery`, and `Settings`. Breadcrumb, activity, attention,
  transition/control, and action status remain visible while one bounded
  asynchronous action runs.
- `n` starts the configured provider in an empty foreground workspace. The
  equivalent owner-local command is `frame start`; project/view entry remains
  navigation-only and does not add a provider picker.
- First-session membership, launch/surface staging, placement ownership, and
  WorkContext acquisition commit atomically. Pre-execution failure restores the
  exact empty workspace; ambiguity after provider execution opens recovery.
- Local state refreshes once per second. Remote state refreshes at the
  configured interval or explicitly with `r`. Structured action failures remain
  visible; unsafe background transfer is retried only after `y` confirmation.
- Closed frames retain bounded provider/runtime/activity/session-count evidence
  in read-only History and never become a project entry target.
- `b` performs Back, `c` performs Human close, and `d` selects direct mode. A
  capability-bound `switchboard_mode("navigator")` tool returns from direct
  mode without a global tmux binding or mandatory skill.

Phase 6F task-to-task selection navigates among existing open sibling frames.
It does not create recursive children or generalize return across nested
ancestry; that remains Phase 6G.

## Automated evidence

The earlier terminal-shell implementation commit
`0477332b440802e3501b8499ea6bc37b404c15ee` passed 113 tests on both the local
host and `snap.lan`. That evidence did not start a managed workspace from the
public TUI and therefore did not close Phase 6F.

The `0.3.1` closure suite adds:

- fresh project-view to managed workspace start without direct registry
  seeding;
- exact pre-provider rollback of session, surface, placement, WorkContext, and
  staged pane;
- immediate task-push eligibility after first-session binding;
- navigator `n` routing through public `frame start`;
- imported-session WorkContext reacquisition;
- stopped provider-session bookkeeping on completed/dismissed child cleanup;
  and
- ten-second default hooks pinned to the resolved immutable release path.

The exact closure implementation commit
`cb28da06d5f2f79864be3e11ebeeef3aec8608ad` passed all 117 local tests,
repository-wide Ruff lint/format, compile checks, and `git diff --check`.

Existing coverage still includes:

- authority-safe focus, explicit background confirmation, atomic foreground
  transfer, exact rollback, and uncertainty recovery;
- plain, in-place, local exact-client, remote-preflight, and hop-limit entry;
- PTY-backed tmux client switching and `detach-client -E` replacement;
- the injected-runner Textual app, one-action state, structured failure, and
  confirmation retry;
- closed-history projection and bounded current-session summaries;
- capability-bound navigator/direct tool mutation; and
- the existing workspace/one-child Back, Human close, Complete-return,
  control-turn, recovery, and provider command contracts.

Remote suite results and managed lifecycle evidence are recorded only after the
closure candidate is installed. No test may use the user's normal tmux server
or native sessions.

## Prior artifact and clean-install evidence

The earlier `0.3.0` build produced byte-identical wheel and sdist artifacts,
and the distribution verifier passed exact member, source-byte, metadata,
migration, removed-module, CRC, and archive-safety audits.

The wheel was installed into a clean virtual environment with Textual `8.2.8`.
The installed `swbctl 0.3.0` and navigator module loaded successfully, and a
temporary configuration completed fresh `init`, canonical `state navigator`,
and compare-and-swap `reset` without hooks, providers, DMS, or persistent state.
The committed `0.3.1` source must repeat the reproducible artifact audit and
clean-install smoke before live acceptance.

## Prior disposable provider and remote-owner evidence

Provider probes used unique temporary homes and tmux sockets, started no prompt
and made no model request:

| Provider | Version | Host | Evidence |
| --- | --- | --- | --- |
| Codex | `0.145.0` | local | native `codex` pane remained live in isolated tmux |
| Claude Code | `2.1.218` | `snap.lan` | native `claude` pane remained live in isolated tmux |

These were provider/shell probes, not a managed workspace lifecycle. Only a
dedicated Codex auth file was copied, with mode `0600`, into its temporary home.
No dedicated Claude credential file existed, so no persistent Claude
configuration was copied. Both tmux servers and homes were removed by their
owning probes.

On `snap.lan`, the exact implementation commit used temporary Config/State
roots and an isolated tmux socket to execute owner-local `view enter`
preflight, create a navigator view, toggle direct and back to navigator, and
verify the final `sidebar:live` plus `active-placeholder:dead` topology. The
installed `swbctl` route was deliberately not replaced for acceptance; fixed
SSH argv, owner identity, attach construction, and exact-client replacement are
covered by deterministic production-path tests.

## Pending managed-session gate

Using a disposable project/worktree, isolated Switchboard roots, an isolated
tmux socket, and new provider UUIDs:

1. install the committed `0.3.1` candidate as the route for new Switchboard
   actions without stopping native sessions;
2. install only Switchboard-owned global hook handlers, pinned to that immutable
   release, and review Codex trust manually through `/hooks`;
3. enter the project in navigator mode and start its empty workspace with `n`;
4. prove trusted `SessionStart`, prompt, tool, and `Stop` delivery is confined to
   the managed pane while pre-existing sessions remain unmanaged;
5. execute workspace -> task -> Complete-return -> workspace, then detach,
   reattach, and toggle navigator/direct mode; and
6. remove the disposable roots/socket/session and preserve only bounded
   acceptance evidence.

Any failed candidate state may be discarded. Existing agent sessions, normal
tmux, and unrelated provider configuration are never stopped for this gate.

## Safety and adoption boundary

No acceptance step:

- removes or modifies unrelated Codex/Claude hooks or edits provider trust
  state programmatically;
- stopped, restarted, detached, resumed, or sent input to an existing agent;
- killed or replaced the user's tmux server;
- changed installed Switchboard state or inferred adoption;
- touched the DMS repository, plugin, cache, or service; or
- required coordinated host downtime.

Phase 6F implementation is closed, but acceptance is not. Native tooling
remains the active workflow until the user explicitly chooses adoption. Phase
6G remains blocked until the disposable managed lifecycle above passes.
