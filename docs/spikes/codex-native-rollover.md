# Codex Native Repeated-Rollover Finding

Date: 2026-07-23

Status: Gate 1 passed; evidence only, not an approved production contract

## Hypothesis

Installed Codex can perform the provider-native Plan-mode
**clear context and implement** action twice in one running TUI while retaining
the source threads, submitting exactly one carried plan to each destination,
and leaving the ordinary destination result untouched.

## Procedure

The unassisted harness used Codex `0.145.0`, a disposable Git repository, a
temporary Codex home, temporary Switchboard roots, and a unique private tmux
server. It copied only the existing login material into the temporary home,
accepted Codex directory trust only after verifying the repository's private
marker and absence of remotes, and enabled three private hooks:
`SessionStart`, `UserPromptSubmit`, and `Stop`.

The harness:

1. entered Plan mode in thread A, generated a harmless no-change plan, named A,
   and selected the native fresh-context implementation action;
2. waited for B's ordinary result without submitting management input;
3. repeated the same operation from B to C;
4. compared the completed provider surface byte-for-byte across a quiet
   interval;
5. verified source/destination structured provider state through an isolated
   stdio App Server; and
6. stopped only the private server and deleted all temporary provider,
   repository, hook, and tmux state.

The raw hook capture was mode `0600` and was deleted. The committed fixture
contains no provider UUID, input, output, transcript, path, pane/process
identifier, or credential.

## Observed Contract

The installed schema fingerprint was
`4242b14096f29619f86c3d27a832d853ceaa55cbf60bd4ae3fafda446b1e8a5c`.
The 49,996 ms run observed:

```text
thread-a SessionStart(startup) -> UserPromptSubmit -> Stop
thread-b SessionStart(clear)   -> UserPromptSubmit -> Stop
thread-b UserPromptSubmit      -> Stop
thread-c SessionStart(clear)   -> UserPromptSubmit -> Stop
```

All retained assertions passed:

- A, B, and C had distinct exact provider identities;
- the pane, TUI process, tmux generation, launch, surface, and process working
  directory remained stable;
- every provider hook reported the disposable repository as its effective
  working directory;
- each `SessionStart(source=clear)` preceded exactly one destination input;
- each destination input contained the exact completed structured source plan;
- A and B remained readable/resumable, and all three threads were nameable;
- B and C result tips remained byte-stable until the next real user action;
- no hook followed C's ordinary `Stop`;
- the disposable repository remained unchanged; and
- pre-existing agent processes and ordinary tmux panes retained their
  identities.

The sanitized fixture is
`spikes/fixtures/thread-workstream/codex/0.145.0/native-rollover.json`.

## Limitations

This proves the installed native Codex path only. It does not prove trusted
Switchboard adoption, atomic authority rotation, concurrent/replayed-event
rejection, historical input fencing, worktree ownership, external-memory
continuity, Claude parity, or ordinary implementation interception.

The first provider thread is allocated lazily when its initial Plan-mode input
is accepted, so the harness names A after that planning turn but before the
fresh-context action. The temporary TUI also requires a one-time directory
trust decision; the harness permits that decision only for its exact
marker-verified, remote-free disposable repository.

## Decision Impact

Gate 1 does not falsify the core Codex direction. Gate 2 adoption/rebinding and
Gate 3 visibility/history must still pass before any production contract can
be approved. No production registry, schema, hook, navigator, provider
session, or installed command changed.
