# Open-source Product Landscape

Status: Research snapshot

Last updated: 2026-07-15

## Purpose

This note records the existing-product research that informs Agent
Switchboard's scope. It answers two questions:

1. Can an existing open-source product already provide the desired workflow?
2. If not, which existing designs should Agent Switchboard learn from without
   expanding into another agent orchestrator or terminal environment?

The comparison is deliberately limited to products with publicly available
source and an explicit open-source license. Paid or source-private products are
not candidates, even when their feature set is relevant.

This is a point-in-time assessment. Projects in this space are changing
quickly, so the source repositories remain authoritative.

## Evaluation Lens

Agent Switchboard needs a specific combination of capabilities:

- Treat Claude Code and Codex as equal provider-native session stores.
- Discover existing history, not only sessions launched by the manager.
- Keep provider session identity separate from tmux pane or window identity.
- Reassociate a terminal surface when its active provider session changes.
- Distinguish working, needs-input, ready, parked, stale, and offline states.
- Attach to or resume the exact selected session.
- Aggregate hosts using ordinary, non-interactive SSH and pull-based snapshots.
- Expose stable command and JSON contracts for TUI, DMS, niri, and scripts.
- Avoid a mandatory daemon in the first implementation.
- Avoid private transcript parsing as the primary correctness mechanism.

No surveyed product satisfies all of these requirements together.

## Market Shape

The open-source projects fall into four broad categories:

1. **Managed runtimes** create and own agent processes, tmux sessions, or
   worktrees. They provide rich lifecycle controls but usually cannot adopt an
   arbitrary terminal that was started elsewhere.
2. **Live observers** discover agents already running in tmux and infer or
   receive their state. They handle active work well but generally do not own
   durable provider history or multi-host aggregation.
3. **History browsers** read provider session stores and resume old
   conversations. They usually do not know which process or terminal currently
   owns a live conversation.
4. **Remote web supervisors** combine history, process control, approvals, and
   remote access behind a persistent web server. They solve a broader problem
   but replace the terminal-centric TUI/DMS workflow.

Agent Switchboard sits between these categories: a provider-native session
index and action router with replaceable frontends. It should not become an
agent execution engine, worktree manager, embedded terminal, or web IDE.

## Closest Products

### Agent Deck

