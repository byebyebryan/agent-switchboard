# Phase 6F.1 Persisted-View Operational Acceptance

Date: 2026-07-22

Status: implementation complete; installed-state acceptance pending

Phase 6F.1 closes the gap between isolated Phase 6F lifecycle acceptance and a
clean persistent pre-adoption baseline. It does not expand task depth or infer
workflow adoption.

## Implemented behavior

- `TmuxExecutor.observe_server_evidence` reads exact socket, PID, and start-time
  evidence without bootstrapping a missing server.
- `ViewRuntime.observe_health` checks every non-retired view against its
  recorded server generation and shell topology without changing the registry
  or tmux.
- Stale views remain durably unchanged but project as `degraded` with one
  bounded warning in HostState, NavigatorState, the resident navigator, and
  `doctor`.
- Explicit view entry/recovery remains the only operation that invalidates old
  locators, creates replacement view shells, or opens provider-resume recovery.
- Core release `0.3.3` carries the health projection and retains the Phase 6F
  provider, hook, transition, and packaging contracts.

## Installed-state gate

Using the normal Config/State roots but no provider lifecycle action:

1. prove the leftover technical-activation view projects
   `view_tmux_replaced` against the restarted normal tmux server;
2. publish a new empty generation from a Config v3 template with navigator as
   both CLI and desktop default;
3. create one fresh persistent navigator view without starting a provider;
4. prove `doctor` and NavigatorState report healthy exact view evidence and no
   imported frames, sessions, surfaces, transitions, control turns, or
   recoveries; and
5. prove the existing native tmux sessions, pane IDs, provider PIDs, hooks, and
   DMS state are unchanged.

Reset is restricted to Switchboard generation state. Previous generations stay
on disk, and native provider sessions continue independently.

## Current evidence

The source implementation projected the installed stale view as `degraded`
with `view_tmux_replaced` while leaving its durable `ready` row unchanged.
Targeted tmux, view, protocol, and CLI tests passed. Full-suite, reproducible
artifact, clean-install, installed reset, and fresh-view evidence remain
pending.
