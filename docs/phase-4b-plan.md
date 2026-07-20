# Phase 4B Plan: Local Curation and Immutable Handoffs

> Historical v1 plan and evidence. Phase 4D moves continuation and primary
> curation to explicit tasks while retaining immutable session handoffs.

Date: 2026-07-19

Status: complete; 4B.1-4B.5 implemented and accepted

## Decision and sequencing

Phase 4B adds the local human curation layer on top of the completed Phase 4A
terminal frontend. It exposes the session names, purposes, pins, immutable
handoffs, wrapping state, and continuation lineage that were deliberately
seeded in the original domain and storage model but do not yet have a public
workflow.

The implementation remains core first:

1. add dedicated atomic storage operations and read models;
2. expose bounded versioned CLI contracts;
3. connect exact handoffs to new-session preparation;
4. add the same operations to the installed TUI; and
5. run isolated installed acceptance without touching unrelated provider
   sessions.

Phase 4B is a local human-operated increment. It does not add the agent tool
server, session capabilities, memory retrieval, remote hosts, or DMS mutation
actions. Those boundaries remain Phase 4C or Phase 5.

The first implementation loop completed 4B.1 through 4B.3 as one vertical
core slice. It adds atomic local curation, a bounded versioned detail contract,
explicit and exact-tmux-current CLI operations, wrap/re-entry lifecycle rules,
and exact handoff continuation through launch reservation and binding. The
Snapshot v1 and provider-native no-prompt boundary remain unchanged. At that
checkpoint, TUI integration and installed isolated acceptance remained
separate 4B.4 and 4B.5 loops.

The second implementation loop completed 4B.4. The pure frontend now retains
bounded per-session detail and rejects stale results, while the installed
command gateway validates session-detail envelopes and sends strict bounded
JSON stdin. The Textual surface exposes name, purpose, pin, handoff, wrap, and
exact-handoff continuation operations without importing storage, provider, or
tmux implementation internals. Mutations apply their validated detail response
before a retained snapshot refresh, and detail or mutation failure preserves
the last-good list and cached detail. Installed isolated acceptance remains
the separate 4B.5 loop.

## 4B.0 substrate audit

The audit found that Phase 4B is primarily a public-operation and invariant
increment, not a new schema increment.

Already implemented:

- `AgentSession` carries `purpose`, `latest_handoff_id`, `wrapped_at`,
  `continued_from_handoff_id`, and `pinned`;
- `Handoff` is immutable, canonically normalized, bounded to 64 KiB per text
  field, content-hashed, sequenced, and source-attributed;
- the initial migration contains every curation field plus an append-only
  `handoffs` table, immutable update/delete triggers, and exact foreign-key
  references from launches and sessions;
- `Registry.append_handoff` atomically assigns a sequence and advances the
  session's latest-handoff pointer;
- launch requests already carry an optional exact `source_handoff_id`, and a
  newly bound provider session already stores that ID as continuation lineage;
- Snapshot v1 already projects the effective name, purpose, latest handoff,
  wrapping, continuation, and pin fields; name provenance remains private
  storage state; and
- the Phase 4A frontend model already retains purpose and pin state for search
  and presentation.

Missing or incomplete at the 4B.0 audit checkpoint:

- no dedicated curation mutation API exists; `upsert_session` is an observation
  merge with stale-time rules and must not be reused as the human editing API;
- handoffs can be appended but cannot be fetched or listed through a bounded
  storage or public read contract;
- wrapping is not an atomic append-and-mark operation;
- successful reopen/resume does not yet clear `wrapped_at`;
- `prepare-new` always stores a null source handoff, and no operation resolves a
  session's latest handoff inside the launch-reservation transaction;
- the CLI has no `show`, `current`, or `session` curation commands;
- the TUI gateway accepts only Snapshot, PresentationPlan, and SessionAction
  envelopes and cannot yet send bounded JSON standard input; and
- the TUI row does not yet expose wrapped state or fetch handoff content on
  demand.

No Phase 4B migration is planned at the outset. A migration is justified only
if implementation proves an invariant cannot be enforced atomically through
the existing schema and transaction boundary.

## Ownership and privacy boundary

The local registry owns explicit curation. Provider reconciliation continues
to own provider-observed metadata and must preserve curated values.

- A curated name records `name_source=curated`. Clearing it restores the
  retained provider name when one exists, otherwise the unknown/null state.
