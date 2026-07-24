# Thread and Workstream Redesign Decision

Date: 2026-07-23

Status: direction accepted; production contract and Phase 6G implementation remain unapproved

The reconciled validation and remaining production gates are recorded in the
[Thread and Workstream Redesign Review](thread-workstream-redesign-review.md).
Their dependency-ordered delivery plan is the
[Thread and Workstream Redesign Roadmap](thread-workstream-redesign-roadmap.md).

## Decision

Advance the thread/workstream direction to production-contract design for
Codex-backed workflows. Do not resume the existing Phase 6G recursive
parent/child return design.

This is a design-direction decision, not implementation approval. Switchboard
does not yet have a viable or adopted product, and Phase 6 creates no backwards-
compatibility obligation. Existing code may be reused when it naturally fits
the new model; misleading concepts and compatibility layers should be deleted
or replaced.

No production registry, persisted type, schema, hook, navigator, installed
command, provider session, or ordinary tmux session changed during the studies.
The Phase 6F.2 implementation remains the repository's technical reference
until a separate clean-break contract, implementation plan, and acceptance
gate are approved.

Codex has one selected v1 structured-plan cutover trigger: provider-native
**clear context and implement**. Ordinary **implement this plan** remains in the
current thread. Its held-input study proves an optional future policy is
technically feasible, but that capability is not activated by this decision.
Conversational thread management requires an explicit user action; natural
language inference remains advisory. Claude Code remains explicit
manual/degraded support until an isolated host can prove its installed
contract.

## Evidence Decision

| Study | Result | Decision impact |
| --- | --- | --- |
| Codex A -> B -> C native rollover | `pass` | A repeated same-TUI boundary exists with one carried plan per destination and no post-result traffic. |
| Trusted adoption and rebinding | `pass` | Same-surface authority and capability can rotate twice; forged, stale, replayed, concurrent, mismatched, and partial events fail closed. |
| Visibility and fenced native history | `pass` | Source/current identities remain visible, result tips remain intact, historical input is dropped, and return to current takes one action. |
| Managed worktree ownership | `pass` | Collision-free isolated workstreams are feasible with conservative exact-clean retirement. |
| External-memory continuity | `pass` | Healthy exact-scope context can be `full`; unavailable, delayed, stale, or wrong-scope context is truthfully `immediate-only`. |
| Claude Code parity | not run | No isolated Claude host or installed CLI was available; automatic parity is not claimed. |
| Ordinary Codex implementation timing | `pass` | Its fixed input can be held before sampling while the exact structured Plan item remains available; exact-once cutover composes with the proven transition transaction. |
| Conversational plan timing | partial | Plan result and later acceptance are observable, but natural language is advisory; explicit result selection supplies artifact provenance while a user action supplies authority. |
| Running-source stable fork | `pass` | Codex can fork through the latest completed turn, execute an alternative immediately, and leave both App Server-owned and TUI-owned source turns active. |
| Combined watched workflow | deferred | This is post-decision acceptance after a production contract exists. |

The retained sanitized evidence is:

- `spikes/fixtures/thread-workstream/codex/0.145.0/native-rollover.json`;
- `spikes/fixtures/thread-workstream/codex/0.145.0/trust-history.json`;
- `spikes/fixtures/thread-workstream/codex/0.145.0/execution-trigger.json`;
- `spikes/fixtures/thread-workstream/codex/0.145.0/running-source-fork.json`;
- `spikes/fixtures/thread-workstream/codex/0.145.0/navigator-running-fork.json`;
- `spikes/fixtures/thread-workstream/git/managed-worktree.json`; and
- `spikes/fixtures/thread-workstream/memory/external-continuity.json`.

All seven results are unassisted `pass` fixtures. Their named assertions,
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
  still agree;
- immediate handoff state remains sufficient without external memory;
- ordinary Plan implementation can be held at `UserPromptSubmit` before
  sampling, with its exact structured Plan item still retrievable;
- blocking removes the execution input text and produces no model `Stop`,
  although Codex retains one content-free source turn; and
- conversational `Stop` result plus later acceptance are observable, but only
  explicit plan selection—not natural language alone—identifies an artifact;
- a user action, not artifact detection, authorizes cutover;
- a Codex fork can contain only the settled prefix while an alternative fork
  completes and the source's later turn remains active;
- the same fork succeeds beside an active native TUI without changing its
  process, managed surface, working directory, or completion state; and
- interruption, independent new work, and stable-prefix fork are distinct user
  intents.

## Required Production Boundaries

The production design must preserve the completed provider result as the
visible tip. It must not inject a claim, synthesis, routing, or status turn
after completion. A prepared exactly-once destination submission must end that
control turn immediately.

Provider hooks are observations, not authority by themselves. Adoption must
fail closed on uncertain identity, ordering, ancestry, concurrency, or atomic
commit outcome. External memory may enrich context but may never mint
authority or define a task boundary.

For v1, ordinary Plan implementation must not be intercepted: **implement this
plan** continues in the current thread, while **clear context and implement**
requests cutover. Any future opt-in forced-cutover policy must separately
require the current structured Plan item, exact fixed coding input, source
identity/revision, and a pre-submit hold. The hook's `permission_mode` is not
collaboration-mode evidence, and that future UX must account for Codex's
content-free blocked turn.

For conversational plans, the first contract should expose an explicit
**implement selected plan in a fresh thread** action. Exact reserved phrases may
alias an explicit action at the next user prompt boundary. A classifier may
suggest that action, but natural-language plan or acceptance inference must not
route.

The navigator is the canonical out-of-band surface. It must allow the user to
interrupt an active turn, start unrelated work in a clean workstream, or fork a
running task through its latest completed turn. Forking must preserve the source
turn, create independent filesystem ownership, and reject an exact-fork claim
when the corresponding filesystem checkpoint is missing or dirty. Interrupt
stops execution but never implies rollback.

For Codex, the validated App Server `thread/fork` operation returns the new
provider identity at the completed-turn boundary. Production design should
compose that exact identity with managed-worktree creation and an exact resume,
rather than assume the CLI fork can preallocate an identity. App Server use
remains capability-gated and unapproved.

If the user focuses the new or forked workstream while the original continues,
the original's later completion produces navigator attention only. Switchboard
must not auto-focus it or write completion traffic into either provider pane.

Historical inspection must remain provider-native and input-fenced. The live
resume did not emit the optional resume hook, so correctness cannot depend on
that hook. If a later provider contract cannot be fenced, full historical
inspection is blocked; a transcript renderer is not an implicit fallback.

Worktree cleanup must never stash, force-remove, or touch a shared, external,
dirty, unmerged, active, or mismatched worktree.

## Roadmap

Phase 6A.1 through 6F.2 remain accepted history. Phase 6G remains paused and its
recursive A -> B -> A return exit is not the next implementation target.

The
[production roadmap](thread-workstream-redesign-roadmap.md)
authorizes Phase 7A contract planning next. It separately sequences durable
state, provider capabilities, recovery, checkpoint/worktree composition,
navigator actions, Plan cutover, continuity, legacy disposal, and
acceptance. S7 becomes that implementation's end-to-end acceptance study.
Explicit navigator actions, conversational aliases, stable-checkpoint fork,
and any later forced-cutover or inference policy remain separate deliverables.

Until that contract is approved, every interface in the proposal and every
spike implementation remains non-normative and non-installed.
