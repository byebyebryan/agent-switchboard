# Phase 4D Plan: Repository, Checkout, and Task Identity

Date: 2026-07-19

Status: accepted design; implementation pending

## Decision

Phase 4D replaces the path-centric project-location contract with a clean v2
project/repository/checkout/task model before remote transport is implemented.
It is a coordinated `0.2.0` cutover in Agent Switchboard and the separate DMS
adapter. Runtime compatibility aliases for Snapshot v1, `locationId`, or
`--location` are deliberately not retained.

The stable hierarchy is:

```text
Project
+-- repository memberships
|   +-- host-local checkouts: main, worktree, or directory
+-- host-owned tasks
    +-- one routed checkout
    +-- one current top-level provider session
    +-- prior wrapped sessions and exact handoffs
```

Projects remain user-defined context and launch boundaries. Repositories are
stable codebase identities. Checkouts are concrete host-local filesystem views.
Tasks are the human workflow unit and may span sequential Codex or Claude
sessions. Provider sessions, runtimes, and surfaces retain their existing
provider-native and tmux-owned identities.

Phase 4D does not create, delete, move, prune, branch, commit, merge, or
otherwise mutate a Git repository or worktree. It discovers and models existing
state only. It also does not add backlog, dependency, scheduling, assignment,
prompt-dispatch, or multi-agent orchestration behavior.

## Domain contract

### Project and repository membership

`ProjectId` remains a globally stable configuration-owned UUID. A project may
declare multiple repository memberships, but exactly one declared membership
is primary when any repositories exist. Phase 4D routes launch and stable
context through only that primary repository. Additional memberships are
validated, materialized, and projected so later multi-repository task work does
not require another identity redesign.

`RepositoryId` is also a globally stable configuration-owned UUID. Repository
identity is never derived from a path, Git remote, branch, HEAD, or local Git
administrative directory. A repository has a bounded display name, a kind of
`git` or `directory`, and repository-relative context sources. Repeated
declarations of the same repository ID must agree on name, kind, and context
sources.

For the v1 migration, each project's UUID becomes its primary repository UUID.
This is valid because identifiers are typed and keeps migration deterministic.
Existing project context sources move to that repository. A legacy project
whose locations prove to be unrelated Git stores is rejected for manual split
rather than silently merged.

### Checkout

`CheckoutId` replaces `LocationId`. It remains a stable configured UUID for a
declared checkout and a stable registry UUID for a discovered linked worktree.
Existing location UUIDs are preserved exactly as checkout UUIDs.

A checkout records:

```text
checkout_id
repository_id
host_id
path
kind                  main | worktree | directory
display_name          optional
branch                 optional observation
head_oid               optional observation
provider_override      optional
transport_override     optional
is_default
declared
present
last_observed_at
```

Git common-directory, absolute Git-directory, and worktree administrative
evidence remain private registry fields. They are not Snapshot fields and are
never accepted from a frontend. Branch and HEAD are mutable observations, not
identity.

Each repository may have one declared default checkout per host. Discovered
worktrees are never promoted to default automatically. They inherit repository
and project defaults unless explicitly declared later.

An open task may use the shared main checkout, or claim one worktree. One
worktree can be claimed by only one open task. Closing a task releases the open
claim but retains its historical checkout association. Reopening fails with a
structured conflict if another open task now owns that worktree. Multiple open
tasks may use a main or directory checkout; the snapshot reports a warning when
multiple live current sessions share one mutable checkout.

### Task

`TaskId` is a random stable UUID owned by one host-local registry. Remote Phase
5 will expose and operate on the owning host's task; cross-host continuation
creates a new target-host task linked by the explicit handoff rather than a
distributed mutable task record.

A task records:

```text
task_id
host_id
project_id
checkout_id           optional until routable
title
purpose               optional
preferred_provider    optional
status                open | closed
pinned
current_session_key   optional
created_at
updated_at
closed_at             optional
```

Titles are required, bounded, normalized text and need not be unique. Purpose
is optional bounded multiline text. A task has at most one current top-level
provider session. Prior member sessions remain immutable task history and may
retain independent provider state, handoffs, and surfaces.

Task creation and session adoption are explicit. Reconciliation never creates
a task from a provider session, path, worktree, name, or continuation chain.
All sessions retained during migration receive `task_id = NULL` and remain in
Inbox until a human adopts them.

