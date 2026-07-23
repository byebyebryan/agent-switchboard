# Proposed Thread and Workstream Redesign

Date: 2026-07-23

Status: studies complete; direction accepted, proposed contracts remain unapproved

> **Decision:** the
> [Thread and Workstream Redesign Decision](thread-workstream-redesign-decision.md)
> accepts this direction for clean-break production-contract design after the
> core and secondary studies passed. This proposal remains non-normative and
> none of its interfaces are approved or implemented.

This proposal records a possible clean-break successor to the accepted Phase 6
workspace/child/return workflow. It does not change the current registry,
commands, hooks, state contract, or installed behavior. Implementation requires
the provider, hook, tmux, memory, history, and Git spikes defined below.

If those studies fail, the proposal may be narrowed or rejected without
creating a compatibility obligation.

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
3. Automatic rerouting occurs before the destination agent begins the user's
   execution turn.
4. The triggering execution turn is sampled by at most one working provider
   thread. Any boundary classifier is advisory and cannot perform the task.
5. The persistent UI identifies the source thread and current destination
   thread before and after a transition.
6. A user can initiate the same transition without depending on task-boundary
   inference.
7. Provider thread history is never silently deleted or rewritten.
8. Historical inspection does not silently change the active workstream tip.
9. Explicit parallel work does not share a mutable checkout by default.
10. Switchboard failure never requires unrelated native agent sessions or the
    user's tmux server to stop.

## Proposed Sequential Happy Path

The desired flow is task-to-task inside one persistent user view:

```text
task A thread remains visible through work, review, and alignment
-> task A produces or records an accepted plan/brief for B
-> user commits to execution ("Implement the plan", "Go ahead with B")
-> Switchboard/provider rolls to a fresh B thread before model work
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

Rerouting that action is therefore proposed only if a spike proves all of:

- reliable compound detection of completed Plan mode -> Default mode;
- supported structured retrieval of the exact approved `PlanItem`;
- blocking before source-thread input is recorded;
- exact-once creation and submission to the destination thread; and
- restoration of source input when any step is uncertain.

Until that proof exists, native clear-context rollover is the preferred Codex
path and ordinary implementation remains in the source thread.

### Generic conversational rollover

Not every task uses Plan mode. A generic provider-neutral transition may be
staged by an agent or inferred before a turn, but uncertainty must keep the
prompt in the current thread.

Model-based classification is not part of the input hot path until latency,
false-positive, cancellation, and exact-once delivery studies pass. Structured
signals such as an accepted provider Plan item take priority over inference.

## User-Initiated Transitions

Automatic operation and manual operation should invoke one semantic command,
not separate workflows. Proposed user actions are:

- **Next prompt starts a new task** from the current workstream tip;
- **Start fresh from this plan** from a selected accepted plan;
- **Fork workstream from here** from the current or a historical task; and
- an equivalent bounded CLI/direct-mode action.

Arming a transition should not open a task-administration form. The user's next
ordinary prompt is the destination thread's first real prompt.

The navigator must show the pending source and intended transition before input
is accepted, and the confirmed source/destination identities after binding.

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
- explicit workstream fork creates a fresh branch and managed worktree;
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

### S2: Codex ordinary-implementation interception

Determine whether **implement this plan** can safely be standardized as a fresh
thread:

1. observe the Plan -> Default transition and exact prompt hook;
2. retrieve the approved Plan item through a supported structured interface;
3. stop the source prompt before it is recorded or sampled;
4. start/bind the destination and submit exactly once;
5. inject failures before and after each external boundary; and
6. restore source usability without an ambiguous duplicate turn.

Failure of S2 narrows initial support to the native clear-context choice.

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
-> native or safely emulated fresh implementation thread
-> visible ordinary result
-> alignment
-> next task rollover
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
- explicit degraded behavior when provider or memory capabilities are absent;
- and failure containment that leaves unrelated native work untouched.

If a provider lacks a safe automatic path, manual fresh-thread transition is an
acceptable initial capability. If provider naming is unreliable, Switchboard
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
