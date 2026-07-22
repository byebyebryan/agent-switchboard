# Foreground Task Session Stack

Date: 2026-07-21

Status: design direction; pre-implementation decision record

Decision update: the automatic first slice accepts fixed, visible child and
parent bootstrap turns plus parking the parent after confirmed child binding.
Guided keep-alive remains an explicit fallback, not the seamless default.

## Summary

Switchboard should make one focused task per agent session feel automatic rather
than asking the user to decide where every task boundary belongs. The user
normally interacts with a durable project-level session. When a distinct piece
of work becomes substantial, the current agent may push a focused child task
session into the foreground. The parent remains durable and recoverable. The
accepted automatic mode parks its process after the child binds; an explicit
guided mode may keep it live.
When the child finishes, Switchboard returns the user to the parent with a
durable handoff. Longer term, a child may push another child, producing a
bounded stack rather than a flat collection of unrelated sessions; the UX
walkthrough below deliberately narrows the first slice.

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

## UX tabletop walkthroughs

The following flows stress-test the design from the user's point of view. They
are design evidence, not accepted implementation behavior. A flow fails when
the registry state is sound but the user must restate intent, type a ceremonial
`continue`, hunt for the prior surface, or infer that a transition failed.

| Flow | Current design result |
| --- | --- |
| Enter a project | Blocked: there is no first-class project workspace |
| Start substantial work | Conditional: switching alone opens an idle child |
| Ask a quick question | Pass if boundary policy is conservative |
| Undo a mistaken split | Blocked after the child starts its first turn |
| Finish and return | Blocked while the parent remains live and idle |
| Push a nested task | Blocked by an exclusive linked-worktree claim |
| Open a task directly | Pass, but automatic return has no destination |
| Transition with two viewers | Conditional on exact client authority |
| Child launch fails | State-safe, but post-turn feedback is missing |
| Slow post-turn hook | Blocked if the hook prepares or waits for a provider |

### Flow 1: enter a project workspace

The automation-first journey cannot begin in the current task-first launcher.
Today the user selects an existing task or explicitly creates one. The proposed
journey needs a primary project action with no task form:

```text
open Switchboard
  -> choose project
  -> open its durable workspace session
  -> ask for work in ordinary language
```

The workspace is a real provider session on the project's default local
checkout. It should not appear as an ordinary Open task, require a synthetic
task title, or become Closed when one child finishes. Its provider and title
remain presentation metadata; project, host, checkout, and provider UUIDs
remain the routing identities.

The durable workspace identity should not require one provider thread to live
forever. Like a task, it may eventually retain prior provider sessions and one
current session so context can roll over without changing the user's entry
point or filling the task list with maintenance records.

This favors a distinct workspace-session role over disguising the root as a
task. The exact schema is still open, but the launcher and authorization model
must treat the workspace as the normal entry point if task management is meant
to become automatic.

Daily activation should use the declared default checkout and provider. A
missing checkout routes to the project manager with an explicit issue. A
missing provider may require one bounded first-open choice that becomes the
workspace preference; it must not force a provider/task form on every open.
The project row's primary action becomes **Open workspace**, while catalog
editing remains an explicit secondary action.

### Flow 2: automatically push substantial work

Consider a user asking the workspace agent to implement a multi-turn feature.
The visible happy path should be:

```text
user request
  -> workspace agent prepares a leased child task and waiting surface
  -> workspace agent says that it is opening the focused task
  -> parent turn ends
  -> the post-turn hook revalidates and switches the same client
  -> the child starts its first turn without another user message
```

The last step is essential. Opening an empty child TUI and asking the user to
type `continue` has automated storage, not workflow.

Local no-model CLI help confirms that both provider CLIs expose a native
mechanism for this shape. Codex `0.144.6` accepts an optional initial prompt on
exact `fork` and `resume`, and Claude Code `2.1.216` accepts a prompt while using
`--resume` with optional `--fork-session`. This is command-shape evidence, not a
full Claude `2.1.216` runtime-compatibility claim. The safer Switchboard contract
is not to persist or replay the raw user prompt. The parent records a bounded
semantic transition brief; the child receives one fixed bootstrap instruction
telling it to read that exact brief through an authorized transition tool and
continue.

