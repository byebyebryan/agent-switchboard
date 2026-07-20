# Phase 3C Plan: Claude Managed-tmux and DMS Parity

> Historical v1 plan and evidence. Phase 4D supersedes its location and
> Snapshot v1 frontend contract; its Claude/tmux evidence remains valid.

Date: 2026-07-18

Status: complete for local Claude known-session resume, new sessions, native
history selection, graceful stop, DMS parity, live compositor focus, and
same-window dedup acceptance

## Decision and boundary

Phase 3C reuses the implemented one-managed-tmux-surface lifecycle for Claude
Code. Claude remains a provider-native interactive CLI. Switchboard owns launch
reservation, exact tmux identity, attachment routing, and lifecycle truth; it
does not parse Claude transcripts, reproduce the native history picker, or use
the Agent View supervisor as a session manager.

The supported Claude profile remains:

```json
{"disableAgentView":true}
```

Every managed Claude process additionally inherits
`CLAUDE_CODE_DISABLE_AGENT_VIEW=1`. This launch-time enforcement is required
even when the durable user setting is already correct. tmux, not a Claude
daemon, keeps a detached interactive process alive.

Phase 3C was delivered in four contract-locked increments:

- open, focus, switch, attach, adopt, or resume a hook-known local Claude
  session;
- execute parked resumes as `claude --resume <provider-session-id>` only after
  the managed surface has an attached client and duplicate-runtime
  reconciliation passes;
- project known Claude sessions and capability truth through the separate DMS
  adapter, then route selection through the same public `prepare-open` action;
- add project-aware new Claude sessions through the same unbound surface and
  hook-binding lifecycle;
- expose Claude's native history picker as exact `claude --resume` in an
  unbound managed surface, without projecting picker rows;
- stop only revalidated launch-owned Claude runtimes through orderly `/exit`
  and a bounded exact-process-group fallback.

No Phase 3C code may add a Claude daemon, call `claude agents`, parse private
history, expose provider argv or tmux locators to DMS, or let DMS read the core
database.

## Refreshed provider contracts

The 2026-07-18 prerequisite refresh advances the exact local contract pins:

| Provider | Tested version | Evidence |
| --- | --- | --- |
| Codex | `0.144.6` | Full app-server discovery completed and the canonical schema fingerprint remained `5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621`. |
| Claude Code | `2.1.214` | The disabled-Agent-View capability probe passed; the native `--resume [value]` UUID/picker surface remains present; an isolated lifecycle smoke emitted exactly one `SessionStart`, `UserPromptSubmit`, and `SessionEnd` with zero turns and provider-reported `total_cost_usd=0`. |

The Claude smoke now installs a self-contained structured blocking hook and
forces an invalid loopback API endpoint. The test therefore fails closed
without making a model request if prompt blocking ever regresses. It does not
depend on the repository package being importable from Claude's hook working
directory.

An earlier development probe did not have that fail-closed property: relative
`PYTHONPATH=src` stopped resolving when the hook ran outside the repository,
so Claude treated the failed prompt hook as non-blocking. One diagnostic run
reported `total_cost_usd=0.080358`; the number of model requests and usage-window
consumption across the other early attempts is unknown. The authenticated
Enterprise subscription did not make this an incremental billing event. Those
probes were stopped, the smoke was replaced by the self-contained blocker and
loopback endpoint above, and all acceptance runs after that correction reported
zero turns and `total_cost_usd=0`.

Five consecutive combined doctor runs passed without changing the latency
budget. Codex warm p95 ranged from `80.6` to `90.2` ms and Claude warm p95 from
`80.9` to `95.9` ms. The prior single readings above `100` ms are treated as
measurement noise, not justification to weaken the gate.

## Core first-increment contract

### Provider-neutral managed surfaces

`LaunchCoordinator` resolves the executable from the selected session's
provider. All surface, launch, pane-metadata, and attach revalidation compares
the expected provider rather than assuming Codex. The persisted launch request
and surface retain the provider identity, so the existing storage constraints
continue rejecting cross-provider binding.

A known Claude session follows the existing action order:

1. full local reconciliation repairs current runtime truth;
2. an existing confirmed managed surface is revalidated and focused, switched,
   or attached;
3. an unmanaged live runtime is adopted only from exact same-user tmux evidence
   and only when the pane carries no conflicting Switchboard metadata;
4. a parked resumable session reserves one idempotent resume launch and creates
   one waiting managed surface;
5. the bootstrap waits for a real client, performs final duplicate-runtime
   reconciliation, extends the provider-binding lease, and only then execs
   `claude --resume <uuid>`;
6. the inherited Claude `SessionStart` hook confirms the expected target UUID
   and binds the launch and surface atomically.

The existing request-ID conflict, launch lease, timeout, rollback, and
one-pending-target rules apply unchanged. A disabled Claude provider may still
focus an already validated live surface, but it may not start a new process.

### CLI and attachment boundary

