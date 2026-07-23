# Phase 6F Terminal-Native Acceptance

Date: 2026-07-22

Status: complete and accepted

Phase 6F completes the resident navigator and terminal-native managed-session
path. Workflow adoption remains a later explicit user decision. Acceptance did
not restart DMS, replace the user's tmux server, or claim authority over
existing Codex or Claude Code sessions.

## Implemented behavior

- `view enter` resolves a project, exact view/frame, or recovery on its owner
  host and selects navigator or direct mode in one command.
- A plain shell execs the exact tmux or configured SSH attachment. A managed
  client stays in place for its current view, switches exactly once for another
  local view, or is replaced only after remote owner preflight.
- Multiple/no exact invoking-client matches block. Cross-host replacement
  carries bounded pane-local hop evidence and the fifth nested hop blocks with
  an exact detach-and-enter-directly instruction.
- Manual focus fences the source before movement, requires an input-fenced live
  target, and atomically commits placements, view cursor, and same-WorkContext
  foreground authority. Known/uncertain background activity requires explicit
  confirmation.
- A provably restored pre-commit failure returns the view to ready with source
  authority unchanged. Uncertain presentation opens recovery and leaves both
  panes input-fenced.
- The resident Textual app has `Views`, `Projects`, `Tasks`, `History`,
  `Recovery`, and `Settings`. Breadcrumb, activity, attention,
  transition/control, and action status remain visible while one bounded
  asynchronous action runs.
- `n` starts the configured provider in an empty foreground workspace. The
  equivalent owner-local command is `frame start`; project/view entry remains
  navigation-only and does not add a provider picker.
- First-session membership, launch/surface staging, placement ownership, and
  WorkContext acquisition commit atomically. Pre-execution failure restores the
  exact empty workspace; ambiguity after provider execution opens recovery.
- Local state refreshes once per second. Remote state refreshes at the
  configured interval or explicitly with `r`. Structured action failures remain
  visible; unsafe background transfer is retried only after `y` confirmation.
- Closed frames retain bounded provider/runtime/activity/session-count evidence
  in read-only History and never become a project entry target.
- `b` performs Back, `c` performs Human close, and `d` selects direct mode. A
  capability-bound `switchboard_mode("navigator")` tool returns from direct
  mode without a global tmux binding or mandatory skill.

Phase 6F task-to-task selection navigates among existing open sibling frames.
It does not create recursive children or generalize return across nested
ancestry; that remains Phase 6G.

## Automated evidence

The earlier terminal-shell implementation commit
`0477332b440802e3501b8499ea6bc37b404c15ee` passed 113 tests on both the local
host and `snap.lan`. That evidence did not start a managed workspace from the
public TUI and therefore did not close Phase 6F.

The `0.3.2` closure candidate includes:

- fresh project-view to managed workspace start without direct registry
  seeding;
- exact pre-provider rollback of session, surface, placement, WorkContext, and
  staged pane;
- immediate task-push eligibility after first-session binding;
- navigator `n` routing through public `frame start`;
- imported-session WorkContext reacquisition;
- stopped provider-session bookkeeping on completed/dismissed child cleanup;
  ten-second default hooks pinned to the resolved immutable release path; and
- explicit least-privilege forwarding of pane-local MCP authority into Codex's
  clean stdio-server environment without placing capability values in argv;
  and
- strict tool-argument validation that accepts the protocol-reserved MCP
  request metadata envelope used by live Codex tool calls.

The final accepted implementation is commit
`a0858a6` (`Refresh time for long-lived MCP tools`). It includes the workspace
startup closure at `cb28da0`, MCP protocol negotiation at `441dd23`, explicit
Codex MCP authority forwarding at `0645e3d`, protocol-reserved request metadata
support at `cfe936b`, and a per-call clock for long-lived MCP servers. The last
fix matters when a parent provider process predates the child completion
transition it later claims.

The exact candidate passed all 125 local tests, repository-wide Ruff
lint/format, compile checks, and `git diff --check`.

Existing coverage still includes:

- authority-safe focus, explicit background confirmation, atomic foreground
  transfer, exact rollback, and uncertainty recovery;
- plain, in-place, local exact-client, remote-preflight, and hop-limit entry;
- PTY-backed tmux client switching and `detach-client -E` replacement;
- the injected-runner Textual app, one-action state, structured failure, and
  confirmation retry;
- closed-history projection and bounded current-session summaries;
- capability-bound navigator/direct tool mutation; and
- the existing workspace/one-child Back, Human close, Complete-return,
  control-turn, recovery, and provider command contracts.

Remote suite results and managed lifecycle evidence are recorded only after the
closure candidate is installed. No test may use the user's normal tmux server
or native sessions.

## Prior artifact and clean-install evidence

