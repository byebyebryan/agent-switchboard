# Phase 2B Plan: Trustworthy Local Claude Runtime Truth

Date: 2026-07-17

Status: core, DMS implementation, user-profile cutover, and live acceptance
complete

## Decision and boundary

Phase 2B is the next implementation batch after the completed Codex vertical
slice. It adds Claude Code as a production provider in the core registry,
Snapshot v1, hook path, and process/tmux reconciliation path. It ends when a
local snapshot can report trustworthy Claude session metadata and foreground
runtime state without depending on Claude Agent View.

The target operational profile sets `disableAgentView=true` in Claude user
settings. Every Switchboard-managed Claude launch also exports
`CLAUDE_CODE_DISABLE_AGENT_VIEW=1`. Claude and Codex therefore share one
session model: Switchboard creates and owns a tmux surface, the provider CLI
runs inside it, and tmux preserves the live process when no frontend is
attached. Claude's subagents, background shell work, and `/loop` schedules
remain provider-internal features of that process.

The batch remains core-first and read-oriented. It does not add Claude launch,
resume, attach, native-history, tmux-surface, Ghostty, niri, or DMS action
policy. Those presentation decisions remain Phase 3C. It also does not parse
Claude transcript contents or private history files, dispatch prompts, install
a service, query the network from a hook, stop existing Agent View workers, or
claim a complete legacy-history browser.

The existing `agent-switchboard-dms` adapter remains the Codex action surface
during Phase 2B. It must continue accepting a mixed-provider Snapshot v1 while
projecting only Codex rows, Codex capability/errors, and Codex launch targets.
The legacy `agentSessions` plugin remains the Claude fallback until Phase 3C.
No DMS action may expose raw tmux locators or provider argv during the pivot.

## Pivot evidence

The contract was refreshed on `snap.lan` on 2026-07-16. The active shell was
already on that host: hostname `80H1VV3`, Python 3.14.6, tmux 3.7b, Claude Code
2.1.210, and Agent View enabled. The 2026-07-17 pivot keeps these captures as
rejection and compatibility evidence; they no longer define the production
discovery adapter.

### Rejected supervisor boundary

`claude agents --all --json` is the documented non-TTY Agent View scripting
path. It was evaluated as a possible discovery boundary.
The dated sanitized sample contains 11 rows and adds valid shapes not present
in the original sample from the same provider version:

- a background `working` row with `status=busy` and a PID;
- a background `done` row with `status=idle` and a still-live PID; and
- an interactive row with a PID but without `id`, `state`, or `status`.

The original sample also contains `working:idle` and an interactive row with
`status=idle`. Both fixtures remain rejection and compatibility evidence; they
are not production normalization inputs after the pivot.

All observed durable session IDs were canonical UUIDs. Observed `startedAt`
values were plausible Unix milliseconds. Current short runtime IDs were eight
characters. None of those private supervisor fields enter the production
registry after the pivot.

Live background PIDs resolved to Claude `bg-spare` processes. Their argv did
not contain either the durable session UUID or the short runtime ID. The
supervisor row may associate its reported PID with its own session at that
observation, but argv is not an independent session-binding signal.

