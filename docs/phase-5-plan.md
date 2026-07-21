# Phase 5 Plan: Pull-based SSH Federation and Remote Actions

Status: implementation complete; deterministic and installed local-Fleet
acceptance passed; guarded live-SSH acceptance paused behind the approved local
project-catalog management follow-up and a configured test host

Phase 5 extends the completed local Snapshot v2, task, managed-tmux, TUI, and
DMS contracts across explicitly configured SSH hosts. It keeps every owning
host authoritative, uses bounded pull-based commands, preserves the local
no-daemon architecture, and does not weaken Snapshot v2's single-host
invariants.

This is one coordinated core and DMS vertical slice delivered through
reviewable commits. Read-only federation landed before remote mutations. The
legacy DMS remote helper remains available until the new path passes live
remote parity acceptance.

## Goals

- Fetch one validated Snapshot v2 from each declared remote with bounded,
  concurrent, noninteractive SSH commands.
- Atomically retain each remote's last successful snapshot and expose explicit
  online, offline, unknown, stale, and incompatible state.
- Present local and cached remote tasks and Inbox sessions through one bounded
  frontend envelope without manufacturing a multi-host Snapshot.
- Route remote prepare, select, attach, history, and safe-stop operations to
  the owning host while DMS invokes only a configured local `swbctl`.
- Continue a task onto another configured host only from an exact immutable
  handoff in a bounded, content-hashed envelope.
- Preserve provider-native storage, tmux ownership, attach-before-start,
  duplicate prevention, request idempotency, and last-good presentation.

## Non-goals

Phase 5 does not run a server, relay, reverse-SSH hook, event push service, or
host-wide daemon. It does not proxy provider I/O, copy transcripts, infer a
distributed mutable task, synchronize repositories/configuration/provider
history, mutate Git state, accept arbitrary SSH option strings, or teach DMS
to construct SSH commands. It does not remove the legacy fallback before
installed parity is proven.

## Preserve Snapshot v2 as a single-host authority envelope

`SnapshotEnvelope` remains unchanged. Its top-level `host.hostId` owns every
checkout, task, session, runtime, surface, capability, and error inside that
envelope. A remote snapshot is parsed and validated exactly like a local one.
Cached bytes are reserialized from the validated model rather than stored as
untrusted source text.

Phase 5 must not concatenate remote records into one `SnapshotEnvelope`; that
would erase owning-host authority and invalidate existing relationships.

## Additive Fleet v1 frontend envelope

The new command is:

```text
swbctl fleet [--refresh] --json
```

`fleet --json` performs no network I/O. It returns a new local retained
Snapshot v2 plus last-good materialized remote records. `fleet --refresh
--json` loads configuration, materializes endpoints, performs one local full
reconciliation, fetches declared remotes concurrently, stores each success or
failure atomically, and returns the same fleet shape.

The canonical envelope is:

```json
{
  "schemaVersion": 2,
  "protocolVersion": 2,
  "fleetVersion": 1,
  "generatedAt": 0,
  "localHostId": "uuid",
  "hosts": [
    {
      "source": "local",
      "remoteName": null,
      "hostId": "uuid",
      "displayName": "starship",
      "reachability": "online",
      "snapshotObservedAt": 0,
      "snapshotReceivedAt": 0,
      "lastAttemptAt": 0,
      "stale": false,
      "error": null,
      "snapshot": {"schemaVersion": 2, "protocolVersion": 2}
    }
  ]
}
```

Nullable timestamps, HostId, and snapshot are permitted for a remote before
its first success. The local entry is first and complete; remote entries follow
by alias. SSH targets never enter this envelope. The encoded Fleet is limited
by the existing public JSON byte bound and contains at most 32 remote entries,
with no duplicate alias or known HostId.

Every embedded snapshot passes `SnapshotEnvelope.from_dict`; host identity and
display name agree with its entry. An offline entry may retain a last-good
snapshot and prior observation/receipt times. Failure never replaces snapshot
bytes. Staleness is receipt age beyond the configured interval, not mutation
authority.

TUI and DMS migrate to Fleet v1 in coordinated commits. Existing `snapshot`
and `list` remain single-host Snapshot v2 compatibility surfaces in Phase 5.

## Catalog compatibility

Project and repository IDs are globally stable; checkouts and tasks remain
host-owned. Fleet assembly compares available catalogs:

- equal ProjectIds retain equal name, aliases, provider default, and transport;
- equal RepositoryIds retain equal name, kind, and context sources;
- equal project/repository pairs retain their primary flag;
- one CheckoutId may occur in only one host snapshot; and
- an endpoint may not change its pinned HostId.

A conflict excludes that remote from actionable rows, retains last-good bytes,
and exposes a bounded host diagnostic. Different host-local checkout IDs,
paths, branches, HEADs, and presence are expected and do not conflict.

## Bounded SSH snapshot transport

Core owns OpenSSH. The exact read argv is structurally equivalent to:

```text
ssh -T -o BatchMode=yes -o ConnectTimeout=5 -- TARGET \
    swbctl snapshot --reconcile none|full --json
```

No local shell is used. `TARGET` is the existing single validated token and may
not begin with `-` or contain whitespace. Remote arguments are fixed safe tokens
or validated UUID/enums; no user-authored remote shell syntax is accepted.

Initial bounds are 32 remotes, 4 concurrent SSH children, a 5-second OpenSSH
connect timeout, a 20-second end-to-end deadline, existing JSON stdout limit
plus one byte, and 64 KiB stderr. Every child gets a new process group and is
TERM/KILL-reaped on timeout, overflow, cancellation, read failure, or an
unexpected exception.

Exit zero, UTF-8, one bounded JSON value, Snapshot validation, host pinning, and
catalog checks all precede cache replacement. Errors are classified and
bounded; raw SSH stderr is not projected. Refreshes are one-shot and leave no
poller after the frontend closes.

## Remote endpoint and cache lifecycle

Full refresh materializes configured aliases. Removed aliases become
undeclared retained history and disappear from ordinary Fleet results. First
success pins HostId; later identity changes fail as
`remote_host_identity_changed` without replacing cache state.

Success records canonical snapshot JSON/hash, HostId, source observation time,
local receipt/attempt time, online reachability, and no transport error. Failure
records only attempt time, offline state, and one bounded classification. The
prior snapshot remains. Older attempt or observation completions are rejected,
so concurrent frontends cannot replace newer state.

## Remote-aware public actions

Commands whose target is not self-describing gain an owning-host selector:

```text
swbctl prepare-task TASK --host HOST ... --json
swbctl prepare-history --host HOST --project PROJECT ... --json
swbctl select-surface SURFACE --host HOST --client CLIENT
swbctl attach-surface SURFACE --host HOST
```

`prepare-open` and `stop-session` infer host from the canonical session key but
may accept `--host` for uniform routing. No host, or the local HostId, preserves
current behavior.

For a remote host, local core resolves exactly one declared pinned endpoint and
invokes the corresponding command through bounded SSH. Cached rows do not
authorize mutation: the owner reconciles current truth and returns the plan or
error. Local core independently validates the response and exact HostId/target.

Preparation forwards frontend presentation capabilities. The owner returns
the existing `focus`, `switch`, `attach`, or `blocked` plan. DMS interprets it
locally and includes owning HostId on select/attach. Desktop application
identity combines host and opaque surface token, so local Ghostty can be
focused without exposing tmux locators.

Remote selection is bounded noninteractive SSH. Remote attachment replaces the
current process with:

```text
ssh -tt -- TARGET swbctl attach-surface SURFACE
```

The owner revalidates surface and lease before tmux attach. Provider bootstrap
still waits for a viewing client. The local host receives no provider argv or
raw tmux target.

## Remote task creation and history

Fleet rows carry private HostId, ProjectId, and host-local CheckoutId routing.
Within a project category, DMS offers creation only for hosts whose snapshot
declares that project and a present declared default checkout in its primary
repository. The visible row names the destination when several hosts qualify.

The initiating frontend generates TaskId and request ID. The target performs
the existing atomic create/reservation and final duplicate checks. Retrying is
idempotent. Remote Claude history uses the same provider-owned picker and
unbound managed surface as local history.

## Cross-host continuation

A task remains host-owned. Moving work creates a destination task linked to an
exact immutable source handoff; it never reassigns the source TaskId.

The source exports one exact handoff through:

```text
swbctl task export-handoff TASK --handoff HANDOFF --json
```

The bounded continuation envelope contains its schema/protocol/version; source
host, project, task, session, and handoff IDs; task title and optional purpose;
summary, next action, source timestamp, and content SHA-256. It contains no
transcript, prompt, path, Git private identity, provider argv, tmux locator, or
capability token.

The destination validates the envelope, requires the same configured ProjectId
and an explicit/default local checkout, stores an immutable imported handoff,
and atomically creates the task and launch reservation. Exact retries are
idempotent; conflicting content fails. The destination agent retrieves the
import through existing bounded current-task context/handoff tools. Nothing is
automatically injected into a provider prompt and the destination never reaches
back to the source.

## TUI behavior

The gateway consumes Fleet v1 and retains one last-good fleet while refreshing.
The TUI shows open tasks across available hosts; qualifies remote rows by host;
exposes reachability, staleness, and catalog issues; provides federated Inbox
and Closed views plus host/project filters; and derives remote actions from
source records and endpoint state. Selection identity is `(host_id, task_id)`
or canonical session key. Offline rows remain inspectable, while mutations
still attempt and revalidate the owner.

## DMS behavior

The adapter advances its private bridge/model contract and invokes only local
`swbctl fleet` and the local action helper. It does not import core, read SQLite,
construct SSH argv, or parse remote config.

Rows remain concise:

```text
task title
project | optional host | optional worktree | state | age
```

