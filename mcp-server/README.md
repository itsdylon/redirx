# RedirX MCP gateway

RedirX is a remote Streamable HTTP MCP service for planning a site migration,
matching URL inventories, resolving exceptions, producing redirect artifacts, and
checking installed redirects. This TypeScript gateway delegates business and
account authority to the existing Flask API, worker and database. There is no
local RedirX binary or stdio server to install.

## Connection and release status

Connect an OAuth-capable Streamable HTTP client to:

```text
https://redirx-mcp-server.onrender.com/mcp
```

The client follows the HTTP 401 protected-resource challenge, discovers the
issuer, and opens browser consent. Sign in through the existing RedirX identity
flow and approve the requesting client. Raw RedirX API keys and browser-session
tokens are not production MCP credentials.

| Configuration | Value |
| --- | --- |
| Protected resource / token audience | `https://redirx-mcp-server.onrender.com/` (trailing slash) |
| Permanent OAuth issuer | `https://redirx-mcp-auth.onrender.com` |
| Required scope | `mcp:tools` |
| API | `https://redirx-api.onrender.com`, pivot resources under `/api/v2` |
| Companion | `https://app.redirx.dev/companion` |
| Product release pin | `5d0117bda961603987a6e5d212c38191ba61edc3` |

The issuer hostname is permanent: changing it later requires an issuer migration,
not merely a DNS alias. The broker validates the existing Supabase identity and
issues resource-bound tokens. The gateway verifies issuer, audience, signature,
subject, client and scope. See the [authorization service](../mcp-auth-server/README.md)
and [production OAuth evidence](../docs/oauth-broker-acceptance-2026-09-19.md).

This README describes the source contract of the current nine-tool release
candidate. API/worker deployment and the lean worker's [idle Linux observation](../docs/release-evidence/worker-idle-cgroup-5d0117b.json)
are recorded separately. The last live production `tools/list` acceptance
captured the earlier thirteen-tool surface (2026-09-21 audit); a fresh
production comparison against this nine-tool candidate is pending its
deployment, and complete 500-page free/501-page paid sandbox journeys remain
pending in the evidence available for this documentation packet. A health response or this tool
table does not prove those journeys passed. Earlier production OAuth acceptance
exercised the legacy gateway. Record each advertised client's version and result;
this document does not claim verified interoperability for particular clients.

## Nine-tool workflow

The [shared contract](../contracts/pivot-v1.json) defines business names and REST
mappings; [gateway schemas](src/tools/pivot.ts) define actual MCP arguments.
`MCP_PIVOT_ENABLED=true` registers these nine business tools and artifact
resources, replacing the legacy tools. The legacy telemetry `get_more_tools`
helper is also disabled in pivot mode.

Object IDs are UUIDs. Idempotency keys are 1–200 characters without controls.
Reuse a key for the same request; changed input under that key conflicts.
When PostHog instrumentation is configured, `tools/list` additionally requires a
`context` string describing the user's intent. Follow the discovered schema,
including that field. The table lists the business arguments.

| Tool | Required business arguments | Result / optional arguments |
| --- | --- | --- |
| `plan_migration` | `old_site`, `new_site`, `idempotency_key` | Durable plan; no crawl or model run. Optional `name`, `site_aliases: {old: [], new: []}`. |
| `import_inventory` | `migration_id`, `side`, `urls`, `idempotency_key` | Explicit HTTP(S) URL strings into an owned immutable inventory; no crawl. Free Jev bounds: 500 old / 2000 new URLs, 2 MiB URL text total. |
| `run_migration` | `migration_id`, `old_inventory_id`, `new_inventory_id`, `idempotency_key` | Free URL-only Jev run. Optional `quote_id`, explicitly verified `confirmed_pairs` (up to 100). Historical payment states remain readable. |
| `refine_matches` | `migration_id`, `run_id`, `expected_seed_revision`, `idempotency_key` | Resume a paused Jev pass or improve unresolved mappings with confirmed examples; up to three total passes. |
| `get_migration` | `migration_id` | Progress/next action. Optional `run_id`, `operation_id`. |
| `list_matches` | `migration_id`, `run_id` | Optional `filter`, opaque `cursor`, `limit` (default 100, maximum 500). |
| `resolve_matches` | `migration_id`, `run_id`, `idempotency_key`, `decisions` | 1–100 audited decisions; inspect per-row outcomes. |
| `export_redirects` | `migration_id`, `run_id`, `format`, `revision`, `idempotency_key` | Immutable artifact. Optional `allow_partial` defaults false. |
| `verify_redirects` | `migration_id`, `artifact_id`, `idempotency_key`, deployment inputs below | Included post-installation check and coverage. |

