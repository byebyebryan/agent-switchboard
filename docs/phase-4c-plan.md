# Phase 4C: Session-Scoped Agent Context and Tools

Date: 2026-07-19

Status: accepted

## Decision and sequencing

Phase 4C adds a small agent-facing surface on top of the accepted local-human
curation and handoff operations from Phase 4B. It does not make Switchboard an
agent supervisor, inject project history into every launch, read provider
transcripts, or let one session control another.

The first increment is deliberately narrower than the eventual tool list:

1. **4C.1: Codex authorization and current context.** Issue a random
   session-scoped capability for new managed Codex launches, validate it
   against the exact current surface and binding, expose bounded current
   project context, and permit only current-session name, handoff, and wrap.
2. **4C.2: read/search completeness.** Add bounded project session listing,
   explicit handoff reads, and metadata/handoff search without provider
   transcript access.
3. **4C.3: transport adapter.** Keep the core operations transport-neutral,
   then project the stable commands through a thin stdio MCP adapter. A
   provider-specific instruction skill remains optional usability material;
   it is not an authorization boundary or a Phase 4C acceptance requirement.
4. **4C.4: Claude parity.** Reuse the accepted capability and context contract
   for managed Claude sessions without re-enabling Agent View or changing the
   provider-owned history picker.
5. **4C.5: optional memory and installed acceptance.** Add a bounded optional
   memory adapter only after the stable/live/recent context contract is proven,
   then run isolated installed acceptance for both providers and transports.

This sequence chooses the existing structured `swbctl` JSON surface as the
canonical executable contract. An MCP server or provider integration must call
the same core operations and must not gain database, tmux, provider, or
authorization logic of its own.

The separate DMS repository is not part of Phase 4C. DMS remains a human
presentation consumer of Snapshot v1, PresentationPlan v1, and SessionAction
v1. Agent context is session-local and must never be added to the desktop
snapshot or launcher model.

## Existing boundary that 4C reuses

Phase 4B already provides:

- exact inherited-tmux current-session resolution;
- confirmed surface/session/launch bindings;
- normalized project and location identities;
- bounded session detail and immutable handoff protocols;
- atomic user curation and wrapping without advancing provider observation
  clocks; and
- managed Codex and Claude launches whose provider process inherits only
  explicitly constructed environment additions.

The existing `launch_intents.capability_hash` column is not an agent bearer
capability. It contains a static fingerprint identifying the already accepted
launch contract for one provider/action path. Phase 4C must preserve that
meaning and add a distinctly named nullable `agent_capability_hash` instead of
silently reinterpreting existing rows or constants.

## 4C.1 authorization contract

### Issuance and storage

Only a newly created managed Codex `new` or `resume` launch receives a 256-bit
random URL-safe capability. Switchboard:

- generates the secret before it creates the managed surface;
- stores only `SHA-256(secret)` in `agent_capability_hash`;
- injects the raw secret as `AGENT_SWITCHBOARD_CAPABILITY` into that one tmux
  surface alongside its existing launch and surface IDs;
- never emits the secret in JSON, errors, snapshots, logs, hooks, handoffs,
  provider discovery, or the registry; and
- does not issue a capability to adopted unmanaged panes, manager surfaces,
  Claude history pickers, or the first Claude increment.

Idempotent preparation reuses the already created surface and its inherited
environment. It does not replace the stored digest or require the caller to
recover the raw secret. A failed or expired launch cannot authorize tools, and
retiring or rebinding the surface makes the old capability unusable even while
its digest remains as immutable launch evidence.

### Exact current-session validation

Every `swbctl agent ...` invocation fails closed unless all of the following
agree at one point in time:

- `AGENT_SWITCHBOARD_CAPABILITY`, `AGENT_SWITCHBOARD_LAUNCH_ID`, and
  `AGENT_SWITCHBOARD_SURFACE_ID` are present and canonically bounded;
- the inherited tmux client resolves to one exact pane with Switchboard
  metadata for those launch and surface IDs;
- the retained surface is local, unretired, role `session`, and confirmed
  bound to the pane's current session;
- the session points back to that surface and is a Codex session for 4C.1;
- the launch is `bound`, points to the same surface and target session, and has
  an `agent_capability_hash`; and
- a constant-time comparison matches the supplied secret's digest.

