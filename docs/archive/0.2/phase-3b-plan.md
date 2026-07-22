# Phase 3B Plan: Project-Aware New Local Codex Sessions

> Historical v1 plan and evidence. Phase 4D removes `prepare-new`, locations,
> and Snapshot v1 in favor of task-aware preparation and Snapshot v2.

Date: 2026-07-16

Status: Core, DMS implementation, and live acceptance complete

## Decision and boundary

Phase 3B was implemented ahead of Phase 2B. The existing local Codex path
already proved discovery, retained and live runtime truth, hook ingestion,
existing-session preparation, managed tmux surfaces, and DMS presentation.
Phase 3B completes that vertical slice by starting a new Codex session from a
configured project location and binding the provider-assigned session UUID back
to the reserved launch and surface.

This ordering is deliberate. It exercises the existing provider-neutral
project, location, launch-request, launch-intent, surface, and presentation
contracts before adding Claude-specific hook, process, and tmux semantics.
Phase 2B was the next provider-expansion batch after Phase 3B and is now
complete.

The first increment is local, Codex-only, tmux-only, and configuration-driven.
It does not include handoff continuation, arbitrary ad-hoc working-directory
entry, project editing commands, Claude actions, remote actions, a TUI, or a
general command launcher.

## Existing substrate

Phase 3B must extend rather than replace the implemented invariants:

- `LaunchRequestKind.NEW` already requires host, provider, project, location,
  and canonical working directory, and contributes all of them to the request
  fingerprint.
- `LaunchIntent` already permits a new launch to begin without a provider
  session ID and to receive that identity only when the launch binds.
- SQLite already stores projects, locations, launch intents, surfaces, leases,
  request fingerprints, and atomic hook/live launch binding.
- The Phase 3A coordinator already implements waiting tmux surfaces,
  attach-before-provider startup, idempotent request retries, rollback, and
  versioned presentation plans.
- Snapshot v1 already carries configured project and location records.

No schema migration or PresentationPlan version change is expected unless an
implementation audit proves an existing invariant insufficient.

## Public core contract

The new preparation command is:

```text
swbctl prepare-new --project <project-id> [--location <location-id>]
  [--provider codex]
  --request-id <uuid>
  [--has-current-terminal]
  [--current-tmux-client <id>]
  [--can-focus-desktop]
  [--can-launch-terminal]
  --json
```

The command returns the existing PresentationPlan v1 envelope. It never
returns a provider command, shell fragment, raw tmux locator, or working
directory. `select-surface` and `attach-surface` remain the only frontend
surface actions.

Preparation loads and validates the current host configuration, materializes
the project catalog, performs bounded local runtime reconciliation, resolves
one launch target, and then reserves or reuses the launch atomically.

### Target resolution

The first increment uses these deterministic rules:

1. `project-id` must identify a declared project with at least one declared
   location on the current host.
2. An explicit `location-id` must belong to that project and current host.
3. Without an explicit location, choose the sole local location, otherwise the
   sole location marked `is_default`; ambiguous or missing choices are blocked.
4. The chosen location path must be absolute, exist, and be a directory at
   preparation time and again immediately before provider execution.
5. An explicit provider wins, followed by the location override and then the
   project default. The resolved provider must be Codex in Phase 3B; a missing
   or unsupported provider produces a structured blocked plan.
6. The canonical selected location path is the launch working directory. DMS
   and other frontends do not supply or reinterpret it.
7. The configured transport must resolve to tmux.

A configuration change that alters the normalized target while reusing one
request ID produces `request_conflict`; it never mutates the original launch.
Different request IDs may intentionally start independent sessions in the same
project and location.

## Launch lifecycle

The successful path is:

```text
resolve configured target
  -> reserve NEW LaunchIntent with no target session
  -> create one launch-owned waiting tmux surface
  -> return focus/switch/attach/blocked plan
  -> wait until a real client views the surface
  -> revalidate lease, surface metadata, project/location, and cwd
  -> transition to provider_started
  -> renew a bounded five-minute provider-identity binding grace
  -> exec the configured Codex executable with no shell in the selected cwd
  -> bind the first exact provider UUID through the launch-aware SessionStart
     hook or equivalent exact live evidence
  -> confirm the symmetric session/surface binding
```

The provider process receives only the launch and surface environment needed
by the existing binding contract. Provider argv remains core-owned. The first
Codex launch uses the native new-session entry point rather than `resume`.

If no client attaches, the lease expires without starting Codex. Surface
creation, terminal launch, provider exec, identity mismatch, or binding failure
must leave a bounded failed/expired launch and reclaim an empty launch-owned
surface. An unexpected hook UUID must not bind an unrelated launch.

If the SessionStart hook is missed, reconciliation may bind only when one
provider session UUID, one process birth, and the complete launch-owned tmux
locator agree. Ambiguous evidence remains unbound and visible as degradation.

## DMS contract

The separate `agent-switchboard-dms` adapter remains thin:

- Extend the bounded Snapshot v1 projection with launch targets containing
  only stable project/location IDs and bounded display fields required by the
  launcher.
- Produce one deterministic new-session item per resolved local location. A
  sole/default location can use the project label; multiple locations include
  the configured location display name.
- Search and ordering remain synchronous over the last-good model. Refresh and
  failure retention retain their current semantics.