Monitoring management and Search Console connection are not part of the public
MCP tool surface; those flows remain available through the existing browser
product and their REST routes.

Important argument details:

- MCP `run_migration` takes **two inventory arguments**. The gateway converts
  these into REST `inventory_ids: {old, new}`; do not send that REST object as an MCP argument.
- Match filters: `all`, `needs_review`, `unmatched`, `approved`, `rejected`.
  Each decision has `mapping_id`, nonnegative `expected_revision`, and `action`:
  `accept_repair`, `set_target`, `approve`, `reject`, `defer`, or `intentional_removal`.
  Optional `target_url` and `rationale` remain subject to server evidence/scope rules.
- Verification requires either an existing `deployment_id`, **or**
  `deployment_confirmation: true`, `live_origin`, and explicit `origin_rewrites`.
  Do not combine the forms. `{}` preserves artifact destinations. Confirm only
  installation that actually happened.

The gateway returns the same canonical JSON envelope in MCP `structuredContent`
and JSON text content for older clients. Every tool advertises its envelope
`outputSchema`:
`contract_version`, durable IDs, `status`, `next_action`, optional progress/retry
timing, `data`, and `error`. Poll with the returned timing and retain the migration
ID for reconnection. Matching success does not imply deployment; partial coverage
does not imply a passed verification.

A backend `failed` outcome sets MCP `isError: true` while preserving its error,
IDs, and recovery action. `partial`, payment and consent handoffs retain their
business states rather than being flattened into an error. Always inspect the
status and per-row outcomes; an accepted tool call is not proof every decision
succeeded. Malformed envelopes are rejected rather than reported as success.

The v2 backend request has a 25-second deadline, including reading its response
body, and honors MCP cancellation. It never automatically retries. This deadline
starts after delegated identity resolution. A timeout or cancellation **does not
undo a mutation**: its outcome may be unknown. Read status with known durable
IDs, then replay only the identical input and original idempotency key if needed.
Never create a replacement key merely because the response was lost.

### First-use guidance available to clients

Pivot initialization includes workflow instructions and points to the static
`redirx://guides/first-migration` resource. Clients can discover it with
`resources/list` and read it without a backend request. Host applications decide
whether to display or include resources; this is not a guarantee that a client
will consume the guide. The nine business tools and owned artifact resources
are unchanged; legacy mode does not receive this pivot guide.

Import actual page URL lists, not a sitemap file URL. `old` contains source pages
needing migration decisions; `new` contains available destination pages. Plan
origins and explicit aliases before importing. Import `complete` means the supplied
list passed syntax/origin checks, not that the entire site was inventoried or
that pages are reachable. A partial import returns bounded `input_issues`: finite
reason counts, total/available diagnostic counts, completeness and truncation,
and at most ten sanitized URL hints. Hints omit queries/fragments; invalid or
long URLs have no hint. Correct the original list under a new key rather than
silently dropping exclusions. Old snapshots may lack complete diagnostics.

### Agent journey: import, review, export

1. `plan_migration` returns a durable `migration_id`. Retain it and each mutation's
   exact arguments/key so the conversation can resume after reconnecting.
2. `import_inventory` separately for `old` and `new`. Preserve raw URL strings,
   case and escaping. Use `data.inventory.id` only if its status is `complete`.
   Correct a partial inventory under a new key; do not silently treat it as complete.
