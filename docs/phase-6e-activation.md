# Phase 6E Coordinated Activation

Date: 2026-07-22

Status: implementation complete; live activation requires an accepted paired
commit and a reachable `snap.lan`.

Phase 6E is a clean break from core `0.2` and DMS `0.4.1`. The executor builds
core `0.3.0` and DMS `0.5.0` twice, verifies byte identity, installs both hosts
from an offline wheelhouse, imports fresh staged generations, cold-starts DMS,
records strict `CutoverEvidence v1`, commits snap before the desktop, installs
trusted hooks, and resumes one captured provider UUID.

Run the coordinator only from the accepted core commit. It copies its exact
script, canonical spec, artifacts, and prepared manifest to the remote host.
It never selects commits, projects, sessions, providers, or paths dynamically.

## Specification

Create a private JSON file with exactly this shape. Replace every angle-bracket
token before validation; do not retain angle brackets in the real file.

```json
{
  "executorVersion": 1,
  "cutoverId": "<new-uuid>",
  "coreCommit": "<40-lowercase-hex-core-commit>",
  "dmsCommit": "<40-lowercase-hex-dms-commit>",
  "sourceDateEpoch": 1784073600,
  "workspace": "/home/bryan/.local/state/agent-switchboard-cutover/<cutover-id>",
  "coreRepo": "/home/bryan/code/agent-switchboard-phase6e",
  "desktop": {
    "dmsRepo": "/home/bryan/code/agent-switchboard-dms-phase6e",
    "pluginDir": "/home/bryan/.config/DankMaterialShell/plugins",
    "pluginState": "/home/bryan/.local/state/DankMaterialShell/plugins/switchboard_state.json",
    "pluginSettings": "/home/bryan/.config/DankMaterialShell/plugin_settings.json",
    "service": "dms.service"
  },
  "hosts": [
    {
      "role": "desktop_primary",
      "hostId": "<desktop-host-uuid>",
      "generationId": "<new-desktop-generation-uuid>",
      "sshTarget": null,
      "python": "/usr/bin/python",
      "legacySwbctl": "/home/bryan/.local/share/agent-switchboard/venv/bin/swbctl",
      "legacyDatabase": "/home/bryan/.local/state/agent-switchboard/switchboard.db",
      "legacyConfig": "/home/bryan/.config/agent-switchboard/config.toml",
      "configRoot": "/home/bryan/.config/agent-switchboard",
      "stateRoot": "/home/bryan/.local/state/agent-switchboard",
      "releaseRoot": "/home/bryan/.local/share/agent-switchboard/releases",
      "binLink": "/home/bryan/.local/bin/swbctl",
      "backupRoot": "/home/bryan/.local/state/agent-switchboard-cutover/backups",
      "providerExecutables": {
        "codex": "/home/bryan/.local/bin/codex"
      },
      "hookFiles": {
        "codex": "/home/bryan/.codex/hooks.json"
      },
      "projectId": "<imported-project-uuid>",
      "stopSessions": ["<desktop-host-uuid>:codex:<provider-session-uuid>"]
    },
    {
      "role": "remote_owner",
      "hostId": "<snap-host-uuid>",
      "generationId": "<new-snap-generation-uuid>",
      "sshTarget": "snap.lan",
      "python": "/usr/bin/python",
      "legacySwbctl": "/home/bryan/.local/share/agent-switchboard/venv/bin/swbctl",
      "legacyDatabase": "/home/bryan/.local/state/agent-switchboard/switchboard.db",
      "legacyConfig": "/home/bryan/.config/agent-switchboard/config.toml",
      "configRoot": "/home/bryan/.config/agent-switchboard",
      "stateRoot": "/home/bryan/.local/state/agent-switchboard",
      "releaseRoot": "/home/bryan/.local/share/agent-switchboard/releases",
      "binLink": "/home/bryan/.local/bin/swbctl",
      "backupRoot": "/home/bryan/.local/state/agent-switchboard-cutover/backups",
      "providerExecutables": {
        "claude": "/home/bryan/.local/bin/claude"
      },
      "hookFiles": {
        "claude": "/home/bryan/.claude/settings.json"
      },
      "projectId": "<snap-imported-project-uuid>",
      "stopSessions": []
    }
  ],
  "currentSessionKey": "<desktop-host-uuid>:codex:<provider-session-uuid>"
}
```

`legacySwbctl` must remain an exact executable path after `binLink` moves to the
staged replacement. Provider versions are recorded observations, not allowlist
inputs. `hookFiles` names the complete provider configuration files preserved
for pre-commit rollback.

## Preparation

Both worktrees must be clean and exactly at the commits in the spec. Use a
Python environment containing `build`; wheel dependencies are downloaded into
the prepared wheelhouse and every host install later uses `--no-index`.

```sh
python scripts/phase6e_cutover.py validate-spec --spec /path/to/phase6e.json
python scripts/phase6e_cutover.py prepare --spec /path/to/phase6e.json
python scripts/phase6e_cutover.py status --spec /path/to/phase6e.json
```

Preparation rejects an existing workspace. Preserve it as immutable evidence
or choose a new cutover ID; never merge two preparation attempts.

Before execution, verify `snap.lan` is reachable, both legacy registries are
quiescent, all listed sessions are verified idle and resumable, and no pending
launch or transition owns a checkout.

After the immutable host backup and hook shutdown, the executor performs one
last full legacy reconciliation. It rejects any live provider session or active
launch intent, then retires only the remaining inactive v0.2 surface records and
records their count and identity digest. This terminal bookkeeping write is
restored from the host backup on every pre-commit rollback; direct database
cleanup is neither required nor accepted operator procedure.

## Execution and recovery

Exit the managed provider and every tmux client. From a plain desktop shell with
no `TMUX` or Switchboard capability variables:

```sh
python /home/bryan/.local/state/agent-switchboard-cutover/<cutover-id>/phase6e_cutover.py \
  execute \
  --spec /home/bryan/.local/state/agent-switchboard-cutover/<cutover-id>/spec.json \
  --confirm <cutover-id>
```

The coordinator disables legacy hooks, reconciles and retires inactive legacy
surfaces, stages a read-only public `swbctl` on both hosts, proves remote
online/offline/online behavior, and proves core, hook, view, and DMS actions
reject mutation while staged. It cold-starts DMS from the versioned artifact; a
plugin reload is not accepted as evidence.

Before the first commit, any failure restores the original public symlinks,
complete hook files, DMS plugin/state/settings, service state, and prior core
generation. The journal records any rollback error explicitly. Snap commits
first. After that boundary, automatic downgrade is forbidden and the journal
records `forward_recovery_required` until both hosts reach the accepted pair.

On success, the executor starts DMS, opens the first local project view in
direct mode, and calls `frame reopen` for the exact imported session key. The
immutable host backups, export bundles, prepared manifest, evidence document,
and journal remain under the configured backup/workspace roots.

## Acceptance boundary

Activation is accepted only when all of these are true:

- both host generations report `committed` and retain identical evidence;
- core doctor, reconciliation, HostState, and NavigatorState evidence covers
  both hosts;
- DMS has a new systemd invocation identity, entry model v1, and cold/warm cache
  provenance under `last_good_switchboard_entry_model_v1`;
- the remote host is observed online, then offline while its staged route is
  hidden, then online again after exact restoration;
- replacement hooks name the stable public `swbctl` symlink;
- the first direct view owns the exact captured provider session UUID; and
- installed core/DMS artifacts expose no old command, protocol, or cache route.

If the remote host is unreachable, stop after preparation and local isolated
tests. Do not manufacture remote evidence and do not commit either generation.
