# View and Frame Workflow

Date: 2026-07-21

Status: accepted Phase 6A.1 interaction contract; implementation pending

## Purpose

This record defines user-visible behavior for durable Switchboard views,
workspace/task frames, and strategic agent control turns. It replaces the
task-first foreground-stack proposal. Product boundaries are in
[the design](design.md); normative states/invariants are in
[the state contract](state-contract.md).

## Mental Model

The user owns a durable terminal view. Switchboard owns navigation around the
native provider pane, not the conversation inside it.

```text
UserView (one host, one cursor)
  navigator mode:  [Switchboard sidebar] [active native provider pane]
  direct mode:                         [active native provider pane]

Project
  workspace frame
    task frame
      nested task frame              later Phase 6 slice
```

Frame lineage answers “where did this work come from?” View selection answers
“what is this terminal showing?” Project entry answers “take me back to my
current work in this project.” These are separate facts.

## Defaults

- A new CLI/SSH view starts in navigator mode.
- Direct mode is an explicit minimal/no-TUI choice.
- A future optional desktop adapter may also request navigator mode.
- Reopening a view preserves its mode.
- `Views` focuses as-is; `Projects` explicitly navigates.
- Automatic task push is conservative and project-configurable.
- Complete-and-return triggers one parent synthesis turn by default.
- Back, focus, mode change, and Human close are model-free.
- Control turns prefer a safely live pane and otherwise exact UUID resume.
- Explicit user instructions override the automatic task-boundary heuristic.
- The first shipped workflow allows workspace to one child; recursive push is
  disabled until its later work-context slice.

## Happy Path: Project to Task to Project

```text
CLI/TUI opens project
  -> core focuses/creates its host-local view
  -> view presents the project's current open frame or workspace
  -> user asks for a substantial outcome
  -> workspace agent prepares a child frame and bounded brief
  -> trusted post-turn hook parks/fences the workspace
  -> core transfers WorkContext foreground authority to the child
  -> child starts from the fixed claim control turn
  -> child claims the brief and works in its native TUI
  -> child stores one exact handoff and requests Complete-and-return
  -> trusted hook presents the exact workspace parent
  -> parent receives one fixed control turn and claims the handoff
  -> child closes immediately after that exact claim
  -> parent synthesizes the result and continues naturally
```

There is no task form, duplicate provider picker, ceremonial `continue`, raw
prompt replay, hidden semantic injection, or manual tmux lookup.

## Entering a View or Project

View activation focuses/attaches exactly that durable view. It never changes
the active frame or navigator/direct mode.

Project activation is explicit navigation:

1. Core resolves the project's workspace placement and owning view.
2. If that view already shows the project, retain its active frame.
3. Otherwise target the most recently focused open descendant owned by that
   view, falling back to the workspace frame.
4. Commit the target with a live view-revision CAS, then focus/attach the view.
5. If the workspace has no placement, single-flight creation of one
   navigator-mode CLI view unless direct mode was explicitly requested.
6. Missing defaults, checkout conflicts, or stale ownership become bounded
   blocked/recovery results.

Different request IDs opening the same unowned project converge on the one
workspace reservation. Clicking a project never focuses a view still displaying
an unrelated project.

## Manual Focus

Focus is navigation, not lifecycle:

- it creates no lineage or handoff;
- it does not close either frame;
- it keeps the source runtime live by default;
- it moves only the selected view's active slot; and
- it transfers foreground authority only when source/target share a WorkContext.

If the target live pane belongs to another view, Switchboard focuses that owner.
It never steals the pane.

A source moved into holding has client input disabled. If source/target share a
WorkContext, known/uncertain background mutation requires explicit confirmation
before lease transfer. The warning states that Switchboard cannot prevent an
already-running process from writing. Automatic transitions never offer this
override.

## Automatic Push

The current provider agent detects semantic task boundaries. Core validates an
explicit tool request but never interprets conversation text.

Implicit push is appropriate only for an independently meaningful outcome
likely to need multiple interactive turns. Incidental questions, ordinary
implementation steps, bounded diagnostics, provider subagents, and background
commands remain in the frame. Uncertainty means stay.

The current agent calls:

```text
task_push(title, brief, purpose?, provider?, park_safe=true)
```

It supplies no source frame/session/view/surface/checkout/launch identity. Core
derives authority from the exact current capability.

Preparation during the source turn:

1. validate frame/view/project/depth/checkout and park-safe claim;
2. commit child frame, transition, planned surface, launch intent, bounded brief,
   and one `claim_brief` ControlTurn;
3. optionally precreate/name a zero-turn provider session through a
   version-gated adapter; and
