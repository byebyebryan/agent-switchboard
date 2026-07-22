# Runtime Operations and Safety

Date: 2026-07-22

Status: normative for all post-Phase 6E development and operation

## Primary invariant

Switchboard is disposable development infrastructure. Existing Codex and
Claude Code sessions are user work and are not disposable.

Installing, upgrading, testing, resetting, repairing, or removing Switchboard
must never require an existing agent session to stop, restart, detach, or
resume. If Switchboard cannot change safely while agents continue running,
Switchboard remains offline or on its prior version until a safe path exists.

## Ownership boundary

Switchboard may observe provider sessions discovered through bounded provider
interfaces. Observation does not grant lifecycle authority.

Switchboard may focus, fence, transition, or stop only an exact provider
surface that it launched, whose current process and tmux identity still match,
and only through the user or agent action that owns that lifecycle operation.
It never kills a tmux server or treats an unrelated provider process as cleanup.

Switchboard configuration, registries, caches, generated views, releases, and
DMS state may be discarded during development. A reset removes only those
Switchboard-owned resources. Provider history, provider processes, user tmux
sessions, repositories, checkouts, and unrelated hook configuration remain
outside the reset boundary.

Remote hosts update independently. No release or reset requires a coordinated
agent outage across hosts.

## Global hooks

Provider hook files are global, but Switchboard authority is pane-local.
Therefore:

- when capability, session, and generation markers are absent, the hook exits
  successfully without reading or writing Switchboard state;
- sessions launched before the generation marker, sessions whose recorded
  generation is no longer current, and sessions whose Switchboard state was
  discarded also exit successfully as deliberately unmanaged;
- when only part of current-generation authority is present, or its managed
  evidence is invalid, the hook fails closed with one bounded, content-free
  diagnostic; and
- hook installation and removal edits only handlers owned by the current
  Switchboard hook identity and preserves all unrelated provider settings.

A broken hook is contained by removing only Switchboard-owned handlers. Agent
sessions continue running while the hook is repaired and tested in isolation.

## SSH-first view access

DMS is an optional desktop picker, not an attachment requirement. From a plain
SSH shell, list durable views and attach directly through the owner-host tmux
server:

```sh
swbctl view list
swbctl view attach --view <view-id>
```

`view attach` revalidates the view, creates and claims its own bounded local
attachment lease, and then execs the exact tmux attachment. It never starts or
resumes a provider. `frame reopen` must finish provider launch and project the
exact surface into the persistent view before it reports success.

Do not run `codex resume` or `claude --resume` after Switchboard has already
opened the managed surface; that would create a second runtime for the same
provider session.

## Development and release workflow

Builds, migrations, DMS adapters, hook behavior, and tmux mechanics are first
validated against temporary config/state roots and isolated tmux servers. Live
acceptance uses new disposable Switchboard views and test provider sessions.

An installed managed session may keep using the immutable release it started
with. A new release becomes the route for new Switchboard actions without
rewriting or stopping that process. State that cannot be migrated safely is
abandoned and recreated; the agent continues independently.

The Phase 6E two-host coordinator was a one-time activation artifact and is not
an operational update mechanism. Its exact executed copy and evidence remain in
the private activation workspace; it is intentionally absent from release and
source-distribution surfaces after acceptance.

## Fresh initialization and reset

`swbctl init --config TEMPLATE` is the normal first-start path. `TEMPLATE` is
Config v3 and may omit `generation_id`; Switchboard allocates and canonically
binds a new ID, creates an empty committed registry, materializes only the
declared catalog, fsyncs both generation directories, validates their final
paths, and atomically publishes `state/current`. It performs no provider probe,
provider launch, hook edit, DMS action, or tmux action.

Reset uses the current configuration unless a replacement template is supplied:

```sh
generation=$(swbctl state host --json | jq -r .generationId)
swbctl reset --confirm-generation "$generation"
```

The exact generation confirmation is a compare-and-swap guard. Reset publishes
a new empty committed generation and retains the previous generation on disk.
It does not retire a view, move or kill a pane, stop a provider, edit hooks,
restart DMS, or kill a tmux server. Old managed views consequently become
unmanaged but remain attachable through tmux; their provider processes continue
unchanged. DMS observes the new generation on its next ordinary refresh.

Hook installation remains a separate opt-in operation after initialization:

```sh
swbctl hooks install --provider codex
swbctl hooks install --provider claude
```

Install only providers that will launch new Switchboard-managed sessions. An
unmanaged provider environment remains a successful no-op even while these
global handlers are installed.
