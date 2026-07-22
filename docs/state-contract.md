# Phase 6 State and Control-Turn Contract

Date: 2026-07-21

Status: accepted Phase 6A.1 normative contract; implementation pending

Target release: `0.3.0`

This document is the storage and state-machine authority for the view-first
replacement. [The design](design.md) owns product boundaries,
[the workflow](view-workflow.md) owns user-visible behavior, and
[the Phase 6 plan](phase-6-plan.md) owns delivery and activation.

## Authority and Transactions

One host owns all rows for its frames, work contexts, views, transitions,
provider sessions, surfaces, and recoveries. Remote and cached state never
authorizes mutation.

Every mutation has a canonical request UUID and normalized semantic
fingerprint. Reusing a request UUID with the same fingerprint is idempotent.
Reusing it for a different semantic target is a conflict. Presentation
capabilities such as “desktop focus is available” are not semantic target
fields, so a focus miss may retry the same request without repeating its state
mutation.

SQLite transactions own durable intent and compare-and-swap decisions. tmux,
provider, SSH, compositor, and process operations are external side effects and
are never described as part of a database transaction. Each external operation
is a saga with durable intent, observed evidence, and a repairable phase.

## Relational Model

All identifiers below are opaque UUIDs except provider session keys and tmux
server/pane evidence. Timestamps are UTC Unix milliseconds.

### Frame

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

Normative invariants:

- Exactly one workspace root exists for `(host_id, project_id)`.
- A task has one parent on the same host, in the same project and work context.
- Parent edges are acyclic. Configured depth is enforced before insertion.
- `current_session_key`, when present, refers to a `FrameSession` for that
  frame.
- `open -> closing -> closed` are the only normal lifecycle edges. Reopen is
  `closed -> open`, clears close reason, and retains identity and lineage.
- `completed` requires one exact completion handoff. `dismissed` forbids one.

### FrameSession

```text
FrameSession
  frame_session_id
  frame_id
  session_key
  ordinal
  membership_reason       started | resumed | rollover | recovery | cutover
  joined_at
```

A provider session belongs to at most one frame. `(frame_id, ordinal)` and
`session_key` are unique. Resuming the same provider UUID into another process
does not create another membership. Provider rollover appends a membership and
updates the frame's current session atomically.

### WorkContext

```text
WorkContext
  work_context_id
  host_id
  checkout_id
  claim_state             released | held | blocked
  claim_generation
  foreground_frame_id     optional
  background_state        safe | known | uncertain
  acquired_at             optional
  released_at             optional
  updated_at
```

The WorkContext is durable; its checkout claim is not permanent. One partial
unique constraint permits only one `held` context per checkout. One foreground
frame exists per held context and must be an open member of that context.

A claim may release only when the context has no live mutation-capable managed
surface, pending transition, or known/uncertain background work. Historical
workspace/task frames remain attached to the released context. Re-entry uses a
generation-checked reacquisition. An explicit human override may transfer a
claim after a bounded warning; automatic transitions never override known or
uncertain background evidence.

Parent-to-child push keeps the claim held and transfers only
`foreground_frame_id`. The target may bind with claim-only authority, but
`transition_claim()` must not release its semantic brief until the foreground
CAS names the target frame.

### UserView and FramePlacement

```text
UserView
  view_id
  host_id
  mode                    navigator | direct
  active_frame_id         optional during recovery
  state                   ready | transitioning | degraded | retired
  revision
  desktop_token
  tmux_server_id
  created_at
  last_attached_at
  updated_at

FramePlacement
  placement_id
  host_id
  view_id
  frame_id
  surface_id              optional
  state                   active | parked | staged | stopped_affinity | orphaned
  generation
  last_focused_at
  updated_at
```

One nonretired placement owns an open frame. One `active` placement exists per
ready/transitioning view and agrees with `active_frame_id`. A live surface
belongs to exactly one placement and view. Stopped affinity preserves the
deterministic home view without pretending a physical pane exists. Moving
stopped affinity is allowed only after duplicate-runtime reconciliation.

