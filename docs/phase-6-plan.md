# Phase 6 Clean-Break View-First Plan

Date: 2026-07-22

Status: Phase 6A.1 through 6E.1 complete; Phase 6F is next

Target core release: `0.3.0`

Target DMS adapter release: `0.5.0`

## Outcome

Phase 6 replaced the `0.2` task-first product with durable host-local views,
unified workspace/task frames, a resident optional navigator, direct mode, and
agent-driven foreground transitions. Core and DMS completed their one-time
coordinated activation. No old public contract remains active.

The architecture is in [the design](design.md), exact user behavior is in
[View and Frame Workflow](view-workflow.md), and canonical ownership, state,
control-turn, and presentation rules are in
[Phase 6 State Contract](state-contract.md).

## Cutover Policy

The completed `0.2` to `0.3` activation was a clean break with bounded offline
salvage, not an in-place upgrade:

- new runtime accepts only Config v3 and the fresh registry baseline;
- old registry/config readers exist only in the repo-owned offline exporter;
- the importer consumes only `CutoverBundle v1`;
- no command alias, dual protocol, frontend compatibility mode, or old cache
  reader ships;
- imported state remains staged and mutation-inert until an explicit cutover
  commit;
- one selected legacy provider UUID was resumed into a fresh view during the
  completed activation; and
- old task rows remain in the immutable backup/bundle for audit only.

This policy describes historical data conversion, not future operations. The
coordinator is retired. Development resets discard Switchboard-owned state and
never quiesce existing providers; see
[Runtime Operations and Safety](operations.md).

## Replacement Contracts

### HostState v1

The owner-host envelope is the source for SSH federation and deterministic
state. Its bounded, host-qualified records cover catalog, WorkContexts, frames,
frame sessions, provider sessions, surfaces, views, placements, transitions,
control turns, recoveries, warnings, and truncation. References are validated
and arrays are deterministically ordered. It contains no prompt, transcript,
provider argv, credential, capability secret, or unrestricted path.

### NavigatorState v1

The frontend envelope aggregates individually validated HostState records. It
projects only view summaries, project entry routes, frame summaries, recovery,
reachability/staleness, warnings, and truncation. It does not expose raw
provider, surface, tmux, checkout-path, or authority material.

### PresentationDirective v1

Core commits or revalidates semantic navigation before returning desktop work:

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

`focus` targets the one canonical DMS desktop client. `attach` grants one
expiring lease after focus miss. `blocked` is bounded and actionable. DMS never
receives a tmux target, provider command, semantic `switch`, or authority to
repeat navigation.

### Canonical mutable state

The fresh registry owns Host, Project, Repository, Checkout, ProviderSession,
immutable historical SessionHandoff, Frame, FrameSession, WorkContext, UserView,
FramePlacement, TransitionBrief, CompletionHandoff, Transition, ControlTurn,
Recovery, Surface, launch intent, capability digest, request idempotency, and
owner-host cache records.

The exact state machines and invariants live in `docs/state-contract.md`. That
document is normative when a delivery slice or frontend plan is less precise.
The baseline contains no task table, task foreign key, Snapshot/Fleet cache, or
task-first frontend state.

Normal open fails closed when the active pointer is missing/torn, the config and
database generation IDs differ, or the old fixed schema-v10 database is found.
It never mutates an old file automatically.

## CutoverBundle v1

The repo-owned offline `0.2` exporter reads exactly schema v10 and Config v2,
requires quiescence, and emits one canonical bounded bundle. It preserves:

- host display identity and aligned provider/remote configuration;
- projects, repositories, memberships, and declared checkouts;
- provider session keys/UUIDs, catalog association, curated metadata,
  resumability, timestamps, last-known status, and exact provider name;
- immutable handoffs with exact session linkage; and
- historical task rows for audit only.

It omits leases, capabilities, PIDs/birth evidence, tmux locators, surfaces,
remote and DMS caches, task membership, and task imported-handoff relationships.

