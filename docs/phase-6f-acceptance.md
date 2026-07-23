# Phase 6F Terminal-Native Acceptance

Date: 2026-07-22

Status: complete; workflow adoption remains a separate user decision

Phase 6F makes the resident navigator and terminal-native entry path complete
enough for an explicit adoption decision. It does not install a new workflow,
edit normal provider hooks, restart DMS, replace the user's tmux server, or
claim authority over existing Codex or Claude Code sessions.

## Accepted behavior

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

The exact implementation commit `0477332b440802e3501b8499ea6bc37b404c15ee`
passed the complete 113-test suite on both the local host and `snap.lan`.
Coverage includes:

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

Both hosts also passed Ruff, compile checks, and `git diff --check`. No test
used the user's normal tmux server or persistent Switchboard roots.

## Artifact and clean-install evidence

Two builds with the same fixed source epoch produced byte-identical wheel and
sdist artifacts. The distribution verifier passed exact member, source-byte,
metadata, migration, removed-module, CRC, and archive-safety audits.

The wheel was installed into a clean virtual environment with Textual `8.2.8`.
The installed `swbctl 0.3.0` and navigator module loaded successfully, and a
temporary configuration completed fresh `init`, canonical `state navigator`,
and compare-and-swap `reset` without hooks, providers, DMS, or persistent state.

## Disposable provider and remote-owner evidence

Provider probes used unique temporary homes and tmux sockets, started no prompt
and made no model request:

| Provider | Version | Host | Evidence |
| --- | --- | --- | --- |
| Codex | `0.145.0` | local | native `codex` pane remained live in isolated tmux |
| Claude Code | `2.1.218` | `snap.lan` | native `claude` pane remained live in isolated tmux |

Only a dedicated Codex auth file was copied, with mode `0600`, into its
temporary home. No dedicated Claude credential file existed, so no persistent
Claude configuration was copied. Both tmux servers and homes were removed by
their owning probes.

On `snap.lan`, the exact implementation commit used temporary Config/State
roots and an isolated tmux socket to execute owner-local `view enter`
preflight, create a navigator view, toggle direct and back to navigator, and
verify the final `sidebar:live` plus `active-placeholder:dead` topology. The
installed `swbctl` route was deliberately not replaced for acceptance; fixed
SSH argv, owner identity, attach construction, and exact-client replacement are
covered by deterministic production-path tests.

## Safety and adoption boundary

No acceptance step:

- edited or installed normal Codex/Claude hooks;
- stopped, restarted, detached, resumed, or sent input to an existing agent;
- killed or replaced the user's tmux server;
- changed installed Switchboard state or inferred adoption;
- touched the DMS repository, plugin, cache, or service; or
- required coordinated host downtime.

Phase 6F closes the implementation and coexistence gate. Native tooling remains
the active workflow until the user explicitly chooses adoption. Phase 6G may
now begin as a separate recursive-frame batch without changing that boundary.
