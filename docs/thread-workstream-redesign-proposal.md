# Proposed Thread and Workstream Redesign

Date: 2026-07-23

Status: studies complete; explicit-intent direction accepted, proposed contracts remain unapproved

> **Decision:** the
> [Thread and Workstream Redesign Decision](thread-workstream-redesign-decision.md)
> accepts this direction for clean-break production-contract design after the
> core and secondary studies passed. This proposal remains non-normative and
> none of its interfaces are approved or implemented.

This proposal records a possible clean-break successor to the accepted Phase 6
workspace/child/return workflow. It does not change the current registry,
commands, hooks, state contract, or installed behavior. The provider, hook,
tmux, memory, history, and Git spikes below establish direction only;
implementation still requires an approved production contract and combined
acceptance.

If installed capability gates or combined acceptance later fail, the proposal
may still be narrowed or rejected without creating a compatibility obligation.

## Why Reconsider the Accepted Workflow

Phase 6 proved that Switchboard can move native provider panes, transfer
foreground checkout authority, and submit bounded control turns. Its accepted
happy path nevertheless failed the most important user-facing test:

```text
child result
-> automatic return to parent
-> parent control/claim turn
-> parent synthesis
```

The transition hid the completed child result that the user most wanted to
read. Mechanically settled state is therefore insufficient acceptance evidence.

The revised direction starts from these observations:

- The visible tip of a completed agent turn is user work product. Switchboard
  must not replace, hide, or append management traffic to it.
- A project is primarily an organizational container for repositories,
  workstreams, tasks, and history. It need not own a permanent parent
  conversation.
- Normal work feels like one continuous project view even when the provider
  conversation rolls from task A to task B.
- Parallel work is an explicit workstream fork, not an accidental copy of a
  giant project-long provider transcript.
- Seamless thread management means the user states a simple intent and
  Switchboard performs the lifecycle transaction. It does not require
  Switchboard to infer when the user wants a transition.
- Git worktrees are the natural filesystem boundary for parallel workstreams.
- Provider-native thread transitions should be adopted where available rather
  than reimplemented without evidence.

## Directional Provider Evidence

Current Codex Plan mode provides a particularly relevant precedent. Its
**Yes, clear context and implement** action:

1. retains the planning thread as resumable provider history;
2. starts a fresh provider thread in the same working directory;
3. carries the approved plan into the new thread as its initial prompt; and
4. continues in the same native TUI.

The current implementation is visible in the official Codex sources:

