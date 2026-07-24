# Phase 6 State and Control-Turn Contract

Date: 2026-07-21

Status: Phase 6B.1 implementation contract locked, implemented, and validated

Target release: `0.3.4`

This is the implemented Phase 6 technical contract, not an adopted product
contract or a backwards-compatibility boundary. The
[thread/workstream roadmap](thread-workstream-redesign-roadmap.md) may replace
its entities, schema, and commands rather than adapt them.

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

### RegistryMetadata and RequestRecord

```text
RegistryMetadata
  singleton               exactly 1
  schema_version          1
  protocol_version        1
  generation_id
  local_host_id
  activation_state        cutover_staged | committed
  created_at
  committed_at            optional

RequestRecord
  host_id
  request_id
  operation
  semantic_fingerprint
  state                   prepared | completed | failed
  result_type             optional
  result_id               optional
  created_at
  completed_at            optional
```

Config, database metadata, and the caller's expected generation and local host
must all agree before normal open. Normal open never creates state. `init` and
confirmed `reset` are the only non-cutover paths that may create an empty
committed database and publish the generation pointer. An old schema-v10,
partially initialized, unknown, or mismatched database fails closed.

`(host_id, request_id)` is unique. Presentation capability is excluded from the
semantic fingerprint. The stored result identifies the committed semantic
outcome, never a serialized desktop directive, so focus fallback can reuse the
request without repeating navigation.

### ProviderSession and historical SessionHandoff

```text
ProviderSession
  session_key             host_id:provider:provider_session_id
  host_id
  provider                codex | claude
  provider_session_id
  project_id              optional
  checkout_id             optional
  name                    optional curated value
  purpose                 optional curated value
  pinned
  runtime_presence        live | stopped | unknown
  resumability            resumable | missing | unknown
  activity                working | needs_input | ready | completed | unknown
  activity_reason         permission | question | elicitation | turn_complete |
                          provider_complete | error | unknown
  created_at              optional
  provider_updated_at     optional
  last_observed_at
  updated_at

SessionHandoff
  handoff_id
  session_key
  sequence
  summary
  next_action
  source                  user | agent | imported
  source_host_id
  content_hash
  created_at
```

ProviderSession is materialized state, not an event log. Phase 6D may add
privacy-safe lifecycle evidence after its hook contract is accepted. A
SessionHandoff is immutable provider history and permits the Phase 6B.2 cutover
to preserve old session handoffs without inventing frames or transitions. It is
not claimable transition authority.

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

The first session in an empty workspace is a single atomic start bundle:
provider session, ordinal-one `started` membership, frame current-session
pointer, planned launch and surface, `active -> staged` placement, and
`released -> held` WorkContext claim. The held foreground is the workspace
frame. Before provider execution, exact rollback removes the bundle and restores
an empty active placement plus released context. After execution is attempted,
the bundle is recovery-owned and is never deleted or retried by inference.

### WorkContext

```text
WorkContext
  work_context_id
  host_id
  project_id
  checkout_id
  claim_state             released | held | blocked
  claim_generation
  foreground_frame_id     optional
  background_state        safe | known | uncertain
  acquired_at             optional
  released_at             optional
  updated_at
```

The WorkContext is durable; its checkout claim is not permanent. Its project is
stored explicitly so the context may be created before its workspace frame and
every member frame can be checked against it. One partial
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

### Surface, launch, capability, and tmux server evidence

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

LaunchIntent
  launch_id
  request_id
  host_id
  frame_id
  provider                codex | claude
  action                  new | resume
  target_session_key      required only for resume
  state                   planned | authorized | started | bound | failed |
                          superseded
  failure                 optional bounded record
  created_at
  updated_at

AgentCapability
  capability_id
  capability_digest       sha256 of raw capability
  host_id
  view_id
  frame_id
  session_key             optional until binding
  surface_id
  launch_id
  tmux_server_id          optional until physical creation
  pane_id                 optional until physical creation
  placement_generation
  issued_at
  expires_at
  revoked_at              optional