The workspace placement is the project entry authority. A project route selects
the most recently focused open descendant in that workspace's owning view,
falling back to the workspace frame. If the view already shows that project,
the active frame remains the target. Concurrent route opens single-flight on
the workspace frame and converge even when callers use different request IDs.

### Surface and tmux server evidence

```text
TmuxServer
  tmux_server_id
  host_id
  socket_path
  server_pid
  server_start_time
  observed_at

Surface
  surface_id
  host_id
  provider
  session_key             optional until binding
  launch_id
  lifecycle_state         planned | live | dead | orphaned | retired
  tmux_server_id          optional until physical creation
  pane_id                 optional until physical creation
  process_id              optional
  process_birth_id        optional
  metadata_generation
  created_at
  updated_at
  retired_at              optional
```

`planned` is durable intent, not a claim that a tmux pane exists. Physical
creation advances the surface only after exact pane metadata and server
generation are re-read. Socket path, server PID, and tmux `start_time` must all
agree. A server restart invalidates every old locator even if socket and pane
names are reused.

Only an exact launch-owned pane/window may be killed. Killing an owning view or
tmux server is never a surface cleanup primitive.

### ViewTransition

```text
ViewTransition
  transition_id
  request_id
  request_fingerprint
  host_id
  view_id
  kind                    focus | push | back | complete_return | human_close |
                          mode | recover
  source_frame_id         optional
  target_frame_id
  work_context_id         optional
  expected_view_revision
  expected_claim_generation optional
  state                   prepared | executing | presented | awaiting_claim |
                          settling | completed | cancelled | superseded | failed
  execution_owner         optional
  lease_expires_at        optional
  transport_phase         intent | moved | inspected | committed | rolled_back
  failure                 optional bounded record
  created_at
  updated_at
```

Legal edges are:

```text
prepared -> executing | cancelled | superseded | failed
executing -> presented | failed
presented -> awaiting_claim | settling | failed
awaiting_claim -> settling | failed
settling -> completed | failed
```

Only `prepared` may be cancelled or superseded. Mode/focus mutations cannot
overtake `executing` or later states. One nonterminal transition exists per
view. Execution claims compare view revision, WorkContext generation, exact
source/target placement, and the transition lease in one transaction.

`transport_phase` makes an uncertain tmux outcome inspectable rather than
repeatable. Reconciliation reads pane metadata and either finishes the intended
placement or rolls back; it never blindly repeats `swap-pane`.

### Handoff and ControlTurn

```text
Handoff
  handoff_id
  transition_id
  source_frame_id
  source_session_key
  target_frame_id
  summary
  next_action
  content_hash
  created_at
  first_claimed_at        optional

ControlTurn
  control_turn_id
  transition_id
  target_frame_id
  target_session_key
  kind                    claim_brief | claim_handoff
  template_version        control.claim.v1
  transport               live_input | resume_initial
  state                   prepared | submitted | observed | claimed | settled |
                          uncertain | failed | superseded
  submission_count
  submitted_at            optional
  observed_prompt_id      optional
  claimed_at              optional
  settled_at              optional
  failure                 optional bounded record
```

Handoff content is immutable and unique per completion transition. Claim is
idempotent for the exact target session and never deletes the handoff.

The only `control.claim.v1` terminal text is:

```text
Call transition_claim() and follow the returned transition instructions.
```

No brief, handoff, prompt, frame title, path, command, or token is interpolated.
The template text is visible provider history but only its version is persisted
by Switchboard.

One control turn permits one submission. `submission_count` has a maximum of
one. A crash or timeout after terminal input becomes `uncertain`; it may be
claimed or resolved from later exact hook evidence, but it is never submitted
again automatically.

For live input, the target must be an exact managed parked pane, input-disabled,
ready after a trusted foreground `Stop`, free of permission state, and covered
by the executing transition. A single tmux command queue moves/selects it,
enables input, sends the literal template plus Enter, and disables input again.
The exact `UserPromptSubmit` observation or a bounded watchdog re-enables input.
Timeout exposes a pending handoff/brief and marks the turn uncertain.