- Selection invokes one repository-owned helper with project/location IDs and
  the configured `swbctl` token. QML never supplies cwd, provider argv, tmux
  locators, desktop tokens, or shell text.
- The bridge invokes only the fixed public `prepare-new` argv and independently
  validates the returned PresentationPlan v1.
- Focus, switch, attach, same-request focus fallback, structured errors, and
  post-success refresh reuse the Phase 3A action path.

The existing session row remains the way to reopen the newly bound session.
Phase 3B does not add a separate session-management UI or a project editor.

## Acceptance gates

### Current validation checkpoint

On 2026-07-16, the core and separate DMS adapter implementation passed their
automated gates. Core completed 443 tests plus Ruff formatting/lint and a
two-build reproducible wheel/sdist audit. The DMS checkout completed 113 Python
tests, 19 deterministic JavaScript behavior groups, QML formatting, Ruff, and
package Pyright through its repository check script.

The editable core and DMS development plugin were then exercised together on
DMS 1.5.0 with Codex 0.144.4. A full live bridge refresh projected one declared
Codex/tmux launch target alongside six retained sessions with no warnings and
without exposing the configured path. A controlled no-client request returned
an attach plan, was intentionally not attached, and returned `launch_expired`
after its 30-second lease. Codex process count remained 2 before and after, and
tmux session count remained 4 before and after.

The user then reviewed and trusted the five installed Agent Switchboard Codex
hooks through `/hooks`; no trust state was edited programmatically. `swbctl
doctor` reported healthy with a 110.6 ms cold hook start and an 86.1 ms warm
p95.

The positive path started one fresh Codex thread,
`019f6e0d-11de-7770-a574-926c2020a739`, in the configured checkout. Codex
process count moved from 2 to 3, tmux session count from 4 to 5, and niri window
count from 1 to 2. Exact live reconciliation bound that provider UUID to the
new launch, declared project/location, tmux pane, and managed surface. A full
DMS refresh then projected seven sessions and one launch target with no
warnings; the new row was live, ready, attached, confirmed, and contained no
prompt, transcript, path in the launch target, or raw launch payload.

Reopening the new session returned `focused` for the same surface while all
three counts remained unchanged. One earlier harness-only launch, invoked from
the active Codex shell without its missing `DISPLAY` and `WAYLAND_DISPLAY`,
expired cleanly without a tmux session or provider process; rerunning with the
actual DMS display environment exercised the intended launcher path. The
initial acceptance then spent more than the original 30-second attachment
lease at the empty Codex prompt. The first `UserPromptSubmit` and `Stop` hooks
were therefore rejected as expired, and exact reconciliation supplied the
designed missed-hook recovery. Review converted that finding into a separate
five-minute provider-identity binding grace after attachment while preserving
the 30-second no-client cleanup bound. A second no-write turn completed without
hook errors and updated the session to ready/turn-complete. The managed
development session and window are left running for dogfooding.

### Core behavior

- Exact project/location/provider resolution, including missing, foreign,
  undeclared, ambiguous, unavailable-path, and unsupported-provider cases.
- Canonical request fingerprints and same-request idempotency across changed
  presentation capabilities.
- Concurrent identical request IDs create exactly one intent and one surface.
- Distinct request IDs can create multiple sessions in one location.
- Provider startup occurs only after attachment and uses exact shell-free argv,
  cwd, and launch environment.
- Final bootstrap validation rejects changed config, missing cwd, stale lease,
  mismatched surface metadata, and an already failed or bound launch.
- Hook binding assigns the new session's project, location, cwd, launch, and
  confirmed surface atomically.
- Exact live recovery covers a missed SessionStart; ambiguous correlation does
  not bind.
- Every failure path reaps or replaces the bootstrap appropriately and leaves
  no unowned live provider or empty permanent surface.

### Protocol and DMS behavior

- Snapshot and PresentationPlan versions remain compatible and privacy-safe.
- Launch-target projection is bounded, deterministic, and rejects inconsistent
  project/location relationships.
- QML exposes new-session items without synchronous process execution.
- The action helper builds fixed argv, validates every plan, preserves one
  request ID across focus fallback, and never interprets paths or shell text.
- Existing-session behavior and the legacy `agentSessions` plugin remain
  unchanged.

### Live acceptance

Using one explicitly configured local project and location:

1. `swbctl doctor` is healthy and the DMS development plugin is loaded.
2. The DMS launcher shows the expected new-Codex action after a full refresh.
3. Selecting it creates exactly one managed Ghostty window, one waiting tmux
   surface, and then one Codex process after attachment.
4. The new session starts in the configured location and binds one durable
   provider UUID to the launch, project, location, and surface.
5. A refreshed snapshot and DMS row show the bound session without leaking
   prompts, transcripts, raw argv, or private hook payloads.
6. Reopening that session focuses the existing surface without another Codex
   process or Ghostty window.
7. A controlled missed-hook exercise binds only through exact live evidence.
8. A controlled no-client or startup failure expires and cleans up without a
   provider process.

The acceptance record must state exact versions, commands, counts, and rollback
state, and must distinguish temporary development deployment from permanent
machine configuration.

## Deferred work

- Phase 3C Claude new/resume/native-history tmux and DMS presentation policy.
- Handoff-based continuation and other Phase 4 curation operations.
- Arbitrary cwd launch, project catalog editing, and a terminal TUI workflow.
- Remote snapshot or action transport.
