# Phase 2 Validation: Read-only Codex Slice

Date: 2026-07-15

## Status and boundary

Phase 2 is partially implemented. The shipped slice is a bounded, read-only
local Codex path built on the Phase 1 core:

- production Codex app-server capability probing and paginated session
  discovery;
- privacy-safe normalization into the provider-neutral session contract;
- all-or-nothing reconciliation of complete scans, including configured
  project-location assignment;
- versioned canonical `snapshot` and `list` JSON;
- retained no-refresh reads, structured provider degradation, and explicit
  session truncation.

This record does not claim Claude discovery, Codex or Claude hook ingestion,
runtime/process/tmux liveness, normalized live-state transitions, hook
installation, launch/tmux actions, DMS integration, remote transport, or a TUI.

## Command behavior

```sh
swbctl snapshot --json
swbctl snapshot --reconcile full --json
swbctl list --json
swbctl list --refresh --json
```

The implicit config path is
`${XDG_CONFIG_HOME:-~/.config}/agent-switchboard/config.toml`. When it is
missing, Switchboard uses an empty document and documented defaults without
creating the file. A first no-refresh command lazily bootstraps the private
host ID and registry so it has a host to snapshot. Subsequent no-refresh reads
use retained state without parsing configuration, materializing projects, or
invoking a provider.

Full refresh loads the implicit config, materializes its declared project
catalog, and runs one Codex discovery cycle when enabled. That cycle uses
bounded subprocesses for version detection, schema generation, and stdio
app-server discovery. A complete scan is reconciled in one database transaction.
An incomplete or unavailable provider result becomes structured capability/error
metadata while previously retained sessions remain unchanged. Configuration,
storage, migration, and protocol failures produce no partial JSON.

Snapshot reads cap deterministic session candidates before Python allocation
and then select rows using their canonical UTF-8 encoded size. Runtime and
surface records that reference omitted sessions are omitted with them. The
registry remains intact, and `snapshot_sessions_truncated` reports only
`retainedCount` and `emittedCount` through the diagnostic-detail allowlist.

## Codex 0.144.4 schema evidence

Four related digests are retained deliberately:

| Evidence | Digest |
| --- | --- |
| Historical experimental schema file, raw SHA-256 | `93b300b8102e48bd1640fe12aec7ed29c215cd882237c70bedd32bf364dacc05` |
| Historical experimental schema, canonical parsed-JSON fingerprint | `f5e8d20f3a8f9bb5e5b23ab0c5aa6bde7b12e7e0713606c5d0132651a4959d37` |
| Production nonexperimental schema file, raw SHA-256 | `8f2f39b5b22a5bf563f63846f52895fd740d2be3dbc0fd93ca54e94ef29421a3` |
| Production nonexperimental schema, canonical parsed-JSON fingerprint | `5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621` |

The raw hashes identify exact retained bytes, including generator formatting.
The canonical fingerprints hash the parsed JSON with deterministic key order
and separators, so they identify the semantic schema used by the production
contract gate. Production schema generation omits the experimental flag and
expects the final fingerprint above. The older Phase 0 raw hash is historical
evidence, not the production fingerprint.

## Bounds, failure, and privacy contract

The production adapter uses argv arrays rather than a shell. Version and schema
probes, app-server requests, cumulative stdout/stderr, individual response
lines, message count, pagination, cursor size, normalized session count, and
cleanup waits are bounded. Generated schemas are read from a private temporary
directory as bounded regular files without following symlinks.

Provider stderr, RPC error messages, previews, turns, transcript content, raw
history paths, raw payloads, and unknown provider fields do not enter retained
records or diagnostics. The only Codex thread fields retained are normalized
session identity, cwd, optional safe name, and timestamps. Cwd and name are
protocol-defined local routing metadata; neither is printed by the live smoke
tool.

The snapshot encoder reconstructs an explicit protocol allowlist and reparses
its own canonical JSON as the final compatibility and privacy gate. Malformed
or oversized provider results return stable structured degradation without
reconciling a partial observation as authoritative absence.

## Durable live smoke

Run from the repository root:

```sh
.venv/bin/python scripts/live_codex_smoke.py --codex /usr/bin/codex
```

The tool creates a temporary Registry and generated host ID, runs the
production `CodexProvider`, reconciles the complete result, builds the canonical
snapshot, and validates it through `SnapshotEnvelope`. It does not read or
write the user's Switchboard state. It prints no raw snapshot and no session or
path data. Success is one sanitized JSON object with provider version,
canonical schema fingerprint, feature names, emitted session count, and elapsed
milliseconds; failure is a generic one-line diagnostic.

The defaults require Codex `0.144.4` and fingerprint
`5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621`.
Explicit expectation flags allow the same smoke to gate a future reviewed
contract without weakening today's default.

## Verification record

The smoke script remains repository-only and is excluded from both
distribution formats. Exact artifact digests are emitted by the verifier and
CI rather than copied into this included source-distribution document: doing so
would make the source archive digest self-referential and stale on every edit.

| Gate | Environment | Recorded result |
| --- | --- | --- |
| Read-only Codex slice | Current development environment | 216 focused provider, reconciliation, storage, snapshot, CLI, protocol, config, and live-smoke tests passed |
| Package/protocol integration | Current development environment | 60 focused tests passed |
| Full source suite and static checks | Current development environment | 299 tests passed; compileall, Ruff format, Ruff lint, and diff checks passed |
| Live production path | `/usr/bin/codex`, expected `0.144.4` contract | Clean capability with `app_server_thread_list` and `schema_fingerprint`; canonical fingerprint `5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621`; 4 sessions; 767 ms |
| Reproducible wheel/source builds | Fixed `SOURCE_DATE_EPOCH=1784073600` | Byte-identical rebuilds passed the verifier; 17 package files, 22 wheel members, and 25 source-distribution members matched the exact allowlists |
| Installed artifacts | Separate fresh environments | Wheel and source distribution passed `pip check`, expanded import coverage, migration v3/current-registry creation, version and command help, isolated-XDG snapshot/list, and installed `SnapshotEnvelope` parsing |

## Remaining implementation scope

- Claude supervisor discovery and capability-gated fallback behavior.
- Codex and Claude hooks, idempotent ingestion, process/tmux liveness, and
  normalized runtime/activity/attachment transitions.
- Explicit hook installation, coexistence checks, and `doctor` diagnostics.
- Launch preparation, leases, tmux surface actions, and project-aware new or
  resume flows.
- DMS local migration and niri/Ghostty presentation parity.
- Pull-based remote snapshots, caching, and remote actions.
- Searchable TUI, curation/handoffs, context retrieval, and session-scoped
  agent tools.
