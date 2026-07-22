# Phase 6 Clean-Break View-First Plan

Date: 2026-07-21

Status: design and tmux feasibility complete; production implementation pending

Target core release: `0.3.0`

Target DMS adapter release: `0.5.0`

## Outcome

Phase 6 replaces the `0.2` task-first product with durable host-local views,
unified workspace/task frames, a resident optional navigator, direct mode, and
agent-driven foreground transitions. The activation is coordinated across core
and DMS. No old public contract remains active after cutover.

The architecture is in [the design](design.md), and exact user behavior is in
[View and Frame Workflow](view-workflow.md).

## Cutover Policy

This is a clean break with a bounded offline data salvage, not an in-place
upgrade:

- new runtime accepts only Config v3 and the new registry baseline;
- old registry/config readers exist only in the repo-owned offline exporter;
- the importer consumes only `CutoverBundle v1`;
- no command alias, dual protocol, frontend compatibility mode, or old cache
  reader ships;
- old provider runtimes are quiesced and resumed by exact UUID into fresh
  views; and
- old task records remain in the immutable backup/bundle but are not imported
  into the active model.

Development may build new internal modules before activation, but no release or
installed plugin exposes two product generations at once.

## Replacement Contracts

### HostState v1

Owner-host envelope for SSH federation and deterministic local state. Required
top-level sections are:

```text
schemaVersion = 1
protocolVersion = 1
generatedAt
host
projects
repositories
checkouts
workContexts
frames
frameSessions
sessions
surfaces
views
transitions
recoveries
warnings
truncation
```

IDs are host-qualified where ownership is not globally configured. References
are validated, arrays are deterministically ordered, and byte/count/depth/text
bounds apply before serialization.

### NavigatorState v1

Aggregated frontend envelope with individually validated host records. It
contains view summaries, project entry routes, frame summaries, recovery rows,
host reachability/staleness, warnings, and truncation. It does not expose full
provider/session/surface detail or raw checkout paths.

### ViewAction v1

Idempotent action result:

```text
actionVersion = 1
hostId
viewId
kind                  focus | switch | attach | blocked
desktopToken
expectedViewRevision  required for cursor mutation
requestId
error                 optional bounded record
```

Frontends receive no tmux session/window/pane target. Core performs or routes
the exact transport mutation.

## New Registry Baseline

The new database starts from one baseline migration and includes:

- host and materialized project/repository/checkout records;
- provider sessions, immutable handoffs, observations, and events;
- frames and ordered frame-session membership;
- work contexts and unique checkout claims;
- durable views, surface placements, and transitions;
- launch intents and exact agent capability digests; and
- new owner-host cache records for `HostState v1`.

It contains no `tasks`, `task_imported_handoffs`, task foreign keys,
Snapshot/Fleet remote cache, or task-first frontend state.

Normal open fails closed when the default `switchboard.db` is an old schema-v10
database. The error instructs the operator to run the cutover plan; it never
mutates that file automatically.

## CutoverBundle v1

The repo-owned offline `0.2` exporter reads exactly schema v10 and Config v2,
requires the old database to be quiescent, and emits one canonical bounded
bundle. It preserves:

- host ID/display metadata;
- provider/remote configuration;
- projects, repositories, memberships, and declared checkouts;
- provider session keys/UUIDs, project/checkout association, curated
  name/purpose/pin, provider name, timestamps, resumability, and last-known
  status;
- immutable handoffs and exact session linkage; and
- a historical copy of task rows for audit only.

It deliberately omits active launch leases, capabilities, runtime PIDs/birth
evidence, tmux locators, surfaces, remote caches, DMS caches, task membership,
and task imported-handoff relationships.

The importer:

1. verifies bundle framing, versions, hashes, bounds, identity references, and
   source quiescence proof;
2. creates a new destination database and Config v3 beside the old files;
3. imports catalog/session/handoff evidence without manufacturing frames;
4. atomically installs the new files only after full validation;
5. retains timestamped read-only backups and the exact bundle; and
6. instructs reconciliation to refresh providers/remotes before any new launch.

