# Switchboard

Switchboard is a local session router for provider-native coding-agent
sessions. The formal project name and technical namespace are **Agent
Switchboard** / `agent-switchboard`; user-facing surfaces use **Switchboard**.

The project preserves Codex and Claude Code as the owners of conversation
history and terminal interaction. Switchboard stores normalized routing and
status metadata, then hands the user back to the unmodified provider UI.

## Implementation status

The current checkout contains the Phase 1 core, the local Codex portion of
Phase 2, and Phase 3A existing-session presentation:

- Python package and finalized `swbctl` executable name
- stable host identity and strict TOML configuration
- provider-neutral domain and state models
- versioned machine protocols and privacy validation
- SQLite schema, migrations, and registry operations
- production Codex `0.144.4` app-server discovery, normalization, atomic
  reconciliation, and canonical local snapshots
- retained no-refresh reads, structured provider degradation, and explicit
  snapshot-session truncation
- privacy-safe Codex lifecycle hook ingestion with atomic launch binding and
  deterministic event ordering
- bounded `/proc` and tmux reconciliation for normalized live activity,
  attachment, and parked state
- explicit, ownership-safe Codex hook installation and effective hook/trust
  diagnostics through the supported app-server contract
- validated tmux surface discovery, creation, metadata, selection, attachment,
  and rollback without shell interpolation
- atomic existing-Codex preparation with live-pane adoption, resumable-session
  leases, waiting bootstrap, idempotent retries, and a final duplicate-runtime
  check before `codex resume`
- exact process/session/tmux correlation that can atomically finish a pending
  resume when Codex omits the expected start hook
- versioned focus, switch, attach, and blocked presentation plans consumed by
  the separate DMS integration
- unit, migration, concurrency, provider, protocol, and packaging tests

The next roadmap batch is Phase 2B: Claude discovery, hooks, supervisor/process
liveness, and normalized runtime truth. Phase 3B then adds project-aware new
local Codex sessions, followed by Phase 3C Claude workspace/surface parity.
The searchable TUI remains Phase 4 and remote SSH transport remains Phase 5. See
[the design](docs/design.md), the
[Phase 1 validation record](docs/phase-1-validation.md), and the
[Phase 2 validation record](docs/phase-2-validation.md), and the
[Phase 3A validation record](docs/phase-3a-validation.md) for the exact boundary
and evidence.

## Local commands

The implemented command surface emits one versioned snapshot envelope:

```sh
swbctl snapshot --json
swbctl snapshot --reconcile live --json
swbctl snapshot --reconcile full --json
swbctl list --json
swbctl list --refresh --json
swbctl hooks install --provider codex --dry-run
swbctl hooks uninstall --provider codex --dry-run
swbctl doctor
swbctl prepare-open <session-key> --request-id <uuid> \
  --can-focus-desktop --can-launch-terminal --json
swbctl select-surface <surface-id> --client <tmux-client-id>
swbctl attach-surface <surface-id>
```

`snapshot --reconcile none` is the default and is equivalent to the retained
read used by `list --json`. On the first invocation, Switchboard lazily creates
its private host identity and registry. If the implicit configuration file at
`${XDG_CONFIG_HOME:-~/.config}/agent-switchboard/config.toml` is absent, it
uses documented defaults without creating that file. Once the registry is
bootstrapped, no-refresh commands read retained state without parsing config or
invoking Codex.

`snapshot --reconcile live` repairs retained process and tmux evidence without
querying Codex history. `snapshot --reconcile full` and `list --refresh` load
configuration, materialize configured projects, and run the bounded Codex
adapter when Codex is enabled. Provider absence, timeouts, incompatible
results, and incomplete pagination return a valid snapshot with structured
capability degradation; they do not erase retained sessions. Core
configuration, storage, migration, or protocol failures exit nonzero with no
partial JSON.

Codex invokes `swbctl event --provider codex` from lifecycle hooks. That
internal fast path accepts one bounded JSON object on stdin, discards prompts,
transcripts, and tool payloads, performs one short local transaction, and emits
no stdout. It does not query providers, tmux, SSH, or the network.

