# Foreground Task Session Stack

Date: 2026-07-21

Status: design direction; pre-implementation decision record

## Summary

Switchboard should make one focused task per agent session feel automatic rather
than asking the user to decide where every task boundary belongs. The user
normally interacts with a durable project-level session. When a distinct piece
of work becomes substantial, the current agent may push a focused child task
session into the foreground. The parent remains intact and idle in its own
native terminal surface. When the child finishes, Switchboard returns the user
to the parent with a durable handoff. A child may push another child, producing
a bounded stack rather than a flat collection of unrelated sessions.

These are foreground user sessions, not provider subagents. A task session may
own the native Codex or Claude Code TUI for dozens of interactive turns. The
user sees and works directly in that provider UI while the parent is merely out
of view; it is not silently continuing model work in the background.

This direction expands the implemented task and agent-tool boundaries. It is
captured before implementation so the lifecycle, authority, provider, and
failure contracts can be reviewed together. Until a later implementation plan
explicitly supersedes them, the current human-only frictionless task-close
contract and the current read-mostly/current-session agent-tool authorization
remain binding.

## Desired experience

The nominal flow is:

```text
project session
    |
    | agent recognizes a real task boundary
    v
task A session                         foreground
    |
    | task A pivots into distinct task B
    v
task B session                         foreground
    |
    | task B completes
    v
task A session + exact B handoff       foreground
    |
    | task A completes
    v
project session + exact A handoff      foreground
```

The user may request any transition explicitly, but ordinary use should not
require them to understand Switchboard's task schema, choose a provider
session command, find a tmux target, or prepare a handoff prompt manually.

The provider TUI remains unmodified. Switchboard performs a bounded state and
surface transition, selects the exact child or parent tmux surface, and then
leaves the interaction path.

## Why subagents are not the abstraction

Provider subagents are appropriate for delegated, bounded work whose result is
returned to the current foreground conversation. They are not a substitute for
a task that becomes the user's main work surface because:

- the user may interact with the new task for many turns;
- the child needs its own durable provider history and task identity;
- the user needs normal provider UI features, approvals, tools, and context;
- the work may itself pivot into another foreground task; and
- hiding the child behind a background-worker UI makes navigation and ownership
  less clear rather than more automatic.

Provider-internal subagents, background shell commands, and scheduled work
remain children of one task session. They do not become Switchboard stack
frames unless the user or foreground agent explicitly promotes the work into a
new durable task session.

## Core invariants

1. A focused provider session belongs to at most one Switchboard task.
2. A task has at most one current top-level provider session, while prior
   provider sessions may remain as retained history.
3. Each non-root stack frame has exactly one parent frame.
4. One frame is foreground for a given user attachment path at a time.
5. Moving a frame out of the foreground does not imply that its provider
   process exited or that provider-internal background work stopped.
6. Push and return intents are durable and idempotent before any surface switch.
7. Provider UUIDs, not names, remain the routing identity.
8. A failed transition leaves the currently visible session usable and never
   starts a duplicate provider runtime.
9. Codex and Claude Code both use isolated native TUI processes in managed tmux
   surfaces. No provider-wide supervisor is required.
10. Switchboard does not proxy terminal input/output or re-render a provider
    conversation.

The project-level root needs an explicit representation. It may become a
distinguished root task or a separate project-session role, but it must still
be a normal durable provider session rather than a hidden orchestration agent.
That schema choice remains open because it affects migration and whether the
one-task-per-session rule applies literally to the root.

## Foreground stack model

The existing `Task.current_session_key`, `AgentSession`, `LaunchIntent`,
`RuntimeLocator`, and `Surface` records provide most of the substrate. The new
behavior needs an explicit transition/stack record rather than inferring
parentage from timestamps or handoff prose. A candidate shape is:

```text
TaskFrame
  frame_id
  project_id
  task_id
  session_key
  parent_frame_id         null only for root
  state                   foreground | suspended | returning | closed
  created_by              user | agent
  transition_reason       bounded semantic label
  source_handoff_id
  pending_transition_id
  created_at
  updated_at

TaskTransition
  transition_id
  kind                    push | return
  source_frame_id
  target_frame_id         nullable until child binding
  target_task_id
  target_session_key      nullable until provider binding
  launch_id               nullable for an already-live target
  handoff_id              required before completed return
  requested_by            user | agent
  state                   requested | waiting_for_stop |
                          launching | switching | completed |
                          failed | cancelled
  request_id
  failure_code
  created_at
  updated_at
```

The exact schema is not yet accepted. The important contract is that the stack
and pending transition are explicit durable state. Process ancestry, tmux
window order, provider fork lineage, and session names are supporting evidence,
not authority.

## Push lifecycle

