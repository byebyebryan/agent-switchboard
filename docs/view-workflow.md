# View and Frame Workflow

Date: 2026-07-21

Status: accepted Phase 6 interaction contract; implementation pending

## Purpose

This record defines the user-visible behavior of durable Switchboard views and
workspace/task frames. It replaces the task-first foreground-stack proposal.
The core model and public boundaries are in [the design](design.md).

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

The view may move between frame stacks from different projects on the same
host. Frame lineage answers “what work did this task come from?” View selection
answers “what is this terminal showing now?” They are separate facts.

## Defaults

- A new DMS desktop view starts in navigator mode.
- A new CLI or SSH view starts in direct mode.
- Reopening an existing view preserves its stored mode regardless of caller.
- A mode change is allowed only while no view transition is settling.
- Automatic task push is conservative and enabled by default per project.
- Explicit user push, stay, back, complete, and close instructions take
  precedence over the automatic heuristic.
- The first shipped workflow allows workspace to one child task. Recursive
  task-to-task push stays disabled until the work-context slice lands.

## Happy Path: Project to Task to Project

```text
DMS or CLI opens project
  -> core creates or focuses a host-local UserView
  -> view opens the project's durable workspace frame
  -> user asks for a substantial outcome
  -> workspace agent prepares a child task frame and bounded brief
  -> trusted post-turn hook swaps to the staged child pane
  -> child claims the brief and continues in its native TUI
  -> user works with the task for as many turns as needed
  -> child completes with one exact handoff
  -> trusted hook resumes and presents the workspace
  -> workspace claims the handoff and reports the result
  -> child closes only after workspace presentation succeeds
```

There is no task form, duplicate provider picker, ceremonial `continue`, raw
prompt replay, or manual tmux target lookup.

## Entering a Project

Each `(HostId, ProjectId)` has one durable workspace frame. It uses the
project's declared default checkout and preferred provider. The workspace frame
persists while its provider session may roll over.

A DMS project action behaves as follows:

1. If its live workspace pane belongs to a view, focus that owning view exactly
   as it is. Do not move the view back to the workspace behind the user's back.
2. If a nonretired view already owns the stopped workspace stack, prepare that
   view for presentation.
3. Otherwise create a navigator-mode view and resume/start the workspace there.
4. A missing checkout/provider or checkout claim conflict becomes a structural
   recovery row rather than a partially created workspace.

A CLI project open follows the same ownership rules but defaults a newly
created view to direct mode.

## Manual Focus

Focus is navigation, not lifecycle:

- it creates no parent/child relationship;
- it creates no handoff;
- it does not close either frame;
- it keeps the source provider runtime live by default; and
- it swaps only the selected view's active slot.

If the target live pane belongs to another view, Switchboard returns a focus
action for that view. It never steals the pane.

If source and target share one work context, core checks background-work
evidence. Known or uncertain background mutation requires explicit human
confirmation before the logical foreground lease moves. The warning explains
that Switchboard cannot prevent an already-running process from writing to the
checkout. Automatic transitions never offer this override.

## Automatic Push

Automatic boundary detection belongs to the current provider agent. Core
validates an explicit `task_push` request but does not interpret conversation
text.

Implicit push is appropriate only when the outcome is independently meaningful
and likely to require multiple interactive turns. Incidental questions,
ordinary implementation steps, bounded diagnostics, provider subagents, and
background shell commands remain in the current frame. False positives cost a
surface change and durable record, so uncertainty always means stay.

The current agent calls:

```text
task_push(title, brief, purpose?, provider?, park_safe=true)
```

It supplies no source frame/session/view/surface/checkout/launch identifiers.
Core derives all authority from the exact current capability.

Preparation while the source turn is active:

1. validate current frame, view revision, project policy, depth, checkout, and
   park-safe claim;
2. create the child frame, transition, launch lease, and staged surface in one
   durable operation;
3. persist a bounded semantic brief, not the raw user prompt;
4. optionally precreate and name a zero-turn provider session through a
   version-gated adapter; and
5. leave the provider inert behind an exact active-pane bootstrap gate.

After the source turn ends:

1. the trusted hook claims the transition and revalidates the same view/frame,
   revision, source pane, and staged target;
2. core swaps the staged pane into the active slot and gives it input focus;
3. the provider starts with one fixed visible instruction to call
   `transition_claim()`;
4. provider binding must match any preallocated UUID;
5. the claim returns the exact brief only to the bound child session; and
6. after binding/presentation succeeds, core parks the source and transfers the
   work-context lease.

The fixed prompt is visible provider history. It is not typed with tmux
`send-keys`, hidden from the user, or constructed from arbitrary prompt text.

## Back

Back means “show my semantic parent without completing this child.” It:

- targets exactly `parent_frame_id`;
- creates no handoff;
- leaves the child open;
- keeps its runtime live by default; and
- applies the same shared-checkout background confirmation as manual focus.

Back may be requested by the user through the navigator/direct command or by
the current agent for deferred post-turn execution. It is not an undo of a
provider turn.

## Complete and Return

The child calls:

```text
task_complete_return(summary, next_action, park_safe=true)
```

Core stores one immutable handoff tied to the exact child session and prepares
the exact parent frame. After the child turn:

1. the hook revalidates the view and transition;
2. a parked parent is resumed with one fixed visible claim instruction;
3. the parent binds and claims the exact handoff;
4. the parent produces the canonical user-visible completion response; and
5. only after parent presentation succeeds does core stop/close the child and
   mark its frame closed with `close_reason=completed`.

If parent resume or claim fails, the child remains usable and the handoff remains
recoverable. Retrying reuses the transition and parent session; it never creates
another handoff or child.

