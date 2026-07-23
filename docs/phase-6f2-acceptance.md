# Phase 6F.2 Automatic Completion-Control Acceptance

Date: 2026-07-23

Status: complete and accepted

Phase 6F.2 closes the defect exposed by the first persistent-view user loop:
the parent control prompt could be visibly prefilled but still wait for the
user to submit it. While that handoff remained open, the closing task also
looked navigable and an already settled transition could retain a stale
recovery record.

## Accepted behavior

- Live control submission uses one uniquely named ephemeral tmux buffer, one
  bracketed paste, and one separate submit key while the target pane remains
  input-fenced.
- The buffer is deleted by the same command queue, and the existing
  at-most-once control-turn boundary remains unchanged.
- Closing task rows are disabled and labelled `finishing`; they cannot issue a
  focus transition.
- Exact transition settlement resolves its matching
  `control_submit_uncertain` record atomically.
- `reconcile` resolves equivalent stale records created by an older immutable
  runtime without resubmitting input or restarting a provider.

## Automated evidence

Commit `b54be0b` adds the implementation and regressions for the real tmux
input boundary, finishing-task presentation, atomic recovery resolution, and
legacy-runtime reconciliation.

The candidate passed:

- all 128 tests, including a real isolated tmux PTY that requires bracketed
  paste markers and a distinct submit carriage return;
- repository-wide Ruff lint and format checks;
- Python byte-compilation; and
- `git diff --check`.

## Live managed-session evidence

The accepted loop used:

- immutable runtime
  `/home/bryan/.local/share/agent-switchboard/releases/core-0.3.4-aa980c42d9f0-b54be0b`;
- fresh Config/State roots under
  `/tmp/agent-switchboard-phase6f2-acceptance-20260723`;
- isolated tmux socket
  `/tmp/agent-switchboard-phase6f2-acceptance-20260723/tmux.sock`;
- a fresh project view, parent Codex UUID, and child Codex UUID; and
- the five explicitly reviewed Switchboard-owned Codex hooks pinned to the
  immutable `0.3.4` executable.

The clean loop proved:

1. the parent prepared push transition
   `77e03bde-1b6b-5469-ac31-b80d4a771563`;
2. trusted `Stop` launched the child, and its fixed startup prompt claimed the
   brief;
3. after that claim turn settled, a separate task turn prepared
   complete-return transition
   `a8655fbc-7df4-5e7e-ae86-aa3f84b1bd3e`;
4. trusted `Stop` returned the same view to the parent and automatically
   submitted the fixed claim prompt without user input;
5. the parent claimed the handoff and reported the supplied summary and next
   action; and
6. the final navigator state had the parent live, ready, and active; the child
   closed and stopped; no control or transition state; no open recovery; and
   no nonterminal transition.

Both transitions finished `completed` with transport `committed`. Both control
turns finished `settled`, each with `submission_count = 1` and an observed
Codex prompt UUID. No tmux buffer remained.

## Safety boundary

Every mutating acceptance command named the isolated tmux socket. Cleanup
killed only that disposable server and its provider processes. The normal tmux
server retained PID `8704` and start time `1784765316`; the acceptance process
did not stop, restart, detach, resume, or send input to a normal provider
session. DMS was not read, restarted, or used.

The normal native pane inventory changed concurrently during acceptance, so it
is not presented as a stable before/after assertion. Socket-qualified command
scope, the unchanged normal tmux server identity, and surviving normal
Switchboard panes are the exact non-interference evidence.

Phase 6F.2 is complete and accepted. Phase 6G recursive task frames are next.
