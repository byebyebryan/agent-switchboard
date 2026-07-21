# Local Project Catalog Management Plan

Status: approved implementation plan; implementation in progress before live
Phase 5 SSH acceptance

## Goal

Close the local project-management loop before asking a second host to consume
it. Switchboard must let a user inspect, add, edit, archive, restore, export,
and import the project/repository/checkout catalog without hand-editing UUIDs.
Configuration v2 remains authoritative; SQLite continues to materialize the
declarations and retain tasks, sessions, discovered checkouts, and history.

The daily task path remains concise. DMS lists local projects and hands catalog
management to a focused TUI. The TUI keeps Open Tasks as its default and adds a
dedicated Projects view for the complete catalog workflow.

## Contract

Core adds a bounded `catalogVersion: 1` envelope and a `swbctl project`
surface for:

- read-only path classification, active/archived list, and detail;
- project add, metadata update, archive, and restore;
- repository add/link/update/primary/unlink;
- checkout add/update/default/archive/restore; and
- project export/import with stable global project/repository identity and new
  host-local checkout identity.

Mutations select immutable UUIDs, support dry-run, and return the exact affected
identities, before/after config hashes, and backup path. Archive/unlink actions
require explicit confirmation. Project/repository IDs are never derived from a
path, remote URL, branch, or HEAD. A new single-repository project receives
distinct random project and repository UUIDs plus one random host-local
checkout UUID.

Path inspection uses fixed-argv, no-network Git probes. It distinguishes an
already declared checkout, a new worktree of a known repository, an unknown Git
repository, and a non-Git directory. It never clones, creates, removes, or
otherwise mutates a repository or worktree.

Project export contains global metadata, repository membership, and stable
project/repository IDs. It excludes host IDs, checkout IDs and paths, SSH
configuration, Git administrative evidence, prompts, transcripts, and provider
state. Import requires a local path for the primary repository, generates
host-local checkout IDs, and fails closed on identity or metadata conflicts.

## Configuration safety

The core is the only writer. It locks and rereads the regular user-owned
configuration file, rejects symlinks and unsafe files, validates a complete
immutable candidate, renders canonical TOML, reparses it, then uses a mode-0600
temporary file, file and directory fsync, and atomic replacement. A real
change first stores the exact prior bytes as a timestamped mode-0600 backup
under XDG state. Dry-runs and no-ops create neither a backup nor a write.

Canonical writes may normalize comments and ordering. A concurrent source
change fails without replacement. Materialization and bounded read-only Git
discovery follow publication; the authoritative config and recorded backup
make a later registry failure recoverable.

## Lifecycle rules

Project archive and checkout archive preserve registry history and are
reversible. They are blocked by open tasks, pending launches, or live managed
sessions that depend on the target. Repository unlink is also blocked by any
retained task or session reference because Snapshot v2 requires the historical
project/repository membership.

TUI tasks may route through any declared member-repository checkout. DMS keeps
the fast path through the primary repository's declared default checkout.

## Frontends

`swbctl tui --view projects` opens the catalog manager; `--project ID` scopes
it, and `--add-project` opens the path-first wizard. Key `4` enters Projects
from the normal TUI. Active and Archived filters expose empty and recoverable
projects. Project detail covers names, aliases, provider defaults, repository
names/kinds/context sources, primary membership, checkout paths/labels/provider
overrides/defaults, and structural add/link/archive/restore actions.

DMS adds a Projects category containing one compact local project row plus Add
Project and Manage Projects actions. Those actions focus or launch one Ghostty
TUI manager; DMS never receives config paths or mutation payloads. A bounded
DMS-owned wrapper refreshes the persisted launcher model when the manager
exits, working around DMS 1.5's ignored `itemsChanged()` signal without leaving
a daemon.

## Delivery and acceptance

1. Contract and roadmap reconciliation.
2. Catalog v1, path inspection, atomic config writer, and backups.
3. Complete CLI mutations and export/import.
4. TUI catalog model, gateway, Projects view, and forms.
5. DMS Projects presentation, focused manager launch, and cache refresh.
6. Full deterministic gates and guarded installed acceptance.

Acceptance begins under private XDG roots and then uses only a test-owned
temporary catalog declaration with an exact config backup and cleanup. It must
not launch, stop, restart, or signal Codex, Claude, tmux provider sessions, or
DMS. Live SSH acceptance resumes only after this local workflow passes.