Binding a new task launch atomically assigns the provider session to the task
and advances `current_session_key`. It is permitted only when the task has no
current session or the prior current session is wrapped with an exact latest
handoff. A provider switch is therefore a continuation, not a second concurrent
top-level task runtime.

Closing a task with a current session requires an explicit handoff, appends it,
marks that session wrapped, and closes the task in one transaction. It does not
stop the runtime or retire the tmux surface. A never-started task may close
without a handoff. Reopening is human-only and revalidates the checkout claim.

## Configuration v2

A nonempty configuration declares `config_version = 2`. An absent file or
empty file continues to mean documented defaults. A nonempty legacy project
catalog fails with a bounded `config_migration_required` diagnostic.

The canonical shape is:

```toml
config_version = 2

[projects."PROJECT-UUID"]
name = "Agent Switchboard"
default_provider = "codex"
default_transport = "tmux"

[[projects."PROJECT-UUID".repositories]]
repository_id = "PROJECT-UUID"
name = "agent-switchboard"
kind = "git"
is_primary = true
context_sources = ["AGENTS.md", "README.md", "docs"]

[[projects."PROJECT-UUID".repositories.checkouts]]
checkout_id = "EXISTING-LOCATION-UUID"
display_name = "main"
path = "/home/bryan/code/agent-switchboard"
is_default = true
```

`swbctl config migrate-v2 --print` is a non-mutating migration assistant. It
accepts an explicit legacy input, validates it through a dedicated bounded v1
reader, probes only configured local paths with fixed Git argv, and emits
canonical v2 TOML to stdout. It never overwrites a config file. Ambiguous or
multi-store legacy projects fail with an actionable diagnostic.

The ordinary v2 parser accepts no `locations` key or location aliases.

## Bounded repository discovery and assignment

For each declared Git checkout, core runs fixed-argv, no-network Git probes
with explicit timeout and stdout/stderr limits. It resolves the worktree root,
absolute Git directory, common directory, and a NUL-delimited porcelain
worktree list. Invalid UTF-8, malformed framing, duplicate roots, paths outside
the observed worktree set, output overflow, timeout, or nonzero exit produce a
repository-scoped diagnostic without manufacturing state.

Discovered checkout records are matched to retained records by declared ID
first, then by repository/host and exact Git administrative evidence. A path or
branch change updates observation fields without changing a proven retained
checkout ID. Missing worktrees remain retained with `present = false` so tasks
and historical sessions do not lose their references.

Session assignment precedence is:

1. exact launch intent task/project/checkout identity;
2. exact retained checkout assignment;
3. Git top-level/common-directory association with one configured repository;
4. canonical longest-path containment for directory repositories; and
5. unassigned Inbox state with an ambiguity diagnostic.

Repository matching may assign a project and checkout, but never a task.

## Storage migration

Schema v7 replaces project locations with repositories, memberships, and
checkouts, renames every session and launch foreign key to `checkout_id`, moves
context sources to the migrated primary repository, and updates all invariant
triggers. Schema v8 adds tasks and nullable task foreign keys to sessions and
launches.

The migration preserves project IDs, location-as-checkout IDs, session keys,
launch IDs, surface IDs, handoff IDs, timestamps, curation, capability hashes,
and remote cache bytes. Existing tasks are not inferred. Snapshot v1 remote
caches remain stored but are marked incompatible until replaced by Snapshot
v2; they are not parsed as current state.

Migration validation maps registry protocol version 1 to schemas 1 through 6
and protocol version 2 to schemas 7 and later so a v6 registry can be validated
before applying v7. Every table rebuild runs with foreign keys disabled only
inside the migration transaction and finishes with `foreign_key_check` clean.

## Snapshot and command protocol v2

Snapshot v2 requires these bounded arrays:

```text
projects
projectRepositories
repositories
checkouts
tasks
sessions
runtimes
surfaces
capabilities
errors
```

Sessions contain optional `taskId` and `checkoutId`. Snapshot validation proves
project membership, primary repository constraints, checkout ownership,
task/checkout/project agreement, current-session backreferences, and the
existing surface/session invariants. Git-private fields never enter the
snapshot.

All public structured envelopes move to schema and protocol version 2. Core and
DMS package versions move to `0.2.0`; a v2 consumer rejects v1 with one bounded
incompatibility error.

The location command surface is replaced by:

```text
swbctl task list [--project ID] [--status open|closed] [--json]
swbctl task show TASK-ID [--json]
swbctl task create --task-id ID --project ID --title TEXT
                   [--checkout ID] [--provider codex|claude] [--json]
swbctl task adopt SESSION-KEY
                  (--task TASK-ID | --task-id ID --title TEXT)
                  [--project ID] [--checkout ID] [--json]
swbctl task title|purpose|pin ...
swbctl task handoff|close ... --json-stdin
swbctl task reopen TASK-ID [--json]
swbctl prepare-task TASK-ID --request-id ID ... --json
swbctl prepare-task --create TASK-ID --project ID --title TEXT
                    [--checkout ID] --provider codex|claude
                    --request-id ID ... --json
swbctl prepare-history --project ID --checkout ID ... --json
```

`prepare-task` opens an unwrapped current session, continues a wrapped current
session from its exact handoff, or starts the first session. A closed task is
blocked. New-task preparation creates the task and launch reservation in one
immediate transaction; caller-generated task and request UUIDs make retries
idempotent.

`prepare-open` remains available for exact Inbox and historical sessions.
`prepare-new` and all `--location` forms are removed.

## Agent tool v2

The session capability remains bound to one exact managed provider session and
surface. The authorized caller now resolves its optional task, repository, and
checkout in the same transaction. Project reads remain available to an Inbox
session with a valid project assignment; task mutation fails with
`task_not_assigned` until human adoption.

The stdio MCP tool list becomes:

```text
project_get_current       project_get_context       project_list_tasks
task_get                  task_get_handoff          task_list_handoffs
task_search               memory_search
task_update               task_set_handoff          task_close
```

`task_update` may change only the current task's title, purpose, or pin.
`task_set_handoff` appends to the current session. `task_close` requires the
explicit handoff and performs wrap-plus-close without stopping a runtime.
Creation, adoption, reopening, provider preference, and checkout changes are
not agent tools.

## TUI and DMS behavior

The TUI becomes the complete management surface. Its default view lists open
tasks, with separate Inbox and Closed views. It adds task creation, explicit
session adoption, checkout selection, task detail/history, close, reopen, and
continue flows. Inbox rows remain exact provider sessions and use
`prepare-open`.

The DMS private model advances to version 3 and projects one row per open task.
It implements native plugin categories with `All tasks`, one category per
declared project, `Inbox`, and `Closed`. The default result includes open task
rows plus one non-actionable Inbox summary, not every unassigned session.

Inside a selected project category, a nonempty query produces bounded
provider-specific creation rows whose title is the query. Selection generates
task and request UUIDs and invokes atomic new-task preparation in the default
checkout. Empty queries do not create unnamed tasks.

Task rows use:

```text
name     task title
comment  project | optional nondefault worktree/branch | state | age
icon     material:terminal       Codex
         material:auto_awesome   Claude
         material:task_alt       no current session
```

Absolute paths never appear in normal task rows. Safe Claude stop and native
history become context actions instead of duplicate list rows. Inbox category
rows remain provider sessions and use the same provider-specific icons.

The DMS bridge remains a strict configured-`swbctl` process consumer. It does
not import core internals, read SQLite, parse provider transcripts, invoke Git,
or gain provider/tmux ownership. Existing abnormal-exit process-group cleanup
and fault-injection coverage remain required.

## Acceptance and rollout

Automated acceptance uses temporary Git repositories with main and linked
worktrees, private XDG roots, private tmux servers, and fake providers. It does
not invoke a model, mutate a user Git repository, or touch a live provider
session.

Before dogfood cutover:

1. Back up the current config and SQLite registry with hashes and modes.
2. Prove v6-to-v8 migration against a copied state tree.
3. Review `config migrate-v2 --print` output and install it explicitly.
4. Install the no-cache core `0.2.0` build.
5. Reload the symlinked DMS plugin only after its v2 bridge and QML are ready.
6. Adopt the current Switchboard session into an explicit task.
7. Verify categories, Inbox summary, concise rows, and same-session reopen
   without another provider process, tmux surface, or desktop window.

Rollback restores the saved config and registry together, reinstalls the prior
core commit, and returns the DMS checkout to its prior commit. Source changes do
not trigger a live DMS reload during partial implementation.

Phase 4D is complete only when both repositories pass their full static/unit
gates, reproducible core distributions pass isolated install tests, the DMS
component harness passes on copied v2 state, live same-session reopen preserves
all runtime/window counts, and the current documentation contains no
unclassified normative `location` or Snapshot v1 contract.