Workspace frames are created lazily on first project entry. Old sessions remain
project history until a user resumes one into a frame. No old task becomes a
new task automatically.

## Config v3

The offline converter carries forward aligned v2 definitions and writes only
the v3 shape. New settings include:

```toml
config_version = 3

[views]
cli_default_mode = "direct"
desktop_default_mode = "navigator"

[automation]
task_push = "conservative"
initial_max_depth = 1
```

Project-level automation may override `task_push` with `conservative` or `off`.
Explicit user push remains available when implicit automation is off. Existing
host/provider/remote/project/repository/checkout/tmux/hook settings are carried
forward when valid. Task-first recent-row and working-directory policies are
removed. Optional memory integration remains explicit and disabled by default;
it contributes no skills or transition authority.

## Delivery Slices

### Phase 6A: design and transport evidence

- Finalize the Frame/View/WorkContext/Transition design.
- Prove dead anchor, fixed sidebar, pane swapping, direct-mode removal,
  navigator recreation, rollback, geometry, metadata, and shared cursor on an
  isolated tmux server.
- Define clean-break contracts, cutover policy, and acceptance gates.

Exit gate: this documentation batch is committed in core and DMS; the retained
spike passes with no provider or model turn.

### Phase 6B: private replacement baseline

- Implement Config v3 and the fresh registry schema in new modules.
- Implement Frame, FrameSession, WorkContext, UserView, placement, transition,
  and recovery storage with migrations only from the new baseline.
- Implement HostState v1, NavigatorState v1, ViewAction v1, and strict fixtures.
- Implement the offline v0.2 exporter and CutoverBundle v1 importer.
- Keep the installed/public `0.2` command surface unchanged during this slice;
  exercise new code only through tests/private harnesses.

Exit gate: new baseline, state contracts, bundle round trip, corruption tests,
and deterministic/reproducible artifacts pass without reading live user state.

### Phase 6C: view shell and replacement navigator

- Implement the tmux anchor/main/parked/staged topology and pane placement
  reconciliation.
- Implement create/open/focus/attach/mode/retire/recover actions with per-view
  revision/lease enforcement.
- Build the compact resident Textual navigator and focused
  project/settings/history/recovery panels.
- Prove multiple independent views and shared cursor behavior.
- Prove real no-model Codex and Claude TUI panes survive swap, resize,
  direct/navigator toggle, detach, and reattach.

Exit gate: local direct and navigator workflows pass from CLI, plain terminal,
tmux, and a test desktop context without exposing old public commands.

### Phase 6D: workspace and one-child automation

- Create one lazy workspace frame per project/host/default checkout.
- Add frame-scoped agent capabilities and the new MCP tool set.
- Implement conservative `task_push`, no-ID transition claim, Back, Complete
  and return, Human close, Cancel, and supersession.
- Add exact trusted post-turn hook settlement with latency bounds.
- Add one-level work-context checkout claim and foreground lease transfer.
- Version-gate fixed bootstrap/fork/resume and Codex prestart naming; keep
  Claude naming curated-only until accepted separately.

Exit gate: deterministic tests cover all state-machine failures and guarded
Codex/Claude acceptance proves one workspace-child-return flow without
duplicates or raw prompt replay.

### Phase 6E: coordinated activation

- Freeze old core/DMS writes and perform an isolated cutover rehearsal.
- Replace the public CLI entrypoints and delete old protocol/action/TUI modules,
  fixtures, tests, and packaging declarations.
- Replace DMS with entry-model v1 and new ViewAction execution.
- Bump core to `0.3.0` and DMS to `0.5.0`.
- Run the quiescent local cutover from a plain shell, refresh all provider/remote
  state, open a fresh view, and resume an exact known provider UUID.
- Reload DMS only after core activation and verify focus/dedup/cache recovery.

