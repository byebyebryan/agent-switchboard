# Phase 1 Validation

> Historical v1 record. Phase 4D replaces its location and Snapshot v1
> contracts with repositories, checkouts, tasks, and Snapshot v2.

Date: 2026-07-15

## Status and boundary

Phase 1 establishes the deterministic local core: package scaffolding, stable
host identity, strict configuration, provider-neutral domain/state logic,
versioned protocols, SQLite storage and migrations, and tests. It does not
claim provider discovery, hook installation, reconciliation commands, tmux
launch execution, DMS migration, remote SSH transport, or a TUI; those remain
later phases.

The final evidence table below is produced from clean installed Python 3.12
and 3.14 environments plus isolated PEP 517 builds. CI repeats source checks on
Python 3.12, 3.13, and 3.14 and performs the distribution checks on Python
3.12.

## Privacy and compatibility contract

Machine envelopes have independent schema and protocol versions. A receiver:

- rejects incompatible versions explicitly;
- requires typed project, location, session, runtime, and surface identities;
- rejects cross-host and inconsistent cross-record references;
- rejects prompts, transcripts, raw payloads, raw argv, credentials,
  authentication tokens, private keys, cookies, and terminal controls;
- accepts safe additive fields from a compatible sender but strips them when
  producing canonical output;
- retains explicit provider capability ranges, schema fingerprints, feature
  flags, and structured degraded reasons.

Only normalized routing and status metadata enters a snapshot or remote cache.
Provider-native systems remain authoritative for conversation content.

## Distribution contract

The project uses Hatchling's reproducible build mode. CI fixes
`SOURCE_DATE_EPOCH=1784073600`, performs two isolated PEP 517 builds, and
requires byte-identical wheels and source distributions.

The wheel contains only the import package, migration modules, console entry
point, wheel metadata, and the MIT license. The source distribution contains
that source plus the README, build metadata, `.gitignore`, MIT license, this
validation record, and the design it references. Neither artifact contains
tests, fixtures, caches, databases, prompts, credentials, or generated
bytecode.

Package metadata carries the `MIT` SPDX expression and identifies the included
license file. Artifact verification checks both declarations and exact license
contents.

## Verification procedure

```sh
python -m compileall -q src tests spikes
ruff format --check .
ruff check .
pytest
git diff --check

export SOURCE_DATE_EPOCH=1784073600
python -m build --outdir /tmp/switchboard-build-a
python -m build --outdir /tmp/switchboard-build-b
python scripts/verify_distributions.py \
  /tmp/switchboard-build-a /tmp/switchboard-build-b
```

Fresh Python 3.12 environments then install the built wheel and source
distribution separately with `--no-deps`. Both installations verify imports,
packaged migrations, creation of a current registry, and `swbctl --version` /
`swbctl --help`.

## Recorded evidence

The acceptance record below reports source checks, test counts, artifact
content counts and reproducibility, and clean wheel/source-distribution smoke
results. Exact artifact digests are emitted by `verify_distributions.py` and
the CI log rather than copied into this file: this file is itself included in
the source distribution, so embedding that archive's digest would make the
digest self-referential and stale on rebuild.

| Gate | Environment | Recorded result |
| --- | --- | --- |
| Bytecode and tests | CPython 3.12.11 | `compileall` passed; 159 tests passed |
| Bytecode and tests | CPython 3.14.6 | `compileall` passed; 159 tests passed |
| Static and repository checks | Ruff plus Git | 26 files formatted; lint, `git diff --check`, trailing-whitespace scan, workflow YAML, and 15 workflow shell scripts passed |
| SQLite safety coverage | CPython 3.12.11 and 3.14.6 | Migration serialization/rollback, foreign keys, launch leases, stale-write rejection, cross-record identity, and privacy regressions passed in the full suite |
| Reproducible distributions | Two isolated PEP 517 builds with the fixed epoch | Wheel and source distribution were byte-identical between builds; 11 package files, 16 wheel members, and 18 source-distribution members matched the exact allowlists |
| Installed artifacts | Separate fresh CPython 3.12 environments | Wheel and source distribution passed `pip check`, isolated imports, packaged-migration checks, current-registry creation, and `swbctl --version` / `swbctl --help` |
| Phase 0 foundations | Retained validation probes | SQLite reservation, isolated tmux handoff, and Codex app-server schema probes passed |