The validation is a same-user attribution and confused-deputy guardrail, not a
security boundary against the owner of the local account. The capability never
authorizes another session, even when both sessions share a project.

Human `swbctl current` and `swbctl session ... --current` keep their existing
tmux-only convenience contract and do not accept the agent capability.

## 4C.1 command surface

The initial commands accept no session or project identity from the caller:

```text
swbctl agent current --json
swbctl agent context --json
swbctl agent name VALUE --json
swbctl agent name --clear --json
swbctl agent handoff --json-stdin --json
swbctl agent wrap --json-stdin --json
```

`current` and all mutations return the existing validated SessionDetail
envelope. Agent handoff input reuses the strict Phase 4B JSON-stdin contract
and stores immutable handoffs with `source=agent`. Name edits retain explicit
agent attribution without changing provider-owned names or observation clocks.
Wrap appends the handoff and sets `wrapped_at` atomically; it does not stop the
provider or decide that the work is complete.

No 4C.1 command can list arbitrary handoff bodies, mutate purpose or pinning,
launch, resume, stop, attach, select a surface, send a prompt, or address a
caller-supplied session key. Those omissions are part of the authorization
contract rather than missing CLI convenience.

## Bounded current-project context

`agent context` returns one versioned envelope assembled on demand from three
authorities:

```text
stable   explicitly configured context source files under the exact location
live     the authorized caller/project/location identity and retained status
recent   bounded same-project local session metadata and latest handoffs
```

It contains no provider transcript, prompt, response, raw argv, environment,
tmux locator, process ID, credential, token, hook payload, memory result, Git
diff, or remote-host cache.

The 4C.1 bounds are fixed and validated on both construction and parsing:

- at most 32 configured text files;
- at most 64 KiB per file and 256 KiB across stable file content;
- at most 20 same-project local sessions, including the current session;
- at most one latest explicit handoff per projected session;
- configured-directory traversal at most 16 levels and 4,096 entries;
- at most 32 bounded source issues; and
- one canonical JSON document within the existing machine-output byte limit.

Configured source paths are project-relative. Resolution starts from the
caller's exact configured project location, rejects missing/non-regular files,
symlink escapes, invalid UTF-8, NUL, and non-text terminal controls, and walks
configured directories deterministically without following directory
symlinks. File identity is the configured relative path; each included record
also carries its content hash, modification observation time, and truncation
state.

Missing or unreadable context sources produce bounded structured issues and do
not suppress valid project/session/handoff context. Exceeding a structural
count produces an explicit truncation flag. The command does not guess common
files that were not configured.

Recent session context is host-local and same-project only. It includes
identity, provider, curated display fields, normalized status, timestamps,
wrapping/pinning state, name attribution when known, and the newest retained
explicit handoff. It does not infer one project-level task state or merge
independent sessions' next actions.

## Attribution and storage

Phase 4C adds nullable `sessions.name_actor` with the bounded values `user` or
`agent`. Existing curated names remain valid with unknown actor. Human name
edits set `user`, agent name edits set `agent`, clearing a curated name clears
the actor, and provider-owned updates never claim agent attribution.

Handoffs already have the durable `user`, `agent`, and `imported` source
vocabulary. The shared atomic curation operation therefore accepts an internal
source argument while the existing human command remains fixed to `user` and
the new authorized command remains fixed to `agent`.

Agent mutations keep the Phase 4B idempotency rule: a caller-supplied handoff
UUID may be replayed only with byte-equivalent canonical content and source.
Conflicting reuse fails without mutation.

## Failure and privacy behavior

- Missing, malformed, mismatched, unbound, expired, or wrong-provider
  authorization exits nonzero, writes no stdout, and emits one bounded safe
  stderr message that never contains the supplied capability.
- A capability mismatch does not reveal whether a different capability,
  launch, surface, or session exists.
- Context source errors are data-level issues inside a valid authorized
  envelope; authorization, schema, registry, and identity errors fail the
  entire command.
- Context file content is allowed to be multiline but is always encoded as one
  canonical JSON string. Terminal controls other than tab/newline are rejected.
- Optional context and later memory failures never block the ordinary human
  snapshot, list, open, new, history, stop, TUI, or DMS paths.
- Agent commands never reconcile providers or call a model. They operate on
  retained metadata plus explicitly configured local files.

## 4C.1 implementation loop