- [Plan implementation selection and carried prompt](https://github.com/openai/codex/blob/main/codex-rs/tui/src/chatwidget/plan_implementation.rs)
- [Fresh-session event and retained source-thread intent](https://github.com/openai/codex/blob/main/codex-rs/tui/src/app_event.rs)
- [Fresh-session dispatch](https://github.com/openai/codex/blob/main/codex-rs/tui/src/app/event_dispatch.rs)
- [Hook lifecycle contract](https://learn.chatgpt.com/docs/hooks)

Codex also exposes provider thread naming through its
[App Server path](https://github.com/openai/codex/blob/main/codex-rs/tui/src/app/thread_routing.rs).
That is useful presentation evidence, but provider names remain mirrors rather
than Switchboard identity or routing authority.

This upstream behavior is a strong design signal, not Switchboard acceptance.
The action is provider-specific, its public observation surface is narrower
than its internal TUI event, and the ordinary **Yes, implement this plan**
choice intentionally stays in the existing thread.

## Proposed Product Thesis

Switchboard should standardize provider thread transitions and add the missing
organizational layer around them:

```text
provider owns
  native conversation UI
  provider thread history
  native fresh-thread/fork/resume operations
  native plan transfer when supported

Switchboard owns
  project/workstream/task/thread organization
  source -> destination lineage
  persistent current/previous visibility
  navigation and read-only historical inspection
  checkout/worktree association and managed-worktree ownership
  provider-neutral transition policy and recovery

external memory owns
  broader cross-session project recall
```

The goal is not to make every provider behave identically internally. The goal
is one predictable user flow with explicit capability and degraded-state
reporting.

## Proposed Model

The current `Frame` model intentionally unifies workspace and task identity.
This proposal instead studies separate semantic and provider lifecycles:

| Entity | Proposed purpose |
| --- | --- |
| `Project` | Organizational container and provider/Git defaults |
| `Workstream` | One sequential line of work with one checkout/worktree |
| `Task` | A meaningful outcome within a workstream |
| `ProviderThread` | One exact provider conversation UUID |
| `ThreadTransition` | Immutable source/destination/provider-trigger lineage |
| `TaskBrief` | Bounded accepted plan or execution brief |
| `Checkout` | Observed or Switchboard-managed repository filesystem view |
| `UserView` | Persistent tmux presentation and navigation cursor |

A task may use more than one provider thread:

```text
Project
  Workstream: main
    Task: Phase 7A
      Thread: plan Phase 7A
      Thread: implement Phase 7A       current
```

A thread transition and a task transition are separate facts. A native
plan-to-fresh-context operation always creates a `ThreadTransition`; policy and
user intent determine whether the destination:

- continues the same task;
- begins the next sequential task; or
- begins a forked workstream.

This avoids treating a provider context-window decision as organizational truth.

## Required Invariants

Any accepted successor design must preserve all of these:

1. A completed assistant result remains the visible tip of its source thread.
2. No post-result control prompt, synthesis turn, or pane switch is required to
   make that result durable.
3. Any requested rerouting occurs before the destination agent begins the
   user's execution turn.
4. The triggering execution turn is sampled by at most one working provider
   thread. Any boundary classifier is advisory and cannot perform the task.
5. The persistent UI identifies the source thread and current destination
   thread before and after a transition.
6. A transition requires an explicit user action; task-boundary inference may
   suggest an action but cannot initiate one.
7. Provider thread history is never silently deleted or rewritten.
8. Historical inspection does not silently change the active workstream tip.
9. Explicit parallel work does not share a mutable checkout by default.
10. Switchboard failure never requires unrelated native agent sessions or the
    user's tmux server to stop.

## Proposed Sequential Happy Path

The desired flow preserves both native Plan choices inside one persistent user
view:

```text
task A thread remains visible through work, review, and alignment
-> task A produces or records an accepted plan/brief for B
-> "implement this plan" continues in task A's current provider thread
or
-> "clear context and implement" explicitly requests a fresh B thread
-> provider rolls to B before implementation model work
-> navigator records and shows A -> B
-> B receives the exact execution prompt plus the accepted plan/brief
-> B performs the work and returns its ordinary native result
-> B's result remains visible until the user's next real action
```

Sequential rollover normally keeps the same workstream, branch, and worktree.
It clears provider conversation context, not filesystem state or project
history.

### Native Codex plan rollover

For **clear context and implement**, Codex already creates the destination
thread and carries the plan. The proposed Codex adapter would observe and adopt
that operation rather than create a competing thread:

```text
managed pane currently bound to thread A
-> SessionStart(source=clear) reports thread B on the same managed surface
-> first UserPromptSubmit on B carries the approved plan
-> Switchboard confirms A -> B lineage and names/organizes both threads
```

`SessionStart(source=clear)` alone does not prove a task boundary because a
generic clear operation may occur mid-task. Plan provenance and the first
submitted input must confirm the semantic classification.

### Ordinary Codex plan implementation

The ordinary **implement this plan** action stays in the existing Codex thread.
Current hooks can observe the generated prompt before accepted input is
recorded, but no stable hook field directly names the TUI selection or carries
the approved plan as a separate field.

The timing study evaluated whether a future opt-in policy could reroute that
action. It required proof of:

- reliable compound detection of completed Plan mode -> Default mode;
- supported structured retrieval of the exact approved `PlanItem`;
- blocking before source-thread input is recorded;
- exact-once creation and submission to the destination thread; and
- restoration of source input when any step is uncertain.

The
[execution-intent timing study](spikes/execution-intent-timing.md)
subsequently proved that the fixed ordinary implementation input can be held
before sampling while the current structured Plan item remains retrievable.
The blocked input text does not enter source history and produces no model
`Stop`, although Codex appends one content-free turn. Combined with the
previously proved transition transaction, ordinary Plan implementation is a
technically viable optional cutover trigger.

That capability is not the proposed v1 policy. Ordinary **implement this plan**
continues in the current thread so the user retains the native choice. A future
explicit preference may force fresh-thread implementation only after product
evidence justifies the surprise and the content-free blocked turn.

### Explicit conversational thread management

Not every task uses Plan mode. At a user prompt boundary, reserved explicit
actions such as **go ahead in a new thread**, **start a new thread**, and
**fork this task** may request provider-neutral thread management. Natural
language that merely resembles acceptance or transition intent remains in the
current thread.

Model-based classification is not part of the input hot path until latency,
false-positive, cancellation, and exact-once delivery studies pass. Structured
signals such as an accepted provider Plan item can validate what to carry, but
do not replace the user's transition request.

The live timing study confirmed that a conversational `Stop` result and the
next user acceptance are both observable without a structured Plan item.
Natural language alone remains advisory. A provider-neutral
**implement selected plan in a fresh thread** action can bind an exact assistant
result or plan document to an authoritative user request without inference;
that installed action is not yet implemented.

## User-Initiated Transitions

The navigator is the canonical thread-management surface because it remains
available while the provider is generating, running tools, or otherwise not at
a user prompt boundary. Conversational commands are prompt-boundary aliases for
the same semantic actions, not the full control surface.

The proposed intent model is:

| User action | Meaning | Source behavior | Destination |
| --- | --- | --- | --- |
| **Implement this plan** | Execute with existing context | Continue current thread | None |
| **Clear context and implement** | Execute accepted plan in fresh context | Preserve source thread | New thread, same task/workstream/worktree |
| **Start a new thread** | Continue sequential work with clean provider context | Preserve idle source thread | New thread, same workstream/worktree |
| **Interrupt** | Stop a mistaken or obsolete active attempt | Interrupt active turn; do not roll back files | No automatic destination |
| **Start new workstream** | Begin unrelated parallel project work | Leave source running or parked | Fresh thread plus independent worktree |
| **Fork this task** | Explore approach B while approach A continues | Leave source running | Provider fork plus sibling workstream/worktree |

The user's action is the confirmation. A second confirmation is required only
for a separately identified hazard such as an incomplete filesystem
checkpoint. Proposed entry points are:

- navigator keys/actions for interrupt, new thread, new workstream, and fork;
- **Start fresh from this plan** from a selected accepted plan or result;
- exact reserved conversational aliases at a user prompt boundary; and
- equivalent bounded CLI/direct-mode actions.

Arming a transition should not open a task-administration form. The user's next
ordinary prompt is the destination thread's first real prompt.

The navigator must show the pending source and intended transition before input
is accepted, and the confirmed source/destination identities after binding.
An out-of-band navigator action may instead collect the destination's first
prompt directly and launch it immediately without writing into the source
provider thread.

Explicit navigation may move focus from running approach A to newly started
approach B. When A later completes, Switchboard marks it `result ready` and
leaves B focused; it never auto-returns, injects a notification into either
provider transcript, or replaces A's result tip.

A same-workstream **Start a new thread** action is rejected while its source
turn is active because both threads would share one mutable worktree. The user
must interrupt first, start an independent workstream, or fork the task.

## Persistent Visibility

The navigator remains visible in full TUI mode while the provider occupies the
right pane. It should retain a compact transition receipt:

```text
Project: Switchboard
Workstream: phase-7

Previous: Plan Phase 7A       codex:93c1...   [Inspect]
Current:  Implement Phase 7A  codex:c407...   [Active]
Reason:   approved plan -> fresh context
```

Names are editable presentation metadata. Switchboard stores its own canonical
title and may best-effort mirror it to the provider. Provider names and mutable
labels never replace exact provider UUIDs or lineage IDs as authority.

Direct mode has no sidebar, so it needs a bounded equivalent such as a tmux
status indicator plus `swbctl current`/`history`. It must not inject transition
status into the agent's result.

## History, Inspection, Resume, and Fork

Every task and exact provider thread remains navigable project history.

Historical inspection is separate from the active workstream cursor:

```text
A -> B -> C                         active tip: C
     ^
     inspected read-only: B
```

The preferred inspection path presents the exact provider transcript through
its native UI while input is disabled and the navigator continues to identify
C as the active tip. Switchboard does not become a transcript renderer.

The word `resume` is reserved for restarting the same provider UUID after its
native process stopped or its managed pane was lost. It does not create new
semantic lineage.

Continuing from a non-tip historical task is instead **Fork workstream from
here**:

```text
A -> B -> C                         workstream 1
     `-> X -> Y                     workstream 2, forked from B
```

The first proposal does not allow in-place mutation of a non-tip historical
thread. That keeps the original workstream linear and makes divergence
explicit.

Forking a currently running task uses the same rule. If approach A is active,
the provider fork branches through the latest completed turn before A:

```text
settled state S
├── approach A                         original workstream, still running
└── approach B                         forked workstream from S
```

The in-progress prompt, partial assistant output, and active tool state are not
copied into B. The user supplies B's first prompt through the navigator. A
separate **Interrupt** action stops A when the intent is correction or pivot
rather than parallel exploration.

## Worktrees and Filesystem Lineage

Git worktrees are proposed as the default filesystem isolation for explicit
parallel workstreams:

```text
Project / Repository
  Workstream 1 -> branch 1 + worktree 1 -> tasks A, B, C
  Workstream 2 -> branch 2 + worktree 2 -> tasks X, Y
```

The worktree belongs to the workstream, not each task:

- sequential task/thread rollover keeps the existing worktree;
- a new unrelated workstream creates a fresh managed worktree from an explicit
  or project-default base commit and has no provider-fork lineage;
- explicit workstream fork creates a fresh branch and managed worktree from
  the source's exact recorded checkpoint;
- switching workstreams restores both provider thread and working directory.

Proposed ownership modes are:

| Mode | Lifecycle |
| --- | --- |
| `managed` | Created by Switchboard and eligible for guarded retirement |
| `external` | User-created; Switchboard may use but never remove it |
| `shared` | Existing primary checkout used by the initial workstream |

Switchboard may remove only a managed worktree whose repository identity, path,
branch, ownership record, clean state, and retirement preconditions still
match. It never automatically stashes, force-removes, or discards dirty state.

A historical filesystem fork can reproduce only a recorded Git tree. Each task
boundary should record `HEAD` and dirtiness. If the historical task ended dirty,
the UI must distinguish conversational lineage from an incomplete filesystem
checkpoint. Automatic checkpoint commits, merge, rebase, and force cleanup are
out of the first proposal.

An exact running-task fork therefore requires both a completed provider turn
and its recorded exact filesystem checkpoint. If that checkpoint was dirty or
cannot be reconstructed, the navigator must block the exact-fork claim and may
offer a distinctly labeled new workstream from recorded `HEAD`. Interrupting a
turn never implies that its partial filesystem changes were reverted.

## Context and External Memory

The immediate transfer resembles Plan mode's clear-context implementation path.
The proposed required capsule is deliberately small:

- exact triggering user prompt;
- accepted provider plan or explicitly staged `TaskBrief`;
- source/destination task, thread, and workstream identities;
- current repository, branch, worktree, and recorded commit; and
- explicit artifacts or acceptance criteria referenced by the brief.

Switchboard should not synthesize or transfer the whole source transcript.

Broader project recall is delegated to an external cross-session memory
capability. Claude-mem may be the first reference integration, but the product
contract must describe capability and health rather than hard-code one
implementation.

The proposed operating profiles are:

| Profile | Behavior |
| --- | --- |
| Full continuity | Cross-session memory healthy plus immediate transition capsule |
| Immediate only | Plan/brief and repository state available; memory unavailable |
| Blocked | Exact prompt or accepted transition artifact cannot be delivered safely |

Memory is advisory and may be eventually updated. It never authorizes routing,
Git mutation, thread identity, or exact-once delivery. The navigator must show
when continuity is degraded rather than silently claiming full recall.

## Provider Adapter Boundary

A provider adapter should expose observed capabilities, not version
allowlists:

- fresh thread with initial input;
- native clear-context/plan rollover;
- exact thread resume;
- native active-turn interrupt with terminal-state confirmation;
- provider-native fork when useful;
- structured plan retrieval;
- thread naming;
- lifecycle hook identity and ordering; and
- safe historical presentation.

Installed version numbers remain diagnostics. Startup and acceptance probes
must validate the behavioral contract and fail closed when it changes.

Native operations are preferred when they preserve the required invariants.
Switchboard-owned emulation is allowed only after equivalent failure and
recovery behavior is proven.

For an out-of-band Codex fork, the proved App Server `thread/fork` path returns
the exact new provider identity and accepts a completed-turn boundary. That is a
better transaction primitive than launching `codex fork`, whose target identity
is provider-allocated after launch. A production adapter would still need to
feature-probe the installed App Server contract, create the matching worktree,
resume the returned identity in a managed pane, deliver B's first prompt exactly
once, and recover partial failure without disturbing A.

## Required Spikes and Studies

All provider work uses isolated provider homes, Switchboard state, tmux servers,
worktrees, and disposable sessions. No study may stop existing agent sessions,
restart the user's tmux server, restart DMS, or install hooks that affect
unmanaged native sessions.

### S1: Codex native plan rollover observation

Prove against the installed Codex contract:

1. create and name an isolated Plan-mode source thread;
2. produce an accepted structured plan;
3. select **clear context and implement**;
4. observe old/new exact UUIDs, hook order, source `clear`, pane/process/cwd
   continuity, and carried plan;
5. prove the old thread remains resumable and both threads can be named;
6. prove Switchboard can register lineage without injecting or duplicating
   input; and
7. prove the source result remains inspectable after the transition.

### S2: Codex ordinary-implementation interception feasibility

Status: trigger timing passed; destination transition reuses already-proven
cutover mechanics.

The study determined whether **implement this plan** could technically support
an optional fresh-thread policy:

1. observe the Plan -> Default transition and exact prompt hook;
2. retrieve the approved Plan item through a supported structured interface;
3. stop the source prompt before it is recorded or sampled;
4. start/bind the destination and submit exactly once;
5. inject failures before and after each external boundary; and
6. restore source usability without an ambiguous duplicate turn.

The live input was held before sampling, the exact structured Plan item remained
available, and source text/model execution did not occur. Automated fault tests
proved exact-once delivery and recovery around the existing transaction. The
sanitized result is
`spikes/fixtures/thread-workstream/codex/0.145.0/execution-trigger.json`.

The proposed v1 policy deliberately does not activate that capability.
**Implement this plan** stays in the current thread; **clear context and
implement** remains the explicit cutover choice.

### S3: Claude Code equivalent study

Identify and validate Claude Code's supported plan-exit, new-session,
resume/fork, naming, hook, and initial-input behavior. The outcome may be:

- native equivalent;
- safe provider-adapter emulation; or
- explicit manual-only/degraded support.

Do not assume matching UI labels or implementation details across providers.

### S4: Navigator visibility and historical inspection

Prove that the sidebar can:

- show source and current exact threads through a same-pane native rollover;
- inspect a parked or stopped historical provider thread without changing the
  active workstream tip;
- fence historical input;
- return to current work in one action; and
- expose equivalent identity in direct mode without transcript injection.

### S4.1: Navigator-initiated stable fork during active work

Status: native provider boundary passed; production navigator composition
remains unapproved.

Using an isolated Codex App Server and disposable repository:

- complete one baseline turn;
- begin approach A and observe its command still running;
- fork through the exact completed baseline turn;
- start and complete approach B in the fork;
- prove A remains active until explicitly interrupted for cleanup;
- prove the fork contains no in-progress A state; and
- prove unrelated agent processes and the user's tmux panes remain unchanged.

The unassisted result is
`spikes/fixtures/thread-workstream/codex/0.145.0/running-source-fork.json`.
A second unassisted run performed the fork beside an actively working isolated
Codex TUI and retained
`spikes/fixtures/thread-workstream/codex/0.145.0/navigator-running-fork.json`.
The provider process, managed pane, and working directory remained stable while
the forked alternative completed. Filesystem isolation composes with S5; no
installed navigator action was created.

### S5: Managed workstream worktrees

Using a disposable repository:

- discover worktrees from `git worktree list --porcelain`;
- create a collision-free managed branch/worktree from a recorded commit;
- bind it to a new workstream and provider cwd;
- switch between independent workstreams;
- surface dirty/ahead/behind/merged status;
- retire only an exact clean managed worktree; and
- prove external or dirty worktrees are never removed.

### S6: External memory continuity

With and without the reference memory provider:

- confirm both source and destination sessions use the intended project memory
  scope;
- measure whether recent planning context is available before the destination's
  first turn;
- exercise delayed, unavailable, and stale retrieval;
- verify the accepted plan remains sufficient immediate context; and
- expose full/immediate-only continuity accurately in the navigator.

### S7: Combined watched workflow

Run the proposed user path end to end:

```text
project/workstream
-> plan thread
-> user chooses same-thread implementation or explicit fresh implementation
-> visible ordinary result
-> alignment
-> next task rollover
-> start unrelated work from the navigator while a task runs
-> fork approach B from approach A's latest settled state
-> inspect an old thread
-> explicit fork from history into a managed worktree
-> switch between both workstreams
```

Acceptance requires exact identities, one execution submission to one provider
thread, intact result tips, no global disruption, and recoverable failure at
every transition.

## Decision Gates

The redesign advances only if the studies establish:

- a stable observable source/destination thread boundary for at least one
  provider;
- exact-once pre-turn delivery without post-turn management traffic;
- persistent source/current visibility;
- native historical inspection with safe input fencing;
- isolated workstream filesystem ownership;
- an out-of-band explicit action that can start or fork work while the source
  is active without copying its in-progress turn;
- explicit degraded behavior when provider or memory capabilities are absent;
- and failure containment that leaves unrelated native work untouched.

If a provider lacks a safe native transition path, an explicit managed
fresh-thread transition is an acceptable initial capability. If provider naming
is unreliable, Switchboard
names remain authoritative. If external memory is delayed, the accepted
plan/brief remains the immediate handoff. If historical dirty state cannot be
reconstructed, the UI must say so rather than manufacturing a snapshot.

## Proposed Roadmap Impact

Phase 6A.1 through 6F.2 remain accepted implementation history. This proposal
does not rewrite that evidence.

Phase 6G recursive parent/child frames should pause before implementation. Its
task A -> B -> A return model deepens the result-hiding workflow that motivated
this review. The next work should be the bounded S1-S6 studies, followed by a
design decision:

- reject or narrow this proposal and resume the accepted roadmap; or
- approve a clean-break thread/workstream contract, archive the superseded
  parent/child workflow, and plan implementation from the proven capabilities.

The studies and decision are now complete. No schema, command, hook, or
frontend contract in this document was approved by that directional decision.
