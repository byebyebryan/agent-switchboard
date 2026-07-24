# Thread and Workstream Redesign Roadmap

Date: 2026-07-23

Status: proposed delivery sequence; Phase 7A contract work is next, production
implementation remains unapproved

Product maturity: no viable or adopted Switchboard product exists yet

Target release: a fresh generation selected by Phase 7A, with no compatibility
target

## Outcome

Switchboard becomes an explicit workstream manager around provider-native
threads, native terminal UIs, and isolated filesystems.

The user decides when to:

- keep implementing in the current thread;
- clear context and implement in a fresh thread;
- interrupt the current attempt;
- start unrelated work in a new workstream; or
- fork the settled prefix of a task into an alternative workstream.

Switchboard performs the requested lifecycle transaction and recovery. It does
not infer that a plan, result, or phrase is a task boundary and route on the
user's behalf.

This roadmap follows the accepted
[Thread and Workstream Redesign Decision](thread-workstream-redesign-decision.md)
and its [deep review](thread-workstream-redesign-review.md). It converts the
remaining production gates into a dependency-ordered delivery sequence. It
does not approve a registry, schema, public command, hook, or installed
behavior.

## Product Maturity and Compatibility Boundary

Phase 6 is accepted engineering evidence, not a viable product release. It was
never adopted as the user's normal workflow, so its registry, configuration,
commands, hooks, state semantics, and UI do not create a backwards-
compatibility obligation.

Phase 7 therefore starts as a new product generation:

- breaking changes are expected when they produce a clearer model;
- fresh initialization is the default;
- no dual schema, compatibility alias, legacy reader, translation layer, or
  in-place upgrade is required;
- old Switchboard-owned development state may be discarded without migration;
- native provider sessions, normal tmux state, repositories, and user
  configuration remain protected even when all Switchboard state is replaced;
  and
- no migration or extraction tool is planned. A concrete future need would be
  a separate offline project, not a compatibility feature in the new runtime.

Existing code is reusable only when it directly expresses the new abstraction,
keeps its natural name and ownership boundary, and is simpler than replacing
it. If reuse requires Frame-shaped compatibility, misleading terminology,
legacy branching, or a translation shim, replace it. Previous work remains
valuable as behavioral evidence, safety knowledge, test infrastructure, and a
source of well-fitted low-level components; it is not the product foundation
that the new design must preserve.

## Working Product Model

These terms describe the product direction. Phase 7A must turn them into an
approved durable contract before implementation:

| Term | Product meaning |
| --- | --- |
| Project | Configured repository context and workstream creation boundary |
| Workstream | Independently focusable line of work with exclusive filesystem ownership |
| Task | User intent that may continue through more than one provider thread |
| Provider thread | Provider-owned conversation and history identity |
| Transition | Explicit, recoverable movement between provider identities or workstreams |
| Checkpoint | Exact clean filesystem state associated with a completed provider turn |
| Pending action | Durable user-authorized lifecycle transaction with one idempotency identity |

A workstream may rotate from thread A to B without changing its task or
filesystem. A fork creates a new workstream whose provider history and
filesystem share the same settled prefix. Starting unrelated work creates a
new workstream without claiming conversation-fork lineage.

The canonical v1 intent matrix remains:

| User intent | Provider lineage | Filesystem lineage | Timing |
| --- | --- | --- | --- |
| Implement here | Same thread | Same worktree | Normal next turn |
| Clear and implement | Fresh thread, same task | Same worktree | Native Plan action |
| Start new thread | Fresh thread, same workstream | Same worktree | Source must be idle |
| Interrupt | Same thread becomes interrupted | Partial changes remain | While active |
| Start new workstream | No conversation-fork claim | Fresh worktree from selected base | While another workstream runs |
| Fork this task | Fork through latest completed turn | Fresh worktree from its exact checkpoint | While the source runs |

## Prior Work: Evidence and Reusable Parts

Reuse is an implementation choice made component by component, never a roadmap
constraint.

