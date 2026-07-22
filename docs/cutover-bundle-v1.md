# CutoverBundle v1 and Generation Activation

Date: 2026-07-22

Status: implemented and validated in the core `0.3.0` clean-break runtime

This is the retained historical import format for the completed `0.2` to `0.3`
clean break, not a live update procedure. Its quiescence rules apply only to
that legacy Switchboard source. The one-shot coordinator is retired; current
operations follow [Runtime Operations and Safety](operations.md) and never stop
existing provider sessions for Switchboard maintenance.

Cutover is an offline, one-way salvage path from exactly registry schema 10,
protocol 2, and Config v2 into the fresh Phase 6 baseline. The normal Phase 6
runtime never opens or migrates the legacy files. Export holds one read
transaction over a read-only source, verifies integrity and quiescence, and
leaves the source byte-for-byte unchanged.

## Canonical bundle

The UTF-8 JSON document is at most 16 MiB, permits at most 20,000 records per
collection, rejects duplicate JSON keys and every unknown or missing field,
normalizes bounded text to NFC, and carries a lowercase SHA-256 `bundleHash`
over the canonical body. Arrays are sorted by stable identity before hashing.

The exact top-level shape is:

```text
bundleVersion = 1
source
configuration
catalog
providerSessions[]
handoffs[]
historicalTasks[]
bundleHash
```

`source` contains `schemaVersion`, `protocolVersion`, `configVersion`,
`hostId`, `exportedAt`, and `quiescent`. `configuration` contains the aligned
host display name plus providers, remotes, defaults, tmux, hooks, and optional
memory settings. `catalog` contains projects, repositories, project-repository
memberships, and host-owned checkouts.

Provider-session records preserve the exact host-qualified session key,
provider UUID, optional catalog association, curated name and purpose, pin,
resumability, last-known activity, and timestamps. Handoffs preserve exact
session linkage, sequence, bounded summary/next action, source host, canonical
content hash, and creation time. Historical tasks preserve their audit fields
only; the importer does not create Frames, WorkContexts, placements, launches,
surfaces, leases, or capabilities from them.

The exporter rejects a source with an active launch, a live provider runtime,
an unretired managed surface, mismatched config/catalog identity, broken
reference, unsupported version, corrupt handoff hash, or failed SQLite
integrity check. It writes an immutable export directory containing the exact
legacy database backup, Config v2, bundle, and hash manifest: directory mode
`0500`, file mode `0400`.

## Generation publication

Import creates config and state under the same opaque generation ID, imports
only catalog/session/handoff evidence, checkpoints the fresh database, writes
the exact bundle and manifest, fsyncs files and parent directories, renames
both generation directories into place, validates them through their final
paths, and only then atomically replaces `state/current`.

```text
config/generations/<id>/config.toml
state/generations/<id>/switchboard.db
state/generations/<id>/cutover-bundle.json
state/generations/<id>/cutover-manifest.json
state/current -> generations/<id>
```

Startup resolves the pointer before open and revalidates it afterward. Config,
database metadata, manifest, and directory identity must agree. Files must be
private regular files reached through the exact generation path. A missing,
unsafe, changed, mismatched, or one-sided active generation fails closed.

## Activation and failure behavior

An imported generation is `cutover_staged`. Host/Navigator state and discovery
may be inspected, while view/frame mutation, provider work, hooks, agent tools,
and desktop actions return `cutover_staged`.

`commit --evidence PATH` accepts only canonical `CutoverEvidence v1`. The
document records the exact core `0.3.0` and DMS `0.5.0` Git commits and artifact
hashes; distinct `desktop_primary` and `remote_owner` host/generation IDs;
strict provider-version observations; HostState and NavigatorState hashes from
both staged generations; the desktop DMS process-start identity and cold/model/
warm-cache hashes; and evidence hashes for every named acceptance check. The
active generation must be one of the two recorded generations, capture must
follow generation creation, and commit must follow capture.

The accepted canonical document is stored privately as
`cutover-evidence.json` beside the registry before the database crosses to
`committed`. A retry must supply byte-identical evidence. Conflicting evidence
fails closed; a committed generation without its evidence file is invalid.
Commit is idempotent and irreversible. `rollback` may restore the previous
pointer only before commit. After commit, recovery is forward-only unless the
operator performs a separate offline restore.

Crash recovery is deterministic at four publication boundaries:

| Last completed boundary | Visible current generation | Recovery |
| --- | --- | --- |
| files fsynced | old/none | remove private staging directories |
| config published | old/none | remove inactive one-sided generation |
| state published | old/none | remove inactive staged pair |
| pointer switched | new staged | retain and validate active generation |

Recovery never removes a committed inactive generation and never repairs an
active torn generation by guessing. The same import may be retried after safe
pre-pointer cleanup.
