# Phase 0 contract spikes

These probes test the provider and transport assumptions in
`docs/product-landscape.md`. They retain structure, lifecycle metadata, and
timings, but not prompts or transcript content.

## Thread/workstream redesign studies

`spikes/thread_workstream/` is a second, explicitly non-production evidence
harness for the proposed thread/workstream redesign. It refuses non-disposable
repositories, roots provider and Switchboard state below one temporary
directory, uses a unique private tmux socket, and records raw provider events
only in a mode-`0600` temporary file. Raw input, output, provider UUIDs, paths,
pane/process identifiers, and credentials are deleted rather than retained.

Only `pass`, `falsified`, and `blocked` sanitized results may be committed.
Assisted or manual runs are diagnostic and cannot produce `pass`.

The harness and its fail-closed privacy/isolation checks run with:

```sh
.venv/bin/pytest -q tests/test_v3_thread_workstream_spikes.py
```

The unassisted repeated native Codex gate runs with:

```sh
.venv/bin/python -m spikes.thread_workstream.codex_rollover \
  --output spikes/fixtures/thread-workstream/codex/0.145.0/native-rollover.json
```

The retained `0.145.0` result is a Gate 1 pass. Its procedure, limitations, and
decision impact are recorded in
`docs/spikes/codex-native-rollover.md`.

Trusted adoption plus native input-fenced history runs with:

```sh
.venv/bin/python -m spikes.thread_workstream.trust_history \
  --output spikes/fixtures/thread-workstream/codex/0.145.0/trust-history.json
```

The retained result passes Gates 2 and 3. See
`docs/spikes/rollover-trust-history.md`.

Execution-intent timing for ordinary Plan implementation and conversational
plans runs with:

```sh
.venv/bin/python -m spikes.thread_workstream.execution_trigger_study \
  --output \
  spikes/fixtures/thread-workstream/codex/0.145.0/execution-trigger.json
```

The retained result proves the ordinary Plan implementation input can be held
before sampling while its structured plan remains available. This is retained
as optional-policy capability evidence; v1 keeps ordinary implementation in the
current thread. It also proves that conversational plan/result/acceptance events
are observable, but natural language alone remains advisory; an explicitly
selected plan supplies artifact provenance and a user action supplies routing
authority. See
`docs/spikes/execution-intent-timing.md`.

Stable-prefix fork while the source's next turn remains active runs with:

```sh
.venv/bin/python -m spikes.thread_workstream.running_fork_study \
  --output \
  spikes/fixtures/thread-workstream/codex/0.145.0/running-source-fork.json
```

The retained result proves an immediate alternative can run from the latest
completed turn without copying or interrupting the source's in-progress turn.
See `docs/spikes/navigator-running-fork.md`.

The stronger same-TUI composition runs with:

```sh
.venv/bin/python -m spikes.thread_workstream.navigator_fork_study \
  --output \
  spikes/fixtures/thread-workstream/codex/0.145.0/navigator-running-fork.json
```

That retained result proves the original native TUI process, managed surface,
working directory, and active turn remain stable while the forked alternative
completes.

Managed-worktree ownership runs with:

```sh
.venv/bin/python -m spikes.thread_workstream.worktree_study \
  --output spikes/fixtures/thread-workstream/git/managed-worktree.json
```

The retained result is a secondary-study pass. See
`docs/spikes/managed-worktree-ownership.md`.

External-memory continuity and deterministic degradation run with:

```sh
.venv/bin/python -m spikes.thread_workstream.memory_study \
  --output spikes/fixtures/thread-workstream/memory/external-continuity.json
```

The retained result proves a healthy read-only reference can provide `full`
continuity while unavailable, delayed, stale, and wrong-scope references
accurately degrade to `immediate-only`. See
`docs/spikes/external-memory-continuity.md`.

## Local checks

```sh
python3 spikes/sqlite_reservation_spike.py
python3 spikes/tmux_gate_spike.py
python3 spikes/tmux_sidebar_swap_spike.py
python3 spikes/codex_app_server_probe.py \
  --schema spikes/fixtures/codex/0.144.4/codex_app_server_protocol.v2.schemas.json
python3 -m py_compile spikes/*.py
ruff check spikes
```

Generate the installed Codex schema before refreshing the fixture:

```sh
codex app-server generate-json-schema --out DIR
```

The retained Phase 0 schema at
`spikes/fixtures/codex/0.144.4/codex_app_server_protocol.v2.schemas.json` was
generated with the historical experimental mode. Its raw SHA-256 is
`93b300b8102e48bd1640fe12aec7ed29c215cd882237c70bedd32bf364dacc05` and its
parsed canonical JSON fingerprint is
`f5e8d20f3a8f9bb5e5b23ab0c5aa6bde7b12e7e0713606c5d0132651a4959d37`.

Production discovery does not pass the experimental flag. The retained
nonexperimental schema under `0.144.4/nonexperimental/` has raw SHA-256
`8f2f39b5b22a5bf563f63846f52895fd740d2be3dbc0fd93ca54e94ef29421a3`
and canonical fingerprint
`5d8251e1e2f713a3c567c927386f84f2f94692d4721b90d8ff36d0ff92877621`.
Raw file hashes and canonical semantic fingerprints are different evidence and
must not be substituted for one another.

## Persistent sidebar pane-swap probe

`tmux_sidebar_swap_spike.py` tests the proposed persistent Switchboard view
without starting Codex, Claude, or a model turn. It creates a private tmux
server whose view session exposes only `main`, plus a separate unattached
holding session. The view contains a fixed sidebar and one visible dummy
project runtime; a dummy child runtime waits behind a durable authorization,
exact-placement, pane-metadata, and client-selection gate in holding. Real
PTY-backed tmux clients exercise the shared right-hand slot.

