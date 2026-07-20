# Switchboard

Switchboard is a local session router for provider-native coding-agent
sessions. The formal project name and technical namespace are **Agent
Switchboard** / `agent-switchboard`; user-facing surfaces use **Switchboard**.

The project preserves Codex and Claude Code as the owners of conversation
history and terminal interaction. Switchboard stores normalized routing and
status metadata, then hands the user back to the unmodified provider UI.

## Implementation status

The current checkout contains the Phase 1 core, the local Codex and Claude
provider foundations from Phase 2, Phase 3A existing-session presentation, and
the complete local managed-tmux paths for new, existing, and history-selected
Codex and Claude sessions:

- Python package and finalized `swbctl` executable name
- stable host identity and strict TOML configuration
- provider-neutral domain and state models
- versioned machine protocols and privacy validation
- SQLite schema, migrations, and registry operations
- production Codex `0.144.6` app-server discovery, normalization, atomic
  reconciliation, and canonical local snapshots
- bounded Claude Code `2.1.214` capability detection for the
  Agent-View-disabled profile without supervisor or transcript discovery
- retained no-refresh reads, structured provider degradation, and explicit
  snapshot-session truncation
- privacy-safe Codex lifecycle hook ingestion with atomic launch binding and
  deterministic event ordering
- privacy-safe Claude lifecycle ingestion with canonical prompt identity,
  retained exact PID/birth evidence, and foreground-only activity semantics
- bounded `/proc` and tmux reconciliation for normalized live activity,
  attachment, and parked state
- explicit, ownership-safe Codex hook installation and effective hook/trust
  diagnostics through the supported app-server contract
- ownership-safe Claude user-hook installation, profile diagnostics, and
  isolated zero-model effective-hook acceptance
- validated tmux surface discovery, creation, metadata, selection, attachment,
  and rollback without shell interpolation
- atomic existing-Codex preparation with live-pane adoption, resumable-session
  leases, waiting bootstrap, idempotent retries, and a final duplicate-runtime
  check before `codex resume`
- atomic existing-Claude preparation through the same managed-tmux lifecycle,
  with forced disabled Agent View and exact `claude --resume` execution
- exact process/session/tmux correlation that can atomically finish a pending
  resume when Codex omits the expected start hook
- atomic project/location/provider resolution and new-session preparation for
  Codex or Claude with an unbound waiting surface, attach-before-start
  bootstrap, exact hook/live identity binding, and same-request idempotency
- provider-native Claude history selection through an unbound managed surface
  running exact `claude --resume`, with hook binding after selection and
  fail-closed surface retirement after cancellation
- ownership-safe Claude stop actions that request exact interactive `/exit`,
  wait a bounded grace period, and restrict fallback termination to the
  revalidated launch-owned process group and tmux surface
- versioned focus, switch, attach, and blocked presentation plans consumed by
  the separate DMS integration
- random per-launch Codex and Claude agent capabilities stored only as digests, with
  exact current-pane/launch/surface/session authorization
- bounded configured-file and same-project context, project-scoped retained
  reads/search, and current-session-only name, handoff, and wrap commands
- a thin dependency-free stdio MCP projection of the same authorized service,
  plus an explicit, disabled-by-default bounded memory MCP adapter
- unit, migration, concurrency, provider, protocol, and packaging tests

