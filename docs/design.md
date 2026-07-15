# Agent Switchboard Design

Status: Draft

Last updated: 2026-07-14

Related research: [Open-source product landscape](product-landscape.md)

## Summary

Agent Switchboard is a local-first session manager for terminal coding agents.
It presents Codex and Claude Code conversations through one searchable session
model, reports whether each session is working, needs input, is ready for the
next prompt, or is parked, and opens each session through the provider's native
resume or attach mechanism.

The project has a frontend-neutral core. A terminal UI is the first full
frontend. DankMaterialShell (DMS), niri, shell, and tmux integrations consume
the same command and JSON interfaces rather than reimplementing discovery or
state tracking.

Agent Switchboard does not replace provider conversation storage or agent
execution. Codex and Claude Code remain the source of truth for transcripts,
history, authentication, and execution. Switchboard owns a durable index,
normalized live status, attachment surfaces, and action routing.

## Problem

A terminal-centric workflow commonly has several long-running Codex and Claude
Code conversations spread across local and remote machines. The provider CLIs
have different lifecycle models:

- Codex conversations are durable, but a live terminal process needs an
  external persistence mechanism such as tmux.
- Claude Code can run background conversations under its own per-user
  supervisor and can attach a terminal to them later.
- Each provider has its own history and resume UI.
- Neither provider gives a single view across providers, hosts, tmux sessions,
  and desktop windows.

The user should not have to remember which host, terminal window, tmux session,
provider picker, or provider-specific command owns a conversation. One manager
should answer:

1. What sessions exist?
2. Which sessions are active?
3. What is each active session doing?
4. Which sessions need attention?
5. How do I open the exact session I selected?

## Goals

- List Codex and Claude Code sessions through one model.
- Keep active and recent parked sessions searchable across configured hosts.
- Normalize useful status into working, needs input, ready, parked, and
  unknown/offline states.
- Open an exact session without forcing the user through another picker when
  its identity is already known.
- Preserve provider-native history, resume, and background behavior.
- Use tmux as the default terminal attachment transport without making tmux a
  session identity or a requirement of the core data model.
- Support local terminal, SSH, tmux, DMS, and niri workflows through the same
  core interfaces.
- Continue working when the TUI or DMS is closed.
- Recover from missed hook events by reconciling provider and process state.
- Avoid a mandatory daemon in the first implementation.

## Non-goals

- Reimplement provider transcripts or context management.
- Parse private transcript formats as the primary history interface.
- Replace Claude Code Agent View lifecycle controls.
- Orchestrate multiple agents, assign work, or send prompts automatically.
- Add another notification system.
- Embed a terminal emulator in the TUI.
- Synchronize transcripts or credentials between machines.
- Provide cloud execution or a web service.
- Stop, delete, archive, or mutate provider sessions in the first release.

## Design Principles

### Provider-native ownership

Codex and Claude Code own conversation content and lifecycle semantics.
Switchboard stores only enough metadata to identify, classify, and open a
session.

### One logical model, provider-specific actions

Frontends see one `AgentSession` model. Provider adapters decide how to
discover, resume, attach, and reconcile each kind of session.

### Session identity is not terminal identity

A provider conversation can move between terminal surfaces. A terminal surface
can also switch from one Claude conversation to another. Session and surface
records therefore have separate stable identities and a mutable association.

### Hooks provide immediacy; reconciliation provides correctness

Hooks update status quickly. Native provider queries and process liveness repair
missed, delayed, or stale events. Neither source is sufficient by itself.

### Frontends are replaceable

The TUI, DMS plugin, and future integrations must consume stable core APIs. No
provider discovery or state transition logic belongs in frontend code.

### Degrade explicitly

Missing hooks, an offline host, an unavailable provider, or an unsupported CLI
version should produce an explicit stale, unknown, or unavailable state rather
than silently showing incorrect data.

## Terminology

**Provider session**
: A durable Codex thread or Claude Code conversation.

