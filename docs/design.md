# Agent Switchboard Design

Status: Implementation baseline

Last updated: 2026-07-15

Related research: [Open-source product landscape](product-landscape.md)

Implementation evidence: [Phase 0 validation](phase-0-validation.md)

## Summary

Agent Switchboard is a local-first, project-aware session and context
switchboard for terminal coding agents. It presents Codex and Claude Code
conversations through one searchable session model, groups ongoing work under
stable projects, reports whether each session is working, needs input, is ready
for review, is ready for the next prompt, or is parked, and opens each session
through the provider's native resume or attach mechanism.

The project has a frontend-neutral core. DankMaterialShell (DMS) is the first
production consumer and proves the local launch and desktop handoff path. A
terminal UI follows on the resulting stable API. DMS, niri, shell, tmux, and
the TUI consume the same command and JSON interfaces rather than reimplementing
discovery or state tracking.

Agent Switchboard does not replace provider conversation storage or agent
execution. Codex and Claude Code remain the source of truth for transcripts,
history, authentication, and execution. Switchboard owns a durable project and
session index, normalized live status, concise explicit handoffs, attachment
surfaces, context routing, and action routing.

## Naming

The formal project name and technical namespace are **Agent Switchboard** and
`agent-switchboard`. Repository, package/distribution, configuration, and state
identifiers use the technical namespace. User-facing titles and ordinary prose
use the shorter **Switchboard** name after the project has been introduced.
The canonical executable is **`swbctl`**. It keeps a short Switchboard-specific
command without colliding with the established Linux `sbctl` command or the
crowded generic `agentctl` name. The short product name does not require a bare
`switchboard` package or command.

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

The user should not have to remember which host, checkout, terminal window,
tmux session, provider picker, or provider-specific command owns a
conversation. One manager should answer:

1. What sessions exist?
2. Which sessions are active?
3. What is each active session doing?
4. Which sessions need attention?
5. How do I open the exact session I selected?
6. Which project does this session belong to?
7. How do I start a focused new session with the right host, directory, and
   project context?

## Goals

- List Codex and Claude Code sessions through one model.
- Keep active and recent parked sessions searchable across configured hosts.
- Group sessions under stable user-defined projects without introducing a task
  or backlog model.
- Start a focused new session from project defaults or from an explicit prior
  handoff.
- Expose bounded project, session, and memory context to agents through stable
  tools so semantic work remains inside the selected agent session.
- Normalize useful status into working, needs input, completed/ready for
  review, ready, parked, and unknown states while tracking host reachability
  separately.
- Open an exact session without forcing the user through another picker when
  its identity is already known.
- Preserve provider-native history, resume, and background behavior.
- Preserve the unmodified Codex or Claude Code terminal UI after selection;
  Switchboard routes to the session and then leaves the interaction path.
- Serve as the expected entry point for newly launched managed sessions so each
  runtime receives a stable project and surface identity from startup.
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
- Proxy, capture, re-render, or synchronize the provider conversation stream.
- Require a persistent wrapper process around Codex or Claude Code.
- Retrofit fully routable surface identity into every live terminal that was
  started outside Switchboard.
- Replace Claude Code Agent View lifecycle controls.
- Maintain tasks, backlogs, assignments, milestones, or project plans.
- Orchestrate multiple agents, assign work, or dispatch prompts automatically.
- Create branches, worktrees, commits, or merges on behalf of a project.
- Add another notification system.
- Embed a terminal emulator in the TUI.
- Provide a standalone desktop, web, or mobile agent client.
- Synchronize transcripts or credentials between machines.
- Provide cloud execution or a web service.
- Stop, delete, archive, or mutate provider sessions in the first release.

## Review Resolutions

The pre-implementation design review produced the following binding changes:

- A leased `LaunchIntent` exists before every managed provider start and is
  atomically bound to the provider UUID by hooks or reconciliation.
- Open/new actions reserve and create waiting surfaces before returning a
  presentation plan; read-only plans cannot create runtimes.
- Project UUIDs are configuration-owned and globally stable across hosts, while
  each host contributes its own configured locations.
- Host reachability, runtime presence, provider resumability, activity/reason,
  and terminal attachment remain separate state axes.
- Claude uses one host tmux workspace with a manager window and exact session
  attachment windows; unobservable Agent View switches degrade surface binding,
  not session truth.
- Handoffs are immutable records, and continuation references the exact handoff
  used to start the next session.
- Provider preview/experimental contracts are capability-gated and supported
  only through versioned fixtures.
- DMS parity proves the local core before a full TUI or agent-tool layer is
  built.
- Presentation plans are shaped by caller capabilities: DMS may focus or
  launch desktop windows, while the TUI may act only on its current terminal or
  identified tmux client.

## Design Principles

### Provider-native ownership

Codex and Claude Code own conversation content and lifecycle semantics.
Switchboard stores only enough metadata to identify, classify, and open a
session.

### Native terminal interaction

Selecting a session ends in the provider's unmodified terminal UI. Switchboard
may resolve an action, focus Ghostty, attach tmux, or create a tmux surface, but
it does not remain between the user and Codex or Claude Code. It does not proxy
standard input/output, render conversation messages, or become authoritative
for the live conversation stream.

Managed launches execute the provider command directly in the selected surface.
Any bootstrap helper must replace itself with the provider process rather than
remaining as a wrapper. Provider-native MCP servers or plugins may expose
Switchboard tools from inside the normal TUI; they do not replace that TUI.

Switchboard is the expected launch entry point once installed. This is how a
session receives its project, location, launch intent, and stable surface token
before the provider starts. Provider-native parked history remains resumable;
an arbitrary live process started elsewhere is only a best-effort observation.

```text
                         prepare open/new action
                                  |
                 reserve or reuse exact tmux surface
                           /              \
                DMS handoff                TUI handoff
          focus niri window or       attach/switch the
          launch Ghostty window      current terminal/client
                           \              /
                    native codex or claude TUI
```

DMS owns desktop-window discovery, focus, and terminal launch. The TUI owns
only the terminal or tmux client in which it is currently running. In either
case, Switchboard leaves the interaction path after the handoff.

### One logical model, provider-specific actions

Frontends see one `AgentSession` model. Provider adapters decide how to
discover, resume, attach, and reconcile each kind of session.

### Projects are context and launch boundaries

A project groups related sessions and records where that work exists on each
host. It supplies launch defaults and context sources; it does not own tasks,
branches, worktrees, plans, or completion criteria. Under the recommended
workflow, one focused provider session represents one task, but Switchboard
does not enforce or model that convention.

### Deterministic core, agent-driven semantics

The core owns identity, state, validation, storage, and command routing.
Agents may propose session names, prepare concise handoffs, and query relevant
project history through explicit tools. Semantic output is attributable,
editable, and never required to list or open sessions.

### Context is retrieved on demand

Starting a session does not inject an entire project history. The agent receives
a project identity and can request stable project sources, active and recent
session handoffs, and optional memory search after it knows the current task.

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

**Project**
: A stable user-defined grouping for ongoing work, with one or more host-local
  locations, launch defaults, and context-source declarations.

**Project location**
: A checkout or working directory for a project on one host. Multiple
  locations may represent remote clones or local worktrees; Switchboard does
  not create or synchronize them.

**Session handoff**
: An optional concise summary and next action explicitly supplied by the user
  or current agent. Handoffs are immutable, versioned records rather than
  mutable fields on a session. They are not copied transcripts or inferred task
  state.

**Launch intent**
: A durable, short-lived record created before a provider process starts. It
  carries the selected project, location, action, and surface until a provider
  session ID exists and can be bound to them.

**Runtime**
: A live provider process or provider-supervised background job associated with
  a session.

