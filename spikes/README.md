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

## Claude checks

`claude_agents_probe.py` runs on a host with a working Claude login:

```sh
python3 spikes/claude_agents_probe.py
```

`claude-hooks.settings.json` is a disposable additional settings file for
interactive lifecycle tests. Its hook script path is intentionally absolute,
as production hook installation must be. Adjust that path when copying the
probe to another host. `hook_capture.py` writes only allowlisted event and
environment fields.

Versioned observed contract shapes live under `spikes/fixtures/`. Validation
conclusions and remaining gates are in `docs/phase-0-validation.md`.
