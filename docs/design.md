# Agent Switchboard Design

Date: 2026-07-22

Status: Phase 6F implementation closure complete; isolated managed-session acceptance pending

Target release: `0.3.2`

The implemented `0.2.0` task-first product is historical input, not a
compatibility boundary. Phase 6 replaced its registry, protocols, command
surface, terminal UI, and optional desktop-adapter model in one completed
technical clean break. This did not cut the user's normal workflow over to
Switchboard; native tooling remains primary until a separate adoption gate.
Normative storage/state rules are in
[State and Control-Turn Contract](state-contract.md),
user behavior is in [View and Frame Workflow](view-workflow.md), and delivery is
in [Phase 6 Plan](phase-6-plan.md).

Deferred research is recorded in
[Cross-host usage tracking discovery](usage-tracking-discovery.md). It supplies
provider evidence and open questions only; no usage phase, public contract, or
implementation is approved.

## Summary

Switchboard owns durable user views around unmodified Codex and Claude Code
terminal UIs. A view is a host-local navigation cursor backed by tmux. Its
primary mode shows a compact resident navigator beside the active provider
pane. Direct mode keeps the same durable view and active provider as one pane
with no Switchboard UI process.

The durable unit of user work is a `Frame`. Each project has one workspace root
frame per host. Focused tasks are child frames. A frame may roll through several
provider sessions without changing its identity, title, lineage, work context,
or place in a view. Provider UUIDs and provider-owned history remain routing
authority for each conversation.

Switchboard does not proxy terminal I/O, re-render conversations, parse private
transcripts, run a provider-wide supervisor, or keep a host daemon alive. It
coordinates identities, pane placement, bounded semantic briefs, immutable
handoffs, visible fixed control turns, and trusted post-turn transitions.

## Product Goals

- Enter a project workspace without first creating or selecting a task.
- Move transparently from workspace to focused task and back again.
- Preserve native provider UI state while switching projects and tasks.
- Make the persistent navigator the primary project/task switching surface
  while preserving a minimal direct mode.
- Use agents at semantic task boundaries without adding model latency to plain
  navigation or lifecycle operations.
- Keep provider sessions, panes, views, frames, and checkouts as separate
  identities.
- Support local and configured remote hosts through bounded state/action
  interfaces.
- Recover explicitly from missed hooks, broken containers, checkout conflicts,
  uncertain control submission, and orphaned managed panes.

## Non-goals

- Backlogs, dependencies, assignments, milestones, or project planning.
- Provider transcript ownership, synchronization, or rendering.
- A terminal emulator embedded inside Switchboard.
- Arbitrary, hidden, content-bearing, or shell-oriented terminal input.
- Provider subagent, background-command, or schedule orchestration.
- Cross-host pane movement inside one view.
- Automatic Git branching, worktree creation, commit, merge, or cleanup.
- Runtime compatibility with `0.2` Snapshot, Fleet, task-first TUI, DMS, CLI,
  configuration, or registry contracts.

## Runtime safety boundary

Switchboard state is disposable; existing Codex and Claude Code sessions are
not. Installation, upgrade, reset, testing, and repair never require provider
sessions to stop or restart. A failed Switchboard remains offline, stays on its
prior version, or discards its own state while provider work continues.

Discovery is observation, not ownership. Switchboard may control or stop only
an exact surface it launched whose capability, process, pane, and generation
still match, and only for the explicit lifecycle action that owns that change.
It never kills a tmux server or uses a provider-wide outage as an activation
mechanism.

Global provider hooks must no-op when pane-local Switchboard authority is wholly
absent. Before explicit adoption, Switchboard testing uses isolated provider
homes, tmux servers, state roots, views, and new test sessions. Existing native
sessions and normal provider hook configuration remain untouched.

SSH users attach directly to a persistent tmux view. DMS and other desktop
adapters are deferred convenience entry points, not runtime or acceptance
dependencies. The full operational contract is
[Runtime Operations and Safety](operations.md).

## Core Model

| Entity | Ownership and purpose |
| --- | --- |
| `Host` | Stable authority boundary for registry, runtimes, views, and actions |
| `Project` | Configured product context and launch defaults |
| `Repository` | Stable codebase identity independent of paths and Git remotes |
| `Checkout` | Host-local filesystem view of one repository |
| `Frame` | Durable workspace or task context with lineage and session history |
| `FrameSession` | Ordered membership of an exact provider session in a frame |
| `WorkContext` | Durable stack identity plus temporary checkout/foreground claims |
| `UserView` | Durable host-local cursor, mode, revision, and active frame |
| `FramePlacement` | Owning view affinity and physical/semantic placement of a frame |
| `Surface` | Physical native-provider pane with exact runtime evidence |
| `ViewTransition` | Durable, idempotent state change between exact frames |
| `Handoff` | Immutable bounded result and next action tied to an exact session |
| `ControlTurn` | One visible fixed prompt submitted at a verified boundary |
| `Recovery` | Durable structural uncertainty with explicit actionability |