**Surface**
: A terminal endpoint through which a user can interact with a runtime. The
  first implementation uses a tmux session/window/pane target.

**Provider workspace**
: An optional provider-specific tmux workspace that can contain manager and
  session surfaces. Claude uses one workspace per host so Agent View and exact
  attachment windows can share one desktop terminal workflow.

**Attachment**
: The current association between a provider session and a terminal surface.

**Parked session**
: A known durable session with no live provider runtime.

**Registry**
: Switchboard's durable local index of projects, sessions, runtime observations,
  surfaces, handoffs, and last-known remote snapshots.

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
                 project/session registry + reconciler
                                |
               swbctl command / JSON / agent tools
                  +-------------+-------------+
                  |             |             |
                 TUI        DMS plugin     agent session
                  |             |             |
              shell/tmux  niri adapter   MCP or CLI tools
```

The initial implementation is process-based rather than service-based:

- Hooks invoke a short-lived `swbctl event` command.
- Frontends invoke `swbctl list`, `swbctl reconcile`, and action commands.
- The TUI refreshes local state and runs bounded reconciliation while open.
- Each remote host maintains its own registry. Aggregators fetch a versioned
  snapshot over SSH.

When Switchboard frontends are closed, no host-wide Switchboard controller or
wrapper remains resident. The provider runtime, its native supervisor where
applicable, and tmux own the normal long-lived path. Hook handlers, remote
snapshots, and command actions are short-lived. If explicitly enabled, a stdio
agent-tool process may live only for the lifetime of the provider session that
launched it.

An optional indexing daemon can be considered later only if measured latency or
event fan-out justifies it. Core behavior must remain available without one,
and such a daemon must never proxy provider input/output or become a relay for
conversation content.

## Core Domain Model

### Project

```text
Project
  project_id
  name
  aliases
  default_provider
  default_transport
  context_sources
  created_at
  updated_at

ProjectLocation
  location_id
  project_id
  host_id
  path
  display_name
  repository_identity
  provider_override
  transport_override
  is_default
  last_observed_at
```

`project_id` is a globally stable user-owned UUID shared by every host that has
a location for the project. It is declared in configuration and is never
derived from a path, hostname, or Git remote. Each host normally declares only
its own locations; remote snapshots with the same `project_id` merge into one
logical project. Conflicting names or launch defaults for the same ID produce a
configuration error instead of silently creating divergent projects.

`location_id` is also a configured UUID so a path can move without changing
the location identity. Project-level defaults must agree across hosts;
provider or transport differences that are intentionally host-specific belong
on the location override. Aliases are unioned after sanitization, while
incompatible names or global defaults remain visible conflicts.

Project configuration is authoritative for identity, names, locations, launch
defaults, and context-source declarations. SQLite materializes configured
projects and retains runtime/session assignments, but it does not become a
second editable source for those fields. Removing a configured project marks
its materialized record undeclared; it does not erase historical sessions or
handoffs. `swbctl project add` performs an atomic structured edit of the
project catalog, and `--print-config` can emit the equivalent declaration for a
dotfiles workflow.

Discovery may suggest a project from Git roots, repository remotes, and
repeated session directories, but it does not silently promote every directory
into a durable project. `repository_identity` assists matching and diagnostics;
it is not the project key.

A session observed without a launch intent may be assigned to an existing
project only when its canonical cwd has one unambiguous configured location:

1. Normalize the configured and observed paths to absolute paths and resolve
   symlinks where the paths exist.
2. Select configured locations that contain the cwd on the same host.
3. Choose the longest matching location path.
4. If equally specific matches disagree, leave the session unassigned and
   report the ambiguity.

This permits Claude Agent View dispatches and ordinary provider launches inside
known repositories to join the right project without manufacturing projects or
using a Git remote as identity. The assignment records
`metadata_source=location_match` and remains user-editable.

A project can contain multiple locations on one host, including worktrees. A
new session chooses a concrete location before launch. Switchboard may warn
when multiple live sessions share one mutable checkout, but concurrency and
worktree policy remain the user's and agents' responsibility.

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

### LaunchIntent

The provider session ID does not exist when a new Codex or Claude session is
requested. A managed launch therefore starts with this record:

```text
LaunchIntent
  launch_id
  request_id
  host_id
  provider
  action                 new | resume | attach | manage
  project_id
  location_id
  cwd
  source_handoff_id
  target_session_key
  surface_id
  transport
  state
  lease_owner
  capability_hash
  created_at
  expires_at
  failure_code
```

`launch_id` is generated by Switchboard and is passed to the bootstrap and
provider process through `AGENT_SWITCHBOARD_LAUNCH_ID`. For a new conversation,
`target_session_key` is absent until `SessionStart` supplies the provider UUID.
The hook atomically creates or updates `AgentSession`, binds it to the launch's
project and location, updates the surface metadata, and marks the intent
`bound`.

For resume or attach, the hook-provided provider UUID must match
`target_session_key`. A mismatch marks the intent failed with
`provider_identity_mismatch`, leaves both provider records intact, and prevents
the surface from being presented as a confirmed binding.

The launch states are:

```text
reserved -> surface_ready -> waiting_for_client -> provider_started -> bound
    |             |                  |              +-----> manager_ready
    |             |                  |                    |
    +-------------+------------------+--------------------+-> failed | expired
```

`manage` is used for a provider manager such as `claude agents`. It creates a
`provider_manager` surface with no target session and reaches `manager_ready`
through process reconciliation rather than a session-binding hook.

Intents use bounded leases. A failed terminal launch, provider startup, or hook
binding does not leave an indefinite reservation. Expired intents and empty
surfaces are reclaimed by reconciliation, while a bounded record is retained
for diagnostics. Provider discovery may bind a started intent after a missed
hook when the surface, process, and provider ID can be correlated safely.

### AgentSession

```text
AgentSession
  key
  project_id
  location_id
  provider
  provider_session_id
  name
  purpose
  cwd
  host_id
  created_at
  provider_updated_at
  last_activity_at
  first_observed_at
  last_observed_at
  runtime_presence
  resumability
  activity
  activity_reason
  attachment
  runtime_locator
  surface_id
  metadata_source
  state_confidence
  state_observed_at
  latest_handoff_id
  wrapped_at
  continued_from_handoff_id
  pinned
```

Names and working directories are display metadata. They are not used as keys
and may change. Project assignment, purpose, handoff references, lineage,
wrapping, and pinning are optional curation metadata. They do not alter
provider-native session identity or execution state.

`wrapped_at` means the user or current agent considers the conversation a
completed handoff. It does not delete provider history. Resuming a wrapped
session clears the wrapped state while retaining every prior handoff.

### Handoff

```text
Handoff
  handoff_id
  session_key
  sequence
  summary
  next_action
  source             user | agent | imported
  source_host_id
  created_at
  content_hash
```

Handoffs are append-only. `latest_handoff_id` is a convenience pointer, not the
record itself. A continued session references the exact source handoff so later
updates to the source conversation cannot rewrite the context from which it was
started. Size limits and control-character filtering apply before storage or
transport.

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
  provider
  transport
  transport_locator
  role                  session | provider_manager
  current_session_key
  binding_confidence
  launch_id
  created_at
  last_observed_at
  client_attached
```

`current_session_key` is mutable and nullable. A manager surface has no current
session. If a Claude terminal switches from session A to B, the surface stays
the same while its session association changes. A binding learned only from an
old hook is not sufficient to focus an exact session; opening requires current
provider attachment evidence, process correlation, or a fresh managed attach.
`provider` is explicit because a waiting surface has no provider session ID yet
and a `provider_manager` surface never binds a session from which provider
identity could be derived.