- Purpose, pin, wrapping, and handoff mutations do not advance
  `last_observed_at`; editing metadata is not evidence that the provider
  session was observed again.
- Local human handoffs use `source=user` and the local stable host ID. The
  caller cannot spoof `source`, `source_host_id`, sequence, timestamp, or hash.
- Phase 4B mutates only sessions owned by the local host. Imported and remote
  handoffs remain read-only until Phase 5 defines their transport envelope.
- Handoff input is explicit bounded summary and next-action text. Switchboard
  does not read a transcript, infer completion, copy a provider conversation,
  or submit the handoff as a provider prompt.

The database remains the authority for immutable handoff identity. Frontends
may supply a stable handoff UUID for idempotent retry, but they cannot rewrite
an existing ID with different content.

## Core curation contract

Dedicated registry operations will cover:

- get one local session detail and its latest handoff;
- list a bounded page of handoffs for one session in deterministic descending
  sequence order;
- set or clear a curated name;
- set or clear a purpose;
- set or clear a pin;
- append one user handoff; and
- atomically append a user handoff and set `wrapped_at`.

Every mutation re-reads the target inside `BEGIN IMMEDIATE`, verifies local
host ownership, and returns the committed session plus affected handoff. It
does not accept arbitrary field mappings.

Wrapping does not stop a runtime or remove provider-native history. A blocked
or failed open leaves wrapping unchanged. Returning to an already-live wrapped
session clears wrapping only after core resolves an executable, revalidated
surface plan; restarting a parked wrapped session clears it only after the
exact provider identity binds successfully. Prior handoffs remain immutable in
both cases.

## Public command and protocol surface

Phase 4B adds these local commands:

```text
swbctl show <session-key> [--json]
swbctl current [--json]
swbctl session name [<session-key>|--current] (<name>|--clear) [--json]
swbctl session purpose [<session-key>|--current] (<purpose>|--clear) [--json]
swbctl session pin [<session-key>|--current] [--off] [--json]
swbctl session handoff [<session-key>|--current] --json-stdin [--json]
swbctl session wrap [<session-key>|--current] --json-stdin [--json]
```

Handoff and wrap input accepts one bounded object:

```json
{
  "handoffId": "optional-client-generated-uuid",
  "summary": "What is complete or established.",
  "nextAction": "The concrete next action."
}
```

Unknown keys, invalid Unicode/control characters, oversize input, and trailing
JSON are rejected before mutation. The optional ID lets the TUI retain one ID
across an in-flight retry; the CLI generates an ID when an interactive human
omits it.

Machine reads and mutations return a dedicated versioned session-detail
envelope containing the bounded session projection and requested handoffs.
Snapshot v1 remains unchanged and does not absorb handoff bodies. Human output
is concise and never prints a provider transcript, private provider path, raw
tmux locator, launch capability, or provider argv.

The TUI continues to invoke fixed installed `swbctl` argument arrays. Its
gateway gains bounded standard input and validates the complete session-detail
response before changing the last-good model.

## Human current-session resolution

`current` and human `--current` resolve only through the inherited tmux
process context. Core reads the exact current pane's Switchboard metadata and
then verifies all of the following against SQLite and a fresh tmux inspection:

- the pane belongs to a non-retired local session surface;
- surface ID, provider, session key, and pane locator agree;
- the surface has a confirmed current-session binding; and
- the retained session points back to that surface.

A plain terminal, manager surface, unbound history/new surface, missing pane
metadata, or stale/mismatched binding fails closed. Resolution never scans for
another client or guesses from cwd, process ancestry, provider history, or the
most recently active session.

This is a same-user human convenience, not the Phase 4C agent authorization
contract. Phase 4C will add capability-bound attribution for structured agent
tools. Phase 4B does not install a provider skill, MCP server, or automatic
agent invocation.

## Exact continuation contract

Continuation extends preparation without injecting prompt text:

```text
swbctl prepare-new ... --from <handoff-id>|<session-key> ... --json
```

When `--from` is a handoff ID, core verifies the immutable handoff and its
source session. When it is a session key, the registry resolves that session's
current `latest_handoff_id` inside the same immediate transaction that reserves
the launch. The persisted launch always contains the exact handoff ID; it never
contains a floating latest-session reference.