| Phase 6 or spike asset | Roadmap treatment |
| --- | --- |
| Host, project, repository, and checkout discovery | Reuse only if it fits the new workstream model without Frame translation |
| Exact provider session, process, pane, cwd, and tmux-generation evidence | Strong candidate for direct reuse as lifecycle evidence |
| Native provider start/resume and managed-surface launch | Extract or reimplement behind the new capability adapter |
| Idempotency, recovery, and generation-publication work | Reapply the proven patterns to new records; do not preserve the schema |
| Resident navigator, direct mode, pane fencing, and focus/attach mechanics | Reuse low-level presentation mechanics where their ownership remains accurate |
| Capability-bound agent tools and no-op hooks without pane-local authority | Carry forward the security rule; rewrite interfaces around new actions |
| Isolated tmux/provider test harness and sanitized fixtures | Extend as evidence infrastructure |
| Managed-worktree ownership and exact-clean retirement spike | Promote only after the compound transaction passes |
| Input-fenced native history inspection | Reuse the proved boundary; do not add transcript rendering |
| External-memory continuity profiles | Reuse as an optional enrichment contract |
| Reproducible build, content audit, and clean-wheel smokes | Retain as release gates |

The following Phase 6 semantics are superseded for the new generation:

- recursive workspace/task `Frame` parent-child control;
- automatic task push, claim, complete-and-return, and parent synthesis;
- `WorkContext` foreground stacks as the source of task lineage;
- post-result handoff or routing turns;
- a model-generated result as transition authority; and
- Phase 6G's A -> B -> A recursive-return exit.

The `0.3.4` tree remains a reference implementation and evidence host while the
new contract is designed. Phase 7 may replace or delete any part of it. The new
runtime must not read or reinterpret old rows as workstreams by default, and
there is no live cutover or data-migration obligation.

## Invariants for Every Phase

Every implementation slice must preserve these boundaries:

- A completed provider result remains the visible provider-pane tip until a
  real user action.
- Switchboard writes no claim, synthesis, routing, completion, or status turn
  after that result.
- One explicit user action authorizes one bounded lifecycle transaction.
- A destination receives at most one prepared first prompt.
- Hooks and provider events are evidence, never sufficient authority alone.
- Ambiguous identity, ancestry, ordering, capability, or commit outcome fails
  closed into visible recovery.
- A mutable checkout is owned by at most one active workstream.
- Source and current thread/workstream identities remain visible during a
  transition.
- Background completion creates attention only; it never steals focus.
- Switchboard never commits user changes to manufacture a fork checkpoint.
- Normal tmux servers, native provider sessions, repositories, and user
  configuration remain outside development and test ownership.
- Ordinary **implement this plan** stays in the current thread.
- Automatic acceptance inference and forced ordinary-Plan cutover remain out
  of v1.

## Delivery Graph

```text
7A production contract
  |
  +----> 7B durable state --------+
  |                               |
  +----> 7C provider lifecycle ---+--> 7D recoverable action engine
                                           |
                                           v
                                  7E checkpoint/worktree composition
                                           |
                                           v
                                  7F navigator and CLI actions
                                           |
                                           v
                                  7G Plan cutover and aliases
                                           |
                                           v
                                  7H history and attention
                                           |
                                           v
                                  7I fresh-generation acceptance
```

Phase 7B and 7C may be developed independently after 7A is approved. Every
later phase is gated by both. S7 is the final combined acceptance study, not an
early implementation shortcut.

## Phase 7A: Production Contract and Fresh-Generation Boundary

Status: next planning phase; no runtime implementation authorized

### Goal

Approve one coherent state, authority, recovery, and fresh-generation contract
before changing production code.

### Deliverables

- Durable definitions and relationships for project, workstream, task,
  provider thread, transition, checkpoint, pending action, surface, view,
  attention, and recovery.
- State machines for implement-here, clear-and-implement, idle new-thread,
  interrupt, new-workstream, task-fork, focus, inspect-history, retire, and
  retry/reconcile.
- Exact action authority and idempotency scopes for navigator, CLI,
  provider-native action adoption, trusted hooks, and agent tools.
- Transaction boundaries and crash matrices, especially for provider
  operations whose responses may be lost.
- Source/current identity, attention, and result-tip presentation rules.
- Filesystem checkpoint, ownership, dirty-state, and retirement rules.
- Installed provider capability and behavioral-fingerprint gates.
- Claude Code's explicit unsupported/manual contract for the Codex-first
  release.
- Fresh-start, legacy-disposal, installation, removal, and failed-publication
  policy.
- A production acceptance matrix mapped to retained spike evidence and S7.

### Required decisions

- Whether the new generation uses a new config/schema/package namespace and
  what version names it.
- Which current modules can retain their implementation and names without
  distorting the new model.
- Whether task is a durable first-class record or a semantic lineage attached
  to workstreams and transitions.
- Whether v1 workstreams own one repository checkout or an atomic checkout set
  for multi-repository projects; mutable checkout sharing is never implicit.
- Which Git repository forms are supported and how non-Git, bare, submodule,
  and other unsupported checkpoint sources fail visibly.
