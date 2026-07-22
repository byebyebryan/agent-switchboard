# Agent Switchboard Design

Date: 2026-07-21

Status: accepted Phase 6 replacement design; implementation pending

Target release: `0.3.0`

The implemented `0.2.0` task-first product is historical input, not a
compatibility boundary. Phase 6 replaces its registry, protocols, command
surface, terminal UI, and DMS model in one coordinated cutover. The detailed
interaction contract is in [View and Frame Workflow](view-workflow.md), and the
delivery/cutover sequence is in [Phase 6 Plan](phase-6-plan.md).

## Summary

Switchboard owns durable user views around unmodified Codex and Claude Code
terminal UIs. A view is a host-local navigation cursor backed by tmux. It may
show a compact Switchboard navigator beside the active provider pane or operate
as one direct provider pane with no Switchboard UI process.

The durable unit of user work is a `Frame`. Each project has one workspace root
frame per host. Focused tasks are child frames. A frame may roll through several
provider sessions without changing its identity, title, lineage, work context,
or place in a view. Provider UUIDs and provider-owned history remain the routing
authority for each conversation.

Switchboard does not proxy terminal I/O, re-render conversations, parse private
transcripts, run a provider-wide supervisor, or keep a host daemon alive. It
coordinates identities, tmux pane placement, bounded transition briefs,
immutable handoffs, and trusted post-turn transitions.

## Product Goals

- Enter a project workspace without first creating or selecting a task.
- Move transparently from workspace to focused task and back again.
- Preserve native provider UI state while switching projects and tasks.
- Offer a persistent navigator without requiring it in direct mode.
- Keep task creation and completion lightweight enough to become automatic.
- Keep provider sessions, terminal panes, views, frames, and checkouts as
  separate identities.
- Support local and configured remote hosts through the same bounded state and
  action interfaces.
- Recover explicitly from missed hooks, broken view containers, checkout
  conflicts, and orphaned managed panes.

## Non-goals

- Backlogs, dependencies, assignments, milestones, or project planning.
- Provider transcript ownership, synchronization, or rendering.
- A terminal emulator embedded inside Switchboard.
- Arbitrary hidden prompt injection or terminal `send-keys` automation.
- Provider subagent, background-command, or schedule orchestration.
- Cross-host pane movement inside one view.
- Automatic Git branching, worktree creation, commit, merge, or cleanup.
- Runtime compatibility with `0.2` Snapshot, Fleet, task-first TUI, DMS, CLI,
  configuration, or registry contracts.

## Core Model

| Entity | Ownership and purpose |
| --- | --- |
| `Host` | Stable authority boundary for registry, runtimes, views, and actions |
| `Project` | Configured product context and launch defaults |
| `Repository` | Stable codebase identity independent of paths and Git remotes |
| `Checkout` | Host-local filesystem view of one repository |
| `Frame` | Durable workspace or task context with lineage and session history |
| `FrameSession` | Ordered membership of an exact provider session in a frame |
| `WorkContext` | Checkout claim and foreground mutation lease for a frame stack |
| `UserView` | Durable host-local cursor, mode, revision, and active frame |
| `Surface` | Physical native-provider pane with a mutable tmux placement |
| `ViewTransition` | Durable, idempotent state change between exact frames |
| `Handoff` | Immutable bounded result and next action tied to an exact session |

### Frame

`Frame` replaces the separate workspace, task, and session-frame identities
proposed during the task-first design:

```text
Frame
  frame_id
  host_id
  project_id
  role                    workspace | task
  parent_frame_id         null only for workspace roots
  work_context_id
  title
  purpose                 optional
  preferred_provider      optional
  lifecycle_state         open | closing | closed
  close_reason            completed | dismissed | null
  current_session_key     optional
  created_by              user | agent | cutover
  created_at
  updated_at
```

