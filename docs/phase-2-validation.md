# Phase 2 Validation: Local Codex Runtime Truth

Date: 2026-07-16

## Status and boundary

Phase 2 is partially implemented. The shipped slice now completes the local
Codex path built on the Phase 1 core:

- production Codex app-server capability probing and paginated session
  discovery;
- privacy-safe normalization into the provider-neutral session contract;
- all-or-nothing reconciliation of complete scans, including configured
  project-location assignment;
- versioned canonical `snapshot` and `list` JSON;
- retained no-refresh reads, structured provider degradation, and explicit
  session truncation;
- privacy-safe lifecycle event ingestion with deterministic idempotency,
  ordering, launch binding, and normalized activity transitions;
- bounded same-user process and tmux reconciliation with non-destructive
  degradation on ambiguous or unavailable evidence;
- explicit user-level hook install/uninstall that preserves unrelated hooks;
- effective hook, trust, executable, provider-version, source, and isolated
  latency diagnostics through `swbctl doctor`.

This record does not claim Claude discovery/hooks/liveness, launch/tmux
actions, DMS selection, remote transport, or a TUI.

## Command behavior

```sh
swbctl snapshot --json
swbctl snapshot --reconcile live --json
swbctl snapshot --reconcile full --json
swbctl list --json
swbctl list --refresh --json
swbctl event --provider codex
swbctl hooks install --provider codex [--dry-run]
swbctl hooks uninstall --provider codex [--dry-run]
swbctl doctor
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

Live reconciliation is a separately bounded repair level. It reads same-user
Linux process evidence and only known/current/default tmux sockets, validates
process birth identity instead of trusting a reused PID, and never starts,
stops, resumes, or focuses a runtime. Missing tools, permissions, timeouts,
malformed output, and ambiguous process/session evidence preserve retained
state and produce structured degradation.

The event path reads one bounded Codex JSON object from stdin, immediately
allowlists lifecycle/identity fields, destroys the raw payload, and applies the
event and any exact launch/surface binding in one immediate transaction. It
does not invoke Codex, tmux, SSH, or the network and writes no stdout.

Hook management targets only `${CODEX_HOME:-~/.codex}/hooks.json`. Installation
removes recognized Switchboard duplicates, adds the five canonical handlers,
and publishes a private file by fsync and atomic replacement. Existing input is
opened once without following links, read with a hard byte bound, and identified
by its directory, inode, version, and content digest. Install and uninstall
serialize cooperating Switchboard writers with a private no-follow `0600`
advisory lock held from before load and merge through atomic publish and file
and directory fsync. A stable, no-follow `CODEX_HOME` directory descriptor
anchors the lock and destination, including when the directory must be created.
The publisher also revalidates its best-effort source token and refuses external
edits or path swaps observed before the final replacement; it does not claim
serialization against arbitrary noncooperating writers in the remaining narrow
window. Uninstallation removes only recognized Switchboard handlers. Both
preserve unrelated valid hook sources, support a non-writing dry run, and never
edit Codex trust state. `doctor` uses app-server `hooks/list`, reports
effective source warnings/errors and handler trust/enablement/path drift, and
benchmarks `swbctl event` only inside temporary HOME, `CODEX_HOME`, and XDG
roots after removing inherited Switchboard and tmux attachment metadata.

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
| Codex hook management and diagnostics | Isolated temporary `CODEX_HOME`, HOME, and XDG roots | 31 focused tests passed, including semantic validation, bounded FIFO/symlink reads, serialized Switchboard writers, best-effort external-edit/path-swap refusal, safe missing-home creation, private lock validation, `hooks/list` environment propagation, trust/path/source diagnostics, dry run, entry-point resolution, attachment stripping, capped diagnostics, and default-budget latency isolation |
| Provider/config/CLI integration | Current development environment | 162 focused tests passed |
| Full source suite and static checks | Current development environment | 410 tests passed; compileall, Ruff format, Ruff lint, and diff checks passed |
| Live production path | `/usr/bin/codex`, expected `0.144.4` contract | Clean capability with `app_server_thread_list` and `schema_fingerprint`; canonical fingerprint `5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621`; 4 sessions; 767 ms |
| Reproducible wheel/source builds | Fixed `SOURCE_DATE_EPOCH=1784073600` | Byte-identical rebuilds passed the verifier; 23 package files, 28 wheel members, and 31 source-distribution members matched the exact allowlists |
| Installed artifacts | Separate fresh environments | Wheel and source distribution passed `pip check`, hook/doctor imports and command help, migration v4/current-registry creation, and isolated-XDG snapshot/list smoke tests |
| Existing DMS read-only consumer | Isolated retained read against the freshly installed current wheel; no live shell or service lifecycle | 92 Python and 16 JavaScript behavior groups passed, QML formatting matched, Ruff and Pyright passed, and the bridge accepted the current empty Snapshot v1 envelope. This does not claim Phase 3 DMS action or presentation parity. |

## Remaining implementation scope

- Claude supervisor discovery with capability-gated fallback, hook ingestion,
  process/supervisor liveness, and normalized runtime transitions.
- Project-aware new-session preparation after the Claude provider foundation.
- Pull-based remote snapshots, caching, and remote actions.
- Searchable TUI, curation/handoffs, context retrieval, and session-scoped
  agent tools.

Existing local Codex resume preparation, tmux surface actions, and DMS
niri/Ghostty parity were completed later in Phase 3A; see
[`docs/phase-3a-validation.md`](phase-3a-validation.md).