- How a completed provider turn is bound to an exact Git checkpoint.
- Which native Codex actions may be securely adopted and which must originate
  in the navigator or CLI.
- How exclusive pending intent is represented while reconciling a lost
  `thread/fork` response.

### Exit gate

The contract is reviewed and explicitly approved; every action has one
authority source, one idempotency boundary, defined crash recovery, and defined
user-visible failure. The no-compatibility position and every exception are
explicit. No production code begins while provider identity, checkpoint
ancestry, repository ownership, or legacy disposal remains ambiguous.

## Phase 7B: Durable State Foundation

Status: blocked on Phase 7A approval

### Goal

Implement the approved clean state model without starting, stopping, focusing,
or mutating a provider session.

### Deliverables

- Fresh registry/configuration baseline for the approved generation.
- No Phase 6 schema reader, command alias, dual-write path, or compatibility
  namespace in the new runtime.
- Referential and uniqueness constraints for workstream, provider-thread,
  surface, checkpoint, transition, pending-action, attention, and recovery
  records.
- Compare-and-swap revisions, leases where required, deterministic ordering,
  and bounded public projections.
- Atomic rotation of current/previous provider bindings and capabilities.
- Durable action receipts and replay-safe state transitions.
- Fresh initialization and legacy rejection/disposal selected in Phase 7A.
- Generation-safe initialization, publication, failed-publication cleanup,
  reset, and stale-owner diagnostics.

### Acceptance

- Strict round trips, bounds, Unicode, reference authority, and deterministic
  output.
- Concurrent action and revision conflicts fail closed.
- Every injected transaction crash leaves either the prior state, the complete
  next state, or one actionable recovery record.
- Old Phase 6 state is rejected with one fresh-start diagnostic.
- Reset and failed-publication cleanup do not touch provider processes, panes,
  tmux servers, or worktrees.

### Exit gate

The new state kernel can model and recover every approved lifecycle action
using fake provider and filesystem adapters. It performs no live provider
control and contains no Frame compatibility layer.

## Phase 7C: Codex Lifecycle and Capability Adapter

Status: blocked on Phase 7A approval

### Goal

Turn the observed Codex contract into a bounded installed capability adapter,
without yet exposing production thread-management actions.

### Deliverables

- Capability checks for start, resume, fork, interrupt, native clear rollover,
  exact thread lookup, and ancestry reconciliation.
- Behavioral/schema fingerprinting in addition to the installed version.
- Exact launch, surface, tmux generation, pane, process birth, cwd, provider
  identity, and source-thread evidence.
- Native clear/new adoption rules that distinguish startup, resume, generic
  clear, replay, stale events, and explicit Plan cutover.
- Provider-side reconciliation queries for a lost or uncertain fork response.
- Managed-pane resume that binds the exact provider-returned identity.
- A capability result that reports Claude automatic lifecycle support as
  unavailable/manual; no Claude adapter is required for the Codex-first
  release.

Current Codex documentation exposes native `/new` and `/fork` commands and App
Server `thread/start`, `thread/resume`, `thread/fork`, and `turn/interrupt`.
Those names are discovery inputs, not a permanent compatibility promise; local
behavioral gates and isolated live evidence remain decisive.

### Acceptance

- Re-run A -> B -> C rollover with the production adapter in isolated roots.
- Re-run forged, stale, replayed, concurrent, wrong-pane, wrong-process,
  wrong-cwd, unknown-predecessor, startup, resume, and partial-commit cases.
- Interrupt an App Server-owned active turn and separately a native-TUI-owned
  active turn.
- Lose a fork response and enumerate provider-recorded ancestry without
  adopting an ambiguous candidate.
- Prove unrelated tmux and provider identities are unchanged.

### Exit gate

The adapter either proves the installed Codex contract or disables the
unsupported action with an actionable diagnostic. No version-only allowlist or
best-effort identity adoption is accepted.

## Phase 7D: Recoverable Explicit-Action Engine

Status: blocked on Phase 7B and 7C

### Goal

Execute one explicit lifecycle intent exactly once across durable state and
provider operations, without a resident provider supervisor.

### Deliverables

- One requested-action envelope with action ID, authority scope, source
  revision, expected capability, and bounded payload.
- Exclusive pending intent and compare-and-swap admission for conflicting
  actions.
- State machines for clear adoption, idle new-thread, interrupt, independent
  workstream start, task fork, first-prompt delivery, focus, and cancellation.
- Exact-once destination-prompt preparation and delivery receipts.
- Recovery for timeouts, process loss, tmux loss, uncertain provider response,
  stale source revision, and partial durable commit.
