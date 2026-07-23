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

## Pre-adoption coexistence

Switchboard has not replaced the user's native workflow. Until an explicit
adoption decision follows TUI-first acceptance, normal Codex, Claude Code, and
tmux work continues independently of Switchboard.

The default development boundary is:

- temporary Switchboard config/state roots and an isolated tmux socket;
- disposable Switchboard views and new test provider sessions only;
- temporary provider homes for hook and launch acceptance;
- no stop, close, restart, detach, resume, or hook edit for an existing native
  agent session; and
- no DMS plugin replacement, service restart, cache requirement, or compositor
  action as a core test or release prerequisite.

An explicitly selected Switchboard-managed test view may be disrupted by the
test that owns it. That authority does not extend to another view, the user's
normal tmux server, a discovered provider session, or any host-wide agent
process. A full workflow cutover is a future user decision, not something an
installer, migration, acceptance script, or release may infer.

## Ownership boundary

Switchboard may observe provider sessions discovered through bounded provider
interfaces. Observation does not grant lifecycle authority.

Switchboard may focus, fence, transition, or stop only an exact provider
surface that it launched, whose current process and tmux identity still match,
and only through the user or agent action that owns that lifecycle operation.
It never kills a tmux server or treats an unrelated provider process as cleanup.

Switchboard configuration, registries, caches, generated views, releases, and
optional-adapter state may be discarded during development. A reset removes
only those Switchboard-owned resources. Provider history, provider processes,
user tmux sessions, repositories, checkouts, and unrelated hook configuration
remain outside the reset boundary.

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
  Switchboard hook identity and preserves all unrelated provider settings;
- installation records the resolved immutable `swbctl` release path, not its
  mutable public symlink, and uses a ten-second provider timeout by default; and
- managed Codex launches explicitly forward only the MCP capability,
  Config/State roots, and isolated tmux socket by variable name; capability
  values never appear in provider argv or durable Codex configuration; and
- Codex hook trust remains an explicit `/hooks` review. Switchboard never edits
  Codex trust state programmatically.

A broken hook is contained by removing only Switchboard-owned handlers. Agent
sessions continue running while the hook is repaired and tested in isolation.

## TUI-first view access

The resident navigator is the primary interface. From a plain shell, enter a
project, exact view/frame, or recovery through the owner host:

```sh
swbctl view enter --host <host-id> --project <project-id>
swbctl view enter --host <host-id> --view <view-id> --mode direct
swbctl view list
swbctl view attach --view <view-id>
swbctl frame start --host <host-id> --frame <workspace-frame-id> \
  --request-id <uuid>
```

`view enter` creates/reuses and prepares the target, then attaches from a plain
shell, stays in place for the current managed view, switches one exact local
tmux client, or owner-preflights and replaces that client for a configured
remote. It never derives SSH endpoints from UI state. `view attach` remains the
lower-level exact-view path: it revalidates, creates and claims its own bounded local
attachment lease, and then execs the exact tmux attachment. It never starts or
resumes a provider. An empty foreground workspace is started explicitly with
`frame start`, or with `n` in the navigator. `frame start` and `frame reopen`
must finish provider launch and project the exact surface into the persistent
view before either reports success.

Do not run `codex resume` or `claude --resume` after Switchboard has already
opened the managed surface; that would create a second runtime for the same
provider session.

Direct mode remains available for a single native-provider pane. DMS is
deferred as a later optional desktop entry/focus adapter and is not required to
create, attach, recover, test, or release a view.

NavigatorState and `swbctl doctor` inspect each recorded view's exact tmux
server generation and shell topology without creating a server or mutating
state. A mismatch is projected as `degraded` with one bounded warning. Explicit
view entry remains the recovery boundary that may invalidate stale locators,
create a replacement shell, and require exact provider UUID resumption.

## Development and release workflow

Builds, migrations, hook behavior, navigator behavior, and tmux mechanics are
first validated against temporary config/state roots and isolated tmux servers.
Live acceptance uses new disposable Switchboard views and test provider
sessions. DMS adapter work is deferred and does not participate in these gates.

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
unchanged. A future optional adapter observes the new generation on its next
ordinary refresh.

Hook installation remains a separate opt-in operation after initialization:

```sh
swbctl hooks install --provider codex
swbctl hooks install --provider claude
```

Install only providers that will launch new Switchboard-managed sessions. An
unmanaged provider environment remains a successful no-op even while these
global handlers are installed.