`swbctl prepare-open` remains the only public existing-session preparation
command. Its stable session key already carries the provider, so no new
provider flag is added. `select-surface` and `attach-surface` revalidate active
local Codex or Claude session surfaces without exposing their tmux locators.

The first increment does not widen `prepare-new --provider`; it remains Codex
only until the new-Claude increment has separate fixtures and live acceptance.

## DMS first-increment contract

The DMS repository continues to invoke only the configured public `swbctl`
executable. Snapshot v1 is validated completely before projection.

Its frontend-owned model advances to model version 2 and contains:

- bounded local Codex and Claude session rows in one deterministic recency
  order;
- separate bounded Codex and Claude capability records so degradation is never
  hidden merely because a session is displayable;
- provider-attributed capability and error warnings;
- the existing Codex-only project launch targets until new-Claude support
  lands.

Session selection passes the canonical Codex or Claude session key to the
existing asynchronous opener. The bridge independently validates that key and
delegates to `swbctl prepare-open`; QML still receives no provider argv, cwd,
tmux locator, compositor ID, or terminal command.

The legacy `agentSessions` plugin remains installed only as the remote-host
fallback. Phase 3C does not disable or modify it.

## Second increment: new Claude session

The provider-neutral `prepare-new` path now accepts explicit or config-resolved
Claude/tmux targets. It reserves the same unbound waiting surface as Codex,
forces `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, and execs plain `claude` only after a
client attaches and the target revalidates. The first matching `SessionStart`
hook binds the provider-assigned UUID, launch, project, location, and surface;
later focus or attachment promotes that confirmed UUID into tmux metadata.

The DMS private model keeps provider identity on every launch target and emits
one explicit Codex and one explicit Claude action per declared local tmux
location. Selection passes only the stable project ID, location ID, and provider
enum through the public `prepare-new` boundary. The provider command, cwd, tmux
locator, launch identity, and desktop token remain core-authored or validated.

Automated acceptance covers explicit Claude selection over a Codex project default,
disabled-provider blocking, exact plain argv, forced environment, attach and
lease revalidation, provider-attributed request identity, hook binding, DMS
projection, provider-specific search/display, and shell-free action plumbing.
Live start/bind/reopen evidence is retained below.

## Final Phase 3C increments

### Native history picker

The explicit `Open Claude history` action creates an unbound managed surface
and execs exact `claude --resume`. The surface remains unbound until picker
selection produces a `SessionStart` UUID. Complete reconciliation retires a
cancelled picker surface and records `surface_terminated` without manufacturing
a session. No picker rows or transcript metadata cross the core or DMS
boundary.

### Graceful stop

`swbctl stop-session` accepts one canonical Claude session key and returns a
versioned action with `stopped`, `already_stopped`, or `blocked` status. It
requires a confirmed current surface, matching bound launch, exact tmux
identity, same-user PID and birth evidence, and a launch-owned process-group
leader. It requests orderly interactive `/exit`, waits a bounded grace period,
and may then terminate only that process group and surface. It never calls
`claude rm`, deletes history, or kills an unmanaged process.

## Phase 3C acceptance

Core acceptance requires:

- exact Codex and Claude contract tests plus the fail-closed live Claude smoke;
- parked Claude resume argv and launch-environment tests;
- existing-surface, live adoption, disabled-provider, duplicate-runtime,
  idempotency, request-conflict, lease-expiry, and attach revalidation tests for
  both providers where behavior is shared;
- native history exact argv, selected-session binding, cancelled-surface cleanup,
  and incomplete-scan preservation tests;
- graceful stop idempotency, exact `/exit`, ownership disagreement, and bounded
  process-group fallback tests; and
- the full Python, Ruff, package, distribution, and diff gates.

DMS acceptance requires:

- mixed-provider fixture projection with both provider capabilities and stable
  recency order;
- canonical Claude key acceptance for prepare-open while malformed or unknown
  providers remain rejected;
- JavaScript validation, display, search, and selection behavior for Claude;
- unchanged Codex project launch behavior and public-process boundary tests;
- provider-native history and conservative `canStop` launcher projection, fixed
  argv, action-envelope validation, and desktop-helper tests; and
- the full Python, JavaScript, QML formatting, Ruff, Pyright, and diff gates.

Live acceptance should use an isolated Switchboard state and a controlled
Claude session UUID. It must prove one waiting tmux surface, exact resume argv,
hook binding, same-session reopen without a duplicate runtime or window, Agent
View daemon absence, native picker selection and cancellation, exact safe stop,
and restoration of the user's pre-test desktop state.

## Implementation checkpoint

The first 2026-07-18 checkpoint passed 469 core tests. The completed Phase 3C
core passes 483 tests, Ruff format/lint, compilation, whitespace checks, and
reproducible wheel/sdist verification. The DMS adapter passes 123 Python tests,
21 deterministic JavaScript behavior groups, QML formatting, Ruff, Pyright,
and whitespace checks.

The final installed doctor sample passed five of eight immediate repetitions.
The three failures were isolated warm-p95 timing misses (`105.1`, `105.9`, and
`111.6` ms) split across the two providers; all version, feature, profile, and
hook-shape checks remained healthy, and the passing warm p95 values ranged from
`78.3` to `95.9` ms. The 100 ms gate is unchanged. This is retained as a local
latency-stability caveat rather than hidden by a larger budget.

The installed wheel then passed an isolated no-model provider/bridge exercise:

- a controlled persistent Claude UUID was created with the structured prompt
  blocker, zero turns, provider-reported `total_cost_usd=0`, and a loopback API
  endpoint;
- one waiting Claude/tmux surface started only after a real attached client,
  executed the installed Claude binary with `--resume <uuid>`, inherited
  `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, and bound through `SessionStart`;
