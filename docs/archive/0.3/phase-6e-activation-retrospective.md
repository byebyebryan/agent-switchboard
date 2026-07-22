# Phase 6E Activation Retrospective

Date: 2026-07-22

Status: completed once; coordinator retired and must not be reused

Core `0.3.0` and DMS `0.5.0` were activated on the desktop and remote owner as
cutover `6ace9d51-c893-4db4-ac59-a79f31800c97`. The accepted evidence file has
SHA-256 `c2b073c112f4849529b70f7ad31d007a50dfe4ea735063bc07caa7de6c46b05e`.
Both generations committed and the exact imported Codex UUID resumed.

The activation process was too disruptive for the value of the migrated
Switchboard state. Repeated full-session shutdowns were used as an integration
test loop even though Switchboard had no meaningful user state and could have
been recreated. That operating assumption is rejected.

Live acceptance also exposed two defects:

- global hooks failed noisily in ordinary provider sessions that had no
  Switchboard authority; and
- `frame reopen` launched the managed provider into the holding session but did
  not project it into the persistent user view.

Both defects are regression-covered in the post-activation code. The normative
replacement policy is [Runtime Operations and Safety](../../operations.md):
Switchboard state is disposable, existing agent sessions are not, DMS is
optional, SSH attachment is first-class, and no future Switchboard operation
may require a provider-wide outage.

The exact coordinator, prepared artifacts, journal, backups, and evidence are
retained in the private activation workspace and Git history. The coordinator
and executable runbook are intentionally not shipped after acceptance.