## State Model

Status is represented on separate axes so host connectivity, provider
retention, process liveness, attention, and presentation do not become
conflated.

### Host reachability

- `online`: the owning host answered the current local query.
- `offline`: the aggregator could not currently query the owning host.
- `unknown`: no current reachability observation exists.

Reachability belongs to the host snapshot or cache entry, not `AgentSession`.
An offline cache preserves the session's last-known runtime and activity state
alongside the snapshot observation time.

### Runtime presence

- `live`: a provider runtime is confirmed alive.
- `stopped`: no provider runtime is alive.
- `unknown`: available evidence is incomplete or contradictory.

### Resumability

- `resumable`: the provider still reports durable history or a tested resume
  path exists.
- `missing`: the provider no longer reports a resumable conversation.
- `unknown`: resumability has not been established.

`parked` is a derived display state for `stopped + resumable`; it is not a
runtime-presence value.

### Activity

- `working`: the provider is processing the current turn.
- `needs_input`: the provider is blocked on a permission, question, or explicit
  user decision.
- `ready`: a live provider is waiting for the next prompt.
- `completed`: the provider reports finished work that remains available for
  review, even if it has already stopped the worker process.
- `unknown`: activity cannot be established.

`activity_reason` is structured where evidence permits. Initial values are
`permission`, `question`, `elicitation`, `turn_complete`, `provider_complete`,
`error`, and `unknown`. Provider adapters may expose a more specific reason
without changing the primary activity value.

### Attachment

- `attached`: a user-facing terminal client is currently associated.
- `detached`: a surface or provider runtime exists without an attached client.
- `none`: no terminal surface exists.
- `unknown`: attachment cannot be established.

### Display status

Frontends derive one primary label using this precedence:

```text
offline host
  -> needs input
  -> working
  -> completed / ready for review
  -> ready
  -> parked
  -> unavailable
  -> unknown
```

The offline label includes snapshot age and does not erase the last-known
status. `unavailable` means `stopped + missing`. Attachment is displayed
separately. Examples include `working, detached` for a Claude background agent,
`completed, detached` for a supervisor-stopped result, and `ready, attached` for
Codex waiting in Ghostty.

### Hook-driven transitions

The initial normalized transitions are:

```text
SessionStart       -> runtime_presence=live, activity=ready
UserPromptSubmit   -> runtime_presence=live, activity=working
PermissionRequest  -> runtime_presence=live, activity=needs_input,
                      activity_reason=permission
PostToolUse        -> runtime_presence=live, activity=working
Stop               -> runtime_presence=live, activity=ready,
                      activity_reason=turn_complete
SessionEnd         -> provisional runtime_presence=stopped
```

Provider-native state and liveness reconciliation may override provisional hook
state. In particular, a Claude session can detach from a terminal while
remaining live under the supervisor, and a completed Claude session may remain
`completed` after its worker process stops.

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

Every adapter returns a versioned capability report containing the provider
version, tested-contract range, discovered features, schema fingerprint where
available, and structured degraded reasons. Adapters must tolerate missing
providers and version-specific capability gaps. The core exposes those gaps to
frontends instead of fabricating support.

The initial fixture baseline is the locally verified Codex `0.144.4` and Claude
Code `2.1.210`, but these are not permanent pins. Codex app-server schemas are
generated from each supported CLI version and contract-tested. Claude Agent
View and supervisor discovery are capability-gated because administrators can
disable them and their preview interface may change.

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
reliable end-of-session signal as Claude, so reconciliation changes
`runtime_presence` to `stopped` when its recorded process is gone and no
replacement runtime is found. The session displays as parked only when its
history remains resumable.

### Opening

- A live session with an existing tmux surface is focused or attached.
- A live process outside a managed surface is attached only when a trustworthy
  existing tmux locator is available. Otherwise the frontend reports that it is
  live but unroutable and does not start a duplicate runtime.
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
blocked                  -> live, needs_input, reason=unknown
done with a live PID     -> live, completed
done without a live PID  -> stopped, completed, resumable
known resumable session  -> stopped, unknown activity, resumable
```

Hooks cover interactive sessions and provide faster transitions than polling.
A permission or question hook may enrich a blocked observation with a specific
reason, but supervisor JSON alone does not expose that distinction.

### Workspace and opening

The first release preserves the existing one-workspace-per-host Claude
workflow. The default tmux layout is:

```text
claude workspace: as-claude-<host-id-prefix>
  manager window: claude agents
  attach windows: claude attach <short-runtime-id>
  resume windows: claude --resume <provider-session-id>