**Runtime**
: A live provider process or provider-supervised background job associated with
  a session.

**Surface**
: A terminal endpoint through which a user can interact with a runtime. The
  first implementation uses a tmux session/window/pane target.

**Attachment**
: The current association between a provider session and a terminal surface.

**Parked session**
: A known durable session with no live provider runtime.

**Registry**
: Switchboard's durable local index of sessions, runtime observations, surfaces,
  and last-known remote snapshots.

**Frontend**
: A user interface or integration that lists sessions and asks the core to
  resolve or perform actions.

## Architecture

```text
                       Provider-native state
                 +-----------------------------+
                 | Codex app-server / processes|
                 | Claude supervisor / CLI     |
                 +--------------+--------------+
                                |
                         provider adapters
                                |
 Hooks -----------------> event ingestion
                                |
                         registry + reconciler
                                |
                    agentctl command / JSON API
                  +-------------+-------------+
                  |             |             |
                 TUI        DMS plugin     shell/tmux
                                |
                         niri window adapter
```

The initial implementation is process-based rather than service-based:

- Hooks invoke a short-lived `agentctl event` command.
- Frontends invoke `agentctl list`, `agentctl reconcile`, and action commands.
- The TUI refreshes local state and runs bounded reconciliation while open.
- Each remote host maintains its own registry. Aggregators fetch a versioned
  snapshot over SSH.

A daemon can be introduced later if measured latency, event fan-out, or remote
refresh costs justify it. The storage and command contracts must not require one.

## Core Domain Model

### Session identity

A session key is namespaced by host and provider:

```text
SessionKey = HostId + ProviderId + ProviderSessionId
```

- `HostId` is a generated stable identifier stored by Switchboard on each host.
  Hostnames and display aliases may change and are not identity.
- `ProviderId` initially supports `codex` and `claude`.
- `ProviderSessionId` is the provider's durable session UUID.

Claude's short background-agent ID is a runtime locator, not a durable session
identity.

### AgentSession

```text
AgentSession
  key
  provider
  provider_session_id
  name
  cwd
  host_id
  created_at
  provider_updated_at
  last_activity_at
  first_observed_at
  last_observed_at
  presence
  activity
  attachment
  runtime_locator
  surface_id
  metadata_source
  state_confidence
```

Names and working directories are display metadata. They are not used as keys
and may change.

### Runtime locator

Provider adapters store only the locator fields they understand:

```text
RuntimeLocator
  pid
  provider_runtime_id
  tmux_session
  tmux_window
  tmux_pane
  observed_at
```

For Claude background sessions, `provider_runtime_id` is the short ID accepted
by `claude attach`. For Codex, the durable session UUID plus a tmux surface is
sufficient.

### Surface

```text
Surface
  surface_id
  host_id
  transport
  transport_locator
  current_session_key
  created_at
  last_observed_at
  client_attached
```

`current_session_key` is mutable. If a Claude terminal switches from session A
to session B, the surface stays the same while its session association changes.

## State Model

Status is represented on separate axes so that execution and presentation do
not become conflated.

### Presence

- `live`: a provider runtime is confirmed alive.
- `parked`: the session is durable but no runtime is alive.
- `offline`: the owning host cannot currently be queried.
- `unknown`: available evidence is incomplete or contradictory.

### Activity

- `working`: the provider is processing the current turn.
- `needs_input`: the provider is blocked on a permission, question, or explicit
  user decision.
- `ready`: a live provider is waiting for the next prompt.
- `unknown`: activity cannot be established.

Activity is meaningful primarily while presence is `live`. The last known
activity may be retained for diagnostics but does not override `parked` or
`offline` in the UI.

### Attachment

- `attached`: a user-facing terminal client is currently associated.
- `detached`: a surface or provider runtime exists without an attached client.
- `none`: no terminal surface exists.
- `unknown`: attachment cannot be established.

### Display status

Frontends derive one primary label using this precedence:

```text
offline -> parked -> needs input -> working -> ready -> unknown
```

Attachment is displayed separately. Examples include `working, detached` for a
Claude background agent and `ready, attached` for Codex waiting in Ghostty.

### Hook-driven transitions

The initial normalized transitions are:

```text
SessionStart       -> presence=live, activity=ready
UserPromptSubmit   -> presence=live, activity=working
PermissionRequest  -> presence=live, activity=needs_input
PostToolUse        -> presence=live, activity=working
Stop               -> presence=live, activity=ready
SessionEnd         -> provisional presence=parked
```

Provider-native state and liveness reconciliation may override provisional hook
state. In particular, a Claude session can detach from a terminal while
remaining live under the supervisor.

## Provider Adapter Contract

Each provider adapter is responsible for the following capabilities where the
provider supports them:

```text
discover_sessions()      -> provider session metadata
discover_runtimes()      -> live runtime observations
normalize_event(event)   -> normalized state transition
new_command(options)     -> argv and launch metadata
resume_command(session)  -> argv and launch metadata
attach_command(runtime)  -> argv and launch metadata
reconcile(registry)      -> corrected session/runtime state
capabilities()           -> detected feature/version support
```

Commands are represented as argument arrays, never interpolated shell strings.
The transport layer decides where and how to execute them.

Adapters must tolerate missing providers and version-specific capability gaps.
The core exposes those gaps to frontends instead of fabricating support.

## Codex Provider

### Discovery

- Use Codex app-server thread listing for durable CLI session metadata.
- Page results rather than relying on a fixed recent-session limit.
- Use hooks to associate live processes with durable session IDs.
- Use process and tmux liveness as reconciliation evidence.
- Keep the existing process/file-descriptor discovery only as a transitional
  fallback while hook coverage is incomplete.

The app-server's thread status is not assumed to represent another running CLI
process. Live activity comes from hooks and liveness observations.

### Status

Codex hooks provide turn-level state. Codex does not currently provide the same
reliable end-of-session signal as Claude, so a session becomes parked when its
recorded process is gone and no replacement runtime is found.

### Opening

- A live session with an existing tmux surface is focused or attached.
- A live process outside a managed surface is adopted when it can be located;
  otherwise the frontend reports that it is active but cannot safely duplicate
  it.
- A parked session creates a managed surface running `codex resume <uuid>`.
- A new session creates a managed surface running `codex` in the selected
  working directory.

tmux is the persistence backend for a live Codex CLI process in the default
transport.

## Claude Provider

### Discovery

- Use `claude agents --all --json` for supervisor-managed sessions, runtime IDs,
  names, working directories, PIDs, and background state.
- Use hooks to register interactive sessions and session switches.
- Merge supervisor and hook records by durable Claude session UUID.
- Treat supervisor state as authoritative for background activity and liveness.

Claude does not currently expose every ordinary historical `/resume` entry
through a documented structured listing command. Therefore:

- Supervisor-managed sessions can be imported immediately.
- Every interactive session encountered after hook installation is retained in
  the Switchboard registry and remains visible when parked.
- Untouched legacy history remains available through a provider-native
  `Open Claude history` action.
- Switchboard will not parse private transcript contents to manufacture a full
  history list. A documented provider API can replace this limitation later.

### Status

Claude supervisor observations map as follows:

```text
working/busy             -> live, working
blocked or needs input   -> live, needs_input
done with a live PID     -> live, ready
known session, no PID    -> parked
```

Hooks cover interactive sessions and provide faster transitions than polling.

### Opening

- An already visible interactive session is focused.
- A background session with a short runtime ID uses `claude attach <id>`.
- A parked known session uses `claude --resume <uuid>`.
- A new interactive session runs `claude` in the selected working directory.

Claude's supervisor is the persistence backend for background sessions. A tmux
surface containing `claude attach` is only a stable terminal view. Switchboard
does not create tmux sessions merely to keep Claude background work alive.

### In-terminal session switching