With `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, the JSON command exited 1, emitted no
stdout, and reported that Agent View JSON was disabled. The installed settings
schema states that `disableAgentView` disables `claude agents`, `--bg`,
`/background`, and the on-demand daemon. This is the chosen operational mode,
so production discovery must not call the JSON command.

The native `claude --resume` picker has no separate structured index on the
tested host. Claude Code 2.1.210 enumerates transcript JSONL filenames and file
metadata, then lazily parses transcript data for titles, prompts, summaries,
search, and preview. `~/.claude/history.jsonl` is prompt-input history rather
than a complete picker database. An exact picker clone would therefore violate
the no-transcript-parsing boundary. Switchboard keeps untouched legacy history
behind the native picker instead.

### Hooks

The retained successful lifecycle evidence remains:

```text
SessionStart -> UserPromptSubmit -> PostToolUse -> Stop -> SessionEnd
```

A permission path emitted `PermissionRequest` followed by a
`permission_prompt` notification. A current budget-rejected turn emitted
`SessionStart -> UserPromptSubmit -> SessionEnd(reason=other)`.

Claude 2.1.210 supplies a canonical `prompt_id` UUID after the first user
input. A no-model probe verified it on `UserPromptSubmit` and the resulting
`SessionEnd`; `SessionStart` correctly omitted it. The hook blocked the prompt
before any API call and completed with zero turns and zero reported cost.
`prompt_id` is the safe turn identity for ordering and idempotency. Prompts,
assistant messages, tool inputs/results, transcript paths, and transcript
contents are not required.

The earlier budget probe also established an acceptance-harness constraint:
`--max-budget-usd 0.05` returned a budget error only after reporting a
$2.28836 cache-creation cost. Phase 2B automation must use the blocking hook
probe for lifecycle wiring and must not treat that CLI option as a hard
preflight spend ceiling.

### Settings and policy

The current user-level `~/.claude/settings.json` has unrelated settings and no
hooks. Claude supports command-hook exec form with an executable plus `args`,
and `/hooks` is a read-only browser showing handler details and source. Direct
user-settings hooks do not use Codex's separate hook-trust hash workflow.

Two policy cases must remain explicit:

- `disableAllHooks` disables user hooks without removing them; and
- managed `allowManagedHooksOnly` can prevent user hooks from loading.

Without a structured effective-hook API, static configuration inspection must
not claim that managed policy allowed a handler. Live acceptance uses a
no-model blocking probe to prove effective loading.

## Provider adapter contract

Add a provider adapter in `agent_switchboard.providers.claude` with the same
bounded, shell-free failure discipline as the Codex adapter. The first tested
contract range is exactly Claude Code `2.1.210`.

The Phase 2B adapter executes only:

```text
<configured-claude> --version
```

Version output, process lifetime, stdout, stderr, and cleanup waits receive
explicit bounds. Abnormal exits kill and reap the complete provider process
group. Raw stderr is never copied into public diagnostics. Reconciliation does
not start Claude or query Agent View; it consumes retained hook identity plus
the core's bounded same-user process and tmux observations.

The normalized metadata allowlist is:

- durable provider session UUID;
- canonical absolute cwd;
- optional bounded display name when supplied by a safe hook field;
- first safe observation as `created_at`; and
- latest safe lifecycle observation as `updated_at`.

Phase 2B does not manufacture a display name from the first or last prompt,
copy a transcript-derived summary, or treat file modification time as user
activity. Unknown hook fields are ignored after structural validation.

## Process, tmux, and retained-history reconciliation

Claude requires provider-specific absence semantics because there is no
complete structured history scan in the selected profile. A session absent
from current process/tmux observations is parked, not missing, when
Switchboard previously learned a valid durable UUID from a hook or launch.
Untouched legacy history remains unknown to the registry and available through
the native resume picker.

One successful Claude reconciliation transaction must:

1. validate every retained known Claude PID using same-user process birth;
2. correlate only launch-owned or hook-bound tmux panes with the exact durable
   session UUID;
3. keep a validated process live even when no tmux client is attached;
4. mark a previously known runtime stopped and resumable only after its exact
   process birth is gone and no replacement provider process is confirmed in
   its bound pane; a stale tmux surface does not keep a runtime live; and
5. leave provider history existence untouched because no complete history scan
   occurred.

The transaction must not expose partial provider writes. Existing runtime
observation provenance can identify prior hook/process/tmux evidence; add a
schema field only if tests prove provenance cannot be recovered safely and
boundedly from the current registry.

PID validation reads bounded Linux `/proc` metadata, verifies ownership, and
uses boot ID plus process start time to reject PID reuse. Raw argv is not
retained or exposed. A generic Claude executable match is never enough to bind
a session; binding requires a trusted launch/surface token and matching hook,
or an already confirmed exact process birth plus tmux locator.

`snapshot --reconcile live` may validate retained known Claude PIDs and clear
stale runtime presence without invoking Claude. Full refresh adds only the
bounded version capability check; there is no supervisor scan. A failed
version or liveness probe preserves retained session and runtime records and
emits structured degradation.

## Claude lifecycle ingestion

Extend the existing fast event path to accept:

```text
swbctl event --provider claude
```

The first production handler set is `SessionStart`, `UserPromptSubmit`,
`PermissionRequest`, `PostToolUse`, `Stop`, and `SessionEnd`. `Notification` is
not required for the first slice because `PermissionRequest` already provides
the trustworthy permission transition and other notification types do not yet
prove a distinct question state.

The normalized mappings are:

| Hook event | Runtime/activity transition | Stable event identity |
| --- | --- | --- |
| `SessionStart` | live, ready, reason unknown | session, source, process birth, bounded occurrence |
| `UserPromptSubmit` | live, working, reason unknown | session, canonical `prompt_id`, event |
| `PostToolUse` | live, working, reason unknown | session, `prompt_id`, tool-use ID |
| `PermissionRequest` | live, needs input, permission | session, `prompt_id`, event, bounded tool name |
| `Stop` | live, ready, foreground turn complete | session, `prompt_id`, event |
| `SessionEnd` | provisionally stopped; preserve resumability/activity until process reconciliation | session, optional `prompt_id`, reason, process birth, bounded occurrence |

Multiple permission requests for the same tool in one prompt collapse to the
same state transition; event-count auditing is not a Phase 2B goal. Missing
`prompt_id` is accepted only for lifecycle points that can occur before the
first prompt, such as `SessionStart` and prompt-input exit.

`Stop` does not mean that provider-internal subagents, background shell jobs,
or `/loop` schedules finished. Phase 2B neither enumerates those children nor
derives a whole-session `completed` state from foreground hook completion.
Users inspect and control that detail through Claude's in-session `/tasks` and
schedule interfaces.

The handler reads one bounded JSON object, allowlists safe identity and state
fields, and destroys the raw object before database or provider access. It
never retains or hashes `prompt`, `last_assistant_message`, `tool_input`,
`tool_response`, background-task descriptions/commands, transcript paths, or
unknown fields. It performs no Claude, tmux, SSH, or network call.

Claude hooks do not establish a managed surface in Phase 2B. tmux environment
inherited by a manually started Claude process is not enough to claim a
session binding. Hook evidence, exact process birth, tmux metadata, and storage
ordering use the existing independent state axes and source priorities so
liveness reconciliation can repair a missed or transient hook transition.

## Hook configuration and diagnostics

Add explicit ownership-safe management at:

```text
swbctl hooks install --provider claude [--dry-run]
swbctl hooks uninstall --provider claude [--dry-run]
```

The target is `~/.claude/settings.json`. Production definitions use Claude's
exec form:

```json
{
  "type": "command",
  "command": "/absolute/path/to/swbctl",
  "args": ["event", "--provider", "claude"],
  "timeout": 1,
  "statusMessage": "Agent Switchboard: tracking Claude session"
}
```

Each event has one identifiable owned handler. Install removes only recognized
Switchboard duplicates, merges the canonical definitions, and preserves every
unrelated setting and hook. Uninstall removes only those owned definitions.
The safe no-follow load, cooperative lock, source-token revalidation, atomic
replacement, fsync, mode preservation, and path-swap defenses already proven
for Codex must be reused rather than weakened or reimplemented ad hoc. A new
settings file is private; an existing valid file retains its mode.

Hook installation does not silently change `disableAgentView` or stop a
running Claude supervisor. The user-level setting is an explicit setup step;
existing Agent View workers may contain active work and require a separate,
user-confirmed migration.

`swbctl doctor` becomes provider-aware while keeping the existing Codex checks.
For Claude it reports executable/version support, whether Agent View is
disabled, any detected legacy supervisor/workers, owned-handler presence and
exact argv, disabled-all-hooks state, path drift, and isolated event latency.
Static inspection reports configured state only; it must not claim
managed-policy effectiveness. The live acceptance script uses the zero-cost
blocking prompt probe to prove effective loading without a model request.
`/hooks` remains the user-facing read-only source inspection; there is no
Codex-style trust approval step to automate.

## Local CLI and Snapshot v1 integration

Full local refresh loops independently over every enabled configured provider.
One provider's failure does not suppress another provider's capability,
sessions, or errors. Capabilities remain sorted by provider in Snapshot v1.

With Agent View disabled, the Claude capability is available with tested
version `2.1.210`, no schema fingerprint, and features `hooks`,
`native_resume`, and `tmux_runtime`. Agent View being disabled is the healthy
profile, not a degradation. If it is enabled, read-only retained snapshots and
hook ingestion remain available, but the capability reports
`agent_view_enabled`; later managed launch/presentation must refuse to start a
competing runtime. Missing executables, unsupported versions, timeout, invalid
hook input, or incompatible process evidence produce stable provider-scoped
errors and leave retained truth unchanged.

The existing protocol-v1 illustrative capability fixture uses
`agent_view_disabled` as a degradation example. Its envelope shape remains
valid, but Phase 2B must replace that Claude-specific example when the provider
capability is implemented; disabled Agent View is now the expected state.

No Snapshot v1 or database migration is expected. The existing session,
runtime locator, capability, error, and provider-manager role contracts already
represent the Phase 2B result; the generic manager role remains unused for
Claude. Implementation must stop and document the exact gap if a migration
becomes necessary rather than smuggling provider-private state into generic
fields.

## DMS compatibility boundary

The DMS repository remains action-frozen for Claude during Phase 2B. Its
Snapshot validator already accepts provider-neutral rows, while its model
projection deliberately selects only `provider=codex` sessions, capability,
errors, and launch targets.

Add a mixed-provider fixture regression in `agent-switchboard-dms` proving
that:

- Claude sessions and capabilities validate without failing the bridge;
- no Claude session item or new-session target is projected;
- Claude-only capability/error state does not become a Codex warning; and
- existing Codex rows and actions are byte-for-byte equivalent after
  projection.

No QML, desktop helper, presentation-plan, or launcher action changes belong
to this phase.

## Delivery slices and review points

1. **Evidence and contract lock**: retain the supervisor fixtures as rejected
   adapter evidence, retain the picker-source and prompt-ID hook evidence, this
   pivot, and the DMS mixed-provider boundary.
2. **Provider capability**: implement the bounded version check,
   Agent-View-disabled configuration diagnosis, capability reporting, fakes,
   and contract tests without session actions.
3. **Atomic runtime truth**: add Claude-specific hook/process/tmux
   reconciliation, PID-birth validation, parked-session absence semantics, and
   storage tests.
4. **Lifecycle path**: add prompt-ID hook normalization, provider-aware atomic
   ingestion, SessionEnd support, privacy tests, and ordering/replay tests.
5. **Configuration and diagnostics**: add ownership-safe Claude hook editing,
   provider-aware doctor output, Agent View migration diagnostics,
   dry-run/uninstall tests, and the zero-cost effective-hook smoke.
6. **Integration and acceptance**: wire full/live refresh and canonical
   Snapshot v1, run the complete core gates, run the DMS mixed-provider gates,
   and execute sanitized live acceptance on `snap.lan`.

Each slice receives a reviewer-style audit before the next one. In particular,
review must challenge Claude-vs-Codex absence semantics, PID reuse, stale tmux
bindings, foreground-turn versus child-task state, private payload retention,
settings-file ownership, legacy-supervisor migration, process-group cleanup,
and capability honesty.

## Acceptance gates

### Automated core

- Production reconciliation never invokes `claude agents`; the retained
  2.1.210 supervisor fixtures remain non-production evidence.
- Unsupported versions, enabled Agent View, conflicting settings, invalid hook
  identities, PID races/reuse, timeout, and process cleanup are deterministic.
- Hook/process/tmux reconciliation is atomic, idempotent, and does not mark
  untouched legacy history missing.
- Foreground activity mapping matches the table above and never reports child
  subagents, background shell jobs, or scheduled tasks as completed.
- Every lifecycle event is bounded, ordered, state-idempotent, and private raw
  fields are absent from SQLite, Snapshot v1, public hashes, and diagnostics.
- Claude settings install/uninstall/dry-run preserve unrelated JSON and survive
  the same symlink, FIFO, race, concurrent-writer, and atomic-publish tests as
  Codex.
- `disableAgentView=true` is healthy; enabled Agent View reports a scoped
  configuration conflict without erasing retained sessions.
- Full refresh emits independent Codex and Claude capability/error records;
  retained and live-only reads preserve their current semantics.
- Full suite, compileall, Ruff format/lint, reproducible wheel/sdist builds,
  installed-artifact smoke, and package allowlists pass.

### Cross-repository DMS

- A mixed-provider current Snapshot v1 passes the DMS bridge and model parser.
- The last-good launcher model remains Codex-only until Phase 3C.
- `./scripts/check` passes with no QML or action-path change.

### Live `snap.lan`

1. Record exact Claude/Python/tmux versions, configured Agent View state, and
   whether legacy supervisor workers exist, without stopping them.
2. Prove `claude agents --json` is unavailable in the disabled profile while
   `claude --resume` still opens its native picker; exit without selecting or
   displaying conversation content.
3. Reconcile one hook-observed Claude process and tmux surface against an
   isolated Switchboard registry; inspect only canonical Snapshot v1 fields.
4. Prove a disconnected tmux client leaves the process live and an exited
   process becomes parked/resumable without any supervisor scan.
5. Dry-run, install, and inspect the six user-level handlers without altering
   unrelated Claude settings.
6. Run the blocking prompt smoke and prove `SessionStart`,
   `UserPromptSubmit`, and `SessionEnd` reach an isolated registry with zero
   model turns and zero reported cost.
7. Uninstall and prove only Switchboard-owned handlers were removed, unless the
   user elects to keep the development installation for dogfooding.
8. Refresh the installed DMS bridge and prove the new mixed-provider snapshot
   does not add Claude items or actions before Phase 3C.

The acceptance record states exact commands, versions, counts,
configuration-conflict behavior, settings restoration, and whether development
hooks were left installed. No prompt, response, transcript path, cwd, session
UUID, runtime ID, PID, raw argv, or private provider payload is printed.

## Implementation and acceptance record

The implementation and non-destructive acceptance gates completed on
2026-07-17; the user-profile cutover completed on 2026-07-18. The current host
reported Python 3.14.6, tmux 3.7b, Claude Code 2.1.210, and `swbctl` 0.1.0.
The reviewed wheel was installed through the existing `uv tool` distribution
rather than from the source checkout.

The core verification entry points were:

```text
python -m compileall -q src tests spikes scripts
ruff format --check .
ruff check .
pytest
python scripts/verify_distributions.py BUILD_A BUILD_B
```

They passed with 465 tests. Two isolated PEP 517 builds at
`SOURCE_DATE_EPOCH=1784073600` were byte-identical and matched the exact
allowlists: 26 package files, 31 wheel files, and 37 source-distribution files.
Fresh wheel and source-distribution environments passed dependency, import,
migration, snapshot, entry-point, and Claude hook dry-run smoke checks.

The cross-repository gate was:

```text
cd /home/bryan/code/agent-switchboard-dms
./scripts/check
```

It passed 115 Python tests and 19 deterministic JavaScript behavior groups.
The mixed-provider fixture produces exactly the pre-existing Codex-only bridge
model, including unchanged launch targets and warnings. No QML, desktop
helper, presentation-plan, or action code changed.

The effective no-model hook check was:

```text
.venv/bin/python scripts/live_claude_smoke.py \
  --claude "$(command -v claude)" \
  --swbctl /home/bryan/code/agent-switchboard/.venv/bin/swbctl