1. Add the schema migration and keep old launch/name rows readable.
2. Generate and inject Codex capabilities only for newly created managed
   surfaces; cover idempotency, failure, and no-secret-output behavior.
3. Implement one transport-neutral authorizer over the current pane, retained
   surface/session/launch, and constant-time digest comparison.
4. Add coherent bounded same-project context reads and the safe configured-file
   reader.
5. Add validated context protocol construction/parsing and the six exact CLI
   forms above.
6. Prove current-only mutation attribution, handoff replay/conflict behavior,
   and fail-closed cross-session attempts.
7. Run the full core/TUI suites, package/import checks, two-build distribution
   audit, and installed isolated no-model acceptance with fake providers and a
   private tmux socket.

## Acceptance gates

4C.1 is complete only when:

- the raw capability exists only in the managed Codex surface environment;
- only its digest is stored and no machine or error output contains it;
- every agent operation revalidates the exact current pane, launch, surface,
  session, provider, and digest;
- adopted, unbound, retired, mismatched, Claude, manager, and plain-terminal
  callers fail closed without mutation;
- context files cannot escape the configured location and all content/count
  bounds are explicit and parser-enforced;
- same-project session context is coherent, bounded, host-local, and contains
  only metadata plus explicit handoffs;
- human and agent curation retain distinct durable attribution;
- existing snapshot, DMS, TUI, provider, hook, and presentation contracts do
  not change; and
- installed acceptance starts no real provider, invokes no model, touches no
  default registry/tmux/DMS state, and cleans only test-owned resources.

## 4C.1 acceptance evidence

Accepted locally on 2026-07-19:

- `compileall`, Ruff formatting, and Ruff lint passed; the dependency-free
  suite passed 555 tests with only the expected Textual-not-installed skip,
  and the optional project environment passed all 20 Textual tests;
- the migration suite proved v1 through v6 creation and upgrade behavior,
  nullable legacy capability/name attribution, and unique agent capability
  digests;
- two clean builds with `SOURCE_DATE_EPOCH=1784073600` were byte-identical and
  passed the distribution content audit: 35 package files, 40 wheel files, and
  50 source-distribution files, with matching SHA-256 digests between builds;
- the final wheel was installed without dependencies under isolated HOME and
  XDG roots, then exercised from one test-owned private tmux surface through
  `agent current`, `context`, `name`, `handoff`, and `wrap`;
- that installed exercise validated the context and detail envelopes, current-
  first caller identity, explicit source content, agent name/handoff
  attribution, immutable handoff order, atomic wrapping, and digest-only
  registry storage;
- a wrong capability in the exact pane failed with no stdout and the one safe
  authorization error, while a scan of all acceptance outputs found no raw
  capability; and
- the exercise invoked no provider or model, never touched the default tmux
  server, registry, provider profiles, or DMS, killed its private tmux server,
  and removed only explicitly named test resources.

After these gates pass, 4C.2 can add the remaining read/search surface without
weakening the current-session mutation boundary.

## 4C.2 bounded read and search contract

The authorized caller may read another retained session only when it is local
to the same host and belongs to the caller's exact project. Read operations do
not accept a project ID and never cross into a remote snapshot. The structured
CLI additions are:

```text
swbctl agent sessions --json
swbctl agent show SESSION_KEY --json
swbctl agent handoff-read HANDOFF_ID --json
swbctl agent handoffs SESSION_KEY [--limit N] --json
swbctl agent search QUERY [--limit N] --json
swbctl agent memory QUERY [--limit N] --json
```

Session listing is capped at 50 current-project sessions with the caller first.
Handoff listing reuses the existing maximum of 100 immutable records. Search
queries are normalized bounded text of at most 256 characters and return at
most 20 results. Search examines only retained session names, purposes,
providers, and explicit handoff summaries/next actions. It never searches
provider IDs as free text, prompts, responses, hook payloads, or transcripts.
Search work is bounded before Python allocation to at most 512 session and
2,048 handoff candidates and reports truncation when either candidates or
matching results are omitted.

Exact handoff reads validate that the handoff's owning session remains in the
same project. Missing and foreign identities use the same bounded not-found
failure so an agent cannot probe other projects.

## 4C.3 MCP transport contract