Import verifies framing, versions, hashes, bounds, references, and source
quiescence; creates a complete inactive generation; imports evidence without
manufacturing frames; and retains read-only backups plus the exact bundle.
Workspace frames remain lazy. An old session stays project history until the
user explicitly resumes it into a frame.

## Generation-Safe Activation

Config v3 and the registry are installed under the same opaque generation ID:

```text
$XDG_CONFIG_HOME/agent-switchboard/generations/<id>/config.toml
$XDG_STATE_HOME/agent-switchboard/generations/<id>/switchboard.db
$XDG_STATE_HOME/agent-switchboard/current -> generations/<id>
```

The config contains the generation ID and the database records it in the
baseline metadata. Import creates both generation directories and their files,
fsyncs file contents and parent directories, validates through their final
paths, then atomically replaces the state-home `current` pointer. The pointer is
the only activation coordinate; independently replacing config and database is
forbidden.

Because config and state homes may be different filesystems, `current` resolves
an ID, not a cross-filesystem file move. Startup resolves the pointer once,
opens the matching config and database, verifies both embedded IDs, and fails
closed if any component is absent or changed during open.

An imported generation starts in `cutover_staged`. It permits only read-only
state, `doctor`, provider/remote discovery, and cutover commands. View/frame
mutation, hooks, provider launch/resume, agent tools, and DMS actions return the
stable `cutover_staged` diagnostic. This lets local, DMS, and remote reads be
validated before authority changes.

The public lifecycle is:

```text
swbctl cutover export
swbctl cutover import
swbctl cutover status
swbctl cutover commit
swbctl cutover rollback
```

`commit` is the explicit irreversible boundary: it verifies paired core/DMS
versions and cold-start evidence, marks the active generation committed, and
allows hooks and mutation. `rollback` is automatic only while staged and
restores the prior pointer/packages/settings from the retained manifest. After
commit, recovery is forward-only or an explicit operator-led restore; new
generation writes are never imported into `0.2`.

## Config v3 Defaults

```toml
config_version = 3
generation_id = "<opaque-id>"

[views]
cli_default_mode = "direct"
desktop_default_mode = "navigator"

[automation]
task_push = "conservative"
complete_return = "synthesize"
initial_max_depth = 1

[control_turns]
transport = "live_first"
```

Projects may set `task_push = "off"` and may override `complete_return` with
`handoff`. `live_first` submits the one fixed visible control prompt only to an
exact verified idle parent through an explicit writable client; otherwise it
uses exact UUID resume with the prompt as initial input. An uncertain live
submission is never retried automatically. Memory integration stays optional,
disabled by default, and has no routing or transition authority.

## Delivery Slices

### Phase 6A: clean-break direction

- Define Frame, WorkContext, UserView, placement, and transition ownership.
- Prove persistent navigator/direct composition and pane survival on an
  isolated tmux server.
- Commit the clean-break product, command, and deletion boundaries.

Exit: original design and feasibility evidence are retained in core and DMS.

### Phase 6A.1: normative repair and hardened evidence

- Make the state contract canonical for semantic, physical, control-turn,
  checkout, recovery, and presentation ownership.
- Permit controlled model turns only at semantic boundaries; keep arbitrary
  prompt injection forbidden and the presentation hot path model-free.
- Replace `ViewAction` with `PresentationDirective`, define Projects navigation
  versus Views focus, and make parent claim the child-close boundary.
- Prove the main-only view, separate holding session, exact authorization gate,
  explicit writable executor, unequal-client policy, transition ordering,
  detach survival, and tmux server-generation fencing with zero model turns.
- Define one generation pointer, staged validation, and explicit commit.

Exit: docs package and the retained 33-check tmux probe pass without provider or
model turns. This slice is complete.

### Phase 6B.1: domain, storage, and protocol baseline

- Implement Config v3 and the fresh registry in private replacement modules.
- Implement all canonical entities, constraints, CAS rules, state machines, and
  deterministic HostState/NavigatorState/PresentationDirective serializers.