Repository: [asheshgoplani/agent-deck](https://github.com/asheshgoplani/agent-deck)

License: MIT

[Agent Deck](https://github.com/asheshgoplani/agent-deck) is the closest
operational match. It provides a mature Go TUI, tmux-backed persistence,
Claude and Codex status tracking, native session IDs, machine-readable CLI
commands, and first-class SSH remotes. Remote sessions can be listed and
attached through normal SSH, and remote commands support JSON output.

Its primary object is nevertheless an Agent Deck instance. It creates tmux
sessions with an `agentdeck_*` prefix and documents existing tmux sessions as
untouched. No current import or adoption path was found for an arbitrary
already-running tmux pane. Claude has global conversation search and import;
Codex history does not have an equivalent first-class browser, although Agent
Deck persists and resumes Codex sessions that it already manages.

Agent Deck is the strongest replacement candidate if all future sessions are
launched through it. It is not a drop-in match when provider-native history and
externally launched terminals must remain first-class.

Useful prior art:

- SSH remote registration, listing, attachment, and JSON output.
- Separate manager instance IDs and provider-native session IDs.
- Hook fast paths combined with tmux polling and liveness repair.
- Durable restart behavior and native resume commands.
- Isolated tmux socket support for non-invasive evaluation.

### ccmux

Repository: [epilande/ccmux](https://github.com/epilande/ccmux)

License: MIT

[ccmux](https://github.com/epilande/ccmux) is the closest match for adopting
the user's current tmux workflow. It discovers supported agents in arbitrary
existing panes, correlates hook markers and TTYs to provider-native session
IDs, and exposes live state through a TUI, JSON CLI, REST API, and SSE stream.
It distinguishes idle, working, and attention reasons such as permission,
plan approval, and questions. It also surfaces Claude background agents.

Its architecture requires a local daemon on `127.0.0.1:2269`. It reads
provider transcripts and captured pane content in addition to hooks, and no
documented multi-host aggregation layer was found. Its durable model is
centered on live or recently finished tmux panes rather than a complete parked
session index.

Useful prior art:

- Hook marker files carrying PID, TTY, native session ID, and transcript path.
- Healing session-to-pane association after startup races or in-terminal
  session changes.
- Explicit attention reasons instead of one undifferentiated waiting state.
- A frontend-neutral event stream for reactive consumers.

### OpenSessions

Repository: [Ataraxy-Labs/opensessions](https://github.com/Ataraxy-Labs/opensessions)

License: MIT

[OpenSessions](https://github.com/Ataraxy-Labs/opensessions) is a Rust tmux
sidebar for existing Amp, Claude Code, Codex, and OpenCode sessions. It tracks
live state, thread-level unseen markers, repository context, and quick tmux
navigation. A local Rust server provides WebSocket state and an HTTP API for
agents and scripts to push status, progress, and logs.

It is explicitly local-only and tmux-only today. Its documented HTTP surface
is primarily a metadata write API; the full read model is coupled to its
sidebar protocol. Claude and Codex discovery read their JSONL stores directly.

Useful prior art:

- A compact sidebar interaction model for many tmux sessions.
- Per-thread unseen and terminal-state markers.
- Separating mux providers, agent watchers, protocol, and rendering packages.

### AgentHUD

Repository: [neochoon/agenthud](https://github.com/neochoon/agenthud)

License: MIT

[AgentHUD](https://github.com/neochoon/agenthud) merges Claude, Codex, Kiro,
and OpenCode session stores into one live TUI. It includes provider and
sub-agent identity, context usage, activity, JSON reports, and a stable
`follow --json` NDJSON feed intended for higher-level supervisors.

It is a read-only monitor rather than an attachment manager. It has no tmux
routing or remote-host aggregation. Its live state is inferred from transcript
tails, and its `waiting` state intentionally covers both a question and an
ordinary completed reply. That does not preserve Switchboard's distinction
between `needs-input` and `ready`.

Useful prior art:

- A machine-consumable, additive NDJSON event contract.
- Unified provider and sub-agent presentation.
- Provider schema documentation and parser contract tests.

### showagent

Repository: [aytzey/showagent](https://github.com/aytzey/showagent)

License: MIT

[showagent](https://github.com/aytzey/showagent) is the strongest history-first
TUI. It lists, searches, resumes, branches, and converts sessions across Codex,
Claude Code, Gemini CLI, OpenCode, jcode, and Pi. It is a static Go binary and
offers stable JSON listing plus exact provider-native resume commands.

It has no live process, tmux, or remote-host model. It reads provider session
stores directly and can write newly converted native-format sessions, making
it intentionally more coupled to private provider formats than Switchboard's
initial design permits.

Useful prior art:

- Exact resume-command recipes as plain data.
- Provider adapter and fixture organization.
- Cross-provider history search and scripting contracts.

### Yep Anywhere

Repository: [kzahel/yepanywhere](https://github.com/kzahel/yepanywhere)

License: MIT

[Yep Anywhere](https://github.com/kzahel/yepanywhere) is the closest functional
match outside the terminal-native category. It interoperates with existing
Claude and Codex CLI history, manages server-owned live processes, provides a
unified attention inbox, and can resume sessions started by other clients.
Experimental multi-machine support uses configured SSH hosts and history
synchronization.

Its product shape is deliberately different: a persistent Node server with a
browser and mobile UI, optional relay access, approvals, diffs, file uploads,
and push notifications. It replaces the desired TUI/DMS surface and expands
far beyond Switchboard's scope.

Useful prior art:

- Provider-history interoperability without a new conversation database.
- Attention-first grouping of many sessions.
- Clear separation between server-owned processes and imported history.

## Secondary and Adjacent Products

- [frayo44/agent-view](https://github.com/frayo44/agent-view) is an MIT
  tmux-session manager with SSH support. It owns the sessions it tracks and
  derives status mainly from pane-output patterns. Its current remote manager
  remembers only the last configured host, so it is not yet a general remote
  aggregator.
- [samleeney/tmux-agent-status](https://github.com/samleeney/tmux-agent-status)
  is a focused hook-driven tmux sidebar and switcher for Claude and Codex. It
  is useful status-state prior art but has no provider-history or cross-host
  index. The README declares an MIT license, although the repository currently
  lacks a standalone license file.
- [smtg-ai/claude-squad](https://github.com/smtg-ai/claude-squad),
  [standardagents/dmux](https://github.com/standardagents/dmux), and
  [raine/workmux](https://github.com/raine/workmux) focus on launching agents
  in isolated git worktrees. They are task/worktree orchestrators rather than
  provider-native session switchboards.
- [nmelo/initech](https://github.com/nmelo/initech) and
  [agent-cockpit/agent-cockpit](https://github.com/agent-cockpit/agent-cockpit)
  provide rich agent-owned runtimes, collaboration, or browser control planes.
  Their server and execution models are intentionally broader than this
  project's indexing and attachment role.

## Excluded Products

- [Quiver](https://github.com/quiveride/quiver-releases) advertises local and
  SSH Claude/Codex session management, but its public repository explicitly
  states that the source repository is private.
- Public repositories without an explicit license were not treated as
  open-source candidates.
- Paid or proprietary terminal managers, hosted agent services, and official
  proprietary desktop products were outside the research scope.

## Product Positioning

The defensible scope for Agent Switchboard is:

> A frontend-neutral, provider-native session index and attachment router for
> terminal-centric local and SSH workflows.

That positioning excludes features already served by mature products:

- No worktree or task orchestration.
- No embedded terminal emulator.
- No diff viewer, approval inbox, or file browser.
- No notification subsystem.
- No agent-to-agent dispatch or prompt automation.
- No mobile or hosted web interface.
- No transcript conversion.

The differentiation is the combination of provider-native identity, exact
surface reassociation, normalized attention state, pull-based SSH aggregation,
and stable interfaces shared by TUI, DMS, niri, tmux, and scripts.

## Implementation Consequences

1. Keep the core model independent of tmux and any specific frontend.
2. Treat provider-native queries and hooks as authoritative before considering
   transcript parsing.
3. Model a mutable surface attachment separately from provider session
   identity; ccmux demonstrates why this repair loop is necessary.
4. Keep remote hosts symmetrical: each host owns local state and exports a
   versioned snapshot over SSH, following the useful part of Agent Deck's
   remote model without introducing a fleet server.
5. Make status reasons structured enough to distinguish permission, question,
   completion, idle readiness, stale observation, and offline host state.
6. Keep `agentctl` output stable and additive so DMS and future frontends do
   not depend on implementation details.
7. Do not adopt another project's private transcript parser as a hidden source
   of truth. Such parsers can be optional compatibility fallbacks later.

## Pre-implementation Validation

Before freezing the provider and surface contracts, run two local spikes:

### Agent Deck spike

- Run Agent Deck on an isolated tmux socket.
- Import or resume one existing Claude conversation.
- Attempt the equivalent flow for an existing Codex conversation.
- Switch Claude conversations inside one managed terminal and observe whether
  the stored native ID follows the active conversation.
- Register one SSH host and inspect local and remote JSON output.
- Determine whether its JSON contracts are sufficient for a thin DMS adapter.

### ccmux spike

- Start it against existing unmanaged tmux sessions before installing hooks.
- Install hooks and verify Claude and Codex native-ID correlation.
- Switch the active Claude conversation inside one pane and verify rebinding.
- Compare working, permission, question, ready, and finished transitions.
- Inspect `show --json` and SSE payloads as possible contract references.

### Decision Gate

- If Agent Deck handles existing Claude and Codex history, in-terminal session
  switching, remotes, and DMS-oriented JSON without forcing an incompatible
  workflow, prefer an integration over a new core.
- If ccmux plus a small remote wrapper covers parked history and exact resume
  actions without inheriting an unwanted daemon/transcript dependency, reduce
  Switchboard to that integration layer.
- Otherwise continue with the current design, keeping the first release
  limited to discovery, normalized state, exact open/attach, SSH snapshots,
  TUI, and JSON commands.