When an interactive Claude terminal switches from session A to B:

1. Claude emits `SessionEnd` for A with the resume/switch reason.
2. Claude emits `SessionStart` for B.
3. The hook inherits the stable Switchboard surface identifier.
4. The registry removes A's surface association and marks A parked unless the
   supervisor still reports it live.
5. The registry associates the existing surface with B.

The TUI and DMS continue to show separate rows for A and B. Only the attachment
moves.

## Event Ingestion

Both provider hook configurations invoke a fast command:

```text
agentctl event --provider <provider>
```

The provider event JSON is read from standard input. The handler:

1. Validates the provider and event schema.
2. Extracts only identity, lifecycle, cwd, and timing fields.
3. Reads `AGENT_SWITCHBOARD_SURFACE_ID` and tmux environment metadata when
   present.
4. Applies the normalized transition in one database transaction.
5. Exits without network access or provider queries.

Hooks must not delay the agent loop. They never contact remote hosts, launch a
frontend, or wait for tmux. Provider-specific hook trust and enablement remain
visible through `agentctl doctor`.

Event writes include an observation timestamp and a source priority. Older
events cannot overwrite newer authoritative observations. A bounded event log
may be retained for diagnostics, but prompts and transcript content are never
stored.

## Registry and Reconciliation

### Storage

The proposed first implementation uses SQLite because hooks can write
concurrently while the TUI or DMS reads, and because atomic updates, indexes,
and schema migrations are useful without requiring a daemon.

```text
${XDG_STATE_HOME:-~/.local/state}/agent-switchboard/switchboard.db
```

The database contains:

- hosts and their stable IDs
- provider sessions and display metadata
- last normalized presence/activity/attachment state
- runtime observations
- surfaces and their current session association
- bounded hook event metadata
- cached snapshots from remote hosts
- schema and protocol versions

It does not contain prompts, model output, transcript bodies, authentication
tokens, or copied provider configuration.

SQLite runs in WAL mode with a bounded busy timeout. Hook writes are short
transactions. Schema migrations are explicit and backward compatibility is
covered by tests.

### Reconciliation

Reconciliation repairs the materialized registry from current evidence:

1. Query provider-native session and runtime data.
2. Inspect known process and tmux locators.
3. Mark dead runtimes and release stale surface associations.
4. Merge newly discovered provider sessions.
5. Preserve parked registry records that remain resumable.
6. Record capability or provider errors without deleting known sessions.

Local reconciliation can run on TUI startup, on explicit refresh, and at a
bounded interval while a frontend is open. Remote reconciliation runs on the
remote host, not over a shared database.

## Surface and Transport Model

### tmux transport

tmux is the default first transport. It provides a consistent way for a TUI,
plain shell, SSH client, or desktop terminal to reach a session.

A managed surface uses an opaque stable name rather than user-controlled
session text:

```text
tmux session: as-<surface-id-prefix>
```

Switchboard stores metadata as tmux user options at the narrowest available
scope:

```text
@agent_switchboard_surface_id
@agent_switchboard_session_key
@agent_switchboard_provider
```

The full locator includes tmux session, window, and pane IDs. Managed launches
start with one agent process, but discovery must tolerate user-created windows,
panes, and layouts.

The default policy is at most one managed surface per logical session. The
surface identity remains stable if an in-terminal provider action changes the
conversation associated with it.

### Attachment behavior

When opening a tmux surface:

- Inside tmux, select the target window/pane and use `switch-client` when the
  target is in another tmux session.
- Outside tmux, attach the current terminal to the target session.
- From a desktop frontend, focus an existing terminal client when possible;
  otherwise launch a terminal that attaches to the target.

The core does not assume Ghostty or niri. Desktop-specific focus and launch
behavior belongs to an integration adapter.

### Direct transport

A direct terminal transport is compatible with the model but is not required
for the first milestone. It would suspend the TUI and run a provider command in
the current terminal. Direct transport does not give Codex process persistence
and requires a separate way to identify desktop windows.