### Frames and sessions

`Frame` replaces separate workspace, task, and session-frame identities. Exactly
one workspace root exists for `(HostId, ProjectId)`. A child is on the same host,
project, and WorkContext as its parent. Closing a task never closes the
workspace. Reopen retains frame identity and lineage.

Provider rollover appends a `FrameSession`; it never creates another user-facing
task. One provider session belongs to at most one frame. A stopped/resumed UUID
remains the same membership even when its native TUI process changes.

Sessions discovered without a frame remain provider history. They do not form
an `Inbox`. A focused recovery action may attach one to a new or existing frame,
but reconciliation never invents the semantic relationship.

### Work context

A WorkContext is durable, but its checkout claim can be released and reacquired.
It identifies the one foreground frame allowed to mutate the checkout while its
claim is held. Workspace-to-child flow keeps the context and transfers only the
foreground lease, preserving uncommitted state.

An automatic transition first proves the source foreground turn complete and
park-safe, moves input authority away from it, transfers the lease, and only
then releases the target's brief. Known or uncertain background mutation blocks
automation. Manual transfer may use an explicit human override after warning
that an already-running process cannot be prevented from writing.

The unique mutable-checkout claim belongs to the WorkContext, not a task. It
releases only when no member has a live mutation-capable surface, pending
transition, or known/uncertain background work. Durable workspace history does
not monopolize a checkout.

### Views, placements, and projects

A view can navigate every configured project/frame owned by one host. It is not
permanently attached to a project. Multiple independent views may exist. Multiple
managed clients attached to one view normally share one cursor; a client using
tmux's independent `active-pane` mode is rejected/degraded rather than silently
violating that contract.

A live provider pane has one owning placement and view. Opening that frame from
another view focuses its owner rather than stealing the pane. Stopped affinity
also has one owner and can move only after duplicate-runtime reconciliation.

`Views` entry means focus as-is. `Projects` entry is explicit navigation: core
routes the workspace's owning view to the project's most recently focused open
descendant, or the workspace itself. Concurrent project opens single-flight on
the workspace frame. Selecting Project A can never focus a view still showing
unrelated Project B.

Closing a terminal client only detaches it. A view remains until explicit
retirement. Retirement requires no transition, desktop lease, or live provider
pane. Frame/provider history survives.

### Transitions and global fencing

Every exact view mutation carries an expected view revision. One nonterminal
transition exists per view. Only `prepared` may be cancelled/superseded;
executing transport must settle or recover.

View revision is not enough for cross-view resources. Storage also fences exact
provider-session launches, live surfaces, WorkContext claims/foreground frames,
workspace project routes, completion handoffs, and desktop attachment leases.
Request UUIDs are idempotent only for one normalized semantic fingerprint.

## Native Provider Runtime

One live managed frame session consists of:

```text
one native Codex or Claude Code TUI process
+ one provider UUID
+ one managed tmux pane
+ one Surface record
+ one owning FramePlacement
```

Codex runs its normal embedded App Server and must not reuse a persistent
default-socket daemon. Claude Agent View remains disabled. Short-lived bounded
Codex App Server stdio clients may discover/precreate/name a session; they never
become the provider runtime.

Provider commands execute directly in tmux. Switchboard leaves no wrapper after
the bootstrap exec and never proxies stdin/stdout. A compact navigator may
remain adjacent, but provider input/rendering are native.

## Controlled Agent Turns

The prohibition is against arbitrary terminal automation, not deliberate agent
use. Switchboard may submit exactly one visible, versioned control prompt to an
exact managed session at a verified turn boundary:

```text
Call transition_claim() and follow the returned transition instructions.
```

No user text, brief, handoff, title, path, token, or command is interpolated.
The capability-bound no-argument tool returns the semantic content only to the
exact target provider UUID.

Transport is live-first. A live parent must be parked, input-disabled, ready
after a trusted `Stop`, free of permission/background ambiguity, and covered by
the executing transition. Otherwise Switchboard resumes the exact UUID with the
same fixed initial prompt. An uncertain live submission is never automatically
retried.

Complete-and-return uses one parent synthesis turn by default. Back, manual
focus, mode change, Human close, and structural recovery remain model-free.
Hooks submit/present within their budget and never wait for model completion.

## tmux View Shell

Switchboard uses the selected/shared user tmux server so an existing client does
not nest another server. Each view records the exact socket path, server PID,
and tmux `start_time`; a generation mismatch invalidates retained locators.
Switchboard sets `destroy-unattached=off` only on sessions it owns.

