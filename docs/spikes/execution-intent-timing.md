# Codex Execution-Intent Timing Finding

Date: 2026-07-23

Status: S2 trigger timing passed; conversational inference remains advisory

## Hypotheses

1. Codex's ordinary Plan-mode **implement this plan** action can be held before
   model sampling while the exact structured Plan item remains retrievable.
2. A plan requested outside Plan mode and the user's later acceptance are both
   observable at the same pre-submit boundary, but unstructured natural
   language must not become routing authority without explicit plan provenance.
3. The held input can compose with the already-proven Switchboard transition
   transaction without duplicate source or destination execution.

## Procedure

The unassisted live study used Codex `0.145.0`, two disposable repositories,
two temporary Codex homes, private Switchboard roots, and two private tmux
servers.

In the first scenario, Codex produced a structured Plan item. The harness
selected ordinary **implement this plan**, held its fixed execution input inside
`UserPromptSubmit`, queried the source through an isolated App Server while the
hook was still pending, and then returned a blocking decision.

In the second scenario, Codex produced a conversational plan in Default mode.
The harness correlated the `Stop` result with the native thread history, sent a
synthetic acceptance, held it inside `UserPromptSubmit`, and compared natural
language with a spike-only explicit selection of that exact result.

Neither held input reached model execution. Raw identities, input, output,
history, paths, process details, and hook coordination files were mode `0600`
temporary data and were deleted.

The automated matrix additionally exercised:

- structured, selected, stale, consumed, wrong-source, wrong-revision, and
  mismatched plan provenance;
- native clear, ordinary Plan implementation, explicit fresh implementation,
  discussion, revision, generic clear, and natural-language signals;
- replayed, stale, and concurrent trigger observations;
- failures before and after destination delivery and binding commit; and
- idempotent recovery without restoring a possibly delivered input to the
  source.

## Observed Contract

The 27,733 ms live result passed all 26 assertions.

For ordinary Plan implementation:

- the fixed coding input was observed while its hook was still pending;
- the completed structured Plan item remained available and exact;
- the source identity, provider process, pane, and working directory remained
  stable;
- no clear session and no execution `Stop` occurred;
- the blocked input text was absent from source history;
- Codex appended one content-free turn record after the block;
- the source composer was restored; and
- the authoritative trigger composed with the spike transaction for one
  destination delivery and one atomic binding commit.

The hook's `permission_mode` is a permission-policy field, not Plan/Default
collaboration-mode evidence. The safe compound signal is instead the current
structured Plan item, exact fixed coding input, source identity/revision, and
pre-submit hold.

For the conversational plan:

- the provider produced no structured Plan item;
- `Stop` exposed the same final result available through native thread history;
- the later acceptance was observable while held before sampling;
- its text was absent from source history and it produced no execution `Stop`;
- the source again gained only one content-free turn and remained usable;
- natural language by itself remained advisory; and
- explicitly selecting the exact result authorized the same exact-once cutover
  transaction.

The sanitized fixture is
`spikes/fixtures/thread-workstream/codex/0.145.0/execution-trigger.json`.
Its behavioral/schema fingerprint is
`4242b14096f29619f86c3d27a832d853ceaa55cbf60bd4ae3fafda446b1e8a5c`.

## Limitations

The live study intentionally did not recreate the destination transition:
pane movement, foreground/checkout transfer, durable transition settlement,
and exact-once delivery were already proved by the current implementation and
the earlier rollover/adoption studies. The held live observations were replayed
through a spike-only transaction model with fault injection.

The explicit conversational-plan selection was also spike-only. No installed
navigator action or public command exists. This study does not approve a
natural-language classifier, measure classifier latency or false positives, or
make phrases such as “go ahead” authoritative.

Codex visibly reports the blocked diagnostic and retains a content-free turn
record. Production UX must decide whether that provider bookkeeping is
acceptable; it cannot claim that blocking leaves no source record at all.

## Decision Impact

The missing timing question for ordinary Plan implementation is answered
positively: Switchboard can hold the execution turn before sampling and recover
the exact structured plan while the source remains bound and usable. Combined
with the previously proved transition machinery, this supports automatic
fresh-thread cutover for the ordinary Plan action.

Provider-neutral conversational rollover also has the necessary observation
points, but not automatic semantic authority. The first production contract
should use an explicit **implement selected plan in a fresh thread** action.
Natural-language classification may suggest that action; it remains a separate
study before it may route automatically.