## Open Resolution

Opening is split into resolution and presentation:

```text
resolve_open(SessionKey) -> OpenPlan
```

An `OpenPlan` is one of:

- focus an existing surface
- attach or switch to an existing tmux target
- create a surface with a provider attach command
- create a surface with a provider resume command
- report a blocked action with a concrete reason

The plan contains structured argv and locator fields. It does not contain a
shell command string.

The TUI can execute a terminal transport directly. DMS consumes the same plan
but delegates niri focus and Ghostty launch behavior to its integration code.

Before creating a surface, the executor revalidates provider liveness and
existing mappings to avoid opening duplicate interactive clients.

## Command Interface

The exact command spelling is not yet final, but the core needs these stable
operations:

```text
agentctl list [filters] [--refresh] [--json]
agentctl show <session-key> [--json]
agentctl reconcile [--host <host>]
agentctl refresh [--host <host>]
agentctl new --provider <provider> --cwd <path>
agentctl resolve-open <session-key> --json
agentctl open <session-key> [--transport tmux|direct]
agentctl event --provider <provider>
agentctl snapshot [--reconcile none|live|full] --json
agentctl doctor
```

`snapshot` is always host-local. It reads the registry on the machine where it
runs and never recursively queries configured remotes. `refresh` is the
aggregator operation that pulls host-local snapshots over SSH and updates the
local cache. `list` reads local and cached remote state; `--refresh` requests a
bounded refresh before returning.

The default snapshot reconciliation mode is `none`, which makes the command a
fast database read. `live` performs bounded runtime and surface liveness checks.
`full` also refreshes provider history and other comparatively expensive
metadata. Exact reconciliation flags remain subject to CLI usability testing,
but the distinction between snapshot, live repair, and full discovery is part
of the protocol.

Machine output is versioned independently of human-readable output:

```json
{
  "schemaVersion": 1,
  "generatedAt": 0,
  "host": {},
  "sessions": [],
  "errors": []
}
```

Unknown fields must be ignored by clients. Incompatible schema versions produce
an explicit error.

## Terminal UI

The TUI is the first complete frontend and exercises the public core contract.
It does not import provider implementation details directly.

### Primary view

The default view presents:

- provider
- session name
- normalized status
- attachment state
- host
- working directory
- last activity

Sessions are sorted by attention and recency:

1. needs input
2. working
3. ready
4. recently parked
5. offline or unknown

All known sessions remain searchable. An empty query may limit parked rows to a
configurable recent count so active work stays scannable.

### Initial actions

- Open the selected session.
- Start a new Codex or Claude session.
- Filter by provider, host, state, and working directory.
- Refresh/reconcile.
- Open the provider-native history picker when Switchboard cannot enumerate old
  provider history.
- Inspect an error or degraded capability.

Destructive actions are excluded from the first release.

### tmux entry points

The TUI must work as:

- a normal terminal command
- a tmux popup
- a dedicated tmux manager session

Selecting a row transfers the current client to the target surface. The TUI
does not render or proxy the agent terminal stream.

## Remote Hosts

### Transport model

Remote aggregation uses command RPC over OpenSSH standard input and output. It
does not require an Agent Switchboard network listener or application daemon.

The data flow is deliberately asymmetric:

```text
Within one host: provider hooks -> local registry
Across hosts:    local aggregator -> SSH snapshot request -> remote registry
```

Hooks push lifecycle events only into the registry on their own host. Frontends
pull versioned snapshots from remote registries. This keeps hook execution
independent of network availability and avoids assigning any machine the role
of a permanent controller.

Full status tracking requires Agent Switchboard and provider hooks on every
managed host. Each host owns its registry and provider reconciliation. No
database file is shared or mounted between machines.

### Snapshot query

The basic read request launches a short-lived remote process:

```text
ssh <target> agentctl snapshot --json
```