```

The workspace is a transport container, not a provider runtime. Agent View and
background workers remain owned by Claude's supervisor. Exact attachment
windows are created only when the selected session is not already confirmed in
a managed surface. They let one Ghostty/tmux workspace contain several exact
session views without opening a separate desktop terminal for every
conversation.

- An interactive session is focused only when current supervisor attachment
  evidence or process correlation confirms its surface.
- A background session with a short runtime ID uses `claude attach <id>`.
- A parked known session uses `claude --resume <uuid>`.
- A new interactive session runs `claude` in the selected working directory.
- `Open Claude workspace` selects or creates the manager window.

Claude's supervisor is the persistence backend for background sessions. A tmux
surface containing `claude attach` is only a terminal view. Switchboard does
not create tmux workspaces or windows merely to keep Claude background work
alive.

On Claude Code 2.1.210, pressing Left from an interactive TUI leaves that
process as the Agent View manager and creates a separate background runtime
with its own provider session UUID and short runtime ID. The manager's
interactive supervisor row is not the background session shown as current and
must never inherit its binding.

### In-terminal session switching

Interactive `/resume` switching on Claude Code 2.1.210 has the observed hook
sequence:

1. Claude emits `SessionEnd` for A with the resume/switch reason.
2. Claude emits `SessionStart` for B.
3. The hook inherits the stable Switchboard surface identifier.
4. The registry removes A's surface association and updates A from supervisor
   evidence rather than assuming it stopped.
5. The registry associates the existing surface with B.

Agent View detach/attach does not emit that sequence in the tested version. A
provider-manager surface has no session binding. An exact attachment is
confirmed while a managed pane's argv contains `claude attach <short-id>` and
that short ID matches live supervisor evidence. The attach command itself did
not emit a lifecycle hook. Reconciliation otherwise clears the binding or
marks it unknown. An unconfirmed old binding is never used to focus an exact
session. The TUI and DMS continue to show separate rows for provider sessions
plus one explicit workspace action.

## Event Ingestion

Both provider hook configurations invoke a fast command:

```text
swbctl event --provider <provider>
```

The provider event JSON is read from standard input. The handler:

1. Validates the provider and event schema.
2. Extracts only identity, lifecycle, cwd, and timing fields.
3. Reads `AGENT_SWITCHBOARD_LAUNCH_ID`,
   `AGENT_SWITCHBOARD_SURFACE_ID`, and tmux environment metadata when present.
4. Applies the normalized transition and launch binding in one database
   transaction.
5. Exits without network access or provider queries and writes no stdout that
   could be injected into provider context.

Hooks must not delay the agent loop. They never contact remote hosts, launch a
frontend, or wait for tmux. Provider-specific hook trust and enablement remain
visible through `swbctl doctor`.

Switchboard hooks coexist with provider plugins such as claude-mem and do not
replace other matching hooks. Installation uses one identifiable user-level
definition per provider. `doctor` detects duplicate Switchboard definitions,
stale trusted hashes, a missing absolute executable, and hook latency above the
configured budget.

Claude Agent View workers spawned from a manager did not inherit a one-off
`--settings` file in the 2.1.210 spike. User-level hook installation is
therefore required for background-worker coverage; launch-only additional
settings are not a supported substitute.

Event writes include an observation timestamp, provider turn/session
identifier where available, source priority, and an idempotency key derived
from the normalized event fields. Older or duplicate events cannot overwrite
newer authoritative observations. Event-kind precedence resolves the small
window where hook processes from one turn complete out of order. A bounded
event log may be retained for diagnostics, but prompts and transcript content
are never stored.

## Registry and Reconciliation

### Storage

The initial implementation uses SQLite because hooks can write
concurrently while the TUI or DMS reads, and because atomic updates, indexes,
and schema migrations are useful without requiring a daemon.

```text
${XDG_STATE_HOME:-~/.local/state}/agent-switchboard/switchboard.db
```

The database contains:

- hosts and their stable IDs
- materialized configured projects, project locations, and launch/context
  preferences
- provider sessions and display metadata
- launch intents, leases, and bounded launch diagnostics
- explicit session purposes, immutable handoffs, wrapping, pinning, and lineage
- last normalized runtime/resumability/activity/attachment state
- runtime observations
- surfaces and their current session association
- bounded hook event metadata
- cached snapshots from remote hosts
- schema and protocol versions

It does not contain prompts, transcript bodies, authentication tokens, copied
provider configuration, or automatically harvested model output. A concise
handoff explicitly submitted through the session tools is permitted and is
identified by its source.

SQLite runs in WAL mode with a bounded busy timeout. Hook writes are short
transactions. Schema migrations are explicit and backward compatibility is
covered by tests.

### Reconciliation

Reconciliation repairs the materialized registry from current evidence:

1. Query provider-native session and runtime data.
2. Inspect known process and tmux locators.
3. Mark dead runtimes and release stale surface associations.
4. Merge newly discovered provider sessions.
5. Bind recoverable launch intents and expire abandoned leases/surfaces.
6. Preserve stopped registry records that remain resumable.
7. Record capability or provider errors without deleting known sessions.

Local reconciliation can run on TUI startup, on explicit refresh, and at a
bounded interval while a frontend is open. Remote reconciliation runs on the
remote host, not over a shared database.

## Surface and Transport Model

### tmux transport

tmux is the default first transport. It provides a consistent way for a TUI,
plain shell, SSH client, or desktop terminal to reach a session.

A managed Codex surface uses an opaque stable tmux session name rather than
user-controlled session text. Claude surfaces are windows inside one opaque
host workspace:

```text
Codex tmux session:    as-<surface-id-prefix>
Claude tmux workspace: as-claude-<host-id-prefix>
Claude window:         as-<surface-id-prefix>
```

Switchboard stores metadata as tmux user options at the narrowest available
scope:

```text
@agent_switchboard_surface_id
@agent_switchboard_session_key
@agent_switchboard_provider
@agent_switchboard_launch_id
@agent_switchboard_surface_role
```

Before a new provider UUID exists, the surface carries `launch_id` instead of a
session key. The binding hook replaces that provisional metadata atomically.
The full locator includes tmux session, window, and pane IDs. Discovery must
tolerate user-created windows, panes, and layouts.

The surface starts a short-lived `swbctl bootstrap <launch-id>` process. The
bootstrap waits until the target surface has a real viewing client so terminal
capability and color probes observe the actual terminal. For a one-window Codex
tmux session, `session_attached > 0` is sufficient. For a Claude workspace, at
least one attached client must currently select the target window; a client
viewing another workspace window does not release the bootstrap. The bootstrap
then performs one final target-session liveness check and replaces itself with
the adapter's native `codex` or `claude` argv. tmux persists the provider
process and terminal state; Switchboard does not keep a wrapper alive after
startup.

If no client attaches within the configured launch timeout, the bootstrap marks
the intent expired and exits. Reconciliation removes the empty surface. This
preserves the proven DMS attach-before-provider behavior without creating a
persistent wrapper.

The default policy is at most one confirmed managed session surface per logical
session on one host. A Claude manager surface is exempt because it is not bound
to a provider session. The surface identity remains stable if an observable
in-terminal provider action changes the conversation associated with it.
SQLite enforces uniqueness for confirmed managed bindings. An unknown binding
does not reserve a session, and reconciliation unbinds rather than guesses when
two observations cannot be ordered safely.

### Attachment behavior

When opening a tmux surface:

- Inside tmux, select the target window/pane and use `switch-client` when the
  target is in another tmux session.
- Outside tmux, attach the current terminal to the target session.
- From a desktop frontend, focus an existing terminal client when possible;
  otherwise launch a terminal that attaches to the target.

The normal Claude workspace policy expects zero or one managed desktop client.
With exactly one attached client, the core may switch that client to the target
window and DMS then focuses the workspace's niri window. With multiple clients,
Switchboard switches only a client that the integration can identify
unambiguously; otherwise it leaves existing clients untouched and launches a
new exact attachment. It never changes every attached client as a shortcut.

The core does not assume Ghostty or niri. Desktop-specific focus and launch
behavior belongs to an integration adapter.

Finding an existing desktop window is therefore not a TUI operation. A TUI has
no portable identity for other terminal-emulator windows and does not ask
Ghostty to create one. It either switches its identified current tmux client or
attaches its current terminal in place. DMS can instead correlate opaque
surface/workspace tokens with niri windows and launch a new configured terminal
when no matching window exists.

### Direct transport

Direct execution is excluded from the first release. Interactive frontends
require tmux so launch identity, persistence, exact attachment, and duplicate
prevention have one tested transport contract. A future direct transport would
need its own durable surface and desktop-window identity rather than weakening
the tmux invariants.

## Launch Preparation

Opening or creating a session is split into atomic preparation and
desktop/terminal presentation:

```text
prepare_launch(LaunchRequest, PresentationContext, request_id)
  -> PresentationPlan
```

`LaunchRequest` selects `open`, `new`, or `manage`. An open request contains a
target session key. A new request contains provider, project, concrete
location, and an optional source handoff. A manage request selects a supported
provider workspace action and has no target session. All use the same intent,
lease, surface, bootstrap, and presentation machinery.

`PresentationContext` describes only what the caller can do with the prepared
surface:

```text
PresentationContext
  has_current_terminal
  current_tmux_client     optional opaque client ID
  can_focus_desktop
  can_launch_terminal
```

A normal TUI supplies a current terminal and, when running inside tmux, its
current client ID. It does not advertise desktop focus or terminal launch. DMS
advertises the desktop capabilities supplied by its niri and configured
terminal integration and does not claim a current terminal. The context is
request-scoped, is never persisted as session truth, and does not make a
caller-supplied tmux client authoritative; action commands revalidate opaque
client IDs before switching them.

A `PresentationPlan` is one of:

- focus an existing surface
- attach or switch to an existing tmux target
- attach or switch to a newly prepared waiting surface
- report a blocked action with a concrete reason

```text
PresentationPlan
  kind                  focus | switch | attach | blocked
  host_id
  surface_id
  workspace_id
  tmux_target
  tmux_client
  desktop_token
  lease_expires_at
  error