```

`planned` is durable intent, not a claim that a tmux pane exists. Physical
creation advances the surface only after exact pane metadata and server
generation are re-read. Socket path, server PID, and tmux `start_time` must all
agree. A server restart invalidates every old locator even if socket and pane
names are reused.

Only an exact launch-owned pane/window may be killed. Killing an owning view or
tmux server is never a surface cleanup primitive.

The raw capability exists only in the launched surface environment. SQLite
retains its digest and exact authority bindings. The environment also carries
the exact generation ID. Global hooks compare that marker to `state/current`
before reading an event; stale or discarded generations are successful no-ops.
No launch becomes `started` until durable authorization and exact physical
placement agree; `bound` requires the one provider session and surface to agree.

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

### TransitionBrief, CompletionHandoff, and ControlTurn

```text
TransitionBrief
  brief_id
  transition_id
  source_frame_id
  source_session_key
  target_frame_id
  brief
  content_hash
  created_at
  first_claimed_at        optional

CompletionHandoff
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

TransitionBrief content is immutable and unique per push transition.
CompletionHandoff content is immutable and unique per completion transition.
Both claims are idempotent for the exact target session and never delete the
semantic record. SessionHandoff history is separate and cannot satisfy a
transition claim.

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
by the executing transition. A single tmux command queue creates one uniquely
named ephemeral buffer, enables input, bracket-pastes the literal template,
deletes that buffer, sends one Enter, and disables input again. The exact
`UserPromptSubmit` observation or a bounded watchdog re-enables input. Timeout
exposes a pending handoff/brief and marks the turn uncertain.

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
  lease_id
  view_id
  request_id
  state                   offered | claimed | expired
  expires_at

HostStateCache
  remote_name
  host_id
  state_json              canonical validated HostState v1
  content_hash
  observed_at
  received_at
  last_attempt_at
  reachability            online | offline | unknown
  bounded_error           optional
```

Recoveries caused by external side-effect uncertainty are durable and have
stable IDs. Pure projection warnings may remain derived. An optional frontend
adapter may execute only `safe_auto`; `open_view` focuses the core recovery
panel, and `manual` is informational until the user enters core.
Checkout/background ambiguity is never an adapter mutation.
When a timed-out control is later claimed and its transition settles, the exact
`control_submit_uncertain` recovery resolves in the same transaction.
Reconciliation resolves only equivalent older records whose control and
transition are already both settled.

One unexpired desktop attachment lease exists per view. Presentation fallback
with the same semantic request may claim it. A concurrent different request
receives `desktop_launch_in_progress`; ambiguous matching desktop windows block
rather than grant another lease.

Cached owner-host state is projection evidence only. It cannot satisfy a local
foreign key, revision, claim, capability, request, transition, or mutation
precondition.

## HostState v1 and NavigatorState v1

`HostState v1` contains:

```text
schemaVersion = 1
protocolVersion = 1
hostStateVersion = 1
generationId
activationState
generatedAt
host
projects
repositories
projectRepositories
checkouts
workContexts
frames
frameSessions
sessions
surfaces
views
placements
transitions
controlTurns
recoveries
warnings
truncation
```

It contains bounded structural summaries, not raw database rows. It excludes
tmux socket/pane/process locators, capability material, provider argv, paths,
brief and handoff bodies, prompts, transcripts, and credentials.

`NavigatorState v1` contains `schemaVersion`, `protocolVersion`,
`navigatorVersion`, `generationId`, `generatedAt`, `localHostId`, `hosts`,
`views`, `projects`, `recoveries`, `warnings`, and `truncation`. It combines one
live local HostState with individually validated cached remote states. Structural arrays
are ordered by host ID then stable entity ID. Cached/remote state never grants
mutation authority. Every host row carries its owner generation ID and an
explicit `stale` boolean in addition to reachability. The top-level generation
is always the local generation and is the only valid frontend-cache provenance.

View rows are presentation-ready summaries authored by core. In addition to
identity, mode, state, revision, and active frame/project IDs, they carry title,
breadcrumb, activity, attention, transition/control state, and last activity.
Frontends do not re-derive any of these fields and NavigatorState never
contains a desktop token.

Each project frame summary carries frame/title/role/parent/lifecycle/activity,
an optional bounded current-session summary (`provider`, `runtimePresence`,
`resumability`, `activity`, `updatedAt`), and `sessionCount`. Closed frames are
retained for read-only History, but project entry and active navigation resolve
only open frames.

Both envelopes retain the existing safety ceilings: 8 MiB encoded JSON, depth
32, 64 KiB per string, 100,000 array items, and 256 object keys. Bounded
non-sensitive future fields are accepted and omitted during canonical
reserialization. Sensitive/raw/prompt/transcript/token/argv fields, terminal
controls, nonfinite numbers, invalid references, and unsafe bounds are rejected.

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
before returning this desktop directive. `switch` is not a frontend directive.
Mutation requests carry expected revisions where the caller addresses an exact
view/frame; the result does not call a precondition “expected.” High-level
project entry resolves a live route and applies its own revision CAS.

An optional desktop adapter's focus-miss retry reuses the request ID with
desktop focus disabled. Because presentation capability is excluded from the
semantic fingerprint, this retry may receive the one attach lease without
repeating project/frame navigation.

## Config v3 Defaults

```toml
config_version = 3
generation_id = "<non-nil UUID>"