The remote command reads its host-local registry, writes one versioned JSON
document, and exits. It does not connect back to the caller, contact another
host, or recursively aggregate its own configured remotes.

When the caller needs stronger liveness evidence, it can request bounded remote
reconciliation in the same process:

```text
ssh <target> agentctl snapshot --json --reconcile live
```

Full provider history discovery is requested less frequently:

```text
ssh <target> agentctl snapshot --json --reconcile full
```

This separation prevents frequent status refreshes from repeatedly starting
Codex app-server history scans or other comparatively expensive provider
queries. Hooks keep normal activity state current; reconciliation repairs
missed events and dead runtimes.

### Aggregation and cache

The local aggregator:

- uses batch-mode SSH with bounded connection timeouts and attempts
- queries configured hosts concurrently
- validates snapshot schema versions
- records the remote observation and receipt timestamps
- atomically replaces the cached snapshot after successful validation
- retains the last successful snapshot when a host is unreachable
- marks retained sessions offline rather than removing them
- keeps connection targets separate from stable host IDs and display aliases

Frontends render cached rows immediately and refresh stale hosts
asynchronously. A remote failure therefore does not delay local results or make
known sessions disappear. The UI exposes snapshot age whenever data is stale or
offline.

`agentctl list` is the frontend-facing merged view. It reads the local registry
and cached remote snapshots. `agentctl refresh` performs remote pulls and
updates that cache. A TUI can run refreshes asynchronously while retaining its
current model; DMS can show cached items and refresh only when its cache is
stale.

### Refresh policy

Remote SSH traffic exists only while a frontend requests it:

- TUI startup requests an initial refresh.
- An open TUI polls live snapshots at a configurable bounded interval.
- Full provider discovery runs on startup, explicit refresh, or a slower
  interval.
- DMS refreshes when the picker is opened and cached data is stale.
- Closed frontends generate no SSH polling traffic.

Exact intervals are configuration and performance choices rather than protocol
guarantees. OpenSSH connection multiplexing may keep a control connection warm
to avoid repeated handshake cost. That is an SSH transport optimization, not an
Agent Switchboard server.

### Remote actions

The owning host always revalidates an action against current provider, runtime,
and surface state. A cached local snapshot is never sufficient authority to
create or duplicate a runtime.

A terminal frontend can open a remote session with an interactive command:

```text
ssh -t <target> agentctl open <session-key> --transport tmux
```

The remote command resolves the current action, adopts or creates the correct
surface, and attaches the SSH terminal. For a Claude background session, that
surface runs the provider-native attach command. For a parked Codex session, it
runs the provider-native resume command under tmux.

DMS uses the same remote action but first asks its local niri integration to
focus an existing terminal already attached to the host and surface. If none
exists, DMS launches the configured terminal around the interactive SSH
command.

New-session and other future mutating actions follow the same rule: send an
explicit command to the owning host, validate there, and return structured
errors. They do not mutate cached remote rows locally.

### Why inter-host push is excluded

Remote hooks do not SSH back to a controller. Network push would require a
stable controller identity, reverse credentials, delivery queues, retries, and
fan-out semantics for multiple frontends. It would also make provider hook
latency and reliability depend on whether another laptop or desktop is awake
and reachable.

Keeping events local means a roaming or disconnected host loses no state. The
next successful snapshot pull observes everything recorded while the frontend
was absent.

### Optional streaming evolution

If measured polling cost or latency becomes unacceptable, the protocol can add:

```text
ssh <target> agentctl watch --jsonl
```

`watch` would be a long-lived remote process scoped to the lifetime of an open
frontend. It would stream versioned snapshots or deltas over SSH standard
output and reconnect after failure. It would not listen on a port or require a
system service.

A permanent daemon is deferred until evidence shows that snapshot polling and
frontend-scoped watch processes are insufficient. The registry and snapshot
protocol must remain usable without one.

The initial milestone is local-only. Remote aggregation follows after the
local registry, status model, and attachment behavior are stable.

