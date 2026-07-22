# Phase 0 Validation

Date: 2026-07-15

## Verdict

The core architecture is feasible without a daemon, terminal proxy, or
provider transcript parser. The launch reservation, client-gated tmux
bootstrap, Codex discovery/hooks, and Claude supervisor/native-TUI contracts
all have working evidence.

The 2026-07-17 design pivot supersedes the Claude supervisor integration
conclusion while retaining its observations as evidence. Production Claude
uses `disableAgentView=true`, hooks, native resume, and the same managed tmux
surface lifecycle as Codex. The supervisor fixtures remain useful rejection
and compatibility inputs; they are not the Phase 2B discovery contract.

Phase 1 and the read-only portion of Phase 2 can proceed. Frontend migration
remains gated on exercising the implemented `swbctl` structured
prepare/select/attach path end to end; that command does not exist yet. This is
an implementation-order gate, not an architecture blocker.

## Tested environment

- starship: Python 3.14.6, tmux 3.7b, Ghostty 1.3.1-arch2, niri 26.04,
  Codex 0.144.4.
- snap.lan: Python 3.14.6, Claude Code 2.1.210, tmux transport over SSH.
- Existing DMS agent picker: 22 unit/integration tests.

## Results

### Launch reservation

`sqlite_reservation_spike.py` started 16 processes against one logical
target. Exactly one process created the launch; all 15 competitors received
the same launch ID. Same-request retries were idempotent, reuse against a
different target was rejected, and an expired claim allowed one replacement.

This validates SQLite as the authoritative atomic boundary. tmux creation
remains a post-commit side effect whose failure is recorded and reconciled; no
design should claim one atomic transaction spans SQLite and tmux.

### tmux presentation

`tmux_gate_spike.py` passed every assertion on an isolated tmux server:

- No provider start without an attached client.
- In a shared workspace, viewing the manager window did not release the target
  window bootstrap.
- Switching the exact client to the target released it.
- Bootstrap `exec` preserved the pane PID.
- A stale client ID was rejected.
- Unpresented expiry retained status 124 before cleanup.
- Provider startup failure retained status 42 before cleanup.

### Codex 0.144.4

- Retained the installed v2 app-server schema with SHA-256
  `93b300b8102e48bd1640fe12aec7ed29c215cd882237c70bedd32bf364dacc05`.
- `thread/list` pagination worked in both normal and state-database-only
  modes. With six non-archived interactive threads, repeated observed medians
  were 45-50 ms normally and about 1 ms or less from the state database.
- Returned threads included stable UUID, cwd, path, source, CLI version,
  recency, status, and name fields. Older thread records retained older CLI
  versions, so normalization must not require the current version per row.
- New-session hook order was `SessionStart -> UserPromptSubmit ->
  PostToolUse -> Stop`; resume started with `SessionStart(source=resume)`.
  An interactive Bash approval emitted `PermissionRequest`.
- Launch and surface IDs plus tmux metadata reached hooks unchanged.
- The redacting hook recorder measured about 18 ms median across 20 local
  invocations.
- Killing the process emitted no session-end hook, while app-server
  `thread/read` still returned the resumable thread. Process/tmux
  reconciliation is therefore required.
- The probe ran with the installed claude-mem hooks enabled; Switchboard hooks
  can coexist rather than replace them.

### Claude Code 2.1.210

- `claude agents --all --json` exposed completed rows without a PID, working
  and blocked rows with a PID, and an interactive manager row without a short
  runtime ID. `blocked` supports `needs_input`, but supervisor JSON alone
  does not distinguish permission from question.
- Pressing Left in a normal TUI left that process as an interactive Agent View
  manager and created a separate background runtime with a different session
  UUID and short ID. No additional lifecycle hook identified that detach.
- `claude attach <short-id>` provided an exact terminal view. The pane argv
  supplied the binding signal; attach itself emitted no lifecycle hook.
  Ctrl+Z closed the disposable attach window while the background runtime
  survived.
- In-terminal `/resume` emitted
  `SessionEnd(reason=resume)` for A followed by
  `SessionStart(source=resume)` for B in the same pane.