The seamless path intentionally costs one parent model turn. Guided manual Back
remains available when park safety or provider resume support is insufficient.

## Human Close

Human close is organizational and model-free. Closing the active task frame:

1. safely stops its managed runtime when ownership is proven;
2. marks the frame closed without manufacturing a completion handoff;
3. presents the exact parent frame; and
4. starts no parent model turn.

If cleanup is uncertain, the frame still closes with a structural warning and
the runtime becomes a recovery target. Closing a workspace root is not a task
action; the user retires its view or changes projects instead.

## Cancel and Supersession

Cancel is valid only before the child binds or begins a turn. It removes the
transition-owned frame/launch/surface and may delete a precreated provider
thread only after proving it is the exact zero-turn transition-owned UUID.

After binding, the user must choose Back, Complete and return, or Human close.
History is never silently deleted.

Every prepared transition records the view revision. If the user focuses
another frame or changes mode before settlement, the transition becomes
`superseded`; a late hook cannot move the view. The prepared child remains
cancelable/retryable according to its binding state.

## Direct and Navigator Modes

Navigator mode owns one compact resident sidebar pane. Its minimum duties are:

- current project/frame breadcrumb;
- projects and open task frames on the current host;
- attention and transition state;
- structural recovery;
- search/focus;
- Back, close, mode toggle, and focused management entry.

Selecting a frame swaps only the provider pane. The sidebar does not exit.
Focused project/settings/history/recovery panels open as a popup or temporary
full-window surface and return to the same view.

Direct mode kills/removes the sidebar pane and process. The agent expands into
the entire `main` window. A hidden dead anchor retains the view session without
a controller process. Toggling navigator mode recreates the sidebar from
registry state and resizes the same provider pane.

Mode toggle is available through core actions, an optional tmux binding, a DMS
secondary action, or a current-session tool/command. It does not require an
installed skill.

## Multiple Views and Clients

- One view may have multiple attached clients; they deliberately see and
  control the same active pane.
- One host may have multiple independent views with different active frames and
  modes.
- A live provider surface has one owning view.
- Opening the same live frame elsewhere focuses its owner.
- A stopped frame can be resumed into a view only after duplicate-runtime
  reconciliation.
- A view never navigates to a remote-host frame. Remote selection presents a
  separate SSH-backed host-local view.

## DMS Entry and Recovery

DMS lists existing views first, then project entry actions, then structural
recovery. It does not list normal tasks or provider sessions.

Recovery contains only:

- failed or blocked transitions;
- live managed surfaces with no valid owning view;
- checkout/work-context conflicts; and
- missing or inconsistent tmux view containers.

Needs-input, working, and offline are ordinary view status badges. Old provider
history and closed frames remain available through the navigator's focused
history panels, not DMS.

## Failure Rules

- Preparation failure: no view movement and no duplicate child.
- Hook absent: leave a durable prepared transition and source view unchanged.
- Child start/bind timeout: reclaim staged resources; source remains usable.
- Child bound, presentation failed: retain one child as a recovery target.
- Source park failed: do not claim seamless completion; keep both states visible
  and block lease transfer.
- Parent unavailable: keep child and handoff usable.
- Sidebar crash: preserve provider pane; restart sidebar only in navigator mode.
- Locator crash window: reconcile from transition intent and pane metadata.
- tmux server loss: mark view container broken and resume provider UUIDs into a
  new shell; never pretend old processes survived.
- Offline remote host: retain stale view summaries but reject mutation.

## Provider Evidence and Constraints

The retained no-model evidence on 2026-07-21 establishes feasibility, not
production acceptance:

- tmux `3.7b` preserved provider-pane IDs, PIDs, metadata, geometry, rollback,
  and a shared cursor across two clients while swapping an active right slot;
- a dead `remain-on-exit` anchor kept the view durable with no process;
- removing/recreating the sidebar produced true direct/navigator modes without
  changing the active provider process;
- an active-pane gate kept the staged child inert despite an attached view;
- Codex `0.144.6` accepts initial prompts on exact `fork` and `resume`;
- Claude Code `2.1.216` accepts a prompt with `--resume` and optional
  `--fork-session`;
- a bounded Codex stdio App Server can precreate, name, verify, and delete an
  empty thread without enabling the default daemon socket; and
- Claude has no accepted equivalent metadata write, so Switchboard's curated
  frame/session name remains authoritative there.

The retained probe is
[`spikes/tmux_sidebar_swap_spike.py`](../spikes/tmux_sidebar_swap_spike.py).
Real provider pane moves/resizes, trusted-hook timing, locator reconciliation,
quiescent registry cutover, and remote view presentation still require Phase 6
acceptance.

## Acceptance Scenarios

1. A DMS project entry opens one navigator-mode workspace view with no task
   form.
2. A CLI project entry opens one direct-mode workspace view.
3. Reopening either view preserves its mode and active frame.
4. A workspace agent prepares exactly one child, ends its turn, and the same
   view starts that child from a bounded brief without another user message.
5. A failed or retried push creates no duplicate frame, provider UUID, launch,
   pane, or handoff.
6. Manual Back leaves the child open and starts no parent turn.
7. Complete and return resumes the exact parent, claims one handoff, then closes
   the child only after successful presentation.
8. Human close returns to the parent without a handoff or model turn.
9. Two clients on one view share the cursor; two views remain independent.
10. Opening a live frame from another view focuses its owner rather than moving
    its pane.
11. Navigator/direct toggles preserve the active provider pane and process.
12. Known/uncertain shared-checkout background work blocks automatic movement
    and requires confirmation for manual lease transfer.
13. A remote selection opens a separate SSH-backed view and never changes the
    local view's tmux session.
14. No normal path invokes an old Snapshot/Fleet/task-first/DMS contract.