Hook installation is always explicit. `hooks install` atomically merges five
identifiable Switchboard handlers into `${CODEX_HOME:-~/.codex}/hooks.json`
with mode `0600`, preserving unrelated matchers and handlers. Install and
uninstall serialize with other Switchboard hook writers through a private
advisory lock; source-token checks also refuse external changes observed before
the final atomic replacement. `CODEX_HOME` itself is opened through a stable
directory descriptor without following a final-component symlink. Run with
`--dry-run` to inspect intent without creating or rewriting anything. Codex
requires the exact definitions to be reviewed and trusted through `/hooks`;
Switchboard never edits Codex trust state. `doctor` checks the effective
`hooks/list` result, executable paths, trust and enablement, source warnings or
errors, and isolated cold/warm event latency. Its latency probe uses temporary
HOME, `CODEX_HOME`, and XDG roots and never writes the user's registry.

`prepare-open` performs a bounded full reconciliation before making an atomic
existing-session decision. A trustworthy live tmux pane can be adopted as a
managed surface. A parked resumable session receives a leased waiting tmux
surface, and Codex starts only after a client attaches; bootstrap revalidates
runtime truth immediately before `exec codex resume <uuid>`. A live runtime
without a trustworthy pane locator returns `unmanaged_surface` and is never
duplicated. Frontends receive only versioned presentation fields and stable
surface IDs. `select-surface` and `attach-surface` revalidate registry and tmux
identity instead of accepting raw frontend tmux targets.

Snapshot assembly reads a bounded deterministic session candidate set and
applies an actual UTF-8 byte budget. If sessions are omitted, the registry is
unchanged and the envelope includes `snapshot_sessions_truncated` with only
retained and emitted counts.

## Requirements and development setup

Python 3.12 or newer is required. The runtime package uses only the Python
standard library.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade build pytest ruff -e .
```

Run the local acceptance gates from the repository root:

```sh
.venv/bin/python -m compileall -q src tests spikes
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/pytest
git diff --check
```

The tests use the `src/` tree directly, so `pytest` also works before package
installation when pytest itself is available.

## Reproducible distributions

The PEP 517 backend is Hatchling. Artifact selection is explicit:

- wheels contain `agent_switchboard`, including migration modules, plus wheel
  metadata and the MIT license;
- source distributions contain the package source, the design and validation
  documents referenced here, the MIT license, and the minimum project files
  needed to build it;
- tests, fixtures, caches, databases, prompts, and credentials are excluded
  from both distribution formats.

Hatchling's reproducible mode uses `SOURCE_DATE_EPOCH` for build timestamps.
CI fixes the value and builds twice, requiring byte-identical wheel and source
archives before testing clean wheel and source-distribution installations.

```sh
export SOURCE_DATE_EPOCH=1784073600
python -m build --outdir /tmp/switchboard-build-a
python -m build --outdir /tmp/switchboard-build-b
sha256sum /tmp/switchboard-build-{a,b}/*
cmp /tmp/switchboard-build-a/agent_switchboard-*.whl \
    /tmp/switchboard-build-b/agent_switchboard-*.whl
cmp /tmp/switchboard-build-a/agent_switchboard-*.tar.gz \
    /tmp/switchboard-build-b/agent_switchboard-*.tar.gz
```

## Privacy and protocol boundary

Snapshot and cache envelopes contain normalized metadata only. They reject:

- prompts, transcripts, raw provider or hook payloads, raw argv, and model or
  tool output;
- credentials, secrets, authentication tokens, cookies, and private keys;
- terminal control characters, including ESC;
- incompatible schema or protocol versions, cross-host records, inconsistent
  identities, and untyped generic collection entries.

Safe additive fields from newer senders are tolerated and discarded during
canonicalization. Explicit capability reports and structured degraded reasons
remain part of the stable protocol.

## License

Switchboard is licensed under the [MIT License](LICENSE).