```sh
.venv/bin/python spikes/tmux_sidebar_swap_spike.py
```

The recorded tmux `3.7b` result on 2026-07-21 passes 33 checks, including:

- a dead placeholder keeps each Switchboard-owned session durable without an
  anchor process, and per-session `destroy-unattached=off` defeats an
  adversarial global `on` value;
- the view session exposes only `main`; even an explicitly attached holding
  client cannot start the child before the durable intent, exact `view:main`
  placement, authorized pane metadata, and exact client selection agree;
- the swapped pane identity and placement are inspectable before the simulated
  durable locator commit, and the right-slot geometry remains stable;
- an unequal read-only `ignore-size` observer does not drive geometry, while an
  `active-pane` client is detected as unsupported rather than silently gaining
  a private navigation cursor;
- complete-and-return presents the parent first, submits only the fixed
  `transition_claim()` control prompt through an explicit writable client,
  fences input until simulated hook observation, and only then removes the
  parked child;
- direct mode and native zoom preserve the provider process while the sidebar
  is removed and reconstructed; and
- killing and recreating the server on the same named socket changes the
  `(socket_path, pid, start_time)` generation.

The probe uses a unique tmux socket, loads no user tmux configuration, emits no
provider paths or process IDs, and kills only its isolated server. It proves the
tmux topology, authorization fencing, explicit-client execution, transition
ordering, and server-generation mechanics. It does not yet prove the compact
Textual sidebar, provider behavior after live pane movement, trusted-hook
timing, real durable locator commits, semantic parent synthesis, or remote-host
views; those remain installed Phase 6 acceptance work.

## Codex thread-name mutation probe

Codex exposes `/rename` inside its interactive TUI but has no `codex rename`
shell command. `codex_thread_name_probe.py` retains the
no-model proof originally implemented by the local `$rename-thread` skill. It
uses `CODEX_THREAD_ID` from the current Codex turn, launches one isolated
short-lived App Server over stdio, calls `thread/name/set`, verifies the result
with `thread/read`, and exits without creating a shared listener.

Run it only from a Codex turn whose title may be changed:

```sh
.venv/bin/python spikes/codex_thread_name_probe.py \
  --title "verify Codex thread naming"
```

The probe prints no thread ID, title, path, prompt, or transcript content. The
App Server method and `CODEX_THREAD_ID` environment contract are experimental,
version-specific evidence. Production naming must capability-gate them and
must not depend on installing or invoking the personal skill.

There is also no native CLI flag or `thread/start` field that atomically creates
a named session. `codex_prestart_name_probe.py` tests the useful composition for
Switchboard instead: inside an isolated temporary Codex home it calls
`thread/start`, confirms the new thread is unnamed, calls `thread/name/set`,
verifies the name with an empty turn list, deletes the temporary thread, and
exits without a model request.

```sh
.venv/bin/python spikes/codex_prestart_name_probe.py
```

The recorded Codex `0.144.6` result on 2026-07-21 reported
`namedBeforeFirstTurn=true` and `modelTurnsStarted=0`. This supports a proposed
Switchboard sequence of precreate, name, close the transient helper, then start
the native TUI with exact `codex resume <UUID> <bootstrap-prompt>`. The final
resume/bind chain still requires installed end-to-end acceptance.

## Production Codex smoke

`live_codex_smoke.py` exercises the production adapter, reconciliation, and
snapshot path against an isolated temporary Switchboard registry. It never
uses the user's Switchboard host ID or database and never prints the live
snapshot, session names, or paths:

```sh
.venv/bin/python scripts/live_codex_smoke.py
.venv/bin/python scripts/live_codex_smoke.py --codex /usr/bin/codex
```

The default gate expects Codex `0.144.4` and the production canonical schema
fingerprint above. Successful output is one sanitized JSON summary containing
only version, fingerprint, feature names, emitted session count, and elapsed
milliseconds. Failure output is intentionally generic.

## Claude checks

`claude_agents_probe.py` runs on a host with a working Claude login:

```sh
python3 spikes/claude_agents_probe.py
```

`claude-hooks.settings.json` is a disposable additional settings file for
interactive lifecycle tests. Its hook script path is intentionally absolute,
as production hook installation must be. Adjust that path when copying the
probe to another host. `hook_capture.py` writes only allowlisted event and
environment fields. `claude-hooks.block-prompt.settings.json` is the no-model
variant: its `UserPromptSubmit` handler records the safe lifecycle shape and
then exits 2, proving prompt identity and effective hook loading before any API
request. On the recorded 2.1.210 run it completed with zero turns and zero
reported cost.

The durable production-path version creates temporary settings and an isolated
Switchboard registry, installs the exact six exec-form handlers into that
temporary document, and adds a second blocking prompt hook:

```sh
.venv/bin/python scripts/live_claude_smoke.py \
  --claude "$(command -v claude)" \
  --swbctl "$(pwd)/.venv/bin/swbctl"
```

It does not invoke Agent View, modify user settings, persist a Claude session,
or print provider identity. Success is one sanitized JSON summary containing
only the version, feature names, lifecycle-event counts, session count, elapsed
time, reported turns, and reported cost.

The original `agents-probe.json` and dated `agents-probe-2026-07-16.json`
captures are both retained because the same provider version produced
different valid optional-field and state/status combinations over time.

Versioned observed contract shapes live under `spikes/fixtures/`. Validation
conclusions and remaining gates are in `docs/phase-0-validation.md`.