```

Fields that do not apply to a plan kind are absent. `tmux_client` is returned
only when one client was identified unambiguously. `desktop_token` is generated
by the core from stable workspace/surface identity but carries no desktop
window semantics; the configured integration decides how to match it.

The returned kind must be executable by the supplied presentation context. A
caller without `can_focus_desktop` never receives `focus`. A `switch` targets
the caller's revalidated `current_tmux_client`, or an unambiguous managed
desktop client for a caller that can focus its window. A caller without a
current terminal or `can_launch_terminal` never receives `attach`. When no safe
handoff is available, preparation returns `blocked` rather than guessing at a
window or changing an unrelated tmux client. These capability checks shape the
presentation plan; they do not weaken launch reservation or duplicate-runtime
prevention.

Preparation performs bounded provider/runtime reconciliation, then opens a
SQLite `BEGIN IMMEDIATE` transaction. It re-reads the current mapping and
either returns a healthy existing surface or inserts a leased `LaunchIntent`.
A partial unique constraint permits only one active or pending launch for a
target session. Provider-manager launches are similarly unique by host,
provider, and manager role. After commit, the core creates the waiting tmux
surface and updates the intent. Creation failure marks the intent failed and
releases the claim. The bootstrap revalidates once more before `exec` so an
externally started runtime cannot be duplicated during the presentation delay.

`request_id` is unique per host and makes frontend retries idempotent. It binds
the normalized `LaunchRequest`, not the request-scoped `PresentationContext`.
A retry may advertise different presentation capabilities and receive a newly
shaped plan for the same reserved surface, but it cannot create a second
intent. Reusing one request ID with a different normalized launch request
returns `request_conflict`; it never mutates the original intent. A read-only
resolver may exist for previews, but its result is advisory and cannot
authorize surface creation. Only launch preparation can create or reserve a
surface.

The returned plan contains a stable surface ID and structured locator fields.
It never contains an interpolated shell command or raw provider argv. Provider
argv remains on the owning host inside the launch intent.

The TUI can attach or switch tmux directly using its current terminal context.
DMS consumes the same presentation plan but delegates niri focus and Ghostty
launch behavior to its integration code. If no client successfully views the
target surface, the waiting launch expires without starting the provider. A
desktop-focus failure after a successful tmux client switch does not undo that
switch or kill the provider; the frontend reports the focus failure and the
session remains routable.

For a `switch` plan, the caller invokes `select-surface` on the owning host.
That command revalidates that the opaque tmux client still belongs to the
expected workspace and selects only that client. A desktop integration then
focuses the matching window; a TUI is already operating its current client and
performs no desktop action. For an `attach` plan, the current or newly launched
terminal runs `attach-surface`. Neither command accepts a raw tmux target from
the frontend.

## Command Interface

The canonical executable is `swbctl`. Its command surface grows in phases
toward these stable operations:

```text
swbctl list [filters] [--refresh] [--json]
swbctl show <session-key> [--json]
swbctl project list [--json]
swbctl project show <project-id> [--json]
swbctl project add --name <name> --location <path> [--id <uuid>]
                     [--print-config]
swbctl project context <project-id> [--query <text>] [--json]
swbctl reconcile [--host <host>]
swbctl refresh [--host <host>]
swbctl new [--project <project-id>] [--location <location-id>]
             [--provider <provider>] [--cwd <path>]
swbctl new --from <handoff-id>|<session-key> [--provider <provider>]
swbctl prepare-new [--project <project-id>] [--location <location-id>]
                     [--provider <provider>] [--from <handoff-id>]
                     --request-id <uuid> --json
swbctl current [--json]
swbctl session name [<session-key>|--current] <name>
swbctl session handoff [<session-key>|--current] --json-stdin
swbctl session wrap [<session-key>|--current] --json-stdin
swbctl session pin <session-key> [--off]
swbctl prepare-open <session-key> --request-id <uuid> --json
swbctl open <session-key> [--transport tmux]
swbctl prepare-workspace <provider> --request-id <uuid> --json
swbctl workspace open <provider>
swbctl select-surface <surface-id> --client <tmux-client-id>
swbctl attach-surface <surface-id>
swbctl event --provider <provider>
swbctl snapshot [--reconcile none|live|full] --json
swbctl doctor
```

`bootstrap` and launch-intent cleanup are internal commands, not normal user
entry points. `open`, `new`, and `workspace open` combine their prepare
operation with terminal-local presentation; desktop integrations call the
corresponding prepare command and perform only the returned focus, switch, or
attach action.
Every prepare operation receives the versioned `PresentationContext` described
above. Combined terminal-local commands may derive it from their process and
tmux environment; integrations send it as structured request data. The exact
CLI flag or standard-input encoding remains part of command usability design,
not the core contract.
When `new --from` receives a session key, preparation resolves and stores that
session's latest handoff ID atomically; the launch never retains a floating
"latest handoff" reference.

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
  "protocolVersion": 1,
  "generatedAt": 0,
  "host": {
    "hostId": "...",
    "displayName": "..."
  },
  "projects": [],
  "locations": [],
  "sessions": [],
  "runtimes": [],
  "surfaces": [],
  "capabilities": [],
  "errors": []
}
```

Unknown fields must be ignored by clients. Incompatible schema versions produce
an explicit error. Host-local snapshots do not contain `offline` session state;
the aggregator records reachability, receipt time, and staleness around the
validated snapshot. Snapshots expose opaque surface IDs and state but not raw
provider argv or credentials. Project records with the same globally stable ID
are merged only after conflict validation.

Snapshot and cache validation is fail-closed at the privacy boundary. Known
project, location, session, runtime, and surface records require their stable
identity fields and must agree with the envelope host and cross-record
references. Incompatible schema or protocol versions, prompts, transcripts,
raw provider or hook payloads, raw argv, credentials, authentication tokens,
and terminal control characters are rejected before caching. Safe additive
fields from a newer compatible sender are accepted for forward compatibility
and discarded during canonicalization.

Machine-readable errors use stable codes rather than requiring clients to
parse prose:

```text
ErrorRecord
  code
  message
  scope                 host | project | provider | session | launch | surface
  host_id
  provider
  session_key
  retryable
  observed_at
  details
```

`details` is versioned structured data with no credentials or raw provider
payload. Human-readable wording may change without breaking frontend routing.

## Project Context and Agent Tools

Project context is a structured, bounded view assembled from sources with
different authority and freshness:

```text
stable     configured files such as AGENTS.md, README, and selected docs
live       active sessions, locations, host availability, and observed Git state
recent     explicit purposes and handoffs from active or recently wrapped sessions
historical optional memory search and provider-native history references
```

There is no single project-level task status or next action. Concurrent
sessions can have independent purposes and next actions. Context responses
preserve source, host, observation time, and staleness so an agent can decide
what is relevant.

The same core operations are exposed to agents through a small MCP server,
provider plugin, or equivalent structured CLI. The initial read-mostly tool
surface is:

```text
project_get_current()
project_list_sessions(project_id, filters?)
project_get_context(project_id, query?)
session_get_handoff(session_key)
session_list_handoffs(session_key)
session_search(project_id, query)
memory_search(project_id, query)
session_set_name(current_session, name)
session_set_handoff(current_session, summary, next_action)
session_wrap(current_session, summary, next_action)
```

Mutating agent tools are restricted to the calling session's curation fields.
They cannot launch, stop, attach to, archive, or send prompts to other
sessions. `memory_search` is an optional adapter; claude-mem is the first
target, but core session routing remains usable when it is absent.

`session_search` searches Switchboard metadata, purposes, and explicit
handoffs; it does not search provider transcripts. Historical transcript search
belongs to `memory_search` or a future documented provider adapter. Every
context result is bounded and includes source, host, record ID, observation
time, and staleness.

Managed launches generate a random session-scoped capability whose hash is
stored with the launch. Optional MCP/plugin tools receive the capability and
surface ID through their process environment. Agent mutations require the
capability and the surface's current confirmed binding. This is a same-user
guardrail and attribution mechanism, not a defense against the local account
owner. Human `swbctl ... --current` commands may instead resolve through the
current tmux pane metadata.

An optional provider skill can guide the current agent to prepare a concise
handoff and invoke `session_wrap`. Switchboard stores the supplied result; it
does not decide that the underlying work is complete.

