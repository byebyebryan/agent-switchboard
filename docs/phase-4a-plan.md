# Phase 4A Plan: Terminal-native TUI Vertical Slice

Date: 2026-07-19

Status: 4A.0 through 4A.2 complete; 4A.3 through 4A.5 planned

## Decision and sequencing

Phase 4 starts with a narrow terminal-native frontend over the completed local
core contract. This is the next vertical slice because local Codex and Claude
discovery, new/resume/history actions, safe Claude stop, tmux lifecycle, DMS
projection, desktop focus, and same-window dedup already have installed live
evidence.

Phase 4A does not begin the curation and agent-context work at the same time.
Naming, pinning, handoffs, wrapping, current-session mutation, agent tools,
memory integration, and remote hosts remain later increments. Separating those
features keeps the first TUI accountable to one question: can a terminal user
reliably find and route the provider-native sessions that Switchboard already
knows how to manage?

The command surface will be:

```text
swbctl tui
```

No second top-level executable is added in Phase 4A.

## Framework and dependency boundary

Phase 4A uses [Textual](https://textual.textualize.io/) for the terminal UI.
It provides asynchronous application primitives, clean application suspension,
and a headless `run_test()`/Pilot harness for deterministic interaction tests.

Textual is an optional packaging extra, not a core runtime dependency:

```text
pip install 'agent-switchboard[tui]'
```

The dependency-free core installation must continue to support every existing
non-TUI command. Invoking `swbctl tui` without the extra returns a concise,
actionable installation error without a traceback. The first implementation
commit records and tests an explicit compatible Textual version range rather
than accepting an unbounded dependency.

## Architectural boundary

The TUI is a frontend consumer of the installed public command protocols. It
does not import provider adapters, open the registry database, parse provider
transcripts, read private provider history, construct provider argv, or derive
tmux locators.

Its gateway invokes the same installed `swbctl` executable with fixed argument
arrays and validates the complete versioned JSON response before publishing a
new model:

- `snapshot --reconcile full --json` for an explicit or periodic refresh;
- `snapshot --reconcile none --json` for a retained read where appropriate;
- `prepare-open` for a known session;
- `prepare-new` for a configured project/location/provider target;
- `prepare-history` for Claude's provider-native history picker;
- `stop-session --json` for an eligible launch-owned Claude runtime;
- `select-surface` and `attach-surface` for final presentation.

The gateway captures bounded stdout and stderr, applies per-command timeouts,
and never uses a shell. A failed refresh leaves the last-good model visible and
adds an explicit frontend error; it does not replace known rows with an empty
view. Only one refresh may be in flight, and an explicit refresh coalesces with
an already-running periodic refresh.

## Terminal-local presentation

Phase 4A owns only the terminal from which it was invoked:

- In a plain terminal, a successful `attach` plan closes the TUI and replaces
  it with `swbctl attach-surface <surface-id>` in that terminal.
- Inside tmux, including a popup or dedicated manager session, the frontend
  resolves the exact current client from the inherited tmux context. It passes
  only that client to preparation and executes `select-surface --client` when
  the plan requests a switch; selecting its already-current surface is a
  validated no-op before the picker exits.
- A blocked plan stays in the TUI and presents its stable reason and message.

The TUI does not enumerate or focus niri windows, launch Ghostty, select a
different tmux client, proxy the provider terminal stream, or remain resident
after transferring control. Desktop focus and new OS-window launch remain the
separate DMS adapter's responsibility.
The TUI never advertises desktop-focus or terminal-launch capability, so a
`focus` plan is incompatible with this presentation context and is rejected.

Every open/new/history request uses a fresh UUID request ID. Retrying the same
in-flight user action retains its request ID until a terminal result is known;
a later independent action receives a new ID. Provider startup remains behind
the existing attach-before-start gate.

## Phase 4A user surface

### Primary list

The initial view contains the bounded Snapshot v1 fields already exposed by
core:

- project and location label;
- provider;
- session label;
- normalized activity and runtime presence;
- attachment state;
- host;
- working-directory display value;
- last activity; and
- degraded capability or session warnings.

The default ordering follows the design's attention order, then recency, with a
stable session-key tie-break. Search is local over already-bounded display
fields and does not query provider transcripts. Filters cover provider,
project, activity, runtime presence, and attachment state. Status uses text and
symbols in addition to color.

### Initial actions

Phase 4A includes:

- refresh and full reconcile;
- open a selected known session;
- start Codex or Claude from a configured local project/location;
- open Claude's native history picker for a configured location;
- safely stop a row only when its public Snapshot fields meet the documented
  conservative eligibility rule, with core revalidation remaining
  authoritative;
- inspect blocked actions, provider degradation, and frontend command errors;
  and
- cancel or quit without changing the selected session.

The TUI may group by project after the ungrouped interaction contract passes.
Fuzzy matching is desirable but not an acceptance dependency; deterministic
case-insensitive token matching is sufficient for the first slice.

## Explicit non-goals

Phase 4A does not add:

- project or location editing;
- arbitrary working-directory launch;
- pins, names, handoffs, continuation, or wrapping;
- current-session resolution or mutation;
- an MCP server, provider plugin, skill, or memory adapter;
- transcript preview or transcript search;
- provider-history enumeration or deletion;
- remote snapshot aggregation or remote actions;
- niri/Ghostty integration;
- a Switchboard daemon, event stream, terminal proxy, or background resident
  TUI; or
- new provider launch and lifecycle semantics.

## Implementation increments

### 4A.0: packaging and framework gate

- Add the optional `tui` dependency and lazy import boundary.
- Add `swbctl tui` help and the missing-extra error path.
- Prove that the base wheel/sdist remains dependency-free and reproducible.
- Record the tested Textual range and run one minimal headless application
  smoke before building product behavior.

### 4A.1: terminal context and action gateway

- Resolve plain-terminal versus exact current-tmux-client context without
  scanning for other clients.
- Implement fixed-argv asynchronous command execution, response size/time
  limits, cancellation, and last-good error behavior.
- Validate Snapshot v1, PresentationPlan v1, and SessionAction v1 through the
  public protocol types, without provider imports.
- Unit-test argv, request-ID reuse, timeouts, malformed output, nonzero exits,
  stale plans, and cancellation.

### 4A.2: pure frontend model

- Map a validated snapshot into stable rows, launch targets, capability state,
  filters, attention ordering, selection retention, and inspectable errors.
- Keep filtering and sorting independent of Textual widgets.
- Test empty, degraded, stale, Unicode, large bounded, and refresh-race models.

### 4A.3: read-only Textual application

- Render status, search, filters, selection, details, help, refresh state, and
  non-color-only status cues.
- Use headless Pilot tests at multiple terminal sizes for keyboard navigation,
  search, filter, resize, refresh, error inspection, and clean exit.
- Keep all provider and reconciliation work off the UI event loop.

### 4A.4: local actions and terminal handoff

- Add known-session open, configured new, Claude history, and safe stop.
- Translate only validated plans into current-client selection or in-place
  terminal attachment.
- Restore terminal state on blocked preparation, command failure, cancellation,
  or an attachment that fails before `exec`.
- Exercise plain-terminal and isolated tmux paths without making a model call.

### 4A.5: installed live acceptance

- Install the built artifact with its TUI extra into an isolated environment.
- Exercise a normal terminal, tmux popup, and dedicated manager session.
- Reopen the same controlled Codex and Claude sessions without duplicate
  provider processes, surfaces, clients, or windows.
- Start controlled new Codex and Claude surfaces only after a real client
  attaches; block prompts so no model turn can occur.
- Exercise Claude history selection/cancellation and safe stop while leaving
  unrelated live sessions untouched.
- Remove isolated registry/tmux state and test-owned processes; move any empty
  provider transcript created by the acceptance harness to desktop trash.

## Implementation checkpoint

As of 2026-07-19, the framework, command-boundary, and model foundation is
complete:

- `swbctl tui` loads Textual only from the optional `tui` extra, currently
  bounded to `textual>=8.2.8,<9`; the base install remains dependency-free and
  reports the exact install command when the extra is absent.
- A neutral executable resolver gives both hooks and the TUI an absolute
  `swbctl` path without coupling the frontend to hook configuration.
- Startup resolves either a plain terminal or the exact inherited tmux client.
  It does not enumerate, select, or affect any other tmux client.
- The asynchronous gateway executes fixed public `swbctl` argv without a
  shell, bounds both output streams and time, kills the whole child process
  group after timeout, overflow, or cancellation, and accepts only the three
  existing versioned response envelopes.
- Snapshot refreshes are coalesced and preserve the last valid snapshot while
  retaining an explicit frontend error for inspection.
- A pure standard-library frontend model projects stable session rows, declared
  local launch targets, provider capability state, conservative Claude stop
  eligibility, and inspectable source/frontend issues. It applies the existing
  display-status precedence, then recency and session-key ordering; filtering,
  Unicode token search, selection retention, snapshot staleness, and stale-race
  rejection remain independent of Textual widgets.

Widgets, user actions, terminal handoff, and installed live acceptance remain
in 4A.3 through 4A.5. This checkpoint made no provider calls and did not attach,
start, stop, or focus any session.

## Acceptance gates

Phase 4A is complete only when all of the following hold:

- `swbctl tui` works in a normal terminal, tmux popup, and dedicated tmux
  manager session.
- The base installation has no third-party runtime dependency and every
  existing CLI/package test still passes.
- The TUI consumes only versioned public command responses and fixed argv.
- Refresh is asynchronous, coalesced, bounded, and last-good preserving.
- Search, filters, ordering, selection, errors, and keyboard navigation have
  deterministic headless tests.
- State is understandable without color and the layout remains usable at the
  documented minimum terminal size.
- Open/new/history actions preserve attach-before-start and duplicate-runtime
  prevention.
- A tmux selection can affect only the invoking client; a plain-terminal
  selection replaces only the invoking terminal.
- Stop remains limited to the existing revalidated launch-owned Claude
  contract.
- Cancellation and failure restore the terminal and do not strand an unbound
  surface beyond the existing lease/cleanup contract.
- Installed no-model acceptance leaves unrelated Codex, Claude, tmux, DMS,
  Ghostty, and niri state untouched.

## Stop conditions

Stop and revise the plan rather than weakening the boundary if implementation
requires any of the following:

- provider imports, raw provider argv, private database reads, transcript
  parsing, or private history enumeration in the frontend;
- a shell command, unbounded subprocess output, or user-controlled argv
  interpolation;
- starting a provider before a real terminal client attaches;
- guessing a tmux client, selecting a client other than the invoker, or
  searching the desktop for a window;
- a resident frontend process after control transfers to the selected session;
- hiding stale, blocked, degraded, or malformed protocol state;
- a model request for automated or live acceptance; or
- curation, current-session authorization, memory, or remote transport changes
  needed merely to ship the session router.

## Review and commit shape

Keep the implementation reviewable in these commits unless the actual diff
shows a smaller split is clearer:

1. optional packaging, command stub, and framework smoke;
2. terminal context plus public-command gateway;
3. pure frontend model and deterministic tests;
4. Textual read-only UI and headless interaction tests;
5. actions, terminal handoff, isolated tmux tests, and live evidence.

Phase 4B can then add curation and immutable handoff contracts. Phase 4C can
add current-session-authorized agent tools and optional memory retrieval.
Neither is allowed to retroactively make Phase 4A depend on transcripts or a
daemon.