Exit gate: installed core and DMS expose no old command/protocol/cache path;
local and two-host acceptance pass; source database/config/cache backups remain
restorable.

### Phase 6F: recursive task frames

- Lift initial depth from workspace plus one child to a bounded recursive stack.
- Generalize work-context ownership and lease transfer across task ancestors.
- Add task-to-task push/return, depth/loop guards, and recursive recovery.
- Keep cross-host push/return out of scope; remote frames remain separate views.

Exit gate: task A pushes task B, B returns exactly to A, and A returns exactly
to workspace while preserving checkout state, handoffs, and provider UUIDs.

## Deletion Matrix at Activation

Delete rather than deprecate:

- Snapshot v2, Fleet v1, PresentationPlan v2, SessionAction v2, and
  TaskCloseAction v2;
- old snapshot/fleet/prepare/select/attach/task-first CLI parsers and gateways;
- old agent MCP schemas and current-task-only authorization assumptions;
- Open/Inbox/Closed TUI models, screens, forms, and compatibility tests;
- schema migrations v1-v10 and their production registration;
- Config v2 normal parser and `migrate-v2` command;
- DMS bridge/action v4, model v5, task/project/history/stop desktop actions,
  and their warm-cache keys; and
- active packaging references to `0.2` phase documents.

Retain only in version control/history or the non-packaged archive:

- implementation/validation records from Phases 0-5;
- rejected provider/supervisor evidence;
- old protocol fixtures needed to test the offline exporter; and
- sanitized cutover rehearsal fixtures.

## Quiescent Live Cutover Runbook

The real cutover is run from a plain shell outside every managed provider pane:

1. Verify both repositories are clean, tagged/backed up, and built from the
   accepted commits.
2. Disable automatic provider transition hooks while retaining lifecycle
   observation configuration for later replacement.
3. Confirm no pending launch/transition and no background command requiring a
   managed checkout.
4. Gracefully stop all core-owned provider runtimes; verify exact UUIDs remain
   resumable in provider-owned history.
5. Run the exporter in read-only mode, verify its manifest/hash, and copy the
   old DB, Config v2, DMS state, and bundle to the cutover backup directory.
6. Install the paired core/DMS builds without activating DMS.
7. Run the importer into new temporary DB/config paths; execute doctor,
   reconciliation, HostState, NavigatorState, and provider discovery checks.
8. Atomically install the new DB/config, reinstall trusted hooks, then open one
   direct local view and resume an exact known session.
9. Activate DMS, clear only its old cache key, and prove Views/Projects/Recovery
   projection plus same-window focus dedup.
10. Bring the remote host forward and prove offline/online recovery before
    declaring the cutover complete.

Rollback before any new Phase 6 frame/session/handoff is created restores the
old package/plugin, DB, config, hook configuration, and DMS state from backup.
After new writes begin, rollback is manual/export-based; the old registry never
imports new-generation data.

## Acceptance Matrix

- Contract: strict v1 round trips, unknown/unsafe fields, bounds, Unicode,
  deterministic ordering, and source-authority references.
- Registry: fresh install, bundle import, source immutability, crash rollback,
  concurrent view revisions, placement repair, and unique ownership.
- tmux: anchor, mode toggle, swap/rollback, provider-pane survival, multiple
  clients/views, server-loss degradation, and no `kill-server` path.
- Workflow: workspace entry, push, Back, Complete-return, Human close, Cancel,
  supersession, park safety, and one-child depth enforcement.
- Frontends: compact navigator, focused panels, direct recovery, DMS entry
  model, cache cold/warm paths, niri focus, Ghostty launch, and remote view
  presentation.
- Clean break: removed commands fail as unknown, old DB/config fail with one
  cutover-required diagnostic, old DMS cache is ignored, and built artifacts
  contain no old active protocol or frontend modules.
- Packaging: full tests, lint, `git diff --check`, two byte-identical builds,
  content audit, clean-wheel install, and installed CLI/DMS smoke tests.