3. `run_migration` with both complete inventory IDs. Supply `confirmed_pairs` only
   for explicitly verified examples. Retain returned `run_id` and `operation_id`.
4. `get_migration` with the selected run, respecting `retry_after_seconds` before
   polling again. If paused, resolve its reported cause before `refine_matches`;
   the required revision is `data.jev.seed_revision`, not a mapping revision.
5. `list_matches` with a consistent run/filter and follow `data.next_cursor` until
   null. Review proposals with the user. Model confidence is an estimate, not a
   calibrated accuracy or permission to approve. Missing matches remain review work.
6. `resolve_matches` in batches of at most 100 explicit decisions. For Jev's
   confirmed example use `set_target`, `target_url`, `mapping_id` and the row's
   `expected_revision`. Inspect every outcome. A conflict requires fresh reads
   and reassessment, not blindly incrementing a revision or resubmitting a guess.
7. `export_redirects` uses the latest `data.selection_revision` as `revision`.
   This is distinct from a row revision and `seed_revision`. Partial export needs
   explicit agreement to exclusions. Read the returned artifact through the
   registered `redirx://` resource; a download is not an installation.
8. After actual installation, `verify_redirects` uses the artifact and explicit
   deployment inputs described above. Report passed, failed and unchecked counts;
   an incomplete check is not proof the migration is healthy.


Checkout, operation status and deployment confirmation also have authorized REST
resources; they are not additional MCP tools. Follow the
[backend contract](../docs/architecture/mcp-primary-contract.md) and returned handoffs.
`import_inventory` is a real MCP tool (see the table above); do not confuse it
with these REST-only resources.

## Historical commercial policy — test mode

New Jev runs use the free limits above; the following content-engine pricing and
capacity describe retained historical/shared workflows, not expanded Jev admission.
The implemented commercial policy remains **`test_only`**. These approved amounts describe
the contract and sandbox checkout, not an announcement of live paid sales:

| Unique eligible old-site pages | Whole-migration price |
| --- | --- |
| 1–500 | Free; no card |
| 501–1,500 | $49 USD |
| 1,501–5,000 | $99 USD |
| 5,001–15,000 | $199 USD |
| Above 15,000 | Explicit custom quote |

Price depends on old-site pages; a larger new site does not silently increase it.
Separate technical limits apply: 15,000 old/20,000 new URLs per content run, plus
inventory/content byte bounds. Admission limits are not proof that every allowed
workload fits the deployed worker. See [capacity evidence](../docs/architecture/worker-resource-acceptance.md).

Discovery/preflight precedes payment; content work requires entitlement. Small
sites receive matching, export and one included post-launch check. Paid migrations
include 30 rerun days from first successful paid execution for the purchased
pair/band, and 30 monitoring days from explicit deployment confirmation. Included
monitoring must activate within **90 days of purchase**. Pause/resume never resets
clocks. Renewal needs explicit consent. Purchased artifact downloads outlive the
rerun window.

Test-only recurring policy: $29/site/month monitoring; $99/month Studio for five
migrations per billing period (each up to 15,000 old pages) and five concurrent
monitored sites. No automatic overages; Agency uses custom grants. Network retries
and eligible reruns do not consume another migration slot.

Payment-required responses contain a quote and recovery action. Complete hosted
test Checkout, then poll/retry the same operation. Verified provider events and
server-side grants authorize work; a browser success URL cannot grant access.
[Real Stripe sandbox evidence](../docs/acceptance/stripe-sandbox-2026-09-19.md)
records its local-database boundary. Production combined workflow acceptance is separate.

## Artifacts, installation and the companion

The schema accepts `apache`, `nginx`, `wordpress`, `vercel`, `cloudflare`, `shopify`,
`csv`, and `json`. Format availability is not proof of interoperability with every
product/version. Inspect returned validation, platform requirements, installation
instructions and limitations; record served-response acceptance for each claimed
platform. The Cloudflare recipe concerns Pages `_redirects`, not every Cloudflare
product. Native deployment connectors and a platform credential vault are outside scope.