```text
as-view-<opaque-id>
  main              [optional sidebar] [active provider or dead placeholder]

as-hold-<opaque-id>  unattached holding session
  placeholder       dead remain-on-exit pane
  parked-*          input-disabled live provider panes
  staged-*          attach/transition-gated bootstraps
```

There is no separate anchor window in the attached view. Normal next/previous
window navigation cannot expose parked/staged panes. Explicitly attaching the
holding session still cannot start a staged provider because visibility alone
is never the gate.

Navigator mode runs a compact sidebar on the left. Direct mode removes its pane
and process. A short-lived executor outside the sidebar performs removal and
registry settlement. Toggling back recreates the sidebar without changing the
provider pane/process. Zoom is display state, not durable view mode.

Pane movement follows a durable saga:

1. commit transition/placement intent and claim its execution lease;
2. revalidate server generation, pane metadata, source/target placement, view
   revision, and WorkContext generation;
3. execute a compound exact tmux command queue;
4. re-read pane IDs, metadata, windows, geometry, and input state;
5. commit locators/placements/view revision/transport phase together; and
6. repair from observed metadata or reverse a validated immediate failure.

Reconciliation never blindly repeats `swap-pane`. Production kills only an
exact revalidated pane/window and never invokes `kill-server`.

Live control input is fenced in the same queue: move/select target, enable
input, send the literal template and Enter, then disable input. Exact
`UserPromptSubmit` or a bounded watchdog re-enables it. The system clipboard and
tmux paste buffers are not involved.

## Public State and Commands

Phase 6 introduces a new public generation. Old protocol types/parsers are
deleted, not aliased.

`HostState v1` is emitted by one owner host and includes bounded host, catalog,
view, frame, placement, WorkContext, transition, control-turn, provider session,
surface, and recovery records. It never contains prompts, transcripts, provider
argv, credentials, or unrestricted filesystem data.

`NavigatorState v1` aggregates individually validated HostState records and
projects only views, project entry routes, navigable frame summaries, recovery,
reachability/staleness, warnings, and truncation. Its top-level generation ID is
the local cache provenance; each host row retains its owner generation ID and
stale bit. Core authors view titles, breadcrumbs, activity, attention,
transition/control state, and last-activity summaries.
Open and closed frame summaries remain bounded and include lifecycle, current
provider/runtime/activity, and session count. Closed rows are history-only and
never become project entry routes.

`PresentationDirective v1` replaces the proposed `ViewAction v1`. Core commits
or revalidates semantic navigation, then returns `focus`, `attach`, or `blocked`
desktop work. It contains opaque view/desktop identities but no tmux target or
provider command. `attach` grants one expiring desktop lease.

The replacement public command tree is:

```text
swbctl init --config CONFIG_V3_TEMPLATE
swbctl reset --confirm-generation GENERATION [--config CONFIG_V3_TEMPLATE]

swbctl state host --json
swbctl state navigator [--refresh] --json

swbctl view enter --host HOST \
  (--project PROJECT [--reuse-view VIEW] | --view VIEW [--frame FRAME] | \
   --recovery RECOVERY) \
  [--mode navigator|direct] [--request-id UUID] \
  [--confirm-background-transfer]
swbctl view open --host HOST (--view VIEW | --project PROJECT) --request-id UUID \
  [--can-focus-desktop] [--can-launch-terminal] --json
swbctl view recover --host HOST --recovery RECOVERY --request-id UUID --json
swbctl view attach --view VIEW [--host HOST] [--request-id UUID]
swbctl view list|show|focus|mode|retire
swbctl frame list|show|start|push|back|complete|close|reopen
swbctl project ...
swbctl session show|stop ...
swbctl hooks ...
swbctl cutover export|import|status|commit|rollback
swbctl doctor
swbctl reconcile
swbctl agent-mcp
```

There are no Snapshot/Fleet, prepare/select/attach-surface, task-first CRUD, or
compatibility aliases in `0.3`.

## Agent Authority and Trusted Hooks

Managed provider sessions receive a random capability bound to exact host,
view, frame, provider session, surface, pane, launch, and generation evidence.
Agent tools accept no source identity, tmux target, launch ID, or arbitrary
command from the model.

Canonical tools are:

```text
switchboard_current()
switchboard_context()
switchboard_history(...)
switchboard_mode(mode)
task_push(title, brief, purpose?, provider?, park_safe)
task_back()
task_complete_return(summary, next_action, park_safe)
transition_claim()
transition_status()
transition_cancel()
```

Skills/commands may explain or alias tools, but are optional and independently
disabled. No ambient skill is required for routing, memory, or correctness.

An agent tool records/prepares while the source turn is active. It does not move
the pane or stop its own runtime. A trusted post-turn hook claims the prepared
transition, performs the bounded presentation/control-submission hot path, and
returns. Short-lived settlement and later exact hooks finish binding, claim,
parking, closing, and recovery; no daemon remains.