The first tool call must atomically claim the pending bootstrap for the current
authorized frame without accepting a transition, task, or session identifier
from the model. A retried initial prompt then observes `already_claimed` and
resumes the bound session instead of creating another task or independently
starting the same work. Provider prompt submission itself is not treated as an
exactly-once transaction.

That fixed first turn is still automatic prompt dispatch. It is a deliberate
expansion of the current boundary and must be versioned, visible in provider
history, idempotent, and separately accepted in implementation. This design
record accepts that expansion for automatic mode because implicit push cannot
meet the desired UX without it.

The waiting surface should be prepared by the authorized agent tool while the
parent turn is still active. Attach-before-start keeps it inert: no provider is
started and no surface moves until the turn finishes. This moves fallible task,
checkout, launch, and tmux preparation into a normal tool result that the agent
can report, rather than hiding it after an optimistic final response.

The post-turn hook has a one-second provider timeout and a 250 ms health budget.
It must not perform discovery, wait for provider binding, or supervise the full
transition. Its hot path should claim the prepared transition, revalidate the
exact client and waiting surface, switch, and return. A transition-owned,
short-lived worker or later lifecycle hook may settle binding, parent parking,
and rollback; no persistent controller remains.

Preparation ordering should keep the parent usable until the child binds:

1. atomically record the push, child task, launch, and waiting surface during
   the agent tool call;
2. let the parent finish its response normally;
3. revalidate and switch the exact client on the post-turn hot path;
4. release attach-before-start and bind the child through `SessionStart`;
5. only then park the parent runtime through bounded transition settlement; and
6. repair or return to the still-live parent on any earlier failure.

This permits a short bounded overlap between runtimes but avoids leaving the
user on a failed child after prematurely stopping the parent.

### Flow 3: stay for incidental work

The workspace should answer status questions, explain code, inspect a small
fact, and perform ordinary steps within the current outcome without creating a
task. False positives are more disruptive than false negatives: they change
surfaces, create durable records, and may start another model turn.

The automatic policy should therefore be asymmetric. An explicit user request
to create or separate a task is sufficient. An implicit agent decision requires
high confidence that the outcome is independently meaningful and multi-turn.
Anything less stays in the current session; the agent may promote it later if
the work actually grows.

### Flow 4: undo, pause, and complete are different

One overloaded `return` action cannot cover the important user intents:

- **Back** changes navigation only. The child stays Open and receives no
  completion handoff.
- **Cancel push** rolls back an unbound transition and its transition-owned child
  reservation. Once a child has bound or started a turn, cancellation becomes
  Back plus an explicit lifecycle choice; history is never silently deleted.
- **Complete and return** writes one exact handoff, returns to the parent, and
  closes the child only after the parent has resumed successfully.
- **Close** remains the existing human organizational action and is not an
  alias for any of the above.

An automatic first child turn narrows the useful cancellation window. This is
another reason to prefer conservative boundary detection rather than adding a
countdown or confirmation dialog to every high-confidence transition.

### Flow 5: automatically complete and return

A child that finishes should not switch away immediately after printing the
only user-visible result; the user may never see it. Returning to a live idle
parent is also insufficient because neither isolated provider TUI offers a safe
external way to make that already-running TUI submit its next turn.

The seamless path therefore requires the parent to be parked after the child
successfully binds. Completion can then use the provider's exact resume path:

```text
child records exact handoff and requests complete-and-return
  -> child turn ends
  -> Switchboard prepares parent resume with a fixed bootstrap instruction
  -> exact client switches to the parent
  -> parent binds, reads the returned handoff, and produces the visible result
  -> child is stopped and closed only after parent presentation succeeds
```

The parent response becomes the canonical user-visible completion. The child
may emit a short transition acknowledgement, but the workflow must not depend
on the user reading a surface that is about to leave the foreground.

Automatic return therefore costs one additional parent model turn and its
latency. A guided mode can focus the parent and wait for the user's next prompt,
but it cannot claim seamless continuation. The implementation plan must measure
and expose this tradeoff rather than hiding the extra turn as infrastructure.

Parking has a real tradeoff: provider-owned background work in the parent may
stop. Automatic mode therefore requires proof that the parent can be parked or
fails the transition without leaving it. An explicit keep-alive mode degrades
return to a guided user action. Silently promising both live parent background
work and automatic parent continuation is not implementable with the selected
isolated TUI model.