An agent cannot safely replace its own foreground TUI while it is still
producing the turn that requested the transition. Push therefore has two
phases:

1. The user or current authorized agent requests a push with a bounded title,
   purpose, and optional initial child prompt.
2. Switchboard validates current-frame authority, creates the child task and a
   leased transition/launch intent, and records the parent link.
3. The current agent finishes its response normally.
4. A trusted post-turn/Stop hook observes the pending transition.
5. Switchboard prepares the child provider session and waiting tmux surface.
6. The provider's `SessionStart` hook binds the child provider UUID to the
   launch, task, frame, and surface.
7. Switchboard projects the task title into provider metadata when supported.
8. The exact tmux client or desktop surface switches to the child.
9. Only after confirmed presentation does the transition become `completed`
   and the parent frame become `suspended`.

For same-provider work, the adapter may use a provider-native fork so the child
inherits useful conversation context while retaining a new durable identity.
For a provider switch, a new session begins from an explicit bounded handoff.
The provider commands and supported flags remain version-gated adapter
contracts rather than shell strings stored in the registry.

If the child fails to start or bind, the parent remains foreground. Expired
waiting surfaces and launch leases are reclaimed by existing reconciliation.
Retry with the same request ID must reuse or repair the same transition rather
than creating another child.

## Return lifecycle

Return is also a deferred, two-phase operation:

1. The user explicitly requests return, or the foreground agent determines
   that the task's completion condition has been met.
2. The agent records a bounded immutable handoff and requests return to the
   exact parent frame.
3. The current turn finishes normally.
4. A trusted post-turn/Stop hook validates the pending return.
5. Switchboard makes the child non-foreground and performs the accepted child
   runtime disposition.
6. Switchboard selects or resumes the exact parent surface and marks it
   foreground.
7. The parent receives the exact child handoff through a bounded context path.

Whether return closes the child task, merely parks it, or offers distinct
`return` and `complete-and-return` operations is not yet resolved. This matters
because the implemented `task close` command is human-only, does not call a
model, does not create a handoff, and commits task state before best-effort
runtime cleanup. The foreground stack must not quietly overload that command
with incompatible semantics.

The initial handoff delivery should not synthesize a hidden provider prompt.
The safer contract is to focus the parent, retain the exact handoff, and expose
it through the parent's Switchboard context on the next user prompt or explicit
`task_get` call. Automatically submitting a parent turn would expand the
prompt-dispatch boundary and requires a separate review.

## Automatic boundary policy

Semantic boundary detection belongs to the foreground agent; the deterministic
core only validates and executes an explicit transition request. A push is
appropriate when the emerging work has an independently meaningful outcome,
is likely to require multiple interactive turns, changes repository/checkout
or provider context, or should be independently resumable and completable.

Do not push for incidental questions, ordinary implementation substeps,
bounded diagnostics, provider subagents, or background commands that remain
part of the current task's outcome.

The agent should use judgment without requiring a confirmation dialog for every
boundary, while preserving explicit user controls to push, stay, return, or
cancel. The stack must have a fixed maximum depth, and repeated automatic
push/return loops must degrade to a visible error rather than churn sessions.
The exact heuristic prompt and depth limit belong in a later implementation
plan and acceptance fixtures.

## Provider runtime decision

Managed Codex and Claude Code sessions use the same surface model:

```text
one live task session
    = one native provider TUI process
    + one managed tmux surface
    + one durable provider UUID
    + one Switchboard task/frame binding
```

Claude Agent View remains disabled. Its supervisor changes process ownership,
session discovery, picker behavior, and foreground/background semantics in ways
that conflict with the tmux-owned model.

Codex must not run a persistent App Server on its well-known default Unix
socket. On Codex `0.144.6`, a plain TUI launch probes
`$CODEX_HOME/app-server-control/app-server-control.sock`. If the socket is
reachable and launch overrides permit reuse, that newly launched TUI attaches
to the shared daemon even without an explicit `--remote` flag. This changes
runtime ownership and the failure/resource boundary for every later matching
TUI launch.

Without the default daemon socket, each Codex TUI starts its own embedded,
in-process App Server and communicates with it through private channels. This
is part of the normal isolated TUI implementation and does not create a shared
listener. Existing TUIs do not dynamically migrate when another App Server
process appears; target selection happens at TUI launch.

Switchboard `doctor` and managed-launch preparation should eventually detect a
reachable default Codex daemon socket and report an incompatible runtime mode.
Managed launch must not silently enter the shared daemon. The precise block,
warning, and migration behavior needs installed acceptance because the
auto-probe is version-specific.

### Rejected shared and mixed runtime modes

