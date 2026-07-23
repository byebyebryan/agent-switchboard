# Phase 6D Acceptance

Date: 2026-07-22

Status: accepted behind the private replacement boundary

Phase 6D adds exact provider launch authority, capability-bound replacement
agent tools, and the conservative workspace-to-one-child workflow. The
installed `swbctl 0.2.0`, its hooks, live registry/config, DMS, and user tmux
state were not changed.

## Accepted behavior

- A workspace agent can prepare one child only from its exact active
  frame/session/surface/launch/placement capability. Tool inputs cannot supply
  those identities.
- Push atomically stores the zero-turn provider UUID, task frame and membership,
  staged placement and Surface, launch intent, transition, immutable brief, and
  one `claim_brief` control turn before it creates an inert holding pane.
- The source `Stop` fences the source, presents the staged child, transfers the
  WorkContext foreground generation, starts the exact native provider command,
  binds its preallocated UUID, and releases the brief only through
  `transition_claim()`.
- Back presents or resumes the exact parent UUID without a control prompt and
  leaves the child open. Human close presents the parent first, then closes the
  exact child as dismissed without a model turn. Both actions are available to
  the private navigator as `b` and `c`.
- Complete-and-return stores one immutable handoff, marks the child closing,
  presents the parent, submits exactly one fixed visible claim prompt, closes
  the child on the parent's exact claim, and settles on the parent's next trusted
  `Stop`.
- Live control submission uses one tmux queue to enable input, send only the
  fixed literal and Enter, and re-fence input. Exact prompt observation or the
  bounded watchdog re-enables input. An uncertain submission is durable and is
  never retried automatically.
- Cancel removes only an exact prepared, unbound, zero-turn push. Explicit
  focus/mode work may supersede only prepared transitions; executing or later
  ownership blocks it.
- Exact stopped-parent resume preserves the provider UUID and can carry the
  fixed control prompt only as initial input. Codex precreation/name/delete and
  provider new/resume/fork argv are version gated.
- The replacement MCP exposes only `switchboard_current`,
  `switchboard_context`, `switchboard_history`, `task_push`, `task_back`,
  `task_complete_return`, `transition_claim`, `transition_status`, and
  `transition_cancel`. The raw capability is never returned or persisted.
- Private Codex and Claude hook routes normalize only privacy-safe lifecycle
  evidence, bind it to inherited exact authority, and route SessionStart,
  UserPromptSubmit, PermissionRequest, and Stop without retaining semantic hook
  payloads.

## Provider contract

The only provider-start prompt accepted by the command builders is:

```text
Call transition_claim() and follow the returned transition instructions.
```

No title, brief, handoff, path, token, command, or user content can replace or
extend it. Provider commands register the capability-bound `switchboard` stdio
MCP explicitly and receive the raw capability only in their pane environment.

The accepted native contracts remain exact:

| Provider | Accepted version | New/resume identity |
| --- | --- | --- |
| Codex | `0.144.6` | precreate/name a zero-turn App Server thread, then exact UUID `resume` |
| Claude Code | `2.1.216` | explicit `--session-id`; exact UUID `--resume` |

Codex fork is guarded but cannot promise a preallocated target UUID, so a
caller that requires one is rejected. Claude fork requires both exact source
and target UUIDs. Codex precreation failure attempts deletion only after an
exact empty-thread read; cancellation uses the same zero-turn proof.

## Automated evidence

The complete repository passes 748 tests. The private replacement subset
passes 77 tests, including real isolated tmux movement and input fencing with a
guarded native-provider harness. Provider-focused tests cover exact Codex App
Server mutation, fixed argv/environment/MCP construction, version rejection,
claim idempotency, hook routing, cancellation, Back, human close,
complete-return settlement, stopped-parent resume, and no-retry uncertainty.

```sh
.venv/bin/python -m compileall -q src tests spikes scripts
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/pytest -q tests/test_v3_*.py
git diff --check
```

## Installed-provider gate

The local installed binaries were probed through the production version gate:

| Provider | Observed local state | Result |
| --- | --- | --- |
| Codex | `0.145.0` | `provider_version_unaccepted`; no provider launch or model turn |
| Claude Code | not installed | `provider_not_found`; no provider launch |

This is intentional fail-closed acceptance: Phase 6D did not silently widen a
tested native contract, mutate provider history, call a model on an unaccepted
binary, or use the remote host after development was constrained to local work.
The accepted-contract command and lifecycle paths are exercised deterministically
with real tmux; the earlier retained provider probes establish the exact
`0.144.6`/`2.1.216` native CLI feasibility.

## Boundary to Phase 6E

Phase 6D remains private. It does not install replacement hooks, register a new
public `swbctl`, activate Config v3, change live user state, or change DMS.
Phase 6E completed a one-time coordinated core/DMS technical activation
rehearsal. Its provider quiescence was historical activation machinery, not a
workflow cutover or reusable operating model.
Post-activation work follows [Runtime Operations and Safety](operations.md):
Switchboard may discard its own state but never requires existing agents to
stop.