### Flow 6: nested tasks and checkout ownership

The proposed arbitrary bounded stack conflicts with the implemented checkout
claim model. A linked worktree may be claimed by only one Open task. If task A
has uncommitted changes and pushes task B:

- a new worktree does not contain A's uncommitted state;
- sharing A's linked worktree violates its exclusive task claim; and
- leaving A's runtime alive weakens any claim that only B can mutate it.

The first automation slice should therefore stop at one child below the
workspace. Work discovered inside that child remains part of the task or uses a
provider subagent/background command. A later nested-task design needs a
stack-level work-context claim, an explicit claim transfer while ancestors are
parked, or another model that preserves uncommitted state without concurrent
ownership.

This is a product-visible limitation and must not vary silently depending on
whether the checkout happens to be `main` or `worktree`.

### Flow 7: direct opens, multiple viewers, and failures

A task opened directly from DMS or the TUI has no parent frame. It remains a
normal recovery and power-user path: the agent may record a handoff, but it
cannot automatically return to a project workspace that was never part of the
attachment path. Human close/reopen remains valid.

Automatic switching must also be scoped to one exact attachment. If multiple
tmux clients view the source surface and the initiating client cannot be
revalidated uniquely, the transition should remain recorded but must not park
the parent or retarget every viewer. The first slice should fail closed rather
than pretend foreground is global session state.

Finally, a post-turn transition can fail after the agent has already said it is
switching. Keeping the parent usable is necessary but not sufficient. The
initiating surface needs one bounded status signal—such as an exact tmux client
message or frontend notification—for started, failed, and ready-to-retry
states. A silent no-op is a broken flow even when the registry is correct.

## Proposed first UX slice

The walkthrough supports this deliberately narrower first increment:

1. Add one durable workspace-session role and a primary **Open workspace**
   action for a configured project/default checkout.
2. Permit automatic push only from a workspace into one child task. Defer
   task-to-grandchild nesting.
3. Use conservative semantic detection; explicit user task intent always wins,
   while uncertain implicit boundaries stay in the workspace.
4. Inherit the workspace's exact checkout. The workspace does not hold an
   exclusive task claim, so the child can claim a linked worktree normally.
5. Store a bounded transition brief and prepare one leased inert surface during
   the agent tool call, then start the child with one fixed, visible bootstrap
   instruction through the provider's tested initial-prompt contract. When the
   child binds, use the task title as its curated Switchboard name and project
   that title into provider metadata when the adapter supports it.
6. Keep the parent live until child binding and presentation succeed, then park
   it for a resumable automatic return.
7. Implement separate Back, Cancel push, and Complete and return operations;
   do not overload human task close.
8. On completion, resume the parent with one fixed bootstrap instruction that
   retrieves the exact child handoff. Close the child only after the parent is
   visibly usable.
9. Require one exact initiating attachment for automatic surface movement and
   expose bounded transition status on that surface.
10. Keep direct task opens and current human close/reopen as explicit recovery
    paths.

Items 5, 6, and 8 are accepted design direction for automatic mode. They do not
change the implemented prompt-dispatch or runtime boundaries until a versioned
implementation plan and provider acceptance explicitly land them.

The resulting visible flow should be this small:

```text
[Agent Switchboard workspace]
User: Implement project import and export.
Agent: I am opening "Project import and export" as a focused task.

[Switchboard: opening Project import and export]
[Project import and export]
Agent: I have the task brief. I am inspecting the catalog contract now.
...normal multi-turn work and approvals...
Agent: The implementation and tests are complete. Returning to the workspace.

[Switchboard: returning to Agent Switchboard]
[Agent Switchboard workspace]
Agent: Project import and export is complete. The child verified ...
```

The bracketed transition lines are Switchboard-owned status, not synthetic
provider conversation. There is no task form, `continue` prompt, handoff editor,
or manual surface lookup in the nominal path.

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
5. Foreground navigation and runtime presence are separate state. A transition
   records whether its source remains live or is parked; neither is inferred
   from moving an attachment cursor.
6. Push and return intents are durable and idempotent before any surface switch.
7. Provider UUIDs, not names, remain the routing identity.
8. A failed transition leaves the currently visible session usable and never
   starts a duplicate provider runtime.
