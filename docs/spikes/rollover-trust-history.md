# Rollover Trust and History Boundary Finding

Date: 2026-07-23

Status: Gates 2 and 3 passed; evidence only, not an approved production contract

## Hypotheses

1. A trusted same-surface state machine can distinguish two legitimate native
   Codex clear-context rollovers from forged, replayed, stale, mismatched, or
   concurrent events and can rotate the active provider binding and capability
   twice without changing the running process environment.
2. A separate navigator can retain previous/current identity and transition
   state without writing into the provider pane.
3. A provider-native resumed historical TUI can be made read-only by placing an
   input-dropping PTY boundary in front of it, without moving the active
   workstream tip or changing the historical turns.

## Procedure

The live harness repeated the isolated native A -> B -> C rollover from Gate 1
and replayed its private in-memory hook observations through the spike-only
adoption state machine. Adoption required exact agreement on:

- active predecessor and new provider identity;
- launch and surface authority;
- tmux server generation and pane;
- provider process birth;
- effective working directory;
- `SessionStart(source=clear)`;
- provider ancestry; and
- provider-side destination existence.

The destination's first input confirmed the tentative binding. The state
machine compared the completed structured source plan to the carried plan
before classifying semantic task lineage. Each confirmed adoption used one
compare-and-swap record containing the destination identity and a newly minted
capability.

The harness then opened a separate navigator window showing sanitized aliases,
parked C, and started a native resume of A through a PTY proxy. It attempted to
submit input to the inspection window, returned to C with one navigator action,
and compared provider surfaces and structured turns before and after.

Raw hook events, provider input/output, exact identities, runtime locations,
process details, and the input-fence status were mode `0600` temporary data and
were deleted. Only the sanitized fixture was retained.

## Observed Contract

The 76,725 ms installed Codex `0.145.0` run passed all 36 assertions:

- A -> B and B -> C each confirmed one destination input after one
  `source=clear` event;
- the active provider binding advanced twice and its capability changed twice;
- launch, surface, server generation, pane, process birth, and working
  directory authority remained exact;
- both plan-provenance comparisons confirmed semantic task transitions;
- pending and confirmed source/current identities were visible in the
  navigator;
- writing the navigator did not change the provider surface;
- the historical PTY started a native provider view of A;
- attempted historical input was counted and dropped at the fence;
- A received no input event and its structured turns remained byte-equivalent;
- the active binding remained C;
- one navigation action returned to the provider window;
- C's provider surface remained byte-identical; and
- the historical runtime, private tmux server, endpoint, capture, and temporary
  root were stopped or deleted.

Automated falsification tests additionally reject:

- agent-shell forgery without a provider ancestor;
- wrong launch, surface, server generation, pane, process birth, or working
  directory;
- missing provider-side destination identity;
- startup/resume events presented as clear events;
- unknown predecessors;
- stale and replayed events;
- concurrent clears;
- destination-input mismatch;
- partial compare-and-swap commits; and
- generic clear events being mislabeled as task boundaries.

The sanitized fixture is
`spikes/fixtures/thread-workstream/codex/0.145.0/trust-history.json`.

## Limitations

The historical Codex resume did not emit the optional
`SessionStart(source=resume)` hook in this run. The exact native resume command,
running fenced provider view, unchanged structured turns, dropped input, and
absence of `UserPromptSubmit` still established the inspection boundary. A
production design cannot rely on the resume hook for historical-view
correctness.

This state machine and navigator are spike code. They do not establish a
production persistence model, crash protocol, installed hook, public command,
or frontend contract. The input fence has been proven only on this local Codex
and tmux contract.

## Decision Impact

The core Codex direction survives all three primary gates. Managed-worktree and
external-memory studies may proceed. No production registry, schema, hook,
navigator, provider session, or installed command changed.