## Frontends

The primary frontend is the resident compact Textual sidebar. Its `Views`,
`Projects`, `Tasks`, `History`, `Recovery`, and `Settings` panels retain the
current breadcrumb plus activity, attention, transition/control, and action
status. Actions run through one bounded asynchronous core command at a time.
Closed frames are read-only history. There is no Open/Inbox/Closed
administrative application.

Direct mode is the minimal frontend. The native provider occupies the whole
view, while agent tools, commands, and trusted managed-session hooks perform
the same frame transitions. Returning to navigator mode recreates only the
sidebar. A capability-bound `switchboard_mode("navigator")` call provides this
return in one ordinary agent tool turn; no global key binding or skill is
required.

DMS work is frozen during TUI-first adoption. A later desktop adapter may offer
`Views`, `Projects`, and safe recovery as a dumb entry/focus convenience over
the same bounded public directives. It must not own navigation semantics,
provider lifecycle, tmux, or acceptance. No DMS service or plugin restart is a
core development or release step.

## Remote Hosts

Views are host-local. `state navigator` obtains bounded HostState records over
fixed SSH argv and retains last-good state with explicit stale/offline markers.
Every mutation routes to the owner and revalidates live state. Selecting a
remote project/view opens or focuses a separate SSH-backed host-local view; a
local view never swaps a remote pane.

Remote collection and owner routing use configured host IDs mapped to fixed SSH
endpoints. Reads are bounded and concurrent, with validated last-good caches;
mutation commands are fixed argv and never derive an endpoint from UI data.
Terminal entry preflights the remote owner before replacing only the invoking
tmux client. Cross-host client replacement is bounded to four nested hops; the
fifth blocks with an explicit detach-and-enter-directly instruction.

## Registry and Configuration Baseline

`0.3` starts from a fresh registry baseline. Config v3 and the database share a
generation ID and are activated through one state-home pointer. Runtime fails
closed on missing/mismatched generations and on the old fixed schema-v10 file.

Normal development starts with `init` or a compare-and-swap `reset`. Both
publish a complete empty committed generation atomically. Reset retains the old
generation and deliberately performs no provider, hook, DMS, pane, session, or
tmux-server lifecycle action, so user work survives even when Switchboard state
is abandoned.

The offline exporter/importer preserves aligned catalog/session/handoff evidence
without importing old task semantics. New settings make task push conservative,
Complete-and-return synthesized by default, and control transport live-first.
The exact staged activation/rollback sequence is in the Phase 6 plan.

## Failure and Recovery

- Preparation failure leaves source/view unchanged.
- A missed hook leaves durable prepared/uncertain state and cannot move a
  changed view.
- A submitted control turn is never submitted again automatically.
- A child cannot receive its brief before WorkContext foreground transfer.
- A parent claim closes the completed child; parent synthesis can retry from the
  immutable handoff.
- Human close never removes the active pane before a parent/placeholder exists.
- Sidebar failure preserves provider pane; navigator may restart from state.
- Locator crashes reconcile from intent, server generation, and pane metadata.
- Missing tmux server marks containers broken and resumes exact provider UUIDs;
  it never pretends old processes survived.
- Checkout/background ambiguity blocks automation and requires core-owned human
  decision.
- Offline remote/cutover-staged hosts remain visible but reject mutation.

## Security and Privacy

- External subprocesses use fixed argv, bounded time/output, and no shell.
- Agent mutation authority is exact-frame/session/pane/generation scoped.
- The only terminal control prompt is fixed, visible, literal, and versioned.
- Hook payloads retain identity/status evidence, never prompt/response content.
- Optional frontend projections exclude paths, credentials, SSH/tmux targets,
  prompts, transcripts, provider argv, and agent capability material.
- Provider metadata writes are presentation-only, never routing authority.
- Offline conversion never mutates its source and activation fails closed on a
  torn generation.

## Accepted Commitments

- Phase 6 is a clean replacement of `0.2`.
- Frames unify workspace/task identity; provider sessions remain separate.
- Views are durable, host-local, and independent from terminal clients.
- Navigator/direct modes share one view/runtime machinery.
- A view session exposes only `main`; holding panes are separate and gated.
- Controlled fixed agent turns are allowed at exact semantic boundaries;
  arbitrary prompt/terminal injection remains forbidden.
- Complete-and-return synthesizes through the parent by default and closes the
  child on exact handoff claim.
- Projects navigate; Views focus as-is.
- WorkContext claims are temporary, generation-fenced, and host-global.
- The resident TUI is primary; direct mode is the no-TUI alternative.
- DMS is deferred as a dumb optional entry/focus adapter and is not an
  acceptance gate.
- Snapshot v2 and Fleet v1 end at the `0.2` boundary.
- Recursive task flow is enabled only after one-child ownership acceptance.