- Add strict fixtures for bounds, ordering, unknown fields, authority
  references, idempotency, and corruption.
- Keep installed `0.2` entrypoints unchanged; tests use private harnesses only.

Exit: fresh state, migrations from only the new baseline, state-machine failure
matrices, and reproducible protocol fixtures pass without live user state.

### Phase 6B.2: exporter, importer, and generation activation

Status: complete in the private replacement. Exact fields, activation ordering,
and failure behavior are recorded in
[CutoverBundle v1 and Activation](cutover-bundle-v1.md).

- Implement the exact v10/v2 exporter and bounded CutoverBundle importer.
- Implement paired generation construction, fsync, pointer switch, mismatch
  rejection, staged restrictions, status, commit, and pre-commit rollback.
- Rehearse against sanitized copies and prove source immutability plus crash
  recovery at every activation boundary.

Exit: deterministic bundle round trip, corrupt/torn generation tests, staged
read-only startup, rollback, and irreversible commit behavior pass.

### Phase 6C: view shell and replacement navigator

Status: complete in the private replacement. Evidence is recorded in
[Phase 6C Acceptance](phase-6c-acceptance.md).

- Implement the main-only view session, separate holding session, dead
  placeholders, pane metadata, server-generation fencing, and repair.
- Implement view create/open/focus/attach/mode/retire/recover with revisions and
  desktop leases.
- Build the compact resident navigator and focused project, settings, history,
  and recovery panels.
- Prove real no-model Codex/Claude panes survive movement, client resize, mode
  changes, and detach. Prove server-generation loss invalidates every locator,
  revokes capability evidence, recreates a degraded placeholder shell, and
  exposes an exact provider-resume recovery target.

Exit: direct and navigator workflows pass from plain CLI, tmux, and an isolated
desktop context without old public commands.

Exact UUID resume/bind after server loss stays in Phase 6D because it uses the
provider launch/resume authority introduced by that slice; 6C must not invent
that authority merely to make a shell repair look complete.

### Phase 6D: workspace and one-child automation

Status: complete in the private replacement. Evidence is recorded in
[Phase 6D Acceptance](phase-6d-acceptance.md).

- Create one lazy workspace frame per host/project/default checkout.
- Add exact frame-scoped agent capabilities and replacement MCP tools.
- Implement conservative push, Back, Complete-and-return, Human close, Cancel,
  claim, supersession, and one-level WorkContext claim transfer.
- Implement fixed control-turn live-first/resume fallback, trusted post-turn
  transport, exact lifecycle settlement, latency bounds, and uncertain recovery.
- Guard provider bootstrap/fork/resume and Codex prestart naming by accepted
  versions; Claude names remain curated until separately proven.

Exit: deterministic failures and guarded installed Codex/Claude acceptance prove
one workspace-child-parent flow, semantic parent synthesis, exact child close,
and no duplicate prompt or runtime.

### Phase 6E: coordinated activation — complete

- Core `0.3.0` and DMS `0.5.0` committed on both hosts with exact artifact,
  staged-read, DMS cold/warm cache, and remote online/offline evidence.
- The selected Codex UUID resumed and the old installed contracts disappeared.
- Post-activation acceptance found and fixed unmanaged global-hook failure and
  missing reopen-to-view projection.
- The one-shot coordinator and executable runbook were retired from active
  source-distribution surfaces. Git history and the private activation
  workspace retain the exact evidence.

Exit: installed core and DMS expose no old command/protocol/cache route;
local/two-host acceptance passed; operational ownership now follows
[Runtime Operations and Safety](operations.md).

### Phase 6E.1: operational closure — complete

- Add normal `init` from a Config v3 template, with no provider, hook, DMS, or
  tmux I/O.
- Add compare-and-swap `reset` that publishes a new empty committed generation,
  retains the previous generation, and never changes provider or tmux runtime.