A shared-server design for both providers is not the selected direction.
Codex's default-socket App Server and Claude Agent View both change provider
session ownership, discovery, picker, attachment, and failure semantics. They
are not merely alternate APIs over the same isolated TUI lifecycle, and their
provider-specific behavior is not symmetric enough to form one dependable
Switchboard contract.

A mixed design with shared Codex sessions and isolated Claude sessions is also
rejected. It would require two meanings for live runtime ownership, surface
attachment, background state, process cleanup, and provider picker results.
The additional programmatic Codex control does not justify that divergence
because the foreground stack needs only native fork/resume, hooks, exact tmux
switching, and bounded metadata writes.

The selected uniform rule is therefore isolated provider TUIs for both Codex
and Claude. Short-lived stdio protocol clients are provider adapter actions,
not shared runtimes and not an exception to that ownership model.

## Safe transient Codex App Server use

The prohibition is against a persistent discoverable daemon, not every use of
the App Server protocol. Switchboard already performs bounded Codex discovery
with a short-lived `codex app-server --stdio` subprocess. Stdio creates no Unix
or network listener, cannot be discovered by another TUI, and exits when its
owning action completes.

The same transport can safely perform a bounded metadata mutation:

```text
spawn:       codex app-server --stdio
initialize:  initialize + initialized
request:     thread/name/set(threadId, name)
verify:      thread/read(threadId, includeTurns=false)
cleanup:     close stdin, bounded wait, terminate/kill on timeout
```

Required safeguards:

- hard-code stdio transport;
- never invoke `codex app-server --listen unix://`;
- never start, enable, or restart a systemd App Server service;
- apply fixed request, total, line, stdout, stderr, and cleanup bounds;
- reap the child on success, protocol failure, timeout, cancellation, or parent
  exit; and
- capability-gate the write method against a tested Codex contract.

This transient process does not adopt, interrupt, receive turns from, or
change the transport of any running TUI. It is an independent metadata client
that shares Codex's durable local store.

## Programmatic Codex thread naming

Switchboard can project a task/session title into Codex after the provider UUID
is known. The existing `SessionStart` binding supplies that UUID. The adapter
then calls `thread/name/set` through the transient stdio process and verifies
the result with `thread/read`.

The operation was proven locally against an active isolated Codex `0.144.6`
thread on 2026-07-21. The title was persisted in Codex's session index, the
transient process exited, the shared service stayed disabled, and no default
socket or App Server process remained.

Naming contracts:

- Switchboard task and curated session names remain immediately authoritative
  for Switchboard presentation.
- Provider naming is a best-effort projection, never an identity write.
- Provider names need not be unique; every action continues to use the UUID.
- A just-created thread may not yet have writable retained metadata. Retry only
  after `SessionStart` or the first completed turn, with a fixed attempt bound.
- A separately running TUI does not receive the transient process's
  `thread/name/updated` notification. Its in-memory `/status` display may remain
  stale until resume, while Switchboard and later Codex history reads see the
  persisted title.
- A later provider-native `/rename` is valid user intent. Reconciliation must
  preserve the existing curated-name/provider-name authority rules rather than
  oscillating between values.

The existing Codex provider's bounded `_AppServer` transport is the natural
implementation substrate, but its current module contract is read-only. A
write capability should be explicit, separately tested, and impossible to
invoke through ordinary discovery.

## Switchboard authority and tools

The current authorized agent tools intentionally cannot launch, stop, attach,
close, or send prompts to provider sessions. Foreground transitions require a
new narrow authority rather than widening every tool:

```text
task_push(title, purpose?, provider?, initial_context?)
task_return(summary, next_action, disposition?)
task_transition_status()
task_transition_cancel()
```

Names are illustrative, not an accepted public API. Each mutation must be
restricted to the caller's confirmed current frame and session-scoped
capability. The caller supplies no source session, source task, parent frame,
surface, tmux target, provider UUID, launch ID, or arbitrary command. Core
derives those from the bound capability and registry.

The tool records intent only. It never changes the TUI surface or terminates
the calling runtime while the agent turn is active. Trusted hooks and existing
presentation commands execute the deferred transition after the turn.

## Surface and frontend behavior

The current attachment path becomes the owner of foreground selection. A DMS
window, Switchboard TUI client, or explicitly identified tmux client may have a
current stack cursor. A transition must switch only that validated client or
desktop window; it must not globally retarget every attachment viewing the
parent session.

Frontends should show a compact breadcrumb rather than another task-management
form:

```text
Agent Switchboard > task A > task B
```

The child provider TUI remains the main screen. Parent frames remain visible in
the ordinary Switchboard task list with a suspended/background-navigation
marker that is distinct from provider activity. If a parent still has
provider-internal work running, its normal activity state remains visible.

## Failure and recovery

- **Agent turn ends without the expected hook:** reconciliation finds the
  durable pending transition and either resumes it with exact evidence or marks
  it failed without switching.