Host is omitted locally and included remotely. Compatible ProjectIds share a
category. Inbox and Closed include remote rows. A project query emits provider
creation rows per eligible host and names the host when needed. The validated
last-good model moves to a new bridge/model-versioned state key. The documented
DMS `itemsChanged()` fallback remains.

## Delivery commits

1. Phase 5 plan and roadmap reconciliation.
2. Fleet protocol, endpoint materialization, SSH runner, and cache refresh.
3. Fleet-aware TUI gateway/model/application.
4. Remote prepare/select/attach/history/stop gateway.
5. Continuation envelope, storage, commands, and agent retrieval.
6. DMS Fleet bridge/model/launcher and desktop routing.
7. Isolated/live acceptance and documentation.

No commit leaves an existing local Snapshot/action consumer silently accepting
an incompatible shape.

## Implementation checkpoint

The seven delivery slices are implemented in reviewable core and DMS commits:

- core emits and retains Fleet v1 through bounded concurrent OpenSSH, exposes
  offline/stale/never-seen state, and preserves Snapshot v2 single-host
  ownership;
- the TUI consumes Fleet v1 with host-qualified task and Inbox identity;
- every prepare/history/select/attach/stop action routes through a configured,
  HostId-pinned owner and validates the returned target;
- continuation exports one exact immutable handoff and atomically stores its
  content-hashed import with the destination task/reservation; and
- DMS bridge v3/model v4 merges compatible projects, retains per-host routes,
  scopes desktop identity by host, and invokes only local `swbctl`.

Core formatting, Ruff, compileall, and all 638 tests pass. The separate DMS
adapter passes 97 Python tests, 15 JavaScript behavior groups, QML formatting,
Ruff, and package Pyright. Its private-state Quickshell harness passes retained
and full Fleet reads, cache round trip, exact query, last-good failures, and
recovery. The installed plugin reports bridge 3/model 4 with one local host and
no failure after the current no-cache core build is installed.

The true `SwitchboardModelV4.js` contract bump required one documented
DMS-only restart to clear Qt's retained JavaScript module state. Exact Codex
and Claude PID sets remained unchanged. `swbctl doctor` reported only warm hook
p95 around 130 ms against the 125 ms performance budget; it found no federation
contract failure.

This host has no configured remote endpoint. Therefore automated two-host
transport/action/continuation coverage and installed local-Fleet coverage are
complete, while the live-SSH steps below remain pending. No provider was
launched, stopped, restarted, or signalled during the installed checkpoint.

Before live SSH acceptance, the approved local catalog follow-up in
[`project-management-plan.md`](project-management-plan.md) closes the missing
project list/add/edit/archive/restore and export/import workflow. Fleet v1 and
the completed remote implementation remain unchanged while that local UX gate
is delivered.

## Automated acceptance

Core tests use temporary XDG roots, private registries, fake snapshots, and a
fake `ssh`. They cover zero/maximum remotes; online/offline/stale/never-seen
state; concurrency and ordering; timeout/overflow/UTF-8/JSON/nonzero/cancel
cleanup; last-good/out-of-order behavior; HostId and catalog conflicts; whole
Fleet bounds; plan host mismatch; exact read/prepare/select/attach argv;
request idempotency; continuation validation; TUI refresh/selection/filtering;
and DMS projection/cache/action routing. They never invoke a provider, user SSH
configuration, default registry/tmux, or live DMS.

## Guarded installed acceptance

The local core/DMS install, private-state harness, cache, and provider
non-interruption portions have passed. Steps requiring a remote endpoint are not
executable on the current one-host configuration and remain open.

After full static/unit/package/isolated gates:

1. Record versions, HostIds, project identity, process baselines, and rollback.
2. Install compatible core remotely without changing hooks or active sessions.
3. Prove retained/full SSH fetch and no surviving background SSH child.
4. Prove controlled offline failure retains last-good rows.
5. Open one managed remote session without another runtime/surface.
6. Create one test-owned remote task and prove reopen deduplication.
7. Continue one test-owned task from an explicit handoff and preserve source.
8. Reload only the Switchboard DMS plugin and verify the same remote path.
9. Clean exact test-owned identities and restore package/config state on error.

Active user Codex or Claude sessions are never stopped, restarted, signalled,
adopted, wrapped, or reassigned for acceptance.

## Rollback and completion

Rollback restores prior core/DMS commits and recorded config/registry copies.
Remote caches are disposable observations; rollback never deletes provider
history or tmux sessions. The legacy helper remains until discovery, offline
retention, open/focus/attach, task creation, and continuation parity pass.

Phase 5 completes only when both repositories pass deterministic and
reproducible-distribution gates; Snapshot v2 remains single-host; Fleet v1 is
bounded and consumed by TUI/DMS; owners revalidate every remote mutation; no
persistent SSH/controller remains; installed open/create preserve dedup; only
bounded handoffs cross hosts; and every legacy path is classified.