4. create/reconcile the staged physical pane as a separate saga, leaving its
   provider bootstrap inert in the unattached holding session.

After the source `Stop`:

1. the trusted hook claims the transition and revalidates view revision,
   WorkContext generation, source pane, staged target, and server generation;
2. source input is disabled and its pane moves to holding;
3. core proves park safety and atomically transfers foreground ownership to the
   child;
4. the child is moved into `view.main` and selected;
5. the bootstrap gate proves exact authorized main placement and starts the
   native provider with the fixed visible control prompt;
6. provider binding must match a preallocated UUID when one exists; and
7. `transition_claim()` returns the brief only after the child UUID, pane,
   placement, transition, and foreground WorkContext all agree.

If foreground transfer fails, the child never receives its brief. Reconciliation
restores the source or leaves both panes input-fenced with one recovery record.

## Back

Back means “show my semantic parent without completing this child.” It:

- targets exactly `parent_frame_id`;
- creates no handoff or control turn;
- leaves the child open;
- keeps its runtime live and input-disabled in holding by default; and
- applies the shared-checkout background confirmation before lease transfer.

If the parent runtime stopped, core resumes its exact provider UUID without an
initial semantic prompt and presents it at the normal idle prompt. Back never
starts a parent model turn.

## Complete and Return

The child calls:

```text
task_complete_return(summary, next_action, park_safe=true)
```

Core stores one immutable handoff tied to the exact child session, marks the
child `closing`, and prepares the exact parent plus one `claim_handoff`
ControlTurn.

After the child `Stop`:

1. claim/revalidate transition, parent, child, view, WorkContext, and panes;
2. input-fence the child and present the exact parent, swapping the child into
   holding so `view.main` always retains a pane;
3. transfer WorkContext foreground ownership to the parent;
4. submit the fixed control prompt to the live verified-idle parent, or resume
   its exact UUID with that initial prompt;
5. return from the trusted hook without waiting for the model;
6. the parent calls `transition_claim()` and receives the exact handoff;
7. that claim closes the child as `completed` and stops its exact parked runtime;
8. the parent synthesizes the user-visible result; and
9. the parent's exact post-claim `Stop` settles the transition.

If the parent model fails after claim, the child remains closed because its
durable handoff was delivered. Claim is idempotent, so a recovery control turn
may return the same handoff without another child or handoff.

If parent presentation/submission fails before claim, the child stays
`closing` and recoverable. Uncertain submission is never automatically repeated.

Projects may override `complete_return` to `handoff`. That mode presents the
parent without a control turn, surfaces a pending handoff in the navigator, and
lets the next normal parent turn claim it.

## Controlled Live Submission

The only terminal-submitted text is the literal `control.claim.v1` template:

```text
Call transition_claim() and follow the returned transition instructions.
```

A live target is eligible only when:

- it is the exact managed target session/pane/placement;
- it is parked in the view's holding session with input disabled;
- a trusted `Stop` proves foreground turn completion;
- no permission question or known/uncertain background mutation exists;
- the executing transition and WorkContext generations still match; and
- no submission has been attempted for this ControlTurn.

One tmux command queue swaps/selects the target, enables input, sends the literal
template and Enter, then disables input. An exact `UserPromptSubmit` observation
or bounded watchdog re-enables it. No clipboard/paste buffer is used.

A timeout after submission becomes `uncertain`: re-enable user input, retain the
handoff/brief, surface recovery, and do not inject again. A later exact claim or
hook may settle the same turn.

## Human Close

Human close is organizational and model-free:

1. validate or prepare the exact parent/placeholder target;
2. present the parent first and move the child into holding;
3. transfer WorkContext foreground authority when applicable;
4. stop only the exact parked child runtime after ownership revalidation; and
5. mark the frame `closed/dismissed` without a handoff or parent model turn.

If the parent cannot be presented, the child remains active/open. If cleanup is
uncertain only after safe presentation, the frame closes with a structural
warning and the runtime becomes a recovery target. Workspace roots are not
closed through this task action.

## Cancel and Supersession

Cancel is valid only while a push is `prepared` and before child binding/input.
It deletes only transition-owned planned/zero-turn resources after exact proof.
After execution begins, the user chooses Back, Complete, Human close, or
recovery; provider history is never silently deleted.

Manual focus/mode changes may supersede only `prepared`. An executing or later
transition owns the view mutation lease and must settle/fail/recover. A late
hook for a superseded transition may record failure but cannot retarget the view.

## Direct and Navigator Modes