Phase 3B implementation and live acceptance are complete in the core and
separate DMS adapter. Phase 2B implementation, Agent View cutover, and live
acceptance are also complete. Phase 3C reuses the Codex managed-tmux lifecycle
for known, new, and provider-history-selected Claude sessions, adds safe stop,
and projects those actions through the separate DMS adapter. Its completed
contract, provider lifecycle, live compositor focus, and same-window dedup
acceptance are recorded in
[`docs/phase-3c-plan.md`](docs/phase-3c-plan.md).
The terminal-native Phase 4A vertical slice has its optional Textual shell,
terminal-context resolver, bounded public-command gateway, and pure searchable
session/launch-target model in place. Its Textual view provides responsive
session navigation, search, filters, details, issues, help, and refresh. It can
open known sessions, start configured Codex or Claude sessions, enter Claude's
native history picker, and request confirmed safe stop through the existing
versioned core commands. Phase 4B now adds on-demand immutable handoff detail,
name and purpose editing, pinning, explicit handoff and wrap forms, and
continuation from the selected exact latest handoff. Successful routing selects
only the inherited tmux client or replaces the restored plain terminal with
`attach-surface`; installed no-model acceptance for the Phase 4A provider
contracts, plain terminal, dedicated tmux manager, popup, and complete action
matrix is recorded in
[`docs/phase-4a-plan.md`](docs/phase-4a-plan.md). Phase 4B installed isolated
acceptance is also complete; its implemented boundary and evidence are recorded in
[`docs/phase-4b-plan.md`](docs/phase-4b-plan.md). Phase 4C completes exact
session-scoped authorization for managed Codex and Claude launches, bounded
current-project context and retained-state search, current-session curation, a
thin stdio MCP transport, and an optional external memory MCP adapter. Its
contract and isolated installed evidence are recorded in
[`docs/phase-4c-plan.md`](docs/phase-4c-plan.md). Remote SSH transport remains
Phase 5. See
[the design](docs/design.md), the
[Phase 1 validation record](docs/phase-1-validation.md), and the
[Phase 2 validation record](docs/phase-2-validation.md), the
[Phase 2B plan](docs/phase-2b-plan.md), and the
[Phase 3A validation record](docs/phase-3a-validation.md) for the implemented
boundary and evidence. The completed Codex vertical-slice contract is in the
[Phase 3B plan](docs/phase-3b-plan.md).

## Local commands

The implemented command surface emits one versioned snapshot envelope:

```sh
swbctl snapshot --json
swbctl snapshot --reconcile live --json
swbctl snapshot --reconcile full --json
swbctl list --json
swbctl list --refresh --json
swbctl show <session-key> --json
swbctl current --json
swbctl session name <session-key> <name> --json
swbctl session purpose <session-key> <purpose> --json
swbctl session pin <session-key> [--off] --json
swbctl session handoff <session-key> --json-stdin --json
swbctl session wrap <session-key> --json-stdin --json
swbctl agent current --json
swbctl agent context --json
swbctl agent sessions --json
swbctl agent show <session-key> --json
swbctl agent handoff-read <handoff-id> --json
swbctl agent handoffs <session-key> [--limit 20] --json
swbctl agent search <query> [--limit 20] --json
swbctl agent memory <query> [--limit 20] --json
swbctl agent name <name> --json
swbctl agent name --clear --json
swbctl agent handoff --json-stdin --json
swbctl agent wrap --json-stdin --json
swbctl agent-mcp
swbctl tui
swbctl hooks install --provider codex --dry-run
swbctl hooks uninstall --provider codex --dry-run
swbctl hooks install --provider claude --dry-run
swbctl hooks uninstall --provider claude --dry-run
swbctl doctor
swbctl prepare-open <session-key> --request-id <uuid> \
  --can-focus-desktop --can-launch-terminal --json
swbctl prepare-new --project <project-id> --location <location-id> \
  --provider codex|claude --request-id <uuid> \
  --can-focus-desktop --can-launch-terminal --json
swbctl prepare-new --from <handoff-id-or-session-key> --request-id <uuid> \
  --can-focus-desktop --can-launch-terminal --json
swbctl prepare-history --project <project-id> --location <location-id> \
  --request-id <uuid> --can-focus-desktop --can-launch-terminal --json
swbctl stop-session <claude-session-key> --json
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

`prepare-new` loads the current validated host configuration, resolves the
selected project, local location, and Codex or Claude provider, then creates an
unbound leased tmux surface. The provider starts without shell interpolation
only after a client attaches. Claude launches additionally force
`CLAUDE_CODE_DISABLE_AGENT_VIEW=1`. The bootstrap revalidates the project,
location, working directory, transport, and surface immediately before `exec`,
then renews a bounded five-minute identity binding grace; the first exact
lifecycle hook or complete live tmux/process correlation atomically assigns the
provider UUID and confirms the session/surface binding.

`prepare-new --from` continues from an immutable handoff without reading a
transcript or injecting provider prompt text. A handoff ID is exact; a session
key resolves its latest handoff in the same transaction that reserves the
launch. Project and location derive from the local source session, the source
provider is the default, and an explicit provider may switch between Codex and
Claude while preserving the exact lineage.

`prepare-history` follows the same attach-before-start lifecycle but launches
Claude's native `claude --resume` picker without supplying or discovering a
session UUID. The picker remains entirely provider-owned. A selected
conversation binds through its exact `SessionStart` UUID; a cancelled picker is
detected by complete tmux reconciliation, which retires the unbound surface and
fails the launch without manufacturing a session.

`stop-session` is intentionally narrower than a generic process killer. It
first performs live reconciliation and requires one confirmed active local
Claude surface whose current session, bound launch, exact tmux locator, process
birth, UID, and process-group ownership all agree. It sends an exact
interactive `/exit`, waits a bounded grace period, and only then may signal that
same launch-owned process group before retiring the exact surface. Already
stopped sessions are idempotent; unmanaged or ambiguous runtimes return a
structured blocked action. Claude history is never deleted.

Snapshot assembly reads a bounded deterministic session candidate set and
applies an actual UTF-8 byte budget. If sessions are omitted, the registry is
unchanged and the envelope includes `snapshot_sessions_truncated` with only
retained and emitted counts.

`show` and the `session` command family expose local human curation through a
separate bounded session-detail envelope; handoff bodies remain outside
Snapshot v1. Name, purpose, and pin changes do not masquerade as provider
observations. Handoff and wrap accept one strict bounded JSON object on stdin,
assign immutable sequence and hash state atomically, and support an optional
client-generated UUID for safe retry. `current` and mutation `--current`
resolve only an exact confirmed session surface inherited from the caller's
tmux pane; plain terminals, manager panes, and stale bindings fail closed.

The `agent` command family is issued only to managed Codex or Claude `new` and
exact `resume` surfaces. Every invocation requires the exact
inherited tmux pane, bound launch/surface/session identities, and a random
per-launch capability whose raw value exists only in that surface environment;
SQLite retains only its SHA-256 digest. The caller cannot provide a session or
project identity for authorization. Reads and search are then restricted to
the caller's configured local project; search covers only curated names,
purposes, and explicit handoffs. `context` additionally reads only explicitly
configured project-relative text sources. Mutations always target the caller.
Agent commands never reconcile providers, read transcripts, call a model, or
enter the DMS path.

`swbctl agent-mcp` exposes those exact operations as newline-delimited stdio
JSON-RPC after MCP initialization. It has no separate storage or authorization
logic and emits protocol frames only on stdout. Memory search remains disabled
unless `[memory]` names one absolute stdio MCP command and tool:

```toml
[memory]
enabled = true
command = ["/absolute/path/to/memory-mcp-server"]
tool = "search"
timeout_seconds = 5
```

Each memory query starts one bounded MCP exchange, passes only query, result
limit, and project name, strips all `AGENT_SWITCHBOARD_*` variables from the
child, accepts text content only, and returns an unavailable envelope on
absence, timeout, or protocol/tool failure. Switchboard never reads the
adapter's private databases or provider transcripts.

`swbctl tui` exposes those operations through the optional Textual frontend.
It loads handoff bodies only for the selected session, keeps the last-good list
and detail on failures, and uses fixed installed commands rather than importing
registry or provider internals. Press `a`/`p` to edit name or purpose, `v` to
pin, `g` to record a handoff, `w` to wrap, `c` to continue from the displayed
latest immutable handoff, and `d` to reload detail.

## Requirements and development setup

Python 3.12 or newer is required. The runtime package uses only the Python
standard library.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade build pytest ruff -e '.[tui]'
```

Run the local acceptance gates from the repository root:

```sh
.venv/bin/python -m compileall -q src tests spikes scripts
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