- Fork reconciliation that accepts one exact ancestry match and blocks on zero
  or multiple candidates; caller-preallocated provider UUIDs are not assumed.
- Event replay and idempotent user retry behavior.

### Acceptance

- Exhaustive state-machine and fault-injection tests around every external
  boundary.
- Repeated A -> B -> C capability rotation survives restart and replay.
- Duplicate action delivery never duplicates provider input.
- Concurrent clear, fork, interrupt, and focus requests settle one winner and
  expose deterministic outcomes for the rest.
- A prepared destination submission ends without any follow-up management
  traffic.

### Exit gate

All provider-only transactions are restart-safe and exact-once from the user's
perspective. Worktree-backed new/fork actions remain disabled until Phase 7E.

## Phase 7E: Checkpoints and Managed Workstream Filesystems

Status: blocked on Phase 7D

### Goal

Compose provider transitions with exclusive filesystem lineage.

### Deliverables

- Recorded project base and completed-turn checkpoint validation.
- Collision-free managed worktree and branch allocation.
- Independent new-workstream creation without conversation-fork lineage.
- Compound task-fork transaction:
  checkpoint validation, worktree creation, `thread/fork`, durable binding,
  managed-pane resume, first-prompt delivery, and optional explicit focus.
- Recovery and cleanup for every partial compound state.
- Conservative managed retirement with exact owner, repository, checkout,
  active-use, cleanliness, and merged-state checks.
- Visible blocked/degraded outcomes when the requested exact checkpoint cannot
  be reconstructed.

### Acceptance

- Run the provider fork and managed-worktree path in one isolated watched test;
  separate passing primitives are no longer sufficient.
- Fork while the source TUI continues, complete the alternative, then let the
  source finish without shared mutable files.
- Crash before and after every provider, registry, worktree, pane, prompt, and
  focus boundary.
- Reject shared, external, mismatched, dirty, unmerged, or active worktrees.
- Never create a Git commit merely to make a source turn forkable.
- Never stash, reset, force-remove, or mutate an unowned checkout.

### Exit gate

New and forked workstreams have exact provider and filesystem ownership,
recover cleanly, and cannot claim stronger ancestry than the recorded
checkpoint proves.

## Phase 7F: Navigator and CLI Thread Management

Status: blocked on Phase 7E

### Goal

Make explicit thread management available even while the provider composer is
not waiting for user input.

### Deliverables

- Navigator actions for interrupt, start new workstream, fork current task,
  start a fresh thread when idle, focus, return, retry recovery, and guarded
  retirement.
- Matching direct CLI actions with explicit request IDs and confirmation
  boundaries.
- Pending/confirmed/blocked transition rows with source and destination
  identities.
- Explicit focus policy: an action may focus its requested destination;
  later background completion only raises attention.
- User-input fencing for managed panes during compound actions.
- Accessibility, key discovery, confirmation, and stale-revision feedback.

### Acceptance

- Initiate interrupt, independent work, and task fork from the navigator while
  the source TUI is actively sampling.
- Return to either workstream in one navigator action.
- Preserve byte-stable completed result tips in every provider pane.
- Prove no navigator status or recovery text enters a provider pane.
- Prove CLI and navigator retries share the same idempotency outcome.

### Exit gate

The navigator is the canonical out-of-band control layer, direct mode has a
complete safe equivalent, and no thread-management operation requires the
provider to be waiting at its composer.

## Phase 7G: Plan Cutover and Conversational Aliases

Status: blocked on Phase 7F

### Goal

Integrate explicit Plan and prompt-bound thread intents without turning
language inference into authority.

### Deliverables

- Secure adoption of provider-native **clear context and implement** as a
  fresh-thread continuation of the selected structured plan.
- Ordinary **implement this plan** remains untouched in the current thread.
- Explicit **implement selected plan in a fresh thread** navigator and CLI
  actions with artifact provenance.
- A small documented set of exact reserved conversational aliases for explicit
  actions at the next user-prompt boundary.
- Optional suggestions for fuzzy natural language that require confirmation
  and cannot route by themselves.
- Generic-clear classification that may rotate the provider thread but cannot
  fabricate a task boundary.

### Acceptance

- Repeat clear-and-implement A -> B -> C through the production stack.
- Prove the selected plan reaches each destination exactly once and only the
  destination samples its execution turn.
- Prove ordinary implementation remains in place.
- Reject plan mismatch, source revision mismatch, duplicated input, forged
  hook evidence, generic clear, and stale alias actions.