The source session must be local, retained, and assigned to a valid local
project location. The continuation defaults to the source provider; an
explicit provider flag may switch Codex and Claude while retaining the same
project/location and exact handoff lineage. Missing handoff, project, location,
or enabled provider returns a stable blocked plan without creating a launch or
surface.

New-session binding already copies the launch's source handoff into
`continued_from_handoff_id`; Phase 4B adds tests that this happens for both hook
binding and runtime-repair binding. Handoff content stays in Switchboard and is
available through detail/TUI views. Provider prompt delivery and agent-side
retrieval remain outside this phase.

## TUI surface

The existing session list remains Snapshot-driven. Phase 4B adds:

- purpose, pinned, wrapped, and continuation cues in the row/detail model;
- on-demand bounded detail loading for latest and prior handoffs;
- edit flows for name and purpose;
- pin/unpin;
- handoff and wrap forms with explicit summary and next action;
- continuation from the selected session's exact latest handoff; and
- last-good refresh behavior after every mutation.

Forms validate locally for responsiveness, but core validation remains
authoritative. Cancel closes the form without issuing a command. A blocked or
malformed mutation remains visible as a frontend issue and does not discard the
previous session list/detail. The frontend never imports storage, provider, or
tmux implementation modules.

## Explicit non-goals

Phase 4B does not add:

- agent-facing MCP/plugin/skill tools or session capabilities;
- optional memory search, transcript search, or automatic handoff generation;
- prompt injection or sending messages to a provider session;
- remote snapshot/action transport or cross-host handoff import;
- DMS curation controls;
- project/location editing, task boards, queues, dependencies, or scheduling;
- provider-history deletion or archival;
- a daemon, resident TUI, or background polling service; or
- a new provider discovery or lifecycle path.

## Implementation increments

### 4B.0: substrate and contract audit

- Inventory domain, migration, storage, snapshot, launch, CLI, gateway, and TUI
  support.
- Lock the local-human versus Phase 4C agent boundary.
- Record the public operations, exact continuation semantics, and non-goals.

### 4B.1: atomic curation core

- Add bounded handoff get/list operations and dedicated session curation
  transactions.
- Add atomic append-and-wrap behavior and idempotent handoff replay.
- Clear wrapping only at the successful revalidated reopen/bind boundaries.
- Cover concurrency, stale observation interleaving, name provenance, local
  ownership, Unicode, bounds, and rollback.

### 4B.2: public detail and mutation commands

- Add the versioned session-detail envelope and strict parsers.
- Add `show`, human tmux `current`, and explicit-key/`--current` curation
  commands.
- Add bounded JSON stdin, stable errors, human formatting, and package/CLI
  smoke coverage.

### 4B.3: exact handoff continuation

- Extend new-session preparation with a handoff or session source.
- Resolve session-latest and reserve the launch in one transaction.
- Preserve exact lineage through Codex and Claude hook/runtime binding.
- Prove request-id idempotency, conflict handling, missing metadata blocking,
  and no provider prompt injection.

### 4B.4: TUI curation and continuation

- Extend the pure model and command gateway before adding widgets.
- Add bounded detail loading, forms, pin/wrap actions, and continuation.
- Exercise keyboard, resize, cancellation, stale detail, malformed output,
  concurrent refresh, and terminal handoff paths headlessly.

Implemented checkpoint:

- Snapshot rows expose purpose, pin, wrapped, latest-handoff, and continuation
  lineage cues; handoff bodies remain in an on-demand bounded detail cache.
- The gateway uses only fixed public argv, validates the complete returned
  detail for the requested session, and bounds both subprocess input and
  output with timeout/cancellation process-group cleanup.
- Edit and handoff modals validate locally, cancel without a command, and keep
  one client handoff UUID across an unchanged failed retry.
- Continuation passes the selected immutable latest handoff ID, so the launch
  cannot silently move to a concurrently appended handoff.
- Headless coverage proves row/detail rendering, keyboard and resize behavior,
  clear/cancel paths, pin/handoff/wrap operations, stale and failed detail
  retention, concurrent refresh, and plain-terminal continuation handoff.

### 4B.5: installed local acceptance

- Build and install the artifact with the TUI extra into an isolated root.
- Exercise explicit-key and verified tmux-current curation through installed
  commands.
- Prove immutable handoff replay, wrap/reopen clearing, and exact continuation
  lineage with private registry and tmux state.
- Exercise the installed TUI action matrix in plain terminal, popup, and
  dedicated manager contexts without a model turn.