```

It used temporary Claude settings and XDG roots, emitted exactly one each of
`SessionStart`, `UserPromptSubmit`, and `SessionEnd` into an isolated registry,
reported one session, zero model turns, and zero cost, and printed no provider
identity or private payload. A separate disposable tmux server proved that a
hook-observed Claude process with no attached client remained `live` and
`detached`; `/exit` then reconciled it to `stopped` and `resumable`. With
`CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, `claude agents --all --json` exited 1 with
zero stdout bytes while `claude --resume` kept the native picker open. No
conversation content was selected or displayed.

Read-only preflight originally found `disableAgentView` unset, no Switchboard
handlers, one Agent View client, one transient daemon supervisor, and active
provider work. The user later confirmed that work was wrapped or paused and
authorized stopping the Agent View daemon. Claude's supported
`daemon stop --any` command stopped that supervisor and reported three
terminated background sessions. The follow-up status reported no running
daemon, an unreachable control socket, and an empty worker roster. One
unrelated non-daemon Claude process remained and was deliberately neither
killed nor adopted.

The migration preserved the existing `0644` settings mode and unrelated
settings while adding `disableAgentView=true`. It left `disableAllHooks` unset
and installed exactly six user-level Switchboard handlers. Every handler uses
the installed absolute `/home/bryan/.local/bin/swbctl` path, exec-form arguments
`event --provider claude`, no matcher, a one-second timeout, and the owned
status marker. A second install was idempotent. Installed `swbctl doctor`
reported both Codex and Claude healthy; Claude 2.1.210 measured a 101.9 ms cold
event start and 91.2 ms warm p95 on that run.

