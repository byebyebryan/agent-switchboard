# Phase 3C Plan: Claude Managed-tmux and DMS Parity

Date: 2026-07-18

Status: first known-session resume increment implemented and provider/bridge
live acceptance complete; live compositor focus/dedup acceptance remains

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

The first increment is deliberately smaller than all of Phase 3C:

- open, focus, switch, attach, adopt, or resume a hook-known local Claude
  session;
- execute parked resumes as `claude --resume <provider-session-id>` only after
  the managed surface has an attached client and duplicate-runtime
  reconciliation passes;
- project known Claude sessions and capability truth through the separate DMS
  adapter, then route selection through the same public `prepare-open` action;
- keep project-aware new Claude sessions, the unbound native history picker,
  graceful stop, and remote hosts for later increments.

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

The legacy `agentSessions` plugin remains installed as the remote and untouched
Claude-history fallback. Phase 3C does not disable or modify it during this
increment.

## Later Phase 3C increments

### New Claude session

Extend provider-neutral `prepare-new` resolution to Claude/tmux locations and
exec plain `claude` with the disabled-Agent-View environment. The first
`SessionStart` hook binds the new UUID to the waiting surface. DMS then projects
Claude launch targets using the same stable project/location IDs.

### Native history picker

Add an explicit `Open Claude history` action that creates an unbound managed
surface and execs `claude --resume`. The surface remains unbound until the
picker selection produces a `SessionStart` UUID. Cancellation retires the
surface without manufacturing a session. No picker rows or transcript metadata
cross the core or DMS boundary.

### Graceful stop

Add an explicit stop command for a validated launch-owned Claude surface. It
requests orderly interactive exit, waits a bounded grace period, and may then
terminate only the launch-owned process group and surface. It never calls
`claude rm`, deletes history, or kills an unmanaged process.

## First-increment acceptance

Core acceptance requires:

- exact Codex and Claude contract tests plus the zero-cost live Claude smoke;
- parked Claude resume argv and launch-environment tests;
- existing-surface, live adoption, disabled-provider, duplicate-runtime,
  idempotency, request-conflict, lease-expiry, and attach revalidation tests for
  both providers where behavior is shared;
- the full Python, Ruff, package, distribution, and diff gates.

DMS acceptance requires:

- mixed-provider fixture projection with both provider capabilities and stable
  recency order;
- canonical Claude key acceptance for prepare-open while malformed or unknown
  providers remain rejected;
- JavaScript validation, display, search, and selection behavior for Claude;
- unchanged Codex project launch behavior and public-process boundary tests;
- the full Python, JavaScript, QML formatting, Ruff, Pyright, and diff gates.

Live acceptance should use an isolated Switchboard state and a controlled
Claude session UUID. It must prove one waiting tmux surface, exact resume argv,
hook binding, same-session reopen without a duplicate runtime or window, Agent
View daemon absence, and restoration of the user's pre-test desktop state.

## Implementation checkpoint

The 2026-07-18 checkpoint passes 469 core tests, Ruff format/lint, compilation,
whitespace checks, and reproducible wheel/sdist verification. The DMS adapter
passes 116 Python tests, 21 deterministic JavaScript behavior groups, QML
formatting, Ruff, Pyright, and whitespace checks.

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

## Stop conditions

Stop rather than weaken the boundary if any of the following occurs:

- Claude `2.1.214` does not bind the requested UUID through the documented
  `SessionStart` hook;
- managed launch can enter Agent View despite the forced environment;
- a live or pending duplicate Claude runtime can be started;
- DMS needs a provider command, private database read, transcript path, raw cwd,
  or tmux locator to perform the action;
- capability degradation must be hidden to display Claude rows;
- a live acceptance step would require a model request.