- A successful interactive turn emitted `UserPromptSubmit -> PostToolUse ->
  Stop`. A denied Bash write emitted `PermissionRequest` plus a
  `permission_prompt` notification; the command was not executed.
- With `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, supervisor JSON was explicitly
  unavailable but native `claude --resume <uuid>` still worked and emitted
  normal resume/exit hooks.
- The separately spawned Agent View worker did not inherit the manager's
  one-off `--settings` file. Switchboard's planned identifiable user-level
  hook installation is therefore required for background-worker coverage.
- The local Claude organization login is disabled, so model-backed Claude
  lifecycle tests ran on `snap.lan`. No host service was installed.

These observations originally motivated a distinct manager-surface role. The
2026-07-17 pivot rejects that product path: the generic role remains in the
provider-neutral schema, but Claude does not create a manager or exact-attach
surface in the supported Agent-View-disabled profile.

### Claude Phase 2B evidence refresh

The retained Claude contract was refreshed on `snap.lan` on 2026-07-16 before
Phase 2B planning. The host was already the active machine rather than an SSH
target for this run: hostname `80H1VV3`, Python 3.14.6, tmux 3.7b, and Claude
Code 2.1.210. Agent View was enabled.

- A sanitized `claude agents --all --json` sample returned 11 rows. In addition
  to the original fixture, the same provider version produced
  `background:working:busy`, `background:done:idle` with a live PID, and an
  `interactive` row with neither `id` nor `status`. The original and dated
  fixtures are both retained because neither observation alone spans the valid
  optional-field combinations.
- Every observed `sessionId` was a canonical UUID, every observed short runtime
  ID was eight characters, and `startedAt` was a plausible 13-digit Unix
  millisecond timestamp. These fields are retained only as evidence about the
  rejected supervisor adapter.
- Live background row PIDs resolved to Claude `bg-spare` processes. Their argv
  contained neither the durable session UUID nor the short runtime ID. A
  supervisor row may therefore associate its reported PID with that row, but
  process argv cannot independently bind a background worker to a session.
- With `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, the JSON command exited 1 with no
  stdout and the documented disabled-Agent-View diagnostic. Native resume and
  hook capabilities remained available and now form the production boundary.
- A bounded non-persistent turn rejected by Claude's budget gate emitted
  `SessionStart`, `UserPromptSubmit`, and `SessionEnd(reason=other)`. A second
  probe blocked `UserPromptSubmit` in the hook before any model request; it
  completed in 42 ms with zero turns and zero reported cost and proved that
  `prompt_id` is a canonical UUID on the prompt and resulting `SessionEnd`.
  `SessionStart` correctly had no prompt ID.
- The first probe also showed that `--max-budget-usd 0.05` is not a reliable
  preflight spend ceiling for this validation shape: Claude reported a
  $2.28836 cache-creation cost before returning `error_max_budget_usd`. No
  further model-backed probes were run.

The focused implementation boundary and acceptance gates derived from this
refresh are recorded in [`docs/phase-2b-plan.md`](phase-2b-plan.md).

### Existing desktop and SSH integration

- DMS agent picker tests: 22/22 passed.
- Local list latency was about 0.24 seconds for 20 rows.
- The existing niri path focused a known remote Claude Ghostty window.
- A disposable Ghostty window was launched and discovered by niri. Repeated
  focus while the desktop was actively being used was nondeterministic, so a
  serialized production focus test remains required before DMS migration.
- A bounded SSH snapshot from an online host completed in about 0.38 seconds;
  an offline configured host failed quickly. Remote probing remains
  open-triggered, not background polling.

## Remaining gates

1. Run contract tests against the first real registry/provider adapters using
   these versioned fixtures.
2. Exercise `swbctl` remote prepare/select/attach and structured error
   responses before launching Ghostty.
3. Serialize the niri focus test so user input cannot race the assertion.
4. Treat unknown provider versions and enabled Agent View as explicit
   capability/configuration conflicts while retaining registry-known sessions.
