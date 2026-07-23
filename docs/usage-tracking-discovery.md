# Cross-host usage tracking discovery

Status: Discovery note; no implementation approved

Last updated: 2026-07-22

Related design: [Agent Switchboard design](design.md)

## Summary

Switchboard can provide a useful Codex and Claude Code usage view across local
and configured-remote hosts, but the provider data does not form one uniform
"usage" counter. The model must keep three concerns separate:

1. **Quota headroom**: current used or remaining quota and its reset time.
2. **Activity**: tokens, sessions, cost estimates, or daily provider activity.
3. **Burn history**: timestamped observations retained by Switchboard so it can
   calculate depletion rates and forecasts.

The current provider sources are sufficient for a useful first version. The
hard design problems are account identity, cross-host deduplication, flexible
limit shapes, source provenance, and history semantics rather than basic data
extraction.

Usage should be a separately versioned, account-oriented projection rather
than fields added directly to host-owned Snapshot v2. Provider collection must
remain local to the host that owns the credentials. The existing bounded SSH
fleet transport can move only normalized, credential-free observations.

## Motivation and current topology

The initial deployment has three materially different account shapes:

| Collector host | Provider account | Observed quota shape |
| --- | --- | --- |
| Local host (`80H1VV3`) | Codex Business | Monthly credit pool; no current rolling session or weekly windows |
| Local host (`80H1VV3`) | Claude Code Enterprise | Session quota; general weekly quota may be absent; model-scoped limits may exist |
| Remote host (`starship`) | Codex Plus | Weekly Codex quota plus a separate model-scoped Spark bucket |

The same design must also tolerate one provider account being logged in on
more than one host. Quota is account-scoped, so duplicate observations of that
account must not be summed.

The intended consumer set is broader than the current DMS plugin. A normalized
Switchboard contract could feed DMS, a future TUI, shell output, or other local
frontends without duplicating provider access and SSH logic in each consumer.

## Data-source findings

These findings were captured on 2026-07-22 from Codex CLI 0.144.6 locally,
Codex CLI 0.145.0 on `starship`, and Claude Code 2.1.216 locally. Provider
versions and response shapes can change; every observation must retain source
and version metadata.

### Codex App Server

The documented Codex App Server account methods are the preferred source:

- `account/read`
- `account/rateLimits/read`
- `account/usage/read`

`account/read` exposes the ChatGPT account email and plan type but no stable
ChatGPT account or workspace identifier. Email is useful display and
verification evidence but is not a sufficient durable Switchboard key.

`account/rateLimits/read` must be treated as a collection of limit buckets.
The response can contain rolling windows, a monthly spend-control limit,
model-scoped limits, reset-credit coupons, or absent windows. A fixed
`primary`/`secondary` model loses information. Collectors should iterate
`rateLimitsByLimitId` when present and use the legacy single `rateLimits`
object only as a fallback.

The local Business observation contained:

- monthly limit: 30,000 credits;
- used at capture: approximately 27,331.07 credits;
- reset: 2026-08-01T00:00:00Z;
- no current rolling session or weekly windows.

The remote Plus observation contained:

- a normal Codex weekly bucket, approximately 93% used at capture;
- a separate `codex_bengalfox` bucket labeled GPT-5.3-Codex-Spark;
- an account-facing Plus plan while the quota bucket used a different opaque
  backend plan label.

Account plan and per-bucket plan therefore need separate fields. Reset-credit
coupon counts must not be presented as spend-credit balance.

`account/usage/read` worked for both ChatGPT-managed accounts and returned
account-level lifetime summaries plus daily token buckets. It is an activity
source, not a monthly credit ledger. No documented conversion maps these token
counts to Business credit debits, monetary cost, model categories, or cached
token accounting.

Switchboard already contains a bounded App Server client in
`src/agent_switchboard/providers/codex.py`. It handles the initialization
handshake, interleaved messages, deadlines, output bounds, and process-group
cleanup. A future usage collector should reuse or extract that transport rather
than introduce a second subprocess implementation.

CodexBar is useful only as a compatibility fallback. Its normalization omitted
the extra Spark bucket, rearranged rolling windows, added third-party pace
fields, and represented Business monthly credits differently from the provider
response.

References:

- [Codex App Server account endpoints](https://learn.chatgpt.com/docs/app-server#auth-endpoints)
- `spikes/fixtures/codex/0.144.4/nonexperimental/codex_app_server_protocol.v2.schemas.json`
- `spikes/README.md`

### Claude Code

Claude exposes useful information through several sources, none of which is a
complete substitute for the others.

`claude auth status --json` is the preferred identity observation. It exposes
login method, provider, email, organization identity/name, and subscription
type without reading credential files directly. Raw identity values should not
be exported to fleet consumers by default.

Claude's documented status-line JSON can contain optional five-hour and
seven-day `rate_limits` windows after an API response. This is a passive,
event-driven source rather than an always-fresh query API, and the documented
availability guarantee currently covers Pro and Max accounts. It should be
accepted when present but absence must remain unknown rather than zero.

The existing AiOverview adapter also uses the private
`https://api.anthropic.com/api/oauth/usage` endpoint. It currently exposes
richer Enterprise quota shapes, including named session, weekly, model-scoped,
extra-usage, and spend data. It is useful as a host-local fallback, but it is
not a published stable contract and must be labeled accordingly in source
metadata.

Claude Code OpenTelemetry is the preferred forward-looking activity source.
It can export token use, cost estimates, sessions, active time, lines of code,
commits, pull requests, organization/user dimensions, installation identity,
and session identity. It does not provide quota headroom. OpenTelemetry is not
currently configured on the local host.

The local Claude transcript tree is not a viable primary source: it was about
9.2 GB across 4,823 JSONL files at discovery time, contains sensitive content,
and would impose substantial repeated I/O. The local statistics cache was also
stale. Switchboard should not scan or transport transcripts for usage
collection.

Enterprise organization analytics and spend-limit APIs may support later
reconciliation when an administrator supplies the appropriate key. They are
delayed or describe overage controls rather than live seat-included quota, so
they are not the first-version headroom source.

References:

- [Claude Code status-line data](https://code.claude.com/docs/en/statusline)
- [Claude Code OpenTelemetry monitoring](https://code.claude.com/docs/en/monitoring-usage)
- [Claude Code analytics](https://code.claude.com/docs/en/analytics)

## Proposed boundary

Keep the existing session fleet and a future usage fleet separate but
composable:

```text
host-local provider adapters
  ├─ Codex App Server account methods
  ├─ Claude auth/status-line data
  ├─ Claude private OAuth fallback
  └─ future Claude OpenTelemetry activity
                 │
                 ▼
          UsageEnvelope v1
                 │
     existing bounded SSH pattern
                 │
                 ▼
       UsageFleetEnvelope v1
          ├─ DMS plugin
          ├─ future TUI
          └─ shell/JSON consumers
```

Snapshot v2 is host-owned and describes projects, tasks, sessions, runtimes,
and surfaces. Usage quota is account-owned, can be observed by multiple hosts,
has a different refresh cadence, and can fail independently. A separate
envelope preserves those semantics and allows each contract to evolve without
silently dropping unknown fields through an older typed Snapshot projection.

A later implementation could expose commands conceptually shaped as:

```text
swbctl usage --json
swbctl usage-fleet --json
swbctl usage-fleet --refresh --json
```

As with the current fleet commands, the default fleet read should be
network-free and return retained local/remote observations. Explicit refresh
should use bounded, noninteractive SSH. A future composite refresh may retrieve
separately versioned session and usage envelopes in one SSH round trip without
combining their schemas.

## Normalized contract requirements

The contract should preserve at least:

- a durable configured account key and user-facing alias;
- provider and account-level plan;
- observing host identity;
- source method, provider version, schema fingerprint, and confidence/stability
  classification;
- provider observation time, Switchboard receipt time, age, and stale state;
- a flexible `limits[]` collection;
- optional provider activity summaries and daily buckets;
- optional extra-usage/spend controls;
- bounded per-account and per-source errors.

Each limit should preserve:

- stable provider limit ID when available;
- display name and optional model/surface scope;
- limit kind, such as rolling session, weekly, monthly spend-control, or
  model-scoped;
- bucket-level plan when different from the account plan;
- used percentage and reset time when available;
- window duration when available;
- exact used, limit, remaining, and unit when available;
- unknown or absent values without converting them to zero.

Provider-specific unknown fields may be retained in bounded diagnostic
fixtures, but the public projection should not expose unbounded raw payloads.

## Account identity and fleet aggregation

Provider session identity is not provider account identity. A future account
key should be independent of the collector host.

Codex currently supplies no stable ChatGPT account/workspace ID through
`account/read`. The safest first contract is a user-configured UUID or stable
alias, with provider/email/plan treated as verification evidence. Claude
organization and account information can support matching, but raw email and
organization identifiers should remain host-local or be redacted/hashed for
fleet output.

Aggregation rules differ by data plane:

- **Quota:** observations for the same account are replicas; the freshest valid
  observation wins. Never sum them.
- **Activity:** distinct host/session activity is generally additive, subject
  to provider event temporality and session/event deduplication.
- **Provider account history:** service-backed daily buckets observed on two
  hosts are replicas and must not be summed.
- **Errors:** retain per-host source failures so a fresh successful replica does
  not hide collector-health problems.

## History and depletion forecasts

Provider facts and Switchboard-derived estimates must remain distinguishable.

For Business monthly credits, persist exact `used` and `limit` observations.
For two samples in the same reset period:

```text
burn_rate = (used_now - used_before) / elapsed_time
projected_exhaustion = observed_at + remaining_now / burn_rate
```

Samples separated by a reset, limit change, account remap, or incompatible
source shape must start a new series. The exact used/limit values should take
precedence over rounded percentages.

For percentage-only rolling windows, the same calculation can provide an
approximate pace, but rounding, sliding-window expiration, and provider-side
recalculation make it less precise. Forecast output must include its interval,
sample count, observation age, and assumptions. A falling used percentage in a
sliding window is not negative consumption.

History should contain normalized observations only—never credentials or raw
transcripts. Collection should use a shared cache and lock so multiple DMS or
TUI instances do not each query providers independently. Recording on material
change plus a heartbeat is preferable to persisting every frontend refresh.

Retention period, heartbeat cadence, material-change thresholds, and forecast
confidence rules remain design decisions.

## Security and reliability constraints

- Query each provider only on the host that owns its login.
- Never read, copy, log, or transport Codex/Claude credential files or tokens.
- Never expose an App Server or OAuth bearer token directly over the LAN.
- Export only normalized, bounded, credential-free JSON over the existing SSH
  trust boundary.
- Keep private/third-party source labels visible in diagnostics.
- Apply subprocess deadlines, response-size bounds, concurrency bounds, and
  abnormal-exit cleanup equivalent to the existing fleet/provider code.
- Preserve the last successful observation with explicit receipt time and stale
  age when refresh fails; do not present retained values as current.
- Do not wait for provider-generated rate-limit events before serving an
  on-demand current snapshot.
- Do not interpret null or missing limits as zero usage or unlimited quota.

## General fixes versus account-specific handling

The following work is general enough to contribute upstream to a usage plugin
or share across Switchboard adapters:

- correct App Server framing and lifetime;
- bounded timeouts and process cleanup;
- flexible limit-array normalization;
- explicit freshness/stale metadata;
- source/version diagnostics;
- shared cache and single-flight refresh behavior.

The following mappings necessarily remain provider/account-shape-specific:

- Codex Business monthly spend-control credits;
- Codex Plus weekly and model-scoped buckets;
- Claude Enterprise absent, session-only, or model-scoped windows;
- Claude's private OAuth fallback and its evolving response fields.

This favors one Switchboard collector contract with provider-specific adapters
and thin frontends rather than a DMS-only collector or a fork that embeds
provider access directly in the UI.

## Proof-of-data sequence before implementation

1. Retain redacted, versioned fixtures for the three initial account shapes:
   local Codex Business, remote Codex Plus with multiple buckets, and local
   Claude Enterprise with optional/absent windows.
2. Define configured account identity and matching behavior, including the case
   where one account is logged in on multiple hosts.
3. Draft `UsageEnvelope v1` and `UsageFleetEnvelope v1` with strict bounds,
   source metadata, freshness, errors, and flexible `limits[]`.
4. Define source precedence independently for identity, quota, activity, and
   retained history.
5. Decide sampling cadence, retention, reset-series boundaries, and forecast
   confidence semantics.
6. Validate deduplication and stale-fallback behavior using fixtures before any
   DMS or TUI work.
7. Implement host-local collectors and the Switchboard JSON interface before
   choosing whether to adapt the existing DMS UI or replace it.

## Open questions

- Where should durable account UUIDs and aliases be configured, and how should
  a host declare that its login is the same account as another host?
- Should raw provider email/organization evidence ever appear in diagnostic
  JSON, or only a local hash and alias?
- What refresh cadence is appropriate for provider courtesy and useful burn
  forecasts?
- How long should normalized quota history be retained?
- What minimum sample interval and confidence threshold should gate depletion
  forecasts?
- Should Claude's private OAuth source be enabled automatically when the
  documented status-line source is absent, or require explicit opt-in?
- When OpenTelemetry is enabled, should Switchboard consume a local aggregate
  or merely link to an external telemetry backend?
- Should the first public contract expose activity at all, or establish quota
  and history first and add activity in a later version?

## Explicit non-goals for the first implementation

- Copying credentials or provider transcripts between hosts.
- Scraping provider dashboards as the primary source.
- Converting token activity into Business credits or money without a documented
  provider conversion.
- Treating rate-limit reset coupons as credit balance.
- Centralizing provider logins in a new daemon or web service.
- Making DMS responsible for SSH, provider calls, history storage, or account
  deduplication.
- Predicting quota exhaustion without displaying freshness and confidence.