- **Child provider starts but does not bind:** the launch lease expires, the
  waiting surface is reclaimed, and the parent remains foreground.
- **Child binds but presentation fails:** retain one child runtime and a
  retryable transition; never launch another child.
- **Naming fails:** keep the Switchboard title, expose a bounded provider
  capability warning, and continue the transition.
- **Parent process exited:** resume the exact durable parent session in its
  existing or a newly prepared managed surface before returning.
- **Parent history is no longer resumable:** keep the child usable and report a
  blocked return with the handoff retained.
- **Switchboard restarts:** reconstruct the stack from durable frames and
  transitions, then validate provider processes and surfaces through existing
  reconciliation.
- **Default Codex daemon appears:** do not launch another managed Codex TUI
  until runtime-mode policy is satisfied; existing isolated TUIs remain
  independently owned.

## Relationship to current contracts

This design is deliberately additive and conflicting in visible places:

- `docs/design.md` currently excludes automatic prompt dispatch and broad
  multi-agent orchestration.
- Current agent tools cannot launch, stop, attach, or close sessions.
- Frictionless task close is human-only and creates no handoff.
- Tasks have one current session but no explicit parent/foreground stack.
- Existing presentation actions are human/frontend initiated rather than
  deferred from an agent tool through a post-turn hook.

The implementation plan must explicitly decide which clauses are superseded.
It must not smuggle the new behavior into `task close`, `session wrap`, or the
read-only provider discovery path under compatible-looking names.

The design continues to preserve these existing boundaries:

- provider-native transcript and history ownership;
- unmodified native TUIs;
- tmux-owned full-session persistence;
- no persistent Switchboard or provider-wide daemon requirement;
- no transcript parsing;
- exact provider UUID and surface routing;
- durable intent before process or presentation mutation; and
- reconciliation as the repair path.

## Acceptance scenarios

1. From a project root session, the agent recognizes a multi-turn deliverable,
   records one push, finishes its response, and the same user attachment opens
   exactly one named child session.
2. The child remains the foreground native TUI for many user turns without
   being represented as a background subagent.
3. The child pushes a grandchild; completing the grandchild returns to the
   exact child, and completing the child returns to the exact root.
4. Every return retains an exact bounded handoff even if provider or surface
   presentation later fails.
5. Retrying any transition after a crash creates no duplicate task, launch,
   provider runtime, or tmux surface.
6. A failed automatic boundary decision can be cancelled or manually returned
   without losing either provider history.
7. Codex child naming persists through a transient stdio App Server while the
   default socket remains absent and unrelated TUIs remain isolated.
8. A provider-native name change never changes routing identity.
9. The existing human `task close` behavior remains unchanged until an explicit
   supersession is accepted and tested.
10. Claude Agent View and the persistent Codex default-socket daemon remain
    absent throughout managed isolated-session acceptance.

## Open decisions before implementation

1. Model the project root as a distinguished task or a separate project-session
   role.
2. Choose separate `return`, `complete-and-return`, and `close` semantics.
3. Decide the exact confidence policy for an agent-initiated automatic push or
   return and the maximum stack depth.
4. Choose handoff delivery on parent resume without silently dispatching a
   model prompt.
5. Define whether a push inherits the same checkout by default or prepares an
   existing worktree when one is available.
6. Define behavior when multiple clients view the same frame and only one
   initiated the transition.
7. Version-gate Codex fork, resume, and name mutation contracts and Claude fork
   semantics with isolated no-model probes.
8. Decide whether provider-native title projection happens at binding, after
   the first completed turn, or through a bounded retry queue.
9. Decide how a manually resumed historical session joins or replaces a stack
   frame.
10. Defer cross-host push/return until the local stack and existing pull-based
    remote action contracts can be reconciled.

## Evidence

- Codex `0.144.6` TUI source selects an embedded in-process App Server when no
  reusable default daemon socket is reachable, and otherwise auto-selects the
  local daemon for eligible launches:
  <https://github.com/openai/codex/blob/rust-v0.144.6/codex-rs/tui/src/lib.rs>
- `/rename` routes `SetThreadName` through the TUI's current App Server session:
  <https://github.com/openai/codex/blob/rust-v0.144.6/codex-rs/tui/src/chatwidget/slash_dispatch.rs>
- `thread/name/set` updates provider-owned thread metadata and emits a name
  notification to clients of that same App Server:
  <https://github.com/openai/codex/blob/rust-v0.144.6/codex-rs/app-server/src/request_processors/thread_processor.rs>
- Switchboard's retained Codex adapter already uses a bounded short-lived stdio
  App Server for discovery; naming can reuse that transport after its current
  read-only contract is deliberately expanded.