## DMS and niri Integration

The existing DMS Agent Picker remains functional during migration. Its final
responsibilities are intentionally narrow:

- invoke `agentctl list --json`
- translate sessions into DMS launcher items
- invoke open-plan resolution
- focus a matching niri terminal window when one exists
- launch the configured terminal when no matching client exists
- expose DMS-specific appearance and refresh settings

Provider discovery, SSH host configuration, status transitions, and tmux
creation move out of QML and the DMS plugin helper into Agent Switchboard.

niri and Ghostty identity should use a stable surface token supplied by the
core rather than a provider session name. This lets a terminal switch Claude
sessions without making the desktop window impossible to find.

## Configuration

Core configuration belongs under:

```text
${XDG_CONFIG_HOME:-~/.config}/agent-switchboard/config.toml
```

Expected settings include:

- local display name and stable host identity
- remote SSH targets and display aliases
- provider enablement and executable overrides
- default transport
- tmux naming prefix and behavior
- refresh and staleness intervals
- recent parked-session limit
- default working-directory selection behavior

Frontend appearance and keybindings do not belong in core configuration.

Configuration and registry schemas are separate. Removing a remote target does
not immediately erase its cached session records; cleanup is explicit.

## Failure Handling

### Missing or incompatible provider

Expose provider capability state and an actionable diagnostic. Other providers
remain usable.

### Hooks unavailable

Discovery and open actions continue. Live activity is marked unknown or derived
from weaker native evidence. `agentctl doctor` explains the missing hook or
trust requirement.

### Stale runtime

Process and provider reconciliation marks the session parked and releases stale
surface mappings. Codex must not remain active indefinitely because it lacks an
end event.

### Offline remote host

Retain the last snapshot, mark it offline with its observation time, and reject
open actions before launching a terminal.

### Provider schema change

Adapters validate required fields and report a capability error. Raw malformed
data is not written into the registry as authoritative state.

### Duplicate surface

Revalidate immediately before creation. Prefer an existing healthy surface and
retire stale mappings. Never start a second interactive process merely because
the frontend cache is old.

## Security and Privacy

- Do not store prompts, transcript bodies, model output, or credentials.
- Treat provider names, cwd values, and remote metadata as untrusted display
  input; remove terminal control characters.
- Build commands as argv arrays and validate provider IDs, session IDs, tmux
  locators, and SSH targets.
- Keep the registry and event files user-readable only.
- Hooks perform local database writes only and must return quickly.
- Preserve normal SSH host-key verification and use batch mode for background
  discovery.
- Require explicit provider hook trust through the provider's supported trust
  mechanism.
- Do not silently install hooks, alter provider settings, or enable remote
  access from a frontend action.

## Migration from DMS Agent Picker

The current plugin already provides useful Codex discovery, process-to-tmux
mapping, remote SSH aggregation, exact session resume, and niri window focus.
Migration should preserve those behaviors while moving ownership in stages.

### Phase 0: Design and scaffold

- Agree on this design.
- Select the implementation language and TUI framework.
- Define versioned JSON fixtures and provider interfaces.
- Establish formatting, tests, CI, license, and release packaging.

### Phase 1: Read-only local core

- Port Codex app-server discovery and metadata normalization.
- Add Claude supervisor discovery.
- Implement the session model and versioned `list --json` output.
- Add provider capability detection and fixtures.

### Phase 2: Registry and hooks

- Add SQLite schema and migrations.
- Add Codex and Claude hook ingestion.
- Implement normalized state transitions and liveness reconciliation.
- Verify Claude A-to-B in-terminal switching.
- Retain observed sessions after they become parked.

### Phase 3: tmux transport and TUI

- Implement stable surfaces and tmux metadata.
- Implement open-plan resolution and duplicate prevention.
- Build the searchable status-oriented TUI.
- Support normal terminal, tmux popup, and switch-client flows.

### Phase 4: Remote hosts

