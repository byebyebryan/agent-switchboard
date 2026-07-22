# Frictionless Task Close Plan

Date: 2026-07-21

Status: implementation contract

> Foreground-stack note (2026-07-21):
> [Foreground task session stack](foreground-task-session-stack.md) explores a
> separate agent-authorized return/complete transition with an exact handoff.
> It does not yet supersede or overload the human-only close contract below.

## Decision

Closing a task is a lightweight organizational action, not a handoff-writing
workflow. One action moves the task out of the default Open view and then makes
a best-effort attempt to stop its safely owned managed runtime. It never asks
for a summary, confirmation, next action, or provider prompt, and it never
calls a model.

Closed tasks and provider-owned conversation history are retained. Selecting a
closed task reopens it and opens or resumes its existing provider session in
one action. Explicit handoffs remain available for provider changes,
cross-host continuation, and users who want a durable checkpoint, but they are
not part of ordinary close.

## Public contract

The human command is:

```text
swbctl task close TASK_ID [--host HOST_ID] [--json]
```

It returns a validated task-close action containing the owning host and task,
the current session when present, a `closed`, `already_closed`, or `blocked`
status, and one runtime disposition:

```text
no_session | already_stopped | stopped | retained | unknown
```

Closing commits the task state before runtime cleanup. A cleanup problem never
reopens the task: successful close may carry a bounded warning when a runtime
is retained or its final state is unknown. A blocked result is reserved for a
task identity, host, or storage precondition that prevents the close itself.
Retries are idempotent and may retry cleanup for an already-closed task.

`prepare-task TASK_ID --reopen` is the one-action closed-task entry point. It
validates the owning host and checkout claim before reopening. If that
precondition fails, the task stays Closed. Once reopen commits, any later
presentation failure leaves the task Open and retryable.

`stop-session` remains a separate explicit action and becomes provider-neutral
for exact launch-owned Codex and Claude runtimes. It sends the provider's
interactive `/exit`, waits a bounded interval, and only then escalates to
identity-checked `SIGTERM` and `SIGKILL`. Unmanaged, adopted, ambiguous, or
identity-mismatched processes are never signalled.

## State and history

- Close preserves TaskId, project, checkout, provider SessionKey, conversation
  history, prior handoffs, and existing `wrapped_at` state.
- Close does not create a handoff and does not modify `wrapped_at`.
- Closed tasks are retained indefinitely in the Closed view; task archival is
  out of scope.
- Same-provider reopen uses provider-native durable history. A provider switch
  or cross-host continuation still requires an exact explicit handoff.
- The task worktree claim remains reserved while the closed task's current
  runtime is live or unknown. Reconciliation releases the claim once that
  runtime is confirmed stopped.

## Frontends and agent tools

The TUI closes directly without opening an editor or confirmation dialog. Its
Closed view opens a selected task through `prepare-task --reopen`; the existing
explicit reopen-only action remains available.

The DMS picker exposes **Close task** as the first secondary action for an open
task, reachable by keyboard (`Tab`, then `Enter`) and the context menu. It
refreshes the Fleet after the result, reports cleanup warnings without
duplicating rows, and shows retained runtime state on Closed rows. Activating a
Closed row reopens and opens it in one action.

Agent-facing `task_close` and `swbctl agent close` are removed. Agents retain
the explicit handoff operations, but task lifecycle remains a frictionless
human action rather than a prompt-driven agent workflow.

## Versioning and compatibility

Snapshot v2, Fleet v1, task statuses, and provider history contracts remain
unchanged. The close result is a separate versioned action envelope. The DMS
adapter advances its bridge/action contract and frontend model together; an
already loaded older adapter continues to use its previous behavior until the
new plugin version is loaded.

This document supersedes the Phase 4D requirement that close create a handoff
and wrap the current session. The historical phase documents remain unchanged
except for a supersession notice so their implementation history stays useful.
