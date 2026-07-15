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
- Group sessions under explicit projects that can have locations on several
  hosts without turning the project into a task board or worktree owner.
- Start a focused session with the project's correct host, directory, provider,
  and an optional explicit handoff from prior work.
- Keep provider session identity separate from tmux pane or window identity.
- Reassociate a terminal surface when its active provider session changes.
- Distinguish working, needs-input, ready, parked, stale, and offline states.
- Attach to or resume the exact selected session.
- Expose bounded project context, session handoffs, and optional memory search to
  the current agent through stable read-mostly tools.
- Aggregate hosts using ordinary, non-interactive SSH and pull-based snapshots.
- Expose stable command and JSON contracts for TUI, DMS, niri, and scripts.
- Avoid a mandatory daemon in the first implementation.
- Avoid private transcript parsing as the primary correctness mechanism.

No surveyed product satisfies all of these requirements together while
preserving the intended terminal, deployment, and ownership boundaries.

## Market Shape

The open-source projects fall into five broad categories:

1. **Managed runtimes** create and own agent processes, tmux sessions, or
   worktrees. They provide rich lifecycle controls but usually cannot adopt an
   arbitrary terminal that was started elsewhere.
2. **Project and worktree workspaces** organize parallel sessions by repository,
   branch, worktree, or task. Project grouping is common, but these products
   normally create the sessions and own the development lifecycle around them.
3. **Live observers** discover agents already running in tmux and infer or
   receive their state. They handle active work well but generally do not own
   durable provider history or multi-host aggregation.
4. **History browsers** read provider session stores and resume old
   conversations. They usually do not know which process or terminal currently
   owns a live conversation.
5. **Remote web supervisors** combine history, process control, approvals, and
   remote access behind a persistent web server. They solve a broader problem
   but replace the terminal-centric TUI/DMS workflow.

Agent Switchboard sits between these categories: a provider-native project,
session, and context index with replaceable frontends. It should not become an
agent execution engine, worktree manager, task board, embedded terminal, or web
IDE.

## Capability Comparison

| Product | Adopts external sessions | Project model | Multi-host | Agent-readable context | Main mismatch |
| --- | --- | --- | --- | --- | --- |
| Happier | Yes, including live Codex, Claude, and OpenCode | Repositories and worktrees | Daemon plus relay | Local transcript memory tools | GUI-first client, wrapper/daemon path, and relay-owned message state |
| Agent Deck | Claude history import; managed live sessions | Manager instances and groups | Registered SSH remotes | No documented project-context tools | Owns prefixed tmux sessions |
| Agent Session Manager | No documented live-session adoption | Explicit projects/workspaces | No documented aggregation | Notes and history search are user-facing | Owns project tmux sessions; no stable API found |
| ccmux | Yes, for existing live tmux panes | Repository metadata, not durable projects | Local only | No documented project-context tools | Mandatory daemon and transcript/pane inspection |
| CCManager | Manager-owned sessions; Claude state can be copied | Projects and managed worktrees | No documented aggregation | No documented agent tool surface | Owns PTYs and Git lifecycle |
| Harnss | Imports Claude history; app-owned live sessions | Folders grouped into spaces | No documented aggregation | Per-project MCP configuration, not handoff retrieval | Full Electron workspace and message store |

This table is intentionally about ownership, not feature count. Happier is the
only surveyed product that strongly covers external adoption, projects,
multi-machine continuity, and agent-readable history together. Its GUI-first
client, machine daemon, relay, and optional transcript takeover conflict with
Switchboard's native-terminal and lightweight-runtime constraints, so it is
architectural precedent rather than a replacement candidate.

## Closest Products

### Happier