[host]
host_id = "<non-nil UUID>"
display_name = "<bounded name>"

[views]
cli_default_mode = "navigator"
desktop_default_mode = "navigator"

[automation]
task_push = "conservative"
complete_return = "synthesize"
initial_max_depth = 1

[control_turns]
transport = "live_first"
watchdog_timeout_seconds = 5
```

The canonical stored Config v3 always contains `generation_id`. An `init` or
reset replacement template may omit it because publication binds the template
to the newly allocated generation before storage and validation.

`complete_return` is `synthesize | handoff` and may be overridden per project.
`handoff` returns to the parent without a control turn; the navigator surfaces
the pending handoff and the next normal parent turn may claim it.

`control_turns.transport` is host-wide `live_first | resume_only`.
`live_first` uses the fenced live path only when every prerequisite is proven,
otherwise exact UUID resume. `resume_only` never attempts live input. Live
submission requires a verified idle owned pane; stop/resume requires a verified
idle owned parent. Active or ambiguous ownership blocks instead of guessing.
Submitted turns are generation-bound and receive one short bounded settlement
watchdog; reconciliation marks overdue turns uncertain and never resubmits.

Provider version output is strict input and retained as telemetry, not an
allowlist. Missing executables, malformed version output, unsupported command
shape, identity mismatch, or observed behavior mismatch fail closed. A newer
Codex or Claude version holding the same tested contract is accepted.

Config v3 also carries providers, remotes, projects, repositories, checkouts,
tmux, hooks, and optional memory configuration. It has no working-directory or
recent-row task-first policy. Memory remains disabled by default and confers no
skill, routing, claim, or transition authority.

## Remaining Legal State Edges

Storage exposes named workflow operations, not arbitrary row updates. In
addition to the Frame, ViewTransition, and ControlTurn edges above, it enforces:

```text
WorkContext
  released -> held | blocked
  held -> released | blocked
  blocked -> released | held       explicit human resolution only

UserView
  ready -> transitioning | degraded | retired
  transitioning -> ready | degraded
  degraded -> ready | retired

FramePlacement
  staged -> active | orphaned
  active -> parked | stopped_affinity | orphaned
  parked -> active | stopped_affinity | orphaned
  stopped_affinity -> staged | orphaned

Surface
  planned -> live | orphaned | retired
  live -> dead | orphaned
  dead -> retired
  orphaned -> live | dead | retired

LaunchIntent
  planned -> authorized | failed | superseded
  authorized -> started | failed | superseded
  started -> bound | failed

ControlTurn
  prepared -> submitted | failed | superseded
  submitted -> observed | uncertain
  observed -> claimed | uncertain | failed
  uncertain -> observed | claimed | failed
  claimed -> settled | failed

Recovery
  open -> resolved | dismissed

DesktopAttachmentLease
  offered -> claimed | expired

RequestRecord
  prepared -> completed | failed
```

Terminal rows are retained for audit and never reopened. A fresh identity is
used for another launch, surface, transition, control turn, recovery, or lease.
Every WorkContext claim edge increments `claim_generation`; every placement
edge increments `generation`; every committed view mutation increments
`revision`.

Transport phase advances `intent -> moved -> inspected -> committed`. A
model-free focus/mode operation may use `intent -> inspected -> committed`.
Only `moved` or `inspected` may advance to terminal `rolled_back`; committed or
rolled-back work is never executed again.

## Host-Global Concurrency Matrix

Storage constraints and CAS enforce:

- one workspace frame per host/project;
- one frame membership per provider session;
- one held WorkContext per checkout;
- one foreground frame per held WorkContext;
- one live surface and one pending launch per provider session;
- one nonterminal transition per view;
- one active placement per view and one owning placement per open frame;
- one transition brief per push, and one completion handoff and control turn per
  completion transition;
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