For `resume_initial`, the same template is the provider CLI's initial prompt on
the exact provider UUID. Attach gating still requires the authorized target pane
in `view.main` before provider exec.

`transition_claim()` revalidates capability, provider UUID, pane, frame,
transition, placement, and WorkContext generation. A child receives its brief
only after foreground lease transfer. A parent claim stamps the handoff and
moves the child from `closing` to `closed/completed`; parent synthesis may then
settle or retry without keeping the child runtime live.

### Recovery and DesktopAttachmentLease

```text
Recovery
  recovery_id
  host_id
  kind
  subject_type
  subject_id
  actionability           safe_auto | open_view | manual
  state                   open | resolved | dismissed
  bounded_explanation
  created_at
  updated_at

DesktopAttachmentLease
  view_id
  request_id
  state                   offered | claimed | expired
  expires_at
```

Recoveries caused by external side-effect uncertainty are durable and have
stable IDs. Pure projection warnings may remain derived. DMS may execute only
`safe_auto`; `open_view` focuses the core recovery panel, and `manual` is
informational until the user enters core. Checkout/background ambiguity is
never a DMS mutation.

One unexpired desktop attachment lease exists per view. Presentation fallback
with the same semantic request may claim it. A concurrent different request
receives `desktop_launch_in_progress`; ambiguous matching desktop windows block
rather than grant another lease.

## PresentationDirective v1

`PresentationDirective v1` replaces the proposed `ViewAction v1` result:

```text
directiveVersion = 1
requestId
hostId
kind                  focus | attach | blocked
viewId                required for focus/attach
viewRevision          required for focus/attach
desktopToken          required for focus/attach
leaseExpiresAt        required only for attach
error                 required only for blocked
```

The core command has already committed or revalidated the semantic view action
before returning this desktop directive. `switch` is not a DMS directive.
Mutation requests carry expected revisions where the caller addresses an exact
view/frame; the result does not call a precondition “expected.” High-level
project entry resolves a live route and applies its own revision CAS.

The DMS focus-miss retry reuses the request ID with desktop focus disabled.
Because presentation capability is excluded from the semantic fingerprint,
this retry may receive the one attach lease without repeating project/frame
navigation.

## Config v3 Defaults

```toml
[automation]
task_push = "conservative"
complete_return = "synthesize"
initial_max_depth = 1

[control_turns]
transport = "live_first"
```

`complete_return` is `synthesize | handoff` and may be overridden per project.
`handoff` returns to the parent without a control turn; the navigator surfaces
the pending handoff and the next normal parent turn may claim it.

`control_turns.transport` is host-wide `live_first | resume_only`.
`live_first` uses the fenced live path only when every prerequisite is proven,
otherwise exact UUID resume. It never weakens to uncertain live input.

## Host-Global Concurrency Matrix

Storage constraints and CAS enforce:

- one workspace frame per host/project;
- one frame membership per provider session;
- one held WorkContext per checkout;
- one foreground frame per held WorkContext;
- one live surface and one pending launch per provider session;
- one nonterminal transition per view;
- one active placement per view and one owning placement per open frame;
- one completion handoff and control turn per transition;
- one active desktop attachment lease per view; and
- one normalized semantic fingerprint per host/request UUID.

Per-view revision is never treated as sufficient protection for a provider
session, checkout, workspace route, or desktop launch owned by another row.

## Workflow Settlement Authority

- Push is successful when the child claims its brief after foreground transfer.
- Back is successful when the exact parent is presented; it has no handoff or
  control turn.
- Complete-and-return marks the child `closing` when the handoff is stored,
  closes it after the parent claim, and settles presentation on the parent's
  exact post-claim `Stop`.
- Human close presents the parent first, then stops and closes the parked child.
- A missing parent keeps the child usable and the handoff unclaimed.
- A missing hook leaves durable prepared/uncertain state and never authorizes a
  duplicate control turn.
- Client detach does not cancel a submitted agent turn; settlement follows
  provider/transition evidence, not terminal-client lifetime.
