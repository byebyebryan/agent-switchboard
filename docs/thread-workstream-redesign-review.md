# Thread and Workstream Redesign Review

Date: 2026-07-23

Status: design direction reconciled and viable; no viable or adopted product,
production contract remains unapproved

## Verdict

The explicit-intent redesign is coherent for Codex and survived the current
falsification pass.

This is design viability, not product viability. Phase 6 produced useful
engineering evidence and components but was never adopted as the user's normal
workflow. It is not a backwards-compatibility boundary for the clean
implementation.

The user chooses whether to stay, clear, interrupt, start independent work, or
fork an alternative. Switchboard automates the lifecycle transaction only
after that choice. Ordinary **implement this plan** stays in the current thread;
**clear context and implement** performs the plan cutover.

No production registry, persisted type, schema, hook, navigator action,
installed command, provider session, or ordinary tmux session changed in this
pass.

## Reconciled Semantics

| Intent | Provider lineage | Filesystem lineage | May run while source is active |
| --- | --- | --- | --- |
| Implement here | Same thread | Same worktree | No new operation |
| Clear and implement | Fresh thread, same task | Same worktree | At the Plan action |
| Start new thread | Fresh thread, same workstream | Same worktree | No; source must be idle |
| Interrupt | Same thread becomes interrupted | Partial changes remain | Yes |
| Start new workstream | No conversation-fork lineage | Fresh worktree from selected project base | Yes |
| Fork this task | Fork through latest completed turn | Fresh worktree from matching exact checkpoint | Yes |

This separates three previously conflated cases:

- correction or pivot stops the active turn;
- unrelated work starts independently; and
- approach B forks the settled prefix while approach A continues.

## Evidence Reviewed

The review reconciled:

- the repeated native clear-context rollover fixture;
- trusted adoption, repeated rebinding, result preservation, and fenced history;
- ordinary Plan input timing as optional-policy capability evidence only;
- explicit-action policy tests;
- managed-worktree ownership and conservative retirement;
- external-memory degradation;
- a direct App Server stable-prefix fork while the source's next turn ran; and
- a separate stable-prefix fork beside an actively working native Codex TUI.

The installed Codex version was `0.145.0`. Current Codex documentation describes
native `/new` and `/fork` commands and App Server `thread/start`,
`thread/resume`, `thread/fork`, and `turn/interrupt` lifecycle primitives:

- [Codex CLI commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)

The retained same-TUI result proves:

- the source command was active before the fork;
- the fork contained only the completed prefix;
- the forked alternative completed immediately;
- the source TUI remained active;
- its process, managed surface, and working directory stayed stable;
- no source completion was fabricated;
- unrelated agent processes and tmux panes were unchanged; and
- all temporary provider, Switchboard, capture, and tmux state was removed.

## Cross-Layer Review

### Intent and authority

Pass. Product policy no longer promotes the proved ordinary-Plan interception
capability into default behavior. Exact user actions authorize transitions;
plan/result selection identifies the artifact to carry. Fuzzy acceptance or
task-boundary inference cannot route.

### Provider lifecycle

Pass for the observed Codex contract. Native clear-context rollover, exact
resume/history, active-turn interruption, and completed-prefix fork primitives
exist. The App Server fork returns the exact destination identity, which can be
resumed in a managed pane.

### Navigator timing

Pass for feasibility. A separate control surface can fork while the native TUI
is busy, so thread management need not wait for the next composer turn.
Conversational aliases remain necessarily prompt-bound.

### Result preservation

Pass as a required boundary. Explicit focus movement is allowed because the
user requested it. A background completion must produce navigator attention
only; it must not auto-focus a pane or append provider traffic.

### Filesystem isolation

Pass for the independent managed-worktree primitive, with one composition gate.
An exact task fork requires a completed provider turn and a matching clean,
recorded Git checkpoint. Dirty historical state cannot be reconstructed
silently. Interrupt never means rollback.

### Privacy and isolation

Pass. Both new live results are unassisted sanitized fixtures. Provider
identities, inputs, outputs, paths, process identifiers, and credentials were
excluded; temporary roots and private tmux resources were deleted; unrelated
local state remained unchanged.

## Remaining Production Gates

These are contract and acceptance work, not design falsifications:

1. Define durable project, workstream, task, provider-thread, transition,
   checkpoint, and pending-action records, including fresh-start disposal,
   repository cardinality, and unsupported checkout behavior.
2. Define navigator and CLI actions with idempotency keys and exact authority
   scopes.
3. Prove crash recovery for the compound fork transaction:
   checkpoint validation, worktree creation, `thread/fork`, durable binding,
   managed-pane resume, first-prompt delivery, and focus.
4. Resolve an uncertain `thread/fork` response without creating or adopting an
   ambiguous duplicate. Codex does not accept a caller-preallocated fork UUID,
   so recovery must reconcile provider-recorded ancestry under an exclusive
   pending intent and block on multiple candidates.
5. Validate navigator-initiated interrupt against a TUI-owned active turn and
   preserve partial transcript and filesystem state without claiming rollback.
6. Compose the provider fork and managed worktree in one isolated watched test;
   the current passing studies prove the primitives separately.
7. Validate background completion attention, explicit focus switching, and
   byte-stable source result tips end to end.
8. Run S7 combined acceptance after the production contract is implemented.
9. Keep Claude Code manual/degraded until an isolated installed contract proves
   parity or safe emulation.

## Phase 7 Roadmap Review

The dependency order is sound after three scope corrections:

- Phase 7F exposes only the core navigator/CLI actions whose lifecycle and
  filesystem transactions already exist; selected-plan actions wait for 7G and
  historical inspection waits for 7H.
- The Codex-first core has no migration, extraction, or external-memory
  deliverable. Old Switchboard state is rejected, and those optional concerns
  cannot delay or distort the first viable product.
- Phase 7A must decide whether a workstream owns one checkout or an atomic
  multi-repository checkout set and must define visible failure for unsupported
  Git or non-Git sources before worktree implementation begins.

No roadmap-order blocker remains. The highest-risk production gates are still
uncertain `thread/fork` reconciliation, completed-turn-to-checkpoint binding,
compound fork crash recovery, TUI-owned interruption, and result-tip
preservation. They occur before their dependent public actions and remain
fail-closed acceptance gates.

## Decision

Proceed to a clean-break production-contract plan for the explicit-intent
Codex-backed design. Do not restore automatic ordinary-Plan cutover and do not
resume the recursive parent/child return design.

The
[Thread and Workstream Redesign Roadmap](thread-workstream-redesign-roadmap.md)
treats navigator actions as canonical, conversational phrases as exact aliases,
and automatic inference or forced cutover as separate future policies. Phase
7A production-contract work is next; runtime implementation remains
unapproved.