- Add stable host identity and snapshot protocol.
- Add host-local snapshot reconciliation modes.
- Add concurrent pull-based SSH aggregation and atomic caching.
- Preserve stale snapshots and expose explicit offline state.
- Add owning-host action revalidation and interactive remote attachment.
- Measure polling with SSH multiplexing before considering `watch --jsonl`.

### Phase 5: DMS migration

- Change DMS to consume Agent Switchboard JSON.
- Add per-session Claude rows and normalized statuses.
- Move shared host/provider settings into core configuration.
- Remove duplicated Python discovery and tmux logic after parity tests pass.

No phase should require a flag day. The existing DMS helper remains available
until its replacement passes equivalent discovery and open-path tests.

## Test Strategy

### Unit tests

- Session identity and merge rules
- Hook transition ordering and stale-event rejection
- Presence/activity/attachment derivation
- Provider schema validation
- Open-plan resolution
- Command argument escaping and validation
- Remote snapshot version handling
- Snapshot reconciliation mode boundaries
- Remote cache freshness and offline derivation
- Database migrations

### Provider contract tests

Use captured, redacted fixtures for Codex app-server and Claude JSON output.
Fixtures must cover missing fields, incompatible versions, completed sessions,
background work, interactive sessions, and provider errors.

### tmux integration tests

Use an isolated tmux server/socket to verify:

- surface creation and metadata
- attach and switch behavior
- pane discovery in user-modified layouts
- session rebinding from Claude A to B
- stale surface cleanup
- duplicate prevention

### End-to-end tests

- Start, park, resume, and reopen a Codex session.
- Start, background, attach, and reopen a Claude session.
- Switch Claude conversations inside one surface and verify both rows.
- Miss a hook event and repair state through reconciliation.
- Verify remote hooks never perform network operations.
- Confirm a remote snapshot never recursively queries other hosts.
- Disconnect a remote host and retain offline cached rows.
- Reject a stale remote open plan when owning-host revalidation disagrees.
- Open the same session from TUI and DMS without creating duplicate runtimes.

## Open Decisions

### Implementation language

The architecture is language-independent. Selection should compare:

- reuse of the existing tested Python helper
- startup latency for frequent hooks
- TUI ecosystem and testability
- SQLite support
- subprocess, PTY, SSH, and tmux handling
- cross-platform packaging for Arch, Ubuntu, macOS, and WSL
- ability to ship a self-contained executable

Python offers the lowest migration cost. Go and Rust offer simpler standalone
distribution at the cost of a rewrite. This decision should be made before
Phase 1 implementation.

### TUI framework

Select after the language decision. The framework must support asynchronous
refresh, fuzzy filtering, deterministic model tests, status styling that does
not rely on color alone, and clean suspension or tmux switching.

### Direct terminal transport

Decide whether direct execution is required in the first release or whether
tmux is an explicit initial dependency of the interactive frontends.

### Legacy Claude history

Keep the native history fallback unless Claude publishes a structured listing
API. A one-time importer based on private files is out of scope by default.

### Surface adoption

Define how aggressively Switchboard should adopt agent terminals that were
started outside Switchboard, especially direct Ghostty windows without tmux.

### Distribution and hook installation

Decide whether releases provide packages, standalone archives, or both, and
whether hook configuration is generated by an explicit install command or
managed entirely by dotfiles.

## Proposed Decisions for Review

- The project name is Agent Switchboard.
- The core and TUI live in this repository.
- The DMS plugin remains a separate thin integration.
- Provider-native storage remains authoritative.
- SQLite is the initial registry; no daemon is required.
- The TUI is the first complete frontend.
- tmux is the default first transport, not a session identity.
- Codex uses tmux for runtime persistence.
- Claude uses its supervisor for runtime persistence and tmux only as a terminal
  surface.
- Hooks provide live status and reconciliation repairs stale state.
- The first milestone is local-only.
- Notifications, orchestration, transcript parsing, and destructive management
  are outside the first release.
