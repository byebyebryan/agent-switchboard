# Thread and Workstream Redesign Decision

Date: 2026-07-23

Status: direction accepted; production contract and Phase 6G implementation remain unapproved

## Decision

Advance the thread/workstream direction to production-contract design for
Codex-backed workflows. Do not resume the existing Phase 6G recursive
parent/child return design.

This is a design-direction decision, not implementation approval. No production
registry, persisted type, schema, hook, navigator, installed command, provider
session, or ordinary tmux session changed during the studies. The accepted
Phase 6F.2 implementation remains current until a separate clean-break contract,
implementation plan, and acceptance gate are approved.

The canonical initial Codex path is provider-native **clear context and
implement**. Intercepting ordinary implementation language remains optional.
Claude Code remains explicit manual/degraded support until an isolated host can
prove its installed contract.

## Evidence Decision

| Study | Result | Decision impact |
| --- | --- | --- |
| Codex A -> B -> C native rollover | `pass` | A repeated same-TUI boundary exists with one carried plan per destination and no post-result traffic. |
| Trusted adoption and rebinding | `pass` | Same-surface authority and capability can rotate twice; forged, stale, replayed, concurrent, mismatched, and partial events fail closed. |
| Visibility and fenced native history | `pass` | Source/current identities remain visible, result tips remain intact, historical input is dropped, and return to current takes one action. |
| Managed worktree ownership | `pass` | Collision-free isolated workstreams are feasible with conservative exact-clean retirement. |
| External-memory continuity | `pass` | Healthy exact-scope context can be `full`; unavailable, delayed, stale, or wrong-scope context is truthfully `immediate-only`. |
| Claude Code parity | not run | No isolated Claude host or installed CLI was available; automatic parity is not claimed. |
| Ordinary Codex implementation interception | deferred | Native clear-context implementation remains the canonical path. |
| Combined watched workflow | deferred | This is post-decision acceptance after a production contract exists. |

The retained sanitized evidence is:

- `spikes/fixtures/thread-workstream/codex/0.145.0/native-rollover.json`;
- `spikes/fixtures/thread-workstream/codex/0.145.0/trust-history.json`;
- `spikes/fixtures/thread-workstream/git/managed-worktree.json`; and
- `spikes/fixtures/thread-workstream/memory/external-continuity.json`.

All four results are unassisted `pass` fixtures. Their named assertions,
isolation checks, cleanup checks, event order, timings, limitations, and
behavioral fingerprints are retained without provider identities, inputs,
outputs, transcripts, runtime locations, process identifiers, or credentials.

## Proven Invariants

The future production contract may rely on these tested properties only when
its own installed capability checks still agree:

- two successive native Codex rollovers create three distinct provider
  identities without restarting or moving the TUI;
- `SessionStart(source=clear)` precedes exactly one carried-plan input at each
  destination;
- completed ordinary results remain byte-stable until a real user action;
- management status is rendered on a separate surface;
- adoption requires the active source, launch, surface, tmux generation, pane,
  provider process birth, working directory, clear source, provider ancestry,
  and provider-side destination to agree;
- provider binding and transition capability rotate in one atomic record;
- plan provenance plus first destination input—not a generic clear—establishes
  semantic task lineage;
- native historical inspection is input-fenced and cannot move the active tip;
- managed worktrees retire only when exact ownership and clean merged state
  still agree; and
- immediate handoff state remains sufficient without external memory.

## Required Production Boundaries

The production design must preserve the completed provider result as the
visible tip. It must not inject a claim, synthesis, routing, or status turn
after completion. A prepared exactly-once destination submission must end that
control turn immediately.

Provider hooks are observations, not authority by themselves. Adoption must
fail closed on uncertain identity, ordering, ancestry, concurrency, or atomic
commit outcome. External memory may enrich context but may never mint
authority or define a task boundary.

Historical inspection must remain provider-native and input-fenced. The live
resume did not emit the optional resume hook, so correctness cannot depend on
that hook. If a later provider contract cannot be fenced, full historical
inspection is blocked; a transcript renderer is not an implicit fallback.

Worktree cleanup must never stash, force-remove, or touch a shared, external,
dirty, unmerged, active, or mismatched worktree.

## Roadmap

Phase 6A.1 through 6F.2 remain accepted history. Phase 6G remains paused and its
recursive A -> B -> A return exit is not the next implementation target.

The next authorized planning work is a clean-break production contract based
on the proved thread/workstream boundaries. It must separately define durable
state, recovery, migration/deletion, installed capability gates, and
acceptance. S7 becomes that implementation's end-to-end acceptance study.

Until that contract is approved, every interface in the proposal and every
spike implementation remains non-normative and non-installed.