9. Codex and Claude Code both use isolated native TUI processes in managed tmux
   surfaces. No provider-wide supervisor is required.
10. Switchboard does not proxy terminal input/output or re-render a provider
    conversation.

The project-level root needs an explicit representation. The UX walkthrough
favors a separate workspace-session role over a distinguished root task, but it
must still be a normal durable provider session rather than a hidden
orchestration agent. The exact schema remains open because it affects migration
and whether the one-task-per-session rule applies literally to the root.

## Foreground stack model

The existing `Task.current_session_key`, `AgentSession`, `LaunchIntent`,
`RuntimeLocator`, and `Surface` records provide most of the substrate. The new
behavior needs explicit lineage, navigation, and transition records rather than
inferring them from timestamps or handoff prose. Foreground is relative to an
attachment path, so it cannot be a global frame state. A revised candidate is:

```text
SessionFrame
  frame_id
  project_id
  checkout_id
  role                    workspace | task
  task_id                 null only for a workspace root
  session_key
  parent_frame_id         null only for root
  lifecycle_state         active | returning | completed | cancelled
  created_by              user | agent
  transition_reason       bounded semantic label
  source_handoff_id
  created_at
  updated_at

NavigationCursor
  cursor_id
  current_frame_id
  pending_transition_id
  state                   active | switching | blocked
  created_at
  updated_at

FrameTransition
  transition_id
  cursor_id
  kind                    push | back | complete_return
  source_frame_id
  target_frame_id         nullable until child binding
  target_task_id          required only for push
  target_session_key      nullable until provider binding
  launch_id               nullable for an already-live target
  handoff_id              required for complete_return
  source_runtime_policy   keep_live | park
  bootstrap_mode          none | fixed
  bootstrap_state         none | pending | claimed | completed
  bootstrap_session_key   nullable until claimed
  requested_by            user | agent
  state                   requested | preparing |
                          launching | switching | completed |
                          failed | cancelled
  request_id
  failure_code
  created_at
  updated_at
```

The exact schema and durable representation of `cursor_id` are not yet
accepted. The important contract is that lineage and pending transitions are
durable while foreground selection is attachment-relative. Process ancestry,
tmux window order, provider fork lineage, and session names are supporting
evidence, not authority.

## Push lifecycle

An agent cannot safely replace its own foreground TUI while it is still
producing the turn that requested the transition. Push therefore has two
phases:

1. The user or current authorized agent requests a push with a bounded title,
   purpose, and transition brief.
2. Switchboard validates current-frame authority, creates the child task,
   leased transition/launch intent, and inert waiting surface, and records the
   parent link without starting a provider.
3. The current agent finishes its response normally.
4. A trusted post-turn/Stop hook claims the prepared transition and revalidates
   the exact client and waiting surface.
5. The exact tmux client or desktop surface switches or attaches to the child,
   releasing the attach-before-start bootstrap.
6. The provider's `SessionStart` hook binds the child provider UUID to the
   launch, task, frame, and surface.
7. Switchboard stores the task title as the child's curated name and records a
   bounded best-effort provider-title projection when supported. The provider
   write does not gate the transition.
8. Only after confirmed binding and presentation does the transition become
   `completed`, the cursor select the child, and the explicit parent runtime
   policy take effect.

For same-provider work, the adapter may use a provider-native fork so the child
inherits useful conversation context while retaining a new durable identity.
For a provider switch, a new session begins from an explicit bounded handoff.
The provider commands and supported flags remain version-gated adapter
contracts rather than shell strings stored in the registry.

If the child fails to start or bind, the parent remains foreground. Expired
waiting surfaces and launch leases are reclaimed by existing reconciliation.
Retry with the same request ID must reuse or repair the same transition rather
than creating another child.

## Automatic session naming lifecycle

Yes, the automatic workflow names the workspace and child sessions. This is
necessary because the child bootstrap instruction is deliberately fixed. If
provider auto-naming used that instruction, every focused task could receive
the same generic title instead of the semantic title already chosen by the
workspace agent.

Naming is a presentation operation, not a routing operation. It never changes
the provider UUID, Switchboard session key, frame lineage, tmux session name,
window, pane, or surface ID. Managed tmux sessions keep their opaque stable
names so shell quoting, collisions, and later title edits cannot invalidate a
runtime locator.

