# Phase 6F.1 Persisted-View Operational Acceptance

Date: 2026-07-22

Status: complete and accepted

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

The accepted gate used the normal Config/State roots but no provider lifecycle
action:

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

## Accepted evidence

Implementation commit `cb8fd1f` (`Surface stale managed view health`) passed
all 126 tests, repository-wide Ruff lint and format checks, compile checks, and
`git diff --check`.

Two isolated builds with `SOURCE_DATE_EPOCH=1784073600` were byte-identical and
passed exact member, source-byte, metadata, migration, removed-module, CRC, and
archive-safety checks:

- wheel SHA-256:
  `da9abf33ac82d6b34d6578480bc65cc6afee690366182ceebbc5edcb434a7c06`;
- sdist SHA-256:
  `aabdf64816fa9150dbae0b5cd638d8f247f8e30eb1d749e9d10954bacd2df2dc`;
- 24 package files, 29 wheel files, and 43 sdist files.

The wheel was installed into immutable release
`core-0.3.3-da9abf33ac82-cb8fd1f` with Textual `8.2.8`. Installed
`swbctl 0.3.3` and `agent_switchboard.navigator` loaded successfully, and the
public `swbctl` route was moved to that release.

Before reset, both source and installed `0.3.3` projected the old view
`64b74097-065d-5e6d-a8f4-f1085422bb88` as `degraded` with
`view_tmux_replaced`. Its durable row remained `ready`, proving observation was
non-mutating.

Reset compare-and-swapped generation
`932ad28d-e8e3-4bce-be8e-9c66c20e31f5` for fresh generation
`f8854456-2c91-4da4-bb05-dbd6292e1e56` and retained the old generation on
disk. The canonical template and generated config are Config v3 with navigator
as both CLI and desktop default, ten-second hooks, conservative task push, and
synthesized complete-return.

Fresh view `837372bc-32d7-5dd6-bc33-168d1f0ae1ce` then reported:

- `ready`, navigator mode, attention `none`, and no warnings;
- `doctor` status `healthy`, with one checked and zero degraded views;
- one empty workspace frame with no current provider session;
- zero frame-session memberships, provider sessions, surfaces, transitions,
  control turns, or recoveries; and
- exact normal-server navigator, active placeholder, and holding placeholder
  topology without attaching a client or starting an agent.

The normal tmux server retained PID `8704` and start time `1784765316`.
Existing Codex panes remained `%1`/`9854`, `%2`/`10311`, and `%3`/`10828`.
The Codex hook file SHA-256 remained
`75c1286ebca621d2eba9d640e4023110ee3132cf464be9f7e2f266f48ecfdc11`.
No hook, trust, provider, DMS, or remote-host mutation was performed.

Phase 6F.1 is complete. Phase 6G may now build recursive task frames on the
clean persistent baseline.