- Keep global hook installation explicitly opt-in and prove unmanaged events
  remain successful no-ops.
- Prove a fresh generation can create and attach a persistent view from the
  SSH-first CLI path, then survive a state reset unchanged.
- Package and smoke the fresh-init/reset surface independently of historical
  cutover machinery.

Exit: empty roots become a usable committed generation without CutoverBundle;
reset abandons Switchboard state without stopping user work; exact acceptance is
recorded in [Phase 6E.1 Acceptance](phase-6e1-acceptance.md).

### Phase 6F: recursive task frames

- Lift the initial one-child limit to a bounded recursive stack.
- Generalize WorkContext claim and foreground transfer across ancestors.
- Add task-to-task push/return plus depth, loop, and recursive recovery guards.
- Keep cross-host push/return out of scope; remote frames remain separate views.

Exit: task A pushes B, B returns exactly to A, and A returns exactly to the
workspace while preserving checkout state, handoffs, and provider UUIDs.

## Deletion Matrix at Activation

Delete rather than deprecate:

- Snapshot v2, Fleet v1, PresentationPlan v2, SessionAction v2,
  TaskCloseAction v2, and the abandoned `ViewAction` proposal;
- old snapshot/fleet/prepare/select/attach/task-first CLI parsers and gateways;
- old agent MCP schemas and current-task-only authorization assumptions;
- Open/Inbox/Closed TUI models, screens, forms, and compatibility tests;
- schema migrations v1-v10 and their production registration;
- Config v2 normal parser and `migrate-v2` command;
- DMS bridge/action v4, model v5, task/project/history/stop desktop actions,
  plugin-reload activation, and old warm-cache keys; and
- active packaging references to `0.2` phase documents.

Retain only in history, the non-packaged archive, or cutover fixtures: Phase
0-5 records, rejected provider/supervisor evidence, old fixtures required by the
offline exporter, and sanitized rehearsal bundles.

## Post-activation operations

There is no reusable live-cutover runbook. Switchboard configuration, registry,
cache, generated view, and DMS state remain disposable during development.
Existing provider sessions and unrelated tmux state remain outside every reset,
upgrade, rollback, and cleanup boundary.

Broken global hooks are contained by removing only Switchboard-owned handlers.
New builds are proven with temporary roots, isolated tmux servers, and new test
provider sessions. Hosts update independently. A running managed provider may
remain pinned to its immutable release; no update waits for or stops it.

From SSH, `swbctl view attach --view VIEW` is the first-class attachment path.
It revalidates and attaches the existing view without DMS and without starting
or resuming another provider process.

## Acceptance Matrix

- Contract: strict v1 round trips, bounds, Unicode, deterministic ordering,
  reference authority, idempotency, and unknown/unsafe fields.
- Registry: fresh install, unique ownership, WorkContext generations, control
  states, concurrent revisions, placement repair, and completion claim ordering.
- Generation: source immutability, crash points, fsync/pointer durability,
  config/DB mismatch rejection, staged mutation blocking, rollback, and commit.
- tmux: main-only view, separate holding, exact authorization, explicit writable
  client, unequal clients, swap inspection/commit order, detach survival,
  provider-pane survival, and server generation loss/recovery.
- Workflow: workspace entry, push, claim, Back, Complete-return synthesis,
  Human close, Cancel, supersession, park safety, one-child depth, and uncertain
  live submission without retry.
- Frontends: compact navigator, direct recovery, DMS model v1, Projects
  navigation, Views focus, cold/warm cache provenance, one desktop client,
  Ghostty attach lease, niri focus, and remote view presentation.
- Clean break: removed commands are unknown, old DB/config fail with one
  cutover-required diagnostic, old DMS cache is ignored, and built artifacts
  contain no old active protocol/frontend modules.
- Packaging: full tests, lint, `git diff --check`, two byte-identical builds,
  content audit, clean-wheel install, and installed paired smoke tests.