Exports bind the requested persisted decision revision and exclude held/rejected
rows by default; partial export must be explicit. Download through the returned
resource, whose reads recheck caller ownership:

```text
redirx://migrations/{migration_id}/artifacts/{artifact_id}
```

Preserve artifact hash/revision through installation and verification. The user's
agent installs using its existing platform access; missing access produces a
handoff, not a claim that redirects are live. Monitoring fixes remain unresolved
until measured repair. Scheduled checks plus email/MCP retrieval do not mean an
offline agent receives automatic work.

The browser companion handles authorization, payment, status, account and exception
review. Durable links use `/migrations/:migrationId`; old `/review/:sessionId`
links remain legacy **run** links. History (`/projects`), purchased exports,
billing returns, account, settings and API-key management retain their authorization
rules. With pivot enabled, `/quick-match`, `/upload`, and `/dashboard` send signed-in
users to `/companion`; signed-out users go through login with `/companion` as return.

## Local development and deployment

From this directory, use the committed lockfile and Node 24.21.0 for release parity:

```sh
npm ci
npm run typecheck
npm test
npm run build
```

Copy `.env.example` for local configuration, then deliberately select pivot and
auth modes: its compatibility defaults do not enable the new product. Production
gateway configuration includes:

```dotenv
MCP_PIVOT_ENABLED=true
MCP_AUTH_MODE=oauth
MCP_OAUTH_PROVIDER=broker
MCP_PUBLIC_URL=https://redirx-mcp-server.onrender.com/
OAUTH_ISSUER_URL=https://redirx-mcp-auth.onrender.com
SUPABASE_AUTH_ISSUER=https://bzpkrjnaatvohsipmupk.supabase.co/auth/v1
REDIRX_BACKEND_URL=https://redirx-api.onrender.com
```

Supply `MCP_INTERNAL_SECRET` privately, byte-identical to the API. Bind `HOST`/`PORT`
for the platform. `POSTHOG_API_KEY`/`POSTHOG_HOST` select existing telemetry. API and
worker activation are separate process settings; the companion uses build-time
`VITE_MCP_PIVOT_ENABLED`. Run `npm start` after building. `npm run dev` is local
watch mode. `MCP_AUTH_MODE=dev` is isolated local/CI authentication only and must
not be exposed as the production MCP path.

Retain the reviewed additive schema and compatible API/worker during release or
rollback. Use the [release runbook](../docs/application-migration-release-2026-09-20.md)
and exact deployment evidence; `render.yaml` has known drift and is not a validated
Blueprint-apply instruction. Account settings and compute changes remain owner-owned.

## Compatibility and acceptance evidence

With pivot disabled, source still registers legacy `discover`, `deep_match`,
`preview`, and `export` adapters against `/api/v1`. These preserve the older
contract rather than define new-product pricing. Supported REST access and prior
purchase rights remain compatible. The direct-Supabase OAuth adapter also remains
a source compatibility default, but its observed generic audience failed the
resource gate. The released issuer is the broker above; do not substitute a generic
audience or disable verification.

- [Native pivot journeys](../docs/acceptance/native-pivot-journey.md) (historical,
  recorded at the eleven-tool pin): actual MCP/Flask/native SQL, with documented
  provider/network fixtures.
- [Production OAuth](../docs/oauth-broker-acceptance-2026-09-19.md): identity,
  resource binding, refresh/restart/replay and legacy backend discovery.
- [Public fixture readiness](../docs/acceptance/public-fixture-readiness-2026-09-20.md):
  bounded provider/storage estimates and a deployed sampler, not yet a completed migration.

`scripts/production-oauth-probe.mjs` is the **legacy-tool** OAuth probe. Its optional
`discover` invocation does not work with the pivot surface (any pin). Use authorization-only
mode when appropriate, and separately reviewed pivot journey acceptance for the new
business tools. Generated documentation does not replace an actual client/version result.