Exactly one open-or-closed workspace frame exists for `(HostId, ProjectId)`.
Closing a task never closes the workspace. A reopened task reuses its frame
identity and lineage. Provider rollover appends a new `FrameSession` and
changes `current_session_key`; it does not create another user-facing task.

Sessions discovered without a frame remain provider history. They do not form
an `Inbox` product category. A focused recovery action may attach one to a new
or existing frame, but reconciliation never invents that semantic relationship.

### Work context

A `WorkContext` owns one concrete checkout claim and identifies the frame that
currently has the foreground mutation lease. Workspace-to-child flow reuses
the same work context so uncommitted state remains visible. Parked ancestors
retain lineage but not mutation authority.

Manual navigation may keep a source runtime live. If source and target share a
work context and background mutation cannot be ruled out, core requires an
explicit human override before moving the logical lease. Automatic transitions
never use that override: they require a park-safe claim and reject known or
uncertain background work.

Existing task-to-worktree uniqueness does not survive as a separate task claim.
The unique mutable-checkout claim belongs to `WorkContext`.

### User view

```text
UserView
  view_id
  host_id
  mode                    navigator | direct
  active_frame_id         optional while recovering
  state                   ready | transitioning | degraded | retired
  revision
  desktop_token
  created_at
  last_attached_at
  updated_at
```

A view can navigate every configured project and frame owned by one host. It is
not permanently attached to a project. Multiple independent views may exist on
one host. Multiple tmux clients attached to the same view intentionally mirror
one cursor and active pane; a user needing independent navigation creates a
different view.

A live provider pane has exactly one owning view. Opening that frame from
another view focuses the owner rather than stealing the pane. A stopped
provider session may be resumed into a new view after duplicate-runtime
revalidation.

Closing a terminal client only detaches it. A view remains until explicit
retirement. Retirement is allowed only when no transition is pending and no
live provider pane remains; frame and provider history survive it.

### Transition

Every view mutation carries the expected view revision. One transition lease
may be active per view. The durable record contains exact source/target frames,
work-context policy, bootstrap state, request ID, and failure information.

Manual navigation or a mode change supersedes a prepared automatic transition.
A delayed hook may mark that transition failed or ready for retry, but it may
not retarget the now-changed view.

## Native Provider Runtime

One live managed frame session consists of:

```text
one native Codex or Claude Code TUI process
+ one provider UUID
+ one managed tmux pane
+ one Surface record
+ optional placement in one UserView
```

Codex runs its normal embedded App Server and must not reuse a persistent
default-socket daemon. Claude Agent View remains disabled. Short-lived bounded
Codex App Server stdio clients may discover sessions, precreate a zero-turn
thread, or project an initial title; they never become the provider runtime.

Provider commands execute directly in tmux. Switchboard leaves no wrapper
around them and never proxies stdin/stdout. A compact navigator may remain in
an adjacent pane, but provider input and rendering remain native.

## tmux View Shell

Each view uses one opaque tmux session:

```text
as-view-<opaque-id>
  anchor        hidden dead remain-on-exit pane
  main          [optional sidebar] [active provider pane]
  parked-*      hidden provider panes
  staged-*      attach-gated provider bootstraps
```

The dead anchor retains the tmux session without a resident process. Navigator
mode runs a compact sidebar in the left pane. Direct mode removes that pane
and process completely. Toggling back recreates it from registry state without
changing the active provider pane or process. tmux zoom is a temporary display
choice and does not change the durable view mode.

Provider frames move with `swap-pane`. Pane-scoped surface metadata follows the
process. A move is coordinated as:

1. record durable intent with source/target pane IDs and expected view revision;
2. execute the exact tmux pane swap;
3. re-read pane IDs, metadata, windows, and geometry;
4. update both surface locators, active frame, revision, and transition in one
   SQLite transaction;
5. reverse the swap when immediate validation fails, or let reconciliation
   finish a crash-interrupted intent from pane metadata.

