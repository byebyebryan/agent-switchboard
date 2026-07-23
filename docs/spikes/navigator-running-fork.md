# Navigator-Initiated Running-Task Fork Finding

Date: 2026-07-23

Status: native stable-prefix and same-TUI composition passed; production navigator remains unapproved

## Hypothesis

While approach A is running, an out-of-band navigator can immediately fork the
same task for approach B without copying A's in-progress turn, interrupting A,
or waiting for the next user prompt boundary.

The authoritative fork point is the latest completed provider turn. The
matching filesystem checkpoint and managed worktree remain a separate
Switchboard responsibility.

## Procedure

Two unassisted studies used Codex `0.145.0`, temporary Codex homes, private
Switchboard roots, disposable Git repositories with no remotes, and—for the
same-TUI study—a private tmux server.

The harness:

1. started a source thread and completed one harmless baseline turn;
2. began a second source turn and observed its harmless shell command running;
3. called native `thread/fork` through the exact baseline turn;
4. confirmed the fork had a distinct identity and provider-recorded source
   lineage;
5. started and completed an immediate alternative turn in the fork;
6. read the source and confirmed its second turn was still active;
7. explicitly interrupted the source turn only for bounded cleanup; and
8. verified the disposable repository, unrelated agent processes, and the
   user's tmux panes were unchanged.

The second harness repeated the boundary beside an isolated native Codex TUI:

1. completed the baseline through the native TUI;
2. started approach A in that TUI and observed its command process running;
3. issued `thread/fork` from a separate isolated App Server client;
4. completed approach B through the fork;
5. proved A's TUI process, managed surface, working directory, and active state
   were unchanged; and
6. stopped the private TUI only after the concurrency proof.

Provider identities, inputs, outputs, paths, process identifiers, and
credentials were excluded from the retained result. Temporary provider and
Switchboard state was deleted.

## Observed Contract

The 8,880 ms live result passed all 10 assertions:

- the baseline turn completed;
- the source command was observed in progress;
- the fork identity differed from the source;
- Codex recorded the source lineage;
- the fork contained only the completed baseline prefix;
- the fork accepted and completed an immediate alternative turn;
- the source baseline remained intact;
- the source's later turn remained active after the alternative completed;
- explicit cleanup interruption produced an interrupted source turn; and
- the disposable repository remained unchanged.

The sanitized fixture is
`spikes/fixtures/thread-workstream/codex/0.145.0/running-source-fork.json`.
The 15,672 ms same-TUI result passed all eight assertions and is retained as
`spikes/fixtures/thread-workstream/codex/0.145.0/navigator-running-fork.json`.
Its behavioral/schema fingerprint is
`4242b14096f29619f86c3d27a832d853ceaa55cbf60bd4ae3fafda446b1e8a5c`.

## Limitations

The studies did not add a production navigator action, persisted workstream
lineage, or installed command. The same-TUI harness validates the process and
surface boundary, not the future Switchboard transaction or UI.

The source and fork intentionally shared one disposable repository because the
provider contract was the variable under test. The separate managed-worktree
study already proves collision-free creation, exact provider working
directory, switching, and conservative retirement. Production acceptance must
compose both results and reject an exact-fork claim when no matching clean
filesystem checkpoint exists.

The App Server-owned source was interrupted and the TUI-owned source was
stopped after their concurrency assertions solely to bound cleanup. The
observed forks themselves neither interrupted nor modified their source turns.

## Decision Impact

The out-of-band navigator design is feasible for Codex. A user can start
approach B immediately from the last settled conversational state while
approach A keeps running.

The App Server path also returns B's exact provider identity. A future
transaction can therefore create the fork, create its managed worktree, and
resume that exact identity in a new managed pane. This avoids depending on the
CLI fork path, whose target identity is allocated during launch.

This establishes the intended distinction:

- **Interrupt** stops a mistaken or obsolete active attempt and does not imply
  filesystem rollback.
- **Start new workstream** begins unrelated parallel work without conversational
  fork lineage.
- **Fork this task** creates a sibling workstream from the last completed
  provider turn and matching exact filesystem checkpoint.

Conversational aliases remain useful at a user prompt boundary, but they cannot
replace the navigator as the always-available management surface.
