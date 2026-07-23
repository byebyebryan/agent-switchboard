# Phase 6E.1 Operational Closure Acceptance

Date: 2026-07-22

Status: complete

## Accepted contract

Switchboard-owned state is disposable; provider processes, provider history,
repositories, unrelated hooks, and tmux state are not. Normal development no
longer depends on the historical Phase 6E cutover importer.

- `init` consumes a Config v3 template, allocates or accepts one generation ID,
  and atomically publishes a complete empty committed generation.
- `reset` requires the exact current generation ID, copies the current Config v3
  unless given a replacement template, and atomically publishes a new empty
  committed generation.
- Reset retains the old generation directories. It performs no provider, hook,
  DMS, pane, session, or tmux-server lifecycle operation.
- Fresh manifests bind the canonical Config v3 bytes by SHA-256. Cutover
  manifests and evidence remain independently valid for the historical
  activation generation.
- Hook installation is separate and opt-in. Unmanaged hook invocations exit
  successfully without reading Switchboard state. Every newly launched managed
  provider receives its exact generation marker; after reset, that stale marker
  turns later global hook invocations into successful no-ops.

## Deterministic evidence

Generation and CLI tests prove:

- a template may omit `generation_id` or reuse an existing canonical config;
- initialization materializes catalog only, with no session, frame, view, or
  transition rows;
- repeated init fails without replacing the current generation;
- reset confirmation is compare-and-swap exact;
- old database content remains present after reset while new state is empty;
- every fsync/publish/pointer fault boundary exposes either the old/none state
  or one complete committed generation, and recovery removes abandoned partial
  generations;
- opt-in Codex hook installation writes only five owned handlers under a
  temporary provider root; and
- a fresh generation creates a persistent navigator view, `view attach --view`
  resolves the exact tmux target without DMS, and reset leaves that complete
  tmux topology unchanged.

The existing Phase 6D workflow acceptance remains the task-transition gate: one
workspace pushes one child, completion returns exactly to the parent, and the
fixed control turn is submitted once. Phase 6G, after the TUI-first Phase 6F
adoption gate, lifts that one-child bound.

## Validation gates

```sh
python -m compileall -q src/agent_switchboard/_v3 tests
ruff format --check .
ruff check .
pytest -q
git diff --check
```

Distribution acceptance builds twice with the fixed source epoch, compares
byte-identical wheel and sdist artifacts, audits exact contents, installs the
wheel into a clean environment, and runs `init`, `state host`, and confirmed
`reset` against temporary roots.
