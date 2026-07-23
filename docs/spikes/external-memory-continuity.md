# External Memory Continuity Finding

Date: 2026-07-23

Status: secondary study passed; evidence only, not an approved production contract

## Hypothesis

External memory can enrich a destination turn when both sides observe timely,
healthy, project-scoped recent planning context. It must remain optional:
the accepted plan and triggering input are the complete immediate transition
capsule, and unavailable, delayed, stale, or wrong-scope memory must degrade
truthfully without gaining routing authority.

## Procedure

The study performed two bounded, read-only lookups against the live
claude-mem `13.11.0` reference before any provider turn. It retained only
health, scope, planning-relevance, timing, and relationship booleans. It did
not retain returned memory content or create provider or memory state.

The same transition capsule was then replayed through deterministic adapters
that were unavailable, over the two-second deadline, stale, and scoped to the
wrong project. A structurally incomplete capsule was covered by the automated
tests.

## Observed Contract

The live reference supplied healthy, exact-project, current rollover/workstream
planning context for both source and destination before the first destination
turn. The study therefore reported `full` continuity.

Every degraded adapter reported `immediate-only`, while preserving the accepted
plan. An incomplete immediate capsule reported `blocked`. No memory outcome
authorized a transition.

The sanitized behavioral fixture is
`spikes/fixtures/thread-workstream/memory/external-continuity.json`.

## Limitations

The live check proves immediate context injection, not long-horizon recall
quality, semantic completeness, confidentiality policy, or production timeout
handling. The deterministic delayed and stale cases exercise classification
rather than a real slow or obsolete service. The study deliberately did not
inspect or score retained memory text.

## Decision Impact

External memory is compatible with the redesign only as optional enrichment.
Production design may report `full` when both scoped observations meet the
proved checks; otherwise it must report `immediate-only`. The accepted plan
must remain sufficient without memory, and memory must never mint authority,
define a task boundary, or block a valid immediate handoff.