Repository: [happier-dev/happier](https://github.com/happier-dev/happier)

License: MIT

[Happier](https://github.com/happier-dev/happier) is the closest functional
match found in the expanded survey. It can browse, follow, and take over
existing Codex, Claude Code, and OpenCode sessions; move a live session between
machines while retaining its provider state and project directory; attach a
terminal to an app-launched session; and persist terminal-started sessions with
tmux. It also has repository-level project and worktree views, transcript
search, and machine-local memory search that agents can call as tools.

Its product and trust boundaries are substantially broader. The normal workflow
uses a wrapper CLI, a long-running machine daemon, and a relay server that stores
encrypted session messages and settings for web, desktop, and mobile clients.
It includes collaboration, approvals, queued prompts, voice control, file and
Git operations, and remote process control. Even when self-hosted, this replaces
more of the existing terminal workflow than Switchboard intends to own.

Useful prior art:

- Explicit distinction between following, importing, taking over, attaching,
  resuming, forking, and moving a provider session.
- Adoption of sessions started outside the manager.
- Project surfaces that choose an exact checkout before launching a session.
- Searchable local memory exposed to both users and agents.
- Cross-machine session identity and action routing.

Happier is not a build-versus-adopt candidate for the intended workflow. Even
its provider-backed Direct mode is presented through a separate application and
depends on a machine daemon and relay. Its adoption semantics, project model,
and memory retrieval remain useful prior art, but the product deliberately owns
more runtime and UI surface than Switchboard should.

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
launched through it. Its remaining gaps are provider-native parked history,
project/context semantics, and fit with the existing DMS integration.

Useful prior art:

- SSH remote registration, listing, attachment, and JSON output.
- Separate manager instance IDs and provider-native session IDs.
- Hook fast paths combined with tmux polling and liveness repair.
- Durable restart behavior and native resume commands.
- Isolated tmux socket support for non-invasive evaluation.

### Agent Session Manager

Repository:
[izll/agent-session-manager](https://github.com/izll/agent-session-manager)

License: MIT

[Agent Session Manager](https://github.com/izll/agent-session-manager) is the
closest TUI match for the new project-level workflow. It groups isolated session
lists into projects/workspaces, allows multiple named sessions in the same
directory, runs Codex, Claude, Gemini, Aider, OpenCode, and custom commands in
tmux, resumes provider conversations, and stores notes, favorites, and groups.
It also provides global history search and session/full-repository diff views.

It is a project-scoped runtime manager: one manager instance owns the tmux
sessions in a project. No documented path was found for adopting arbitrary live
tmux panes, aggregating several SSH hosts, or exposing project context as tools
to the running agent. Its notes and history search are nevertheless direct
precedent for Switchboard's handoff and retrieval surfaces.

Useful prior art:

- A compact project selector above multiple concurrent provider sessions.
- Named parallel sessions without requiring one worktree per conversation.
- User-authored notes and global provider-history search.
- Session groups and favorites as lightweight curation rather than tasks.

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

## Project-aware Managers

Project grouping, parallel sessions, search, and project-aware launch are already
well represented. These products are not direct fits because each also owns the
terminal, worktree, task, or application runtime around those sessions.

- [kbwo/ccmanager](https://github.com/kbwo/ccmanager) (MIT) manages Codex,
  Claude, and other providers across multiple projects and worktrees, with live
  status and per-project configuration. It is self-contained rather than
  tmux-based and creates, merges, and deletes worktrees. Its context-transfer
  feature copies Claude's private session files between worktrees, so it is
  provider-specific rather than a general handoff model.
- [OpenSource03/harnss](https://github.com/OpenSource03/harnss) (MIT) maps
  projects to folders, groups projects into spaces, scopes sessions and history
  per project, and provides full-text search across session titles and messages.
  It can import Claude CLI conversations and uses Codex app-server, but it is an
  Electron workspace with its own chat, terminal, Git, worktree, browser, file,
  and MCP surfaces.
- [agent-of-empires/agent-of-empires](https://github.com/agent-of-empires/agent-of-empires)
  (MIT) is a mature tmux-backed TUI and web manager with per-repository config,
  many providers, persistent sessions, worktrees, sandboxes, status, diffs, and
  direct tmux attachment. It can run on a remote host over SSH, but its dashboard
  manages its own prefixed tmux sessions rather than aggregating arbitrary
  provider sessions across configured hosts.
- [Nimbalyst/nimbalyst](https://github.com/Nimbalyst/nimbalyst) (MIT) is the
  maintained successor to the deprecated Crystal project. It provides
  project-level session tracking, parallel Codex and Claude sessions,
  search/resume, session-to-file links, worktrees, and a session Kanban. It also
  has an explicit task model that humans and agents can edit and execute, making
  it the broader project-management direction Switchboard is avoiding.

The design implication is not that Switchboard needs its own richer project
manager. It is that `Project` should remain a deliberately small join over
locations, sessions, launch defaults, and context sources. Project grouping by
itself is not differentiation.

## Context and Agent Tool Precedents

Several open-source systems demonstrate useful forms of agent-readable project
state, but all cross into orchestration:

- [fynnfluegge/agtx](https://github.com/fynnfluegge/agtx) (Apache-2.0) exposes a
  multi-project task board through MCP. Each task owns a worktree, tmux window,
  durable agent conversation, workflow phase, and orchestrator-controlled
  lifecycle. It validates the usefulness of agent-callable project state while
  also showing why Switchboard should not add a `Task` object.
- [SeemSeam/claude_codex_bridge](https://github.com/SeemSeam/claude_codex_bridge)
  (AGPL-3.0) is a project-level tmux workspace for mixed provider teams. A
  project config declares the agent topology, a daemon keeps runtime state
  alive, agents can delegate through explicit channels, and
  `.ccb/ccb_memory.md` supplies shared project memory. Its shared-memory pattern
  is useful precedent; its fixed team topology and inter-agent dispatch are not.
- [gastownhall/gastown](https://github.com/gastownhall/gastown) (MIT) models
  projects as rigs, persists work in a Git-backed issue ledger, and provides
  agent identities, mailboxes, handoffs, worktrees, supervisors, and merge
  queues. Its `seance` command can discover predecessor sessions and ask them
  questions, which is strong precedent for on-demand historical retrieval, but
  the overall system is a multi-agent operating environment rather than a
  session switchboard.

These products support a constrained tool surface: let the current agent read
project/session context and curate its own name and handoff. They do not justify
giving an agent authority to create tasks, advance workflows, control other
sessions, or mutate Git through Switchboard.

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
- [morapelker/hive](https://github.com/morapelker/hive) (MIT) is a desktop
  project/worktree manager with spaces, built-in Codex and Claude sessions, and
  cross-worktree context connections. It is useful project-navigation prior art
  but owns worktree and agent execution.
- [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) (Apache-2.0)
  combines project issues, agent workspaces, branches, terminals, dev servers,
  diff review, and pull requests. Its repository currently announces that the
  product is sunsetting, so it is historical task/workspace precedent rather
  than a replacement candidate.
- [nmelo/initech](https://github.com/nmelo/initech) and
  [agent-cockpit/agent-cockpit](https://github.com/agent-cockpit/agent-cockpit)
  provide rich agent-owned runtimes, collaboration, or browser control planes.
  Their server and execution models are intentionally broader than this
  project's indexing and attachment role.

## Excluded Products

- [Quiver](https://github.com/quiveride/quiver-releases) advertises local and
  SSH Claude/Codex session management, but its public repository explicitly
  states that the source repository is private.
- [superset-sh/superset](https://github.com/superset-sh/superset) uses the
  Elastic License 2.0, and
  [saltbo/agent-kanban](https://github.com/saltbo/agent-kanban) uses the
  Functional Source License 1.1. Both publish source but impose field-of-use or
  service restrictions, so neither was treated as open source here.
- [Dicklesworthstone/ntm](https://github.com/Dicklesworthstone/ntm) describes
  its license as MIT with an OpenAI/Anthropic rider. The rider denies rights to
  named parties and must remain on derivatives, so it is not an OSI-style open
  source license despite the useful tmux manager implementation.
- Public repositories without an explicit license were not treated as
  open-source candidates.
- Paid or proprietary terminal managers, hosted agent services, and official
  proprietary desktop products were outside the research scope.

## Product Positioning

The broader search confirms that this is a common and useful product category.
Open-source products already cover project grouping, parallel named sessions,
status, search/resume, worktrees, remote clients, and agent-readable memory.
Agent Switchboard should not claim any one of those features as novel.

The defensible scope for Agent Switchboard is:

> A project-aware, frontend-neutral index and attachment router for
> provider-native sessions and context in terminal-centric local and SSH
> workflows.

That positioning excludes features already served by mature products:

- No worktree or task orchestration.
- No embedded terminal emulator.
- No standalone desktop, web, or mobile agent client.
- No PTY proxy, conversation renderer, or persistent provider wrapper.
- No diff viewer, approval inbox, or file browser.
- No notification subsystem.
- No agent-to-agent dispatch or prompt automation.
- No transcript conversion.
- No relay-owned conversation store or mandatory wrapper CLI.

The differentiation is narrower than the first survey suggested. It is the
combination of provider-native parked history, project records independent of
tasks and worktrees, exact reassociation of managed surfaces, normalized
attention state, pull-based SSH aggregation without a
fleet service, unmodified provider-native terminal interaction, bounded
agent-facing context tools, and stable interfaces shared by TUI, DMS, niri,
tmux, and scripts. When the picker closes, Switchboard should leave no
host-wide controller, wrapper, or standalone UI running. Any optional
agent-tool process is scoped to the native provider session that requested it.

That combination is meaningful for this existing workflow, but it is an
integration product rather than a general agent workspace. If an existing core
can expose the required index and actions, a thin adapter is preferable to
building a competing session database.

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
6. Keep `swbctl` output stable and additive so DMS and future frontends do
   not depend on implementation details.
7. Do not adopt another project's private transcript parser as a hidden source
   of truth. Such parsers can be optional compatibility fallbacks later.
8. Keep `Project` as a small join over locations, sessions, launch defaults, and
   context sources. Do not add task state to compete with agtx or Nimbalyst.
9. Expose memory and history as bounded, attributed queries. Happier and Gas
   Town validate retrieval, but not automatic history injection.
10. Restrict agent mutations to the current session's name and handoff. CCB and
    agtx demonstrate how quickly broader tools become orchestration.
11. End every open action in the native Codex or Claude Code TUI. Never proxy or
    re-render the provider's terminal stream.
12. A launch bootstrap may wait for a real tmux client and perform final
    duplicate validation, but it must `exec` the provider and leave no wrapper
    around the running TUI. tmux owns terminal persistence.
13. Represent pre-provider work with a leased launch intent; a provider UUID
    cannot be the identity of an operation that must exist before the provider
    starts.
14. Give projects globally stable configured IDs so independent host registries
    can merge locations and sessions without path or Git-remote heuristics.
15. Keep host reachability outside session runtime state, and preserve completed
    attention independently of whether a provider worker still has a PID.
16. Treat Claude Agent View as a provider manager surface, not as proof that one
    specific conversation occupies the terminal.

## Pre-implementation Validation

The product survey already establishes that the closest alternatives own an
incompatible runtime, daemon, transcript, worktree, or UI boundary. Installing
all of them is therefore not an implementation gate. External-product spikes
remain optional when a specific reusable library or protocol question emerges.

Before implementation, run three focused local contract spikes against the
actual dependencies.

### Claude lifecycle spike

- Capture and validate `claude agents --all --json` for working, needs-input,
  completed-with-PID, completed-without-PID, attached, and stopped sessions.
- Observe hooks and supervisor JSON while starting, backgrounding, attaching,
  detaching into Agent View, switching through `/resume`, and attaching another
  session from Agent View.
- Determine exactly which transitions can confirm a terminal surface binding
  and which must degrade to an unknown binding.
- Verify one tmux workspace containing an Agent View manager window and several
  exact `claude attach` windows without creating extra desktop terminals.
- Repeat with Agent View disabled to prove the provider-native fallback.

### Codex lifecycle spike

- Generate and retain the installed app-server JSON schema, then capture
  paginated `thread/list` output and capability/version metadata.
- Measure normal and state-database-only list latency with the current history
  size so refresh policy is based on evidence.
- Capture `SessionStart`, `UserPromptSubmit`, `PermissionRequest`,
  `PostToolUse`, and `Stop` ordering for new and resumed sessions.
- Verify process/tmux reconciliation after the Codex process exits without a
  session-end hook.
- Verify coexistence with the currently installed claude-mem hooks and the
  provider trust flow.

### Launch and transport spike

- On an isolated tmux socket, create a waiting launch and prove the provider is
  not started before a real client views the target surface and that the
  bootstrap then replaces itself with the provider process. In a shared Claude
  workspace, a client viewing another window must not release the bootstrap.
- Run simultaneous prepare requests from separate processes and prove exactly
  one leased surface is created and idempotent retries return it.
- Fail terminal presentation and provider startup independently; verify lease
  expiry, diagnostic retention, and empty-surface cleanup.
- Exercise local attach, tmux `switch-client`, niri focus, Ghostty launch, and
  remote SSH prepare/select/attach using structured responses. A stale client
  ID must not switch another attached client.
- Confirm a remote preparation error is shown before a desktop terminal is
  launched.

### Decision Gate

- Do not implement per-session Claude focus from an evidence source that the
  lifecycle spike cannot confirm. Preserve correct session rows and degrade
  only the surface binding.
- Do not use Codex app-server fields outside captured, versioned contract
  fixtures. Unsupported versions must produce capability errors and retain
  registry-known sessions.
- Do not begin frontend migration until launch reservation, client-wait, `exec`,
  expiry, and concurrent duplicate tests pass.
- If any spike invalidates the no-daemon, no-wrapper, native-TUI boundary,
  narrow the affected feature instead of adding a hidden controller or terminal
  parser.
- Add agent context tools only after project identity, immutable handoffs, and
  current-session authorization are proven through the local workflow.
