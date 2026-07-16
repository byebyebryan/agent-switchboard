# Switchboard

Switchboard is a local session router for provider-native coding-agent
sessions. The formal project name and technical namespace are **Agent
Switchboard** / `agent-switchboard`; user-facing surfaces use **Switchboard**.

The project preserves Codex and Claude Code as the owners of conversation
history and terminal interaction. Switchboard stores normalized routing and
status metadata, then hands the user back to the unmodified provider UI.

## Implementation status

The current checkout contains the Phase 1 core plus a bounded, read-only Codex
slice of Phase 2:

- Python package and finalized `swbctl` executable name
- stable host identity and strict TOML configuration
- provider-neutral domain and state models
- versioned machine protocols and privacy validation
- SQLite schema, migrations, and registry operations
- production Codex `0.144.4` app-server discovery, normalization, atomic
  reconciliation, and canonical local snapshots
- retained no-refresh reads, structured provider degradation, and explicit
  snapshot-session truncation
- unit, migration, concurrency, provider, protocol, and packaging tests

The Phase 2 implementation is deliberately partial. Claude discovery,
provider hooks and live-state reconciliation, launch/tmux actions, DMS
migration, remote SSH transport, and the TUI are not implemented. See
[the design](docs/design.md), the
[Phase 1 validation record](docs/phase-1-validation.md), and the
[Phase 2 validation record](docs/phase-2-validation.md) for the exact boundary
and evidence.

## Local read-only commands

The implemented command surface emits one versioned snapshot envelope:

```sh
swbctl snapshot --json
swbctl snapshot --reconcile full --json
swbctl list --json
swbctl list --refresh --json
```

`snapshot --reconcile none` is the default and is equivalent to the retained
read used by `list --json`. On the first invocation, Switchboard lazily creates
its private host identity and registry. If the implicit configuration file at
`${XDG_CONFIG_HOME:-~/.config}/agent-switchboard/config.toml` is absent, it
uses documented defaults without creating that file. Once the registry is
bootstrapped, no-refresh commands read retained state without parsing config or
invoking Codex.

`snapshot --reconcile full` and `list --refresh` load configuration,
materialize configured projects, and run the bounded Codex adapter when Codex
is enabled. Provider absence, timeouts, incompatible results, and incomplete
pagination return a valid snapshot with structured capability degradation;
they do not erase retained sessions. Core configuration, storage, migration,
or protocol failures exit nonzero with no partial JSON.

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