- Confirm no pre-existing provider process, tmux client/server, registry,
  transcript, or desktop window was changed; remove only test-owned state.

### Installed local acceptance

The 2026-07-19 acceptance built the working tree based on commit `80fa0b8` as
a wheel and installed it into two fresh Python 3.14 environments under one
isolated root. The dependency-free environment and the environment with
`textual==8.2.8` both passed `pip check` and reported
`agent-switchboard==0.1.0`.

The base-install gate found one packaging-path defect before any session work:
`swbctl tui` imported `rich` before `textual`, but the actionable missing-extra
handler recognized only `textual`. The handler now recognizes either direct
optional import and has parameterized regression coverage. A rebuilt base
wheel then exited with the documented `pip install 'agent-switchboard[tui]'`
message, empty stdout, and no traceback; Textual remained absent from that
environment.

Installed commands ran against a private XDG root, registry, work directory,
and tmux socket:

- explicit-key name, purpose, pin, handoff, and wrap returned fully validated
  session-detail envelopes;
- replaying the same client handoff UUID and content retained one immutable
  row, while conflicting content failed without output or mutation;
- the retained provider name and provider identity remained intact, curation
  did not advance the original observation timestamp, two handoffs retained
  distinct sequence/hash state, and Snapshot v1 contained no handoff body;
- `current` and a purpose mutation with `--current` succeeded only from the
  exact metadata-bound private pane, while a plain-terminal `current` failed
  closed;
- wrap set `wrapped_at`, and a successful installed `prepare-open` revalidated
  the private surface, returned an attach plan, and cleared wrapping; and
- installed `prepare-new --from <exact-handoff-id>` persisted that ID before
  presentation, then test-owned binding retained the same continuation lineage
  for both Codex and Claude sessions.

Provider capability reconciliation used only the repository's bounded fake
executables. Codex received version, schema, and empty app-server discovery
requests; Claude received only `--version`. Neither fake received an
interactive resume/new invocation, and no model or provider transcript was
created.

The installed Textual frontend then ran through real bounded PTYs with a strict
fixture executable at its public `swbctl` boundary:

- the plain-terminal matrix loaded bounded detail, cleared a name, edited a
  purpose, toggled pinning, submitted handoff and wrap JSON stdin with distinct
  client UUIDs, reloaded detail, and prepared continuation from the displayed
  exact handoff before exiting cleanly;
- a dedicated manager client rendered, loaded detail, and issued continuation
  with only its inherited private client TTY; and
- a popup did the same, then returned to the unchanged attached manager pane
  and client.

All fixture continuation plans remained deliberately blocked, so the frontend
stayed resident until explicit clean exit and never attached or started a
provider. The harness bounded PTY output and process lifetimes and found no
traceback. Cleanup stopped only the private tmux server and moved the three
test-owned temporary roots to desktop trash; no test process remained, and the
pre-existing tmux socket set returned to its exact baseline.

The developer's live Claude process count and default registry digest changed
repeatedly while acceptance ran, consistent with the explicitly concurrent
active Claude work and hooks. The harness therefore did not claim that live
state was static: every installed command used the private XDG root and fake
provider paths, every tmux operation named the private socket, and cleanup
signalled only the private server/test process groups. The real Codex process
count remained stable, and no acceptance command invoked a real provider,
default registry command, desktop focus operation, or DMS action.

## Acceptance gates

Phase 4B is complete only when:

- curation never reuses observation timestamps or overwrites provider-owned
  identity/runtime state;
- handoffs remain append-only, bounded, hashed, ordered, and idempotent under
  concurrent writes;
- wrap commits its handoff and timestamp atomically and only successful
  re-entry clears the timestamp;
- all machine output is fully validated and handoff bodies remain outside the
  host snapshot;
- `--current` succeeds only for the exact confirmed inherited tmux pane;
- continuation persists one exact handoff ID before a surface is created and
  both providers retain it after binding;
- the TUI uses only installed public commands and preserves its last-good model
  on every failure path;
- the dependency-free base installation still works without Textual;
- the full unit, integration, protocol, package, Ruff, compile, and distribution
  suites pass; and
- installed isolated acceptance leaves unrelated live work untouched.

Phase 4C can then define capability-authorized current-session agent tools and
bounded context/memory retrieval on top of these stable human curation and
handoff operations.