- Preserve the source result and destination result until the next real user
  action.

### Exit gate

Plan cutover is explicit, repeatable, and secure. Automatic ordinary-Plan
interception, semantic acceptance inference, and forced cutover remain separate
future policy studies.

## Phase 7H: History, Result Tips, and Attention

Status: blocked on Phase 7G

### Goal

Make parallel work understandable without sacrificing native result and
history ownership.

### Deliverables

- Navigator source/current identities, pending transitions, background
  completion attention, and explicit focus return.
- Navigator and CLI actions for input-fenced historical inspection.
- Provider-native historical inspection behind an input-dropping PTY boundary.
- Workstream/thread history and ancestry presentation from bounded metadata,
  without transcript storage or rendering.
- Bounded transition and selected-plan metadata sufficient for immediate
  continuation without an external-memory service.

### Acceptance

- Park the current workstream, inspect an earlier thread, attempt input, and
  prove no prompt, turn, or history mutation reaches it.
- Return to current in one action without moving the workstream tip.
- Complete background source and fork turns in both orders; raise attention
  without auto-focus or provider traffic.
- Preserve provider panes byte-for-byte after result completion.

### Exit gate

Parallel work remains legible and history remains provider-native and
read-only. Basic continuation has no external-memory dependency. If native
history cannot be fenced for an installed provider, full historical inspection
is explicitly disabled.

## Phase 7I: Fresh-Generation S7 Acceptance

Status: blocked on Phase 7H

### Goal

Prove the complete fresh product as installed, then make it available for an
explicit user adoption decision.

### Combined S7 workflow

The installed acceptance must:

1. Enter a project in the resident navigator.
2. Produce a structured plan and choose both ordinary implementation and
   clear-and-implement in separate isolated cases.
3. Rotate A -> B -> C without duplicate input or post-result traffic.
4. Start unrelated work in a separate managed workstream.
5. Fork an active task through its latest completed checkpoint while the
   source continues.
6. Interrupt one active turn without claiming filesystem rollback.
7. Let background work finish, observe attention, and focus it explicitly.
8. Inspect source history through the input fence and return in one action.
9. Retire only an exact clean managed workstream.
10. Restart Switchboard at injected compound-action crash points and reconcile
    to one deterministic result.

### Release gates

- Fresh install, legacy-state rejection, removal, reset, failed-publication
  cleanup, and stale-owner recovery.
- Removed Phase 6 commands are unknown, old config/state fail with one
  fresh-start diagnostic, and built artifacts contain no active legacy
  semantic modules or compatibility adapters.
- Full automated tests, lint, compile validation, and `git diff --check`.
- Two byte-identical builds, exact package-content audit, clean-wheel install,
  and isolated command/navigator smokes.
- Sanitized installed fixtures for capability fingerprint, action order,
  isolation, privacy, cleanup, and timings.
- Exact pre/post identities for unrelated tmux panes, tmux servers, provider
  processes, repositories, worktrees, and user configuration.
- No provider UUIDs, prompts, transcripts, unrestricted paths, process IDs, or
  credentials in committed evidence.

### Exit gate

S7 passes unassisted on the installed Codex-backed product, removal and
failed-publication safety are proven, and the user explicitly approves workflow
adoption. A failed core gate stops the release; it does not trigger a
production fix-forward that weakens identity, result preservation, or
filesystem ownership.

## Deferred Beyond the Core Release

- Automatic interception of ordinary **implement this plan**.
- Forced cutover as a configurable default.
- Fuzzy plan/acceptance inference with routing authority.
- Mid-turn fork beyond the latest completed provider turn.
- Dirty or uncommitted historical filesystem reconstruction.
- Automatic commit, merge, stash, reset, force-removal, or conflict resolution.
- Cross-host workstream fork or pane movement.
- Transcript rendering or provider-history ownership.
- External-memory integrations. If later added, they must report `full` or
  `immediate-only` continuity accurately and must never mint authority.
- Claude automatic parity until an isolated installed contract proves native
  rollover or safe emulation.
- DMS or another desktop shell as a release dependency.

## First Recommended Work

Begin and stop at Phase 7A.

The first deliverable should be a reviewable production contract plus
state-machine and crash-matrix fixtures. It should explicitly decide the
durable entities, action authority, checkpoint binding, uncertain-fork
reconciliation, repository ownership, unsupported checkout behavior, legacy
disposal, and capability gates. Only after that contract is approved should
implementation start with the Phase 7B state foundation and Phase 7C Codex
adapter.
