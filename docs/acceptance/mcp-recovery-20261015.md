# MCP recovery and review transport checks

The gateway now delivers the canonical operation envelope as both structured MCP
output and the existing JSON text. All nine tools advertise the same output
schema. Backend failed outcomes set `isError`; payment, consent and partial
outcomes retain their business status and recovery data. Malformed envelopes
cannot pass as success. The backend continues to own approval, revisions,
ownership, admission, artifacts and every mutation.

The v2 request has a 25-second deadline covering headers and response body, and
accepts MCP cancellation. There are no automatic retries. A lost mutation
response explicitly warns that it may already have completed and instructs
status readback and identical-input, same-key replay. Aborting a gateway wait is
not rollback. Historical non-envelope middleware errors retain their safe code,
message and recovery fields; arbitrary response/debug text is excluded.

Tool descriptions and the gateway README now explain stable pagination,
explicit Jev `set_target` confirmation, per-row outcomes, conflict recovery,
selection versus seed versus row revisions, and export versus installation.
The README separates current free Jev admission from historical content pricing.

## Verification

Executed locally in the isolated MCP surface checkout on October 15, 2026:

- `npm test --prefix mcp-server`: **101 tests passed, 0 skipped**, seven suites.
- `npm run typecheck --prefix mcp-server`: passed.
- `npm run build --prefix mcp-server`: passed.
- `node scripts/check_pivot_contract.mjs`: nine pinned tools and shared policy valid.
- `git diff --check`: passed.

The initial sandboxed full suite was blocked by the existing loopback OAuth
fixture (`listen EPERM`). The complete passing run used permission for that
local fixture server. Node was 25.8.1, using the existing workspace's installed
MCP dependencies through an untracked symlink; this is not a fresh install or
release-Node acceptance claim.

New tests execute the native MCP SDK with deterministic backend responses:
structured/text equivalence, output schema discovery, failed/partial outcomes,
lost-mutation recovery, and a paginated read → explicit decision → revision-bound
export → owned artifact read journey. The journey preserves URL case, escaping,
queries and opaque cursors. Client tests verify timeout, response-body deadline,
cancellation, no dispatch on pre-cancelled requests, no hidden retries, malformed
success rejection, and historical error compatibility. Existing OAuth ownership,
resource and legacy tool suites also pass.

## Limits and next work

No live deployment, real user migration, browser-client interoperability or
provider/model quality was exercised. Fixtures validate transport behavior,
not backend SQL approval authority. The timeout starts after identity resolution;
the shared delegation resolver still needs its own bounded network handling.
PostHog instrumentation and real client resource rendering should be included in
release acceptance. Persistent agent-independent saved migration discovery is
still limited: callers must retain the migration ID or use companion history.
