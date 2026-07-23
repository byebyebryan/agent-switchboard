# Switchboard

Switchboard is a persistent user view over provider-native coding-agent
sessions. Its primary interface is a tmux-resident navigator beside the active
native provider pane; direct single-pane mode remains available when no
Switchboard UI is wanted. Switchboard owns host-local project/task frames and
tmux presentation while Codex and Claude Code continue to own conversation
history and terminal UI.

## Release shape

Core `0.3.0` is the clean-break Phase 6 generation. It exposes only Config v3,
registry schema v1, HostState/NavigatorState/PresentationDirective v1, and the
view/frame workflow. Snapshot/Fleet, task-first CRUD, the old administrative
TUI, old migrations, and compatibility aliases are not installed. The compact
resident navigator is the current primary UI.

The offline `cutover export` command is the only component that understands an
exact Config v2/schema-v10 source. It requires a quiescent legacy registry and
produces a self-authenticating bundle without mutating the source.

The primary command surface is:

```text
swbctl init --config CONFIG_V3_TEMPLATE
swbctl reset --confirm-generation GENERATION [--config CONFIG_V3_TEMPLATE]

swbctl state host --json
swbctl state navigator [--refresh] --json

swbctl view open --host HOST (--view VIEW | --project PROJECT) --request-id UUID \
  [--can-focus-desktop] [--can-launch-terminal] --json
swbctl view recover --host HOST --recovery RECOVERY --request-id UUID \
  [--can-focus-desktop] [--can-launch-terminal] --json
swbctl view attach --view VIEW [--host HOST] [--request-id UUID]

swbctl view list|show|focus|mode|retire
swbctl frame list|show|push|back|complete|close|reopen
swbctl project ...
swbctl session show|stop ...
swbctl hooks ...
swbctl cutover export|import|status|commit|rollback
swbctl doctor
swbctl reconcile
swbctl agent-mcp
```

Remote reads and mutations use configured fixed SSH endpoints. Cached remote
state is presentation-only; every mutation executes and revalidates on its
owner host. Provider versions are strict observations rather than allowlists:
missing/malformed executables and behavioral or identity mismatch fail closed.

Phase 6E used DMS adapter `0.5.0` for a one-time technical activation rehearsal;
that adapter is no longer a release, development, or acceptance dependency.
DMS integration is deferred as an optional convenience entry point after the
TUI-first workflow is accepted.

Switchboard is still in pre-adoption validation. Normal work continues through
native Codex, Claude Code, and tmux. Testing uses isolated Switchboard roots,
tmux servers, views, and provider sessions; it does not stop existing agents,
restart user tmux, edit normal provider hooks, or restart DMS. `init` and
`reset` publish fresh committed generations without provider or tmux lifecycle
operations.

Design and operations:

- [Agent Switchboard Design](docs/design.md)
- [State and Control-Turn Contract](docs/state-contract.md)
- [View and Frame Workflow](docs/view-workflow.md)
- [Runtime Operations and Safety](docs/operations.md)
- [Phase 6 Clean-Break Plan](docs/phase-6-plan.md)
- [Phase 6E.1 Acceptance](docs/phase-6e1-acceptance.md)
- [CutoverBundle v1 and Activation](docs/cutover-bundle-v1.md)

Historical `0.2` records remain outside the installed package under
[`docs/archive/0.2`](docs/archive/0.2/README.md).
The completed Phase 6E activation retrospective is retained under
[`docs/archive/0.3`](docs/archive/0.3/README.md).

## License

Switchboard is licensed under the [MIT License](LICENSE).