The first-slice lifecycle is:

1. `task_push` validates and stores one bounded task title with the task and
   transition before any provider process starts.
2. The child starts with the fixed bootstrap instruction. Until `SessionStart`
   binds its provider UUID, there is nothing provider-specific to rename.
3. The binding transaction gives the child session the current task title as a
   curated Switchboard name with `name_actor=agent`. Switchboard can therefore
   display the useful title immediately even if the provider write is
   unsupported or fails.
4. After binding, a transition-owned bounded action attempts to project that
   same title into provider metadata. This stays off the post-turn
   surface-switch hot path and cannot delay or roll back a successful push.
5. Codex has no `codex rename` shell command. Its documented user-facing
   operation is interactive `/rename`; the retained local `$name-thread` skill
   proved that a noninteractive helper can reach the same metadata through a
   transient stdio App Server call to `thread/name/set(threadId, name)`, verify
   it with `thread/read`, and reap the process. Switchboard must port that
   bounded helper into its version-gated adapter rather than type `/rename`
   into the TUI, invoke a personal skill, or enable the shared default-socket
   daemon.
6. A known not-yet-writable metadata result may receive one bounded retry after
   the first completed turn. Unsupported providers and exhausted failures keep
   the curated Switchboard name and report bounded diagnostic state; they do
   not create another session or keep retrying in the background.

A workspace root uses a bounded label derived from the project name, such as
`Agent Switchboard workspace`; a task child uses the exact current task title.
If a workspace or task rolls over to a new provider thread, the new UUID is a
new one-shot projection target. Resuming the same UUID does not rename it
again. Provider names do not need to be unique.

The projection is an initial default, not a permanent synchronization loop.
Later task-title edits, Switchboard session-name edits, and provider-native
`/rename` operations remain distinct explicit user intent. Reconciliation may
update `provider_name`, but it must preserve a curated Switchboard name and
must not reapply the automatic projection merely because the two values differ.
This prevents an oscillation between Switchboard and a provider TUI. A future
explicit **Rename current session** action may deliberately update both stores
under the same bounded adapter contract.

Claude Code keeps the curated Switchboard name in the first slice until a
provider-native metadata write is separately proven and version-gated. The
workflow must never simulate support by injecting a slash command or terminal
keystrokes.

## Return lifecycle

Back and complete-and-return are also deferred, two-phase operations:

1. The user requests Back, or the foreground agent records a bounded immutable
   handoff and requests Complete and return to the exact parent frame.
2. Switchboard validates a live parent or prepares one inert waiting resume
   before the current turn ends.
3. The current turn finishes normally.
4. A trusted post-turn/Stop hook revalidates and switches the exact client.
5. A parked parent starts and binds through attach-before-start.
6. Only after confirmed parent presentation does the cursor select the parent.
7. Back leaves the child Open. Complete and return applies the accepted child
   runtime disposition, retains the exact handoff, and closes the child.

This transition-owned completion remains distinct from the implemented human
`task close` command, which does not call a model, create a handoff, or navigate
to a parent. The foreground stack must not quietly overload that command with
incompatible semantics.

The tabletop walkthrough shows that focusing the parent and waiting for the
next user prompt is a guided return, not seamless continuation. The proposed
automatic path uses one fixed, visible bootstrap instruction that retrieves the
exact handoff through an authorized transition tool. Arbitrary hidden prompts
remain excluded. Guided return remains available only as the explicit fallback
when automatic parent parking or resume cannot be proven safe.

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
Bootstrap turns may not initiate another implicit transition before the cursor
is stable; an explicit new user instruction is required. The exact heuristic
prompt and depth limit belong in a later implementation plan and acceptance
fixtures.

## Provider runtime decision

Managed Codex and Claude Code sessions use the same surface model:

```text
one live managed frame
    = one native provider TUI process
    + one managed tmux surface
    + one durable provider UUID
    + one Switchboard workspace/task frame binding
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

The operation was proven locally by the `$name-thread` skill against an active
isolated Codex `0.144.6` thread on 2026-07-21. The title was persisted in
Codex's session index, the transient process exited, the shared service stayed
disabled, and no default socket or App Server process remained. The proof is
now retained as
[`spikes/codex_thread_name_probe.py`](../spikes/codex_thread_name_probe.py);
the personal skill itself is not a production dependency.

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
task_push(title, purpose?, provider?, brief)
task_back()
task_complete_return(summary, next_action)
task_transition_begin()
task_transition_status()
task_transition_cancel()
```