The earlier `0.3.0` build produced byte-identical wheel and sdist artifacts,
and the distribution verifier passed exact member, source-byte, metadata,
migration, removed-module, CRC, and archive-safety audits.

The wheel was installed into a clean virtual environment with Textual `8.2.8`.
The installed `swbctl 0.3.0` and navigator module loaded successfully, and a
temporary configuration completed fresh `init`, canonical `state navigator`,
and compare-and-swap `reset` without hooks, providers, DMS, or persistent state.
The accepted `0.3.2` source repeated the reproducible artifact audit. Two
isolated builds with `SOURCE_DATE_EPOCH=1784073600` were byte-identical and
passed exact member, source-byte, metadata, migration, removed-module, CRC, and
archive-safety checks:

- wheel SHA-256:
  `911f90dec540ed375b8e2ff8c474b5834bfbc54bf14e04d6c58e8c9400919f72`;
- sdist SHA-256:
  `da547880e51885136ba52e2c6cd1fcc2203ec1c89eda9467677684319c0fdc1a`;
- 24 package files, 29 wheel files, and 42 sdist files.

The wheel was installed into a clean immutable virtual environment with
Textual `8.2.8`. Installed `swbctl 0.3.2` and
`agent_switchboard.navigator` loaded successfully.

## Prior disposable provider and remote-owner evidence

Provider probes used unique temporary homes and tmux sockets, started no prompt
and made no model request:

| Provider | Version | Host | Evidence |
| --- | --- | --- | --- |
| Codex | `0.145.0` | local | native `codex` pane remained live in isolated tmux |
| Claude Code | `2.1.218` | `snap.lan` | native `claude` pane remained live in isolated tmux |

These were provider/shell probes, not a managed workspace lifecycle. Only a
dedicated Codex auth file was copied, with mode `0600`, into its temporary home.
No dedicated Claude credential file existed, so no persistent Claude
configuration was copied. Both tmux servers and homes were removed by their
owning probes.

On `snap.lan`, the exact implementation commit used temporary Config/State
roots and an isolated tmux socket to execute owner-local `view enter`
preflight, create a navigator view, toggle direct and back to navigator, and
verify the final `sidebar:live` plus `active-placeholder:dead` topology. The
installed `swbctl` route was deliberately not replaced for acceptance; fixed
SSH argv, owner identity, attach construction, and exact-client replacement are
covered by deterministic production-path tests.

## Managed-session acceptance

The final trial used a disposable directory, fresh Config/State roots, an
isolated tmux socket, and new Codex provider UUIDs. The user reviewed and
trusted the installed Switchboard-owned hooks, then detached from the managed
view.

The first lifecycle attempt exposed one real defect: `AgentToolService` fixed
its timestamp when the stdio MCP server started. A long-lived parent could
therefore receive a later completion prompt but fail its handoff claim with a
timestamp older than the transition. Commit `a0858a6` changed the default MCP
clock to read once per tool call while retaining deterministic injected clocks
for tests.

The clean rerun proved:

1. public `view open` created a navigator view and public `frame start` launched
   an empty managed workspace;
2. the parent called `task_push`, trusted `Stop` committed the transition, and
   a new managed child called `transition_claim`;
3. after the child's claim turn settled, `task_complete_return` committed a
   completion handoff and returned the same view to the exact parent provider
   UUID;
4. the long-lived parent claimed the handoff successfully; both transitions
   ended `completed`, `committed`, and control-turn `settled`;
5. the child frame closed as `completed`, its provider session became
   `stopped`, and its surface became `dead`, while the parent stayed live,
   resumable, ready, and turn-complete;
6. navigator mode showed a resident 32-column navigator beside the active
   agent, direct mode expanded that same agent to the full 160x50 client, and
   returning to navigator restored the split;
7. detach left the view ready and reattach restored the same navigator plus
   parent panes; and
8. the normal tmux server still had the same three native Codex panes and PIDs
   (`9854`, `10311`, and `10828`) after acceptance.

The trusted hook configuration was not rewritten for the rerun. Its existing
immutable `0.3.2` handler remained compatible, while newly launched managed
sessions used the fixed `a0858a6` MCP runtime. This avoided another trust cycle
and did not alter unmanaged sessions.

## Safety and adoption boundary

No acceptance step:

- removes or modifies unrelated Codex/Claude hooks or edits provider trust
  state programmatically;
- stopped, restarted, detached, resumed, or sent input to an existing agent;
- killed or replaced the user's tmux server;
- changed installed Switchboard state or inferred adoption;
- touched the DMS repository, plugin, cache, or service; or
- required coordinated host downtime.

Phase 6F is complete and accepted. Native tooling remains the active workflow
until the user explicitly chooses adoption. Phase 6G is unblocked and is the
next roadmap batch.