- full reconciliation reported one live confirmed Claude session and one bound
  surface; reopening reused that only active surface and retained one launch and
  one Claude pane;
- the DMS bridge emitted model v2 with one live Claude row, an available Claude
  capability, and no warnings; `switchboard-open` exercised the Ghostty attach
  path against the same core surface;
- no Claude Agent View/background daemon was present, the controlled Claude
  process exited cleanly, reconciliation returned the session to stopped, and
  the isolated state was removed.

The control shell's inherited compositor environment could not observe the
launched Ghostty window through niri even though the transient Ghostty scope
was active and the tmux client attached. Provider start/bind, bridge projection,
desktop-helper launch, and core same-surface dedup therefore passed, but live
niri focus and same-window dedup remain an explicit acceptance item rather than
a claimed result.

The installed new-session increment then passed an isolated, prompt-free live
exercise:

- core reserved one unbound Claude surface and did not start the provider until
  a real tmux client attached;
- the resulting process executed plain `claude`, inherited
  `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, and emitted a provider-assigned UUID
  through the installed `SessionStart` hook;
- that UUID bound to the expected launch, project, location, and surface with
  confirmed live runtime evidence;
- reopening the stable session key returned the existing surface and promoted
  the confirmed UUID into its tmux metadata without starting a second process;
- the DMS bridge emitted model v2 with the live confirmed Claude row, both
  provider launch targets, and no warnings; and
- `/exit` stopped only the test-owned Claude process, full reconciliation
  reported it stopped and resumable, the isolated tmux server exited, and the
  empty test transcript was moved to the desktop trash.

The pre-existing active Claude session remained alive throughout. No prompt was
submitted and no model turn was requested. The new-session exercise did not
open a Ghostty window, so it adds provider/bridge and same-surface dedup evidence
without changing the remaining live niri focus/same-window acceptance item.

The installed final-increment exercise then completed the remaining provider
and adapter lifecycle:

- the DMS bridge projected the controlled confirmed Claude session with
  `canStop=true` while keeping provider argv and tmux identity private;
- the core stop action sent exact interactive `/exit`, retired the controlled
  surface, removed only its Claude process, and left the pre-existing active
  Claude process alive;
- `prepare-history` opened Claude Code 2.1.214's native resume picker with Agent
  View disabled, selection rebound the same controlled UUID to the new managed
  surface, and stop remained idempotently scoped to that launch-owned runtime;
- cancelling a second native picker retired its unbound surface and recorded a
  failed history launch with `surface_terminated` rather than creating a
  session; and
- the isolated registry and tmux server were removed, while the test-owned
  transcript was moved to the desktop trash.

No prompt was submitted and no model turn was requested during this final
exercise.

### Compositor closeout

A follow-up 2026-07-18 exercise recovered the active niri environment from the
DMS user service instead of relying on the control shell. It launched the
installed DMS helper with an isolated Switchboard state tree and dedicated tmux
socket; the wrapper explicitly removed the outer `TMUX` value so the managed
surface could not reuse the user's server.

The exact managed Ghostty application ID resolved to one niri window. Reopening
the controlled Claude session through `switchboard-open` returned `focused` for
the same surface, retained that same niri window ID, and left the matching
managed-window count at one. The provider started only after the real terminal
client attached. No prompt was submitted and no model turn was requested.

The public stop action removed only the controlled Claude runtime and managed
window. The pre-existing active Claude session remained alive, no test-owned
process or surface remained, the isolated state was removed, and the empty
test transcript was moved to the desktop trash. This closes the earlier live
niri focus and same-window dedup observation gap without widening the core/DMS
boundary.

## Stop conditions

Stop rather than weaken the boundary if any of the following occurs:

- Claude `2.1.214` does not bind a resumed requested UUID or a new
  provider-assigned UUID through the documented `SessionStart` hook;
- managed launch can enter Agent View despite the forced environment;
- a live or pending duplicate Claude runtime can be started;
- DMS needs a provider command, private database read, transcript path, raw cwd,
  or tmux locator to perform the action;
- capability degradation must be hidden to display Claude rows;
- a live acceptance step would require a model request.