Names are illustrative, not an accepted public API. Each mutation must be
restricted to the caller's confirmed current frame and session-scoped
capability. The caller supplies no source session, source task, parent frame,
surface, tmux target, provider UUID, launch ID, or arbitrary command. Core
derives those from the bound capability and registry.

The tool may atomically record the intent and prepare one leased inert waiting
surface. It never starts a provider, changes the visible TUI surface, or
terminates the calling runtime while the agent turn is active. Trusted hooks,
short-lived transition settlement, and existing presentation commands execute
the visible transition after the turn.

## Surface and frontend behavior

The current attachment path becomes the owner of foreground selection. A DMS
window, Switchboard TUI client, or explicitly identified tmux client may have a
current stack cursor. A transition must switch only that validated client or
desktop window; it must not globally retarget every attachment viewing the
parent session.

The first automatic slice requires exactly one revalidated client on the source
surface. A manual frontend action may carry its own exact client identity, but
an agent request from a multiply viewed provider pane cannot prove which viewer
submitted the turn and must fail closed.

Frontends should show a compact breadcrumb rather than another task-management
form:

```text
Agent Switchboard > task A > task B
```

The child provider TUI remains the main screen. A frontend displaying the same
cursor may mark ancestor frames as background navigation, distinct from
provider activity. Other frontends show ordinary task/runtime state; they must
not present a cursor-relative `suspended` label as global truth. If a parent
runtime remains live, its normal activity state remains visible.

## Failure and recovery

- **Agent turn ends without the expected hook:** reconciliation finds the
  durable pending transition and either resumes it with exact evidence or marks
  it failed without switching.
- **Child provider starts but does not bind:** the launch lease expires, the
  waiting surface is reclaimed, and the parent remains foreground.
- **Child binds but presentation fails:** retain one child runtime and a
  retryable transition; never launch another child.
- **Post-turn transition fails:** keep the current session usable and emit one
  bounded status message to the exact initiating surface; do not fail silently.
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

These describe the longer-term end state. The proposed first UX slice above
intentionally stops at one workspace child until checkout ownership is revised.

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

1. Define the separate workspace-session role's exact project,
   checkout, provider, and migration schema.
2. Define the fixed child-bootstrap and parent-return text, capability bounds,
   retry behavior, and measurable model-turn latency and cost.
3. Define the safe parent-parking precondition and rollback. Automatic mode
   parks; an explicit keep-alive choice uses guided return.
4. Define storage and transaction semantics for separate Back, Cancel push,
   Complete and return, and human Close operations, including which transitions
   may close their exact child task.
5. Define the exact high-confidence automatic-boundary policy and its fixtures.
6. Enforce a maximum depth of one child for the first slice, then design
   stack-level checkout ownership before enabling nested tasks.
7. Define the exact initiating-attachment proof, multi-client failure behavior,
   and bounded transition status surface.
8. Version-gate Codex fork/resume/initial-prompt/name contracts and Claude
   fork/resume/initial-prompt semantics with isolated no-model probes.
9. Define the durable diagnostic/idempotency representation for the accepted
   one-shot provider-name projection and its single bounded retry.
10. Decide how a directly or manually resumed historical session joins a
    workspace path or remains a parentless recovery flow.
11. Define workspace provider-thread rollover without changing durable
    workspace identity or manufacturing user tasks.
12. Defer cross-host push/return until the local workspace flow and existing
    pull-based remote action contracts can be reconciled.

## Evidence

- Local no-model CLI help on 2026-07-21 confirmed that Codex `0.144.6` accepts
  an initial prompt on exact `fork` and `resume`, and Claude Code `2.1.216`
  accepts a prompt with `--resume` plus optional `--fork-session`.
- Codex's command overview has no thread-rename shell command. The
  supported interactive surface is `/rename`; the repo-owned no-model probe
  retains the local `$name-thread` skill's experimental stdio bridge for
  version-gated adapter work.
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