Navigator mode owns one compact sidebar pane with breadcrumb, projects/open
frames, attention, transition/control state, recovery, search/focus, Back,
close, mode toggle, and focused management entry.

Direct mode removes sidebar pane/process through an external short-lived
executor. The provider fills `main`. A dead `remain-on-exit` placeholder keeps
`main` durable when no provider exists. Toggling navigator recreates the sidebar
without changing provider pane/process.

Mode changes are allowed only without an executing-or-later transition. Zoom is
preserved/restored as display state and never changes durable mode.

## Multiple Views and Clients

- One view may have multiple Switchboard-managed clients sharing the cursor.
- Managed attach rejects/degrades tmux `active-pane`; it never claims a shared
  cursor when clients have independent selection.
- Different client sizes follow an explicit tmux window-size policy; read-only
  clients do not become the mutation executor.
- One host may have multiple independent views.
- One live/stopped frame affinity has one owning view.
- Opening a live frame elsewhere focuses its owner.
- A view never navigates to a remote frame; remote selection opens a separate
  SSH-backed host-local view.

An SSH client uses the primary terminal-native path. `swbctl view list`
discovers durable local views and `swbctl view attach --view VIEW` revalidates
one view, acquires its own bounded attachment lease, and execs the exact tmux
attach command. It never starts or resumes a provider. A provider `frame
reopen` reports success only after the exact surface has moved from holding
into `view.main`.

## Optional Desktop Entry and Recovery

A future dumb desktop adapter may list Views, Projects, then structural
Recovery. It never lists normal tasks or provider sessions and does not
participate in the TUI-first adoption gate.

- View selection preserves active frame/mode.
- Project selection performs the explicit project route.
- One canonical desktop-adapter client exists per view; other tmux clients do not
  share its compositor identity.
- Concurrent launch fallback uses one expiring desktop lease.
- Multiple matching canonical windows block instead of launching another.
- `safe_auto` recovery may execute directly; `open_view` opens core recovery;
  `manual` only explains the next step.

Needs-input, working, ready, stopped, stale, and offline remain view badges.

## Failure Rules

- Preparation failure: source/view unchanged and no duplicate child.
- Hook absent: durable prepared state; no view movement.
- Child bind timeout: reclaim exact staged resources; source remains usable.
- Child bound before presentation failure: retain one recovery target.
- Foreground transfer failure: do not release brief.
- Control submit uncertain: re-enable input, retain semantic payload, never
  auto-resubmit.
- Parent unavailable before claim: keep child closing/usable and handoff durable.
- Parent failure after claim: child closed; retry synthesis from same handoff.
- Sidebar crash: provider survives; restart only in navigator mode.
- Locator crash: reconcile intent against server generation/pane metadata.
- tmux server loss: invalidate locators and resume exact UUIDs into a new shell.
- Offline/cutover-staged host: retain state but reject mutation.

## Provider and tmux Evidence

Retained evidence establishes feasibility, not production acceptance:

- tmux `3.7b` preserves pane/process identity and supports input-off fencing;
- a compound `enable -> literal send -> disable` queue delivers the fixed text
  while client input stays fenced before and afterward;
- Codex `0.144.6` accepts an initial prompt on exact `fork` and `resume`;
- Claude Code `2.1.216` accepts a prompt with `--resume` and optional
  `--fork-session`; and
- bounded Codex stdio App Server clients can precreate/name/delete an empty
  thread without a shared daemon.

The retained topology/control proof is
[`spikes/tmux_sidebar_swap_spike.py`](../spikes/tmux_sidebar_swap_spike.py).
Phase 6D implementation and guarded installed-provider evidence are recorded in
[Phase 6D Acceptance](phase-6d-acceptance.md).

## Acceptance Scenarios

1. TUI Project entry navigates to that project; View entry focuses as-is.
2. CLI Project entry uses the same route with navigator-mode creation default.
3. Reopening preserves view mode and active frame.
4. Push releases exactly one child brief only after foreground transfer.
5. Retried push creates no duplicate frame, UUID, launch, pane, or handoff.
6. Back leaves child open and starts no parent turn.
7. Complete submits one parent control turn and closes child on exact claim.
8. Uncertain submission never produces an automatic duplicate turn.
9. Human close presents parent first and starts no model turn.
10. Normal view window navigation cannot expose/activate holding panes.
11. Shared managed clients share cursor; `active-pane` is rejected/degraded.
12. Direct/navigator toggles preserve provider pane/process, including zoom.
13. Shared-checkout ambiguity blocks automation and requires core confirmation.
14. Remote selection opens a separate SSH-backed view.
15. No normal path invokes Snapshot/Fleet/task-first/DMS compatibility.