Production code kills only a specifically owned pane/window/session after
revalidation. It never invokes `tmux kill-server` or restarts the user's server.

An attach-gated bootstrap starts a provider only after its exact pane owns
input in a viewed window. `session_attached` alone is insufficient because a
staged pane may share the same attached view session while remaining hidden.

## Public State and Actions

Phase 6 introduces a new public generation. Old protocol types and parsers are
deleted rather than retained as aliases.

### HostState v1

`HostState v1` is emitted by one owning host. It contains bounded host,
project, repository, checkout, view, frame, work-context, transition, provider
session summary, surface-placement, and recovery records. It never contains
prompts, transcripts, provider argv, SSH targets, credentials, or unrestricted
filesystem data.

### NavigatorState v1

`NavigatorState v1` is the local core's bounded aggregation of owner-host
states. It preserves host authority, reachability, last-good staleness, and
host-qualified identities. It projects only what navigator/DMS frontends need:

- views and their active frame summary;
- project entry routes;
- navigable frame summaries;
- structural recovery records; and
- bounded warnings/truncation.

It replaces both Fleet and the frontend-specific task/Inbox model.

### ViewAction v1

`ViewAction v1` represents a host-qualified `focus`, `switch`, `attach`, or
`blocked` result. It carries opaque view and desktop identities, never tmux
targets or provider commands. Request IDs make prepare/open retries idempotent.
Cursor mutations additionally require `expectedViewRevision`.

### Command surface

The replacement public command tree is:

```text
swbctl state host --json
swbctl state navigator [--refresh] --json

swbctl view list|show|open|focus|attach|mode|retire|recover
swbctl frame list|show|push|back|complete|close|reopen
swbctl project ...
swbctl session show|stop ...
swbctl hooks ...
swbctl doctor
swbctl reconcile
swbctl agent-mcp
```

There are no `snapshot`, `fleet`, `prepare-open`, `prepare-task`,
`prepare-history`, `select-surface`, `attach-surface`, task-first CRUD, or
compatibility alias commands in `0.3`.

## Agent Authority and Trusted Hooks

Managed provider sessions receive a random capability bound to exact host,
view, frame, session, surface, pane, launch, and expiry evidence. Agent tools
accept no source frame, source session, tmux target, launch ID, or arbitrary
command from the model.

Canonical tools are:

```text
switchboard_current()
switchboard_context()
switchboard_history(...)
task_push(title, brief, purpose?, provider?, park_safe)
task_back()
task_complete_return(summary, next_action, park_safe)
transition_claim()
transition_status()
transition_cancel()
```

Skills and provider commands may explain or alias these tools, but they are
optional and disabled independently. No ambient skill is required for routing,
memory, or transition correctness.

An agent tool records and prepares a transition while the source turn is still
active. It does not start a provider, move the visible pane, or stop its own
runtime. A trusted post-turn hook claims the exact prepared transition,
revalidates the view revision and pane, performs the bounded presentation hot
path, and returns within the configured hook budget. Short-lived settlement or
later hooks finish binding/parking; no controller daemon remains.

Child and returned-parent sessions receive one fixed visible bootstrap
instruction to call `transition_claim()`. That no-argument tool returns the
bounded brief or exact handoff only to the bound session. It is idempotent for
that session and cannot be claimed by another provider UUID. Raw user prompts
and transcript excerpts are never persisted or replayed.

## Frontends

### Navigator

The Textual frontend is replaced rather than extended. Its primary mode is a
resident compact sidebar showing the current breadcrumb, projects, open task
frames, attention, transition status, and recovery. Selecting a local frame
swaps the right pane and leaves the sidebar resident.

Focused project/catalog, settings, provider-history, and recovery panels may
use a tmux popup or temporary full-window view. They consume the new state and
actions and return to the same view. There is no Open/Inbox/Closed application
or retained task-first administrative mode.

### DMS

DMS is an entry and recovery picker only. Its new model contains:

- `Views`: focus or attach a durable view without changing its current frame;
- `Projects`: focus the owning view as-is or create a navigator-mode view when
  no owner exists; and
- `Recovery`: blocked transitions, orphaned live surfaces, checkout conflicts,
  and broken view containers.

Needs-input and offline state remain badges on ordinary view rows. DMS exposes
no task, Inbox, Closed, create, close, reopen, history, or stop rows. Core owns
all semantics and emits `NavigatorState v1`; the adapter validates, caches, and
renders a small entry-model v1.

Desktop application identity is derived from `(HostId, ViewId)`, not a surface.
DMS focuses an existing matching window or launches the configured terminal
with a fixed `swbctl view attach` command.

## Remote Hosts

Views are host-local. `state navigator` obtains bounded `HostState v1` records
over fixed SSH argv and retains last-good state with explicit offline/stale
markers. Every mutation routes to the owning host and revalidates live state;
cached data never authorizes mutation.

Selecting a remote project or view opens/focuses a separate SSH-backed terminal
view. A local view never swaps a remote provider pane into its tmux session.

## Registry and Configuration Baseline

`0.3` starts with a fresh registry schema rather than schema v11. Normal runtime
code refuses the old v10 file with a cutover-required diagnostic. A separate
offline exporter and one-time importer preserve aligned identities and evidence
without teaching the new registry old task semantics.

Config v3 preserves host, provider, remote, project, repository, and checkout
definitions while adding view defaults and conservative automation policy. The
normal parser accepts only v3; the offline cutover converter is the only v2
reader in the new source tree.

The exact backup, quiescence, import, rollback, and activation gates are in the
Phase 6 plan.

## Failure and Recovery

- A failed child preparation leaves the source frame and view unchanged.
- A child that binds before presentation failure remains one recoverable
  runtime; retry never starts a duplicate.
- A missed post-turn hook leaves a durable prepared transition but may not move
  a view after its revision changes.
- A sidebar crash does not affect the provider pane; navigator mode may restart
  the sidebar from view state.
- A missing tmux view container becomes a structural recovery item. Core may
  rebuild it only after checking for an already-live owning pane.
- A pane found under another view focuses its owner rather than relocating it.
- A failed locator commit is repaired from durable intent and pane-scoped
  surface metadata.
- An unavailable parent keeps the child usable with its handoff retained.
- An offline remote host remains visible but cannot accept mutations.
- Checkout/background conflicts block automatic transition and require an
  explicit human decision for manual navigation.

## Security and Privacy

- All external subprocesses use fixed argv, bounded time/output, and no shell.
- Agent mutation authority is current-frame and exact-pane scoped.
- Hook payloads contain identity/status evidence, not transcript content.
- State protocols exclude paths from DMS-facing projections and exclude
  credentials, SSH targets, prompts, transcripts, and provider argv entirely.
- View IDs, frame IDs, session keys, and surface IDs are opaque UUIDs.
- Provider metadata writes are presentation-only and never routing authority.
- Offline conversion reads the old registry/config only after secure backup and
  writes a new destination; it never mutates the source in place.

## Accepted Commitments

- Phase 6 is a clean replacement of the `0.2` product shape.
- Frames unify workspace and task identity; provider sessions remain separate.
- Views are durable, host-local, and independent from terminal clients.
- Navigator and direct modes share the same view/runtime machinery.
- Native provider panes move; provider processes are never attached to a new
  conversation dynamically.
- Automatic workflow uses bounded briefs, fixed visible bootstrap turns, and
  trusted hooks.
- No compatibility protocols, aliases, task-first frontend, or in-place v10
  migration ships in `0.3`.
- DMS is a dumb entry/recovery picker, not the task navigator.
- Snapshot v2 and Fleet v1 end at the `0.2` boundary.
- Recursive task-to-task flow is designed now but enabled only after one-child
  workspace flow and work-context ownership pass acceptance.