## Terminal UI

The existing DMS picker is the first production consumer during migration. A
small TUI follows once local discovery and atomic open preparation pass parity
tests. It exercises the same public core contract and does not import provider
implementation details directly.

### Primary view

The default view presents:

- project
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
3. completed / ready for review
4. ready
5. recently parked
6. offline or unknown

All known sessions remain searchable. An empty query may limit parked rows to a
configurable recent count so active work stays scannable.

The TUI can group sessions by project. A project row exposes its available
locations and active, parked, pinned, and recently wrapped sessions. An
unassigned group retains ad-hoc and legacy sessions.

### Initial actions

- Open the selected session.
- Start a new Codex or Claude session from a project or explicit cwd.
- Start a new focused session from a prior handoff.
- Filter by project, provider, host, state, and working directory.
- Pin, name, hand off, or wrap a session without managing tasks.
- Refresh/reconcile.
- Open the provider-native history picker when Switchboard cannot enumerate old
  provider history.
- Open the host's Claude workspace/Agent View independently of any session row.
- Inspect an error or degraded capability.

Destructive actions are excluded from the first release.

### tmux entry points

The TUI must work as:

- a normal terminal command
- a tmux popup
- a dedicated tmux manager session

Selecting a row transfers the current client to the target surface. The TUI
does not render or proxy the agent terminal stream.

From a plain terminal, selection closes the picker and attaches that terminal
to the selected surface in place. From inside tmux, including a popup or a
dedicated manager session, selection targets only the current tmux client and
uses window selection or `switch-client` as appropriate. The first TUI does not
search for existing Ghostty/niri windows, focus a different desktop window, or
launch a new OS terminal window. Those are DMS integration capabilities, not
portable terminal behavior.

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
ssh <target> swbctl snapshot --json
```

The remote command reads its host-local registry, writes one versioned JSON
document, and exits. It does not connect back to the caller, contact another
host, or recursively aggregate its own configured remotes.

When the caller needs stronger liveness evidence, it can request bounded remote
reconciliation in the same process:

```text
ssh <target> swbctl snapshot --json --reconcile live
```

Full provider history discovery is requested less frequently:

```text
ssh <target> swbctl snapshot --json --reconcile full
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
- marks the host cache offline without overwriting last-known session state
- keeps connection targets separate from stable host IDs and display aliases

Frontends render cached rows immediately and refresh stale hosts
asynchronously. A remote failure therefore does not delay local results or make
known sessions disappear. The UI exposes snapshot age whenever data is stale or
offline.

`swbctl list` is the frontend-facing merged view. It reads the local registry
and cached remote snapshots. `swbctl refresh` performs remote pulls and
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

A desktop frontend first prepares the action noninteractively on the owning
host:

```text
ssh <target> swbctl prepare-open <session-key> \
  --request-id <uuid> --json
```

The remote command returns a structured error, an existing surface, or a newly
prepared waiting surface. Only after success does the frontend focus an
existing local terminal or launch the configured terminal around:

```text
ssh -t <target> swbctl attach-surface <surface-id>
```

`attach-surface` revalidates the surface and any pending lease, then replaces
itself with an exact tmux attach/current-client switch operation. The provider
bootstrap starts only after a client views the target surface. A failed
preparation therefore does not flash a disposable Ghostty window, and
concurrent local/remote frontends share the same launch reservation.

When preparation returns a `switch` plan for one already attached remote tmux
client, DMS first runs:

```text
ssh <target> swbctl select-surface <surface-id> --client <opaque-client-id>
```

After the owning host confirms the switch, DMS focuses the existing local
Ghostty/niri window associated with that host workspace. If the client changed
or disappeared, the command returns a structured stale-plan error and DMS may
prepare again; it does not guess another client.

New-session and other future mutating actions follow the same rule: send an
explicit command to the owning host, validate there, and return structured
errors. They do not mutate cached remote rows locally.

For cross-host `new --from`, the initiating host resolves an exact immutable
handoff and sends a bounded JSON envelope on SSH standard input to the target.
The envelope contains the handoff ID, source session/host, project ID, summary,
next action, timestamp, and content hash. SSH authenticates the caller; the
target validates size, schema, hash, and that it has a configured location for
the project before recording an imported handoff and launch intent. No target
host reaches back to the source and no transcript content is transferred.

```text
ssh <target> swbctl prepare-new --request-id <uuid> --json-stdin --json
```

The request envelope also selects the target's configured location. A local
frontend then presents the returned surface through the same
`attach-surface` path used by remote open.

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
ssh <target> swbctl watch --jsonl
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

- invoke `swbctl list --json`
- present project groups and project-aware new-session actions
- translate sessions into DMS launcher items
- invoke atomic open/new preparation
- focus a matching niri terminal window when one exists
- launch the configured terminal when no matching client exists
- expose DMS-specific appearance and refresh settings

Provider discovery, SSH host configuration, status transitions, and tmux
creation move out of QML and the DMS plugin helper into Agent Switchboard.

niri and Ghostty identity use the opaque tmux workspace/surface token supplied
by the core rather than a provider session name. A Claude desktop window is
matched to its host workspace; exact tmux window selection remains a transport
action inside that workspace.

For each open or new action, DMS supplies a presentation context with desktop
focus and configured-terminal launch capabilities. On `focus`, it resolves the
opaque desktop token and focuses the matching niri window. On `switch`, it
invokes `select-surface` for the returned tmux client and then focuses that
client's window. On `attach`, it launches a new Ghostty window whose command is
the structured `attach-surface` operation. The core neither enumerates niri
windows nor launches Ghostty itself.

## Configuration

Core configuration belongs under:

```text
${XDG_CONFIG_HOME:-~/.config}/agent-switchboard/config.toml
```

The file is optional. If this implicit default path is absent, Switchboard
behaves as though it read an empty TOML document and uses documented defaults;
it does not create the file. An explicitly supplied missing path, an unreadable
file, malformed TOML, or invalid values remain errors. Stable host identity and
the registry are state rather than configuration and are created lazily under
the XDG state directory when an operation needs them.

Expected settings include:

- local display name
- remote SSH targets and display aliases
- globally stable project IDs, names, host-local locations, and defaults
- configured stable context sources
- provider enablement and executable overrides
- default transport
- tmux naming prefix and behavior
- refresh and staleness intervals
- recent parked-session limit
- default working-directory selection behavior

Illustrative host-local configuration:

```toml
[host]
display_name = "starship"

[projects."7e5945a5-39e0-48b0-a0c1-a1599b32af93"]
name = "cubey"
default_provider = "codex"
default_transport = "tmux"
context_sources = ["AGENTS.md", "README.md", "docs"]

[[projects."7e5945a5-39e0-48b0-a0c1-a1599b32af93".locations]]
location_id = "f246cc26-bb02-4fd7-b00d-d3b834f932ec"
display_name = "starship checkout"
path = "/home/bryan/code/cubey"
is_default = true

[remotes.snap]
ssh_target = "snap.lan"
display_name = "snap"
```

Another host declares the same project UUID and a different location UUID/path.
The UUIDs, project name, and global defaults are shared; host-local location
overrides may differ.

The generated host UUID is stored separately under the state directory with
mode `0600` at `.../agent-switchboard/host-id`; it is not regenerated from the
hostname and is not normally edited in shared configuration. Hosts
participating in the same project use the same configured `project_id` while
declaring their own local paths. Chezmoi may render those host-local location
blocks from one shared project catalog.

Frontend appearance and keybindings do not belong in core configuration.

Configuration and registry schemas are separate. Removing a remote target does
not immediately erase its cached session records; cleanup is explicit.