With the retained user profile, `claude agents --all --json` exited 1 with zero
stdout bytes, the native resume picker remained open without selecting or
displaying content, and the transient daemon stayed stopped. A blocking
no-model probe loaded the actual user hooks, redirected Switchboard state to a
temporary registry, observed exactly one each of `SessionStart`,
`UserPromptSubmit`, and `SessionEnd`, reported one session, zero turns, and zero
cost, and retained none of its private sentinel.

Finally, the installed DMS bridge read an isolated retained Codex snapshot and
then the same snapshot with an added Claude session. Every projected field
except the expected generation timestamp remained identical: one Codex item,
zero launch targets, and zero warnings. Claude items and actions therefore
remain absent until Phase 3C. The development installation is intentionally
left in the dogfood state with Agent View disabled and all six Switchboard
handlers installed.

## Proposed commit boundaries

Keep reviewable behavior together rather than splitting code from the tests
that define it:

1. core evidence/plan commit;
2. core Claude capability and atomic hook/process/tmux reconciliation commit;
3. core lifecycle hook management, diagnostics, and CLI integration commit;
4. DMS mixed-provider compatibility-test commit.

Review found that core slices 2 and 3 share the event schema, retained runtime
identity, CLI, doctor, fake provider, and installed-artifact gates. They should
ship as one coherent core implementation commit rather than manufacture a
partially usable intermediate commit. The DMS fixture and exact projection
tests remain a separate repository commit. The final diff review and complete
acceptance gates precede both commits and pushes.

## Deferred work

- Phase 3C Claude new/resume/native-history presentation, surface binding,
  graceful managed-runtime stop, DMS items/actions, Ghostty, and niri behavior.
- Untouched legacy `/resume` history beyond sessions observed through hooks or
  selected through the native picker.
- Remote Claude snapshots/actions, SSH caching, and offline policy.
- TUI, curation, handoffs, context routing, and agent tools.
- Provider-version expansion beyond explicit fixture and live-contract review.
