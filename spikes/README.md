# Phase 0 contract spikes

These probes test the provider and transport assumptions in
`docs/product-landscape.md`. They retain structure, lifecycle metadata, and
timings, but not prompts or transcript content.

## Local checks

```sh
python3 spikes/sqlite_reservation_spike.py
python3 spikes/tmux_gate_spike.py
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