## Failure Handling

### Missing or incompatible provider

Expose provider capability state and an actionable diagnostic. Other providers
remain usable.

### Hooks unavailable

Discovery and open actions continue. Live activity is marked unknown or derived
from weaker native evidence. `swbctl doctor` explains the missing hook or
trust requirement.

### Stale runtime

Process and provider reconciliation marks runtime presence stopped and releases
stale surface mappings. The display becomes parked only if the provider session
remains resumable. Codex must not remain active indefinitely because it lacks
an end event.

### Offline remote host

Retain the last snapshot and its session states, mark the host cache offline
with its observation time, and reject open preparation before launching a
terminal.

### Provider schema change

Adapters validate required fields and report a capability error. Raw malformed
data is not written into the registry as authoritative state.

### Duplicate surface

Atomic launch leases and a partial unique constraint select one winner. Prefer
an existing healthy surface, return the same result for an idempotent retry,
and retire stale mappings. Never start a second interactive process merely
because the frontend cache is old.

### Launch never attaches or binds

The waiting bootstrap expires without starting the provider, marks the intent
failed or expired, and exits. Reconciliation removes the empty surface. If the
provider started but its hook was missed, bounded provider/process correlation
may bind the intent; otherwise the runtime remains visible as unbound and is
never duplicated automatically.

### Project definition conflict

Snapshots containing the same `project_id` with incompatible identity fields
are retained under their owning hosts but are not merged. Frontends show a
configuration error with both sources. Session data is never discarded to
resolve the conflict.

### Missing project location

Retain the project and its known sessions, but reject a new launch on a host
where no configured location currently exists. Do not guess a checkout path or
clone a repository automatically.

### Memory provider unavailable

Return project metadata and explicit handoffs without historical memory
results. Report the optional adapter error without blocking list, open, or new
session actions.

## Security and Privacy

- Do not store prompts, transcript bodies, credentials, or automatically
  harvested model output. Explicit concise handoffs are allowed.
- Treat provider names, cwd values, and remote metadata as untrusted display
  input; remove terminal control characters.
- Build commands as argv arrays and validate provider IDs, session IDs, tmux
  locators, and SSH targets.
- Use request IDs and unguessable launch capabilities; store capability hashes,
  not bearer values.
- Keep the registry and event files user-readable only.
- Hooks perform local database writes only and must return quickly.
- Preserve normal SSH host-key verification and use batch mode for background
  discovery.
- Require explicit provider hook trust through the provider's supported trust
  mechanism.
- Do not silently install hooks, alter provider settings, or enable remote
  access from a frontend action.
- Use an absolute installed `swbctl` path in generated hook configuration and
  verify noninteractive local/SSH PATH behavior in `doctor`.

## Migration from DMS Agent Picker

The current plugin already provides useful Codex discovery, process-to-tmux
mapping, remote SSH aggregation, exact session resume, and niri window focus.
Migration should preserve those behaviors while moving ownership in stages.

### Phase 0: Contract spikes and scaffold

- Run the focused provider and transport spikes defined in the product
  landscape document.
- Record results and retained contract fixtures in
  `docs/phase-0-validation.md` and `spikes/fixtures/`.
- Capture versioned Codex app-server, Codex hook, Claude supervisor, and Claude
  hook fixtures from the tested local versions.
- Verify Claude Agent View detach/attach observability and document the exact
  degraded behavior when no binding signal exists.
- Verify attach-before-provider startup, concurrent open reservations, and
  niri/Ghostty focus on an isolated tmux socket.
- Scaffold the Python package, formatting, tests, CI, explicit license status,
  and release packaging.

### Phase 1: Domain, configuration, and storage

- Add the stable host-ID file and configuration parser.
- Add globally stable configured projects and host-local locations.
- Add the SQLite schema and migrations for sessions, immutable handoffs,
  launch intents, runtime observations, surfaces, events, and remote cache.
- Define versioned snapshot, capability, error, and presentation-plan fixtures.
- Implement pure merge, state derivation, launch transition, and validation
  logic before provider subprocess integration.

The implemented Phase 1 boundary, verification commands, privacy contract, and
artifact evidence are recorded in
[`docs/phase-1-validation.md`](phase-1-validation.md). That evidence does not
claim implementation of Phase 2 provider adapters or either production
frontend.

### Phase 2: Read-only providers, hooks, and reconciliation

- Port Codex app-server discovery and metadata normalization.
- Add capability-gated Claude supervisor discovery.
- Add Codex and Claude hook ingestion with launch binding and idempotency.
- Implement normalized state transitions and liveness reconciliation.
- Retain observed sessions after runtimes stop and preserve resumability and
  completed-attention state independently.
- Implement versioned local `snapshot` and `list --json` output.

### Phase 3: Atomic launch, tmux, and DMS parity

- Implement launch leases, waiting bootstrap, stable surfaces, and tmux
  metadata.
- Implement `prepare-open`, `prepare-new`, `prepare-workspace`,
  `select-surface`, `attach-surface`, project-aware new-session flows, and final
  pre-exec duplicate checks.
- Implement the one-workspace Claude tmux policy and manager/session windows.
- Change DMS local rows to consume core JSON and presentation plans while
  preserving niri focus, Unicode behavior, and current failure reporting. Its
  existing remote helper remains the fallback until Phase 5.
- Pass local parity tests before removing equivalent local discovery or tmux
  paths from the existing helper.

### Phase 4: Curation, context, and TUI

- Add current-session resolution, immutable handoffs, wrapping, pinning, and
  continuation metadata.
- Build the searchable status-oriented TUI on the already proven core API.
- Support normal terminal, tmux popup, and switch-client flows.
- Add the session-scoped agent tool surface and optional memory adapter only
  after current-session authorization and bounded retrieval contracts pass.

### Phase 5: Remote hosts

- Add SSH snapshot transport around the existing stable host identity and
  snapshot protocol.
- Add host-local snapshot reconciliation modes.
- Add concurrent pull-based SSH aggregation and atomic caching.
- Preserve stale snapshots and expose host reachability without overwriting
  last-known session state.
- Add remote `prepare-open`, `prepare-new`, `select-surface`, `attach-surface`,
  and bounded handoff envelopes.
- Move DMS remote rows to the snapshot/presentation protocol only after SSH
  bounds, stale-cache, Unicode, and error-path parity tests pass; then remove the
  remaining legacy helper paths.
- Measure polling with SSH multiplexing before considering `watch --jsonl`.

No phase should require a flag day. The existing DMS helper remains available
until its replacement passes equivalent discovery and open-path tests.

## Test Strategy

### Unit tests

- Session identity and merge rules
- Global project identity, cross-host conflict detection, location selection,
  canonical path containment, ambiguous matches, and session assignment
- Launch-intent transitions, lease expiry, idempotent retries, binding, and
  recovery after missed hooks
- Immutable handoff, wrapping, pinning, import, and continuation semantics
- Agent tool current-session authorization
- Hook transition ordering and stale-event rejection
- Host reachability, runtime, resumability, activity/reason, and attachment
  derivation
- Provider schema validation
- Atomic prepare/presentation resolution
- Command argument escaping and validation
- Remote snapshot version handling
- Snapshot reconciliation mode boundaries
- Remote cache freshness and offline derivation
- Database migrations

### Provider contract tests

Use captured, redacted fixtures for Codex app-server/hooks and Claude
supervisor/hooks. Fixtures must record the provider version and schema
fingerprint and cover missing fields, incompatible versions, disabled Agent
View, completed sessions with and without PIDs, background work, interactive
sessions, and provider errors.

### tmux integration tests

Use an isolated tmux server/socket to verify:

- surface creation and metadata
- provider does not start before a real client views the target surface,
  including a non-selected window in an already attached Claude workspace
- bootstrap replaces itself with the provider and expires cleanly without a
  client
- attach and switch behavior
- stale or mismatched client IDs cannot switch another tmux client
- pane discovery in user-modified layouts
- one Claude workspace with manager and exact-attach windows
- manager launch reaches `manager_ready` without manufacturing a provider
  session binding
- confirmed session rebinding from Claude A to B and clearing an unconfirmed
  manager binding
- stale surface cleanup
- concurrent prepare calls across processes create exactly one surface

### End-to-end tests

- Start, park, resume, and reopen a Codex session.
- Start multiple independent sessions under one project.
- Bind a provider UUID back to a new launch's project and surface.
- Start a new session from project defaults and from an exact immutable
  handoff.
- Wrap a session through the current-session agent tool and reopen it.
- Continue operating when the optional memory adapter is unavailable.
- Start, background, attach, and reopen a Claude session.
- Open several Claude sessions as windows in one workspace.
- Switch Claude conversations through `/resume` and Agent View and verify that
  only confirmed surface bindings are focused.
- Miss a hook event and repair state through reconciliation.
- Verify remote hooks never perform network operations.
- Confirm a remote snapshot never recursively queries other hosts.
- Disconnect a remote host and retain last-known cached session state under an
  offline host observation.
- Reject stale remote preparation when owning-host revalidation disagrees,
  without launching a terminal.
- Send a bounded handoff envelope from one host to another and retain its source
  attribution.
- Open the same session from TUI and DMS without creating duplicate runtimes.
- List a manually launched live session without a trusted surface as
  unroutable, and refuse to duplicate it.

## Known Gaps and Accepted Limitations

### Manually launched live sessions

Switchboard is expected to be the entry point for new managed Codex and Claude
Code sessions. Launching through Switchboard establishes the project, location,
launch intent, and stable tmux surface before the provider starts.

Sessions launched manually outside Switchboard may still be discovered through
provider-native queries or hooks. Provider-native parked history remains
resumable through a new managed surface. A currently live manual session is
handled on a best-effort basis:

- If a trustworthy tmux pane locator is available, Switchboard may attach it.
- If no routable surface is known, the session remains visible as live with an
  unavailable open action and an `unmanaged_surface` reason.
- Switchboard never starts a second interactive runtime merely to compensate
  for a missing terminal locator.

The first release does not attempt to retrofit stable identity into arbitrary
bare Ghostty windows or other terminals. Improving that case is optional future
work, not an acceptance criterion for the core workflow.

### Native Claude Agent View switching

Claude's supervisor is authoritative for session activity, but the tested
Agent View detach/attach transition emitted no corresponding hook. Switchboard
therefore confirms a background surface only by correlating a managed pane
running `claude attach <short-id>` with the supervisor row for that short ID.
A manager pane is always unbound. After any transition that loses this process
correlation, the session list remains correct but the surface becomes
`binding_confidence=unknown`. Opening that session creates or selects a fresh
exact-attach window instead of focusing a possibly stale binding. This is an
explicit degraded routing case, not a reason to parse terminal output or
private transcript files.

### Provider contract evolution

Codex app-server schemas are version-specific and Claude Agent View is a
preview capability. Unsupported versions retain registry-known sessions and
provider-native history actions, but structured discovery/status features may
be marked unavailable. Shipping a new supported version requires fixture and
contract-test coverage; unknown fields alone do not require a release.

## Resolved Implementation Decisions

### Product naming

The formal project name is Agent Switchboard and the technical namespace is
`agent-switchboard`. The user-facing product name is Switchboard. Technical
paths remain namespaced, and executable spelling is decided independently as
part of distribution design.

### Implementation language

The initial implementation uses Python 3.12 or newer. It reuses the tested DMS
discovery, SSH, tmux, and niri logic, and the standard library covers SQLite
and subprocess orchestration. Phase 0 measured acceptable hook startup;
installed CLI startup remains a pre-migration acceptance gate once the command
exists. Release packaging must provide an absolute executable path on Arch and
Ubuntu. A later rewrite requires measured startup, packaging, or maintenance
evidence, not preference alone.

### Project authority

Configuration owns globally stable project identity and launch/context fields.
Each host materializes those declarations and contributes host-local locations.
SQLite owns observed sessions, assignments, handoffs, launches, runtimes,
surfaces, and cache. Git identity suggests locations but never creates or merges
projects automatically.

### Interactive transport

tmux is required for first-release interactive actions. Direct transport is
deferred until it can satisfy the same surface identity and duplicate
prevention contracts.

### First production frontend

DMS is migrated immediately after the local launch path reaches parity. The TUI
is built on the resulting stable API rather than serving as the first test of
provider and transport behavior.

## Remaining Non-blocking Decisions

These choices do not block Phase 1 or the read-only portion of Phase 2. Each
must be resolved before implementation reaches the phase that owns it.

### TUI framework

Select before Phase 4. The framework must support asynchronous
refresh, fuzzy filtering, deterministic model tests, status styling that does
not rely on color alone, and clean suspension or tmux switching.

### Legacy Claude history

Keep the native history fallback unless Claude publishes a structured listing
API. A one-time importer based on private files is out of scope by default.

### Agent tool transport

Choose between a stdio MCP server, provider-specific plugins, and a structured
CLI wrapper. All transports must enforce current-session-only mutations and
share the same tested core operations.

### Project context sources

Define how configured files, Git observations, session handoffs, and optional
memory results are bounded, timestamped, and exposed without automatically
loading an entire project history into every new session.

### Distribution and hook installation

Decide whether releases provide packages, standalone archives, or both, and
whether hook configuration is generated by an explicit install command or
managed entirely by dotfiles.

Switchboard is distributed under the MIT License. Package metadata carries the
`MIT` SPDX expression, and wheel and source artifacts include the license text.

## Accepted Design Commitments

The following decisions form the implementation baseline:

- The core and TUI live in this repository.
- The DMS plugin remains a separate thin integration.
- Opening a session always ends in the unmodified provider-native terminal UI.
- Switchboard does not proxy terminal I/O and leaves no persistent wrapper
  around provider processes.
- No host-wide Switchboard daemon or wrapper remains resident merely because
  managed sessions are running; optional agent tools are session-scoped.
- Switchboard is the expected entry point for new managed sessions; arbitrary
  live terminals launched elsewhere are a documented best-effort gap.
- Projects are stable context and launch profiles, not task containers.
- One focused session per task is a recommended convention, not an enforced
  domain model.
- The deterministic core exposes bounded tools that let the selected agent
  perform semantic naming, handoff, and context retrieval.
- Provider-native storage remains authoritative.
- SQLite is the initial registry; no daemon is required.
- Python is the initial implementation language.
- DMS is the first production consumer; the TUI follows the proven core API.
- tmux is the required first-release interactive transport, not a session
  identity.
- Every managed provider start is represented by a leased launch intent before
  a provider session ID exists.
- Opening atomically prepares a surface before presentation; read-only plans do
  not authorize creation.
- Projects have globally stable configured IDs and merge host-local locations
  through snapshots.
- Codex uses tmux for runtime persistence.
- Claude uses its supervisor for runtime persistence and one host tmux workspace
  for manager and exact-attachment surfaces.
- Host reachability, runtime presence, resumability, activity/reason, and
  attachment are separate state axes.
- Handoffs are immutable records and continuation references an exact handoff.
- Hooks provide live status and reconciliation repairs stale state.
- The first milestone is local-only.
- Task management, notifications, orchestration, transcript parsing, and
  destructive provider management are outside the first release.