`swbctl agent-mcp` is a dependency-free stdio MCP server over the same
`AgentToolService`; it contains no independent registry, tmux, provider, or
authorization behavior. It supports newline-delimited UTF-8 JSON-RPC 2.0,
the MCP `2025-11-25`, `2025-06-18`, and `2025-03-26` revisions, initialization,
ping, `tools/list`, and `tools/call`. Batches, reused request IDs, oversized
lines, calls before initialization, and unknown methods fail with bounded
protocol errors. Standard output contains protocol frames only.

The stable tools are:

```text
project_get_current       project_get_context       project_list_sessions
session_get               session_get_handoff       session_list_handoffs
session_search            memory_search
session_set_name          session_set_handoff       session_wrap
```

Read tools are annotated read-only. Mutations accept no session identity and
remain restricted to the authorized caller. Every successful call returns the
same validated structured envelope exposed by the CLI, both as
`structuredContent` and canonical JSON text for compatibility. Tool execution
failures return `isError=true`; framing or schema failures use JSON-RPC errors.

## 4C.4 Claude parity contract

New managed Claude `new` and exact `resume` surfaces receive the same random
capability and can use the identical CLI/MCP operations after hook/live
identity binding. Authorization compares the retained provider rather than
assuming Codex. Claude Agent View remains disabled and provider history remains
native. History-picker, adopted, manager, unbound, failed, and expired surfaces
receive no usable capability.

No Claude agent tool starts, resumes, stops, reconciles, or sends input to a
provider. The capability authorizes only metadata/file reads and current-
session curation after the ordinary managed launch has independently bound.

## 4C.5 optional memory adapter contract

Memory is disabled by default. `[memory]` may enable one explicit absolute
stdio MCP command and tool name (default `search`), with a one-to-30-second
timeout. Switchboard performs a fresh bounded MCP initialization and tool call
for each query, passing only the bounded query, result limit, and configured
Switchboard project name. It accepts text tool content only and caps the joined
UTF-8 result at 64 KiB.

The child environment removes `AGENT_SWITCHBOARD_CAPABILITY`, launch ID, and
surface ID. It receives no registry path or Switchboard envelope. The adapter
does not read claude-mem databases, settings, transcripts, logs, or private
plugin files. Absence, timeout, malformed output, tool error, or nonzero exit
returns a valid unavailable memory envelope and never blocks other agent or
human operations.

## Final 4C acceptance gates

Phase 4C is complete when tests and final installed artifacts prove:

- same-project reads/search cannot disclose foreign-host/project records and
  all list, query, candidate, input, and output bounds are parser-enforced;
- the CLI and MCP surfaces invoke the same service results and mutations;
- malformed MCP lifecycle, framing, arguments, request reuse, and oversized
  input fail without leaking the session capability;
- Codex and Claude new/resume surfaces receive provider-scoped capabilities,
  while history/adopted/manager surfaces do not;
- disabled, missing, failed, slow, and successful fake memory adapters are
  isolated and never inherit the raw capability; and
- installed wheel acceptance exercises fake bound Codex and Claude surfaces,
  every CLI/MCP operation, and fake memory over private tmux/XDG roots without
  invoking a provider/model or touching DMS/default state.

## Phase 4C completion evidence

Accepted locally on 2026-07-19:

- `compileall`, Ruff formatting, and Ruff lint passed; the complete optional
  environment passed all 586 tests, including all 20 Textual tests;
- unit and protocol coverage exercised project/host isolation, list/search
  candidate and result bounds, every CLI form, MCP lifecycle and all eleven
  tools, request reuse, batches, invalid arguments, oversized framing, safe
  tool errors, Claude capability parity, and disabled/missing/slow/successful
  memory adapters;
- two clean builds with `SOURCE_DATE_EPOCH=1784073600` were byte-identical and
  passed the distribution audit: 37 package files, 42 wheel files, and 52
  source-distribution files, with matching SHA-256 digests between builds;
- the final dependency-free wheel was installed in a fresh environment and
  exercised against separately bound fake Codex and Claude launches. Each ran
  all eleven MCP tools and successful fake memory search with no raw capability
  in frames or the memory child environment;
- the installed `swbctl` then ran every agent CLI operation plus `agent-mcp`
  from an exact fake bound Claude pane on a test-owned private tmux socket,
  registry, project, HOME, and XDG roots; and
- acceptance started no provider, invoked no model, did not read provider or
  claude-mem private state, never entered DMS, and cleaned the test-owned tmux
  server independently of any live user session.
