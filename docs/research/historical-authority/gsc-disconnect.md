# Historical disconnect commits before best-effort Google revocation

The retained browser disconnect read a refresh token, sent it in Google URL
parameters, waited for the response, then deleted credentials by user ID. Native
fixtures reproduced deletion of a newly reconnected account during that wait.
The browser also left `gsc_agent_accounts` in its previous state/epoch, allowing
pending agent consent to remain valid after a browser disconnect.

Staged migration **066** adds one service-role-only RPC,
`disconnect_legacy_gsc_connection(uuid)`. It uses the same account-first lock order
as 040, removes the user's current credential, advances the account epoch and sets
its state to disconnected in one transaction. Pending and in-flight older agent
consent can no longer finish against that epoch. A disconnect with no stored
credentials still invalidates pending agent consent. Other users are untouched.
Suppressed deletes or account updates raise and roll back instead of producing a
false acknowledgment.

The server validates the owner-bound receipt, exact disconnected flag, integer
epoch/deletion count and expected token presence. Only after confirmed local
success does it attempt Google revocation, with the removed token in a **form
body**, never URL parameters. The token-bearing RPC receipt stays server-side;
the browser retains its existing `{success:true}` response. There is no later
local delete that could erase a reconnect while revocation was pending.

Unknown/malformed database acknowledgment returns
`gsc_disconnect_unconfirmed` 503 with connection-status readback guidance. The
server neither retries the mutation nor revokes a token from an unconfirmed
receipt. A Google timeout/failure remains best effort after the local transaction;
it does not undo disconnection or trigger credential logging.

## Native evidence

All three initial regression tests failed against the preceding implementation:
the later reconnect disappeared, pending agent state remained connecting, and the
token appeared in the request's `params` dictionary. They pass with 066 and the
new service ordering.

The completed package passes **55 tests, zero skips**, on disposable PostgreSQL 18.4
and Python 3.12.15: 12 new disconnect tests plus the 43 historical-refresh and
historical/agent GSC regression tests. Dotenv is disabled and the old JWT-state
roundtrip test receives a fixed fixture-only signing key. Google is entirely
stubbed; database authority uses the actual 024/040/047/066 SQL under service_role.

The new tests positively cover:

- Later historical and real 040 agent reconnects during delayed revocation; new
  credentials and the agent's new connected epoch survive the return.
- Pending and already-consumed agent consent invalidation, including an account
  with no stored credential and an explicit repeated disconnect.
- An unchanged second owner's complete credential row.
- Suppressed credential deletion and suppressed account acknowledgment, with
  both halves of the transaction restored and no provider request.
- A real committed disconnect followed by lost transport acknowledgment: 503,
  one RPC attempt, no revoke, and readback confirms disconnected.
- Wrong-owner/malformed receipts rejected without revocation, service-role-only
  grants, best-effort provider timeout, form-body token placement and a positive
  browser response containing no token or internal receipt fields.

Run through the disposable-loopback backend harness:

```text
backend.tests.test_legacy_gsc_disconnect
backend.tests.test_legacy_gsc_refresh
backend.tests.test_gsc_service
backend.tests.test_migration_gsc
```

The latter historical state-token suite needs an explicit fixture
`GSC_STATE_SECRET`; no real secret or environment file is needed. Root separately
validates integrated work on PostgreSQL 17 and coordinates required-CI enrollment.

## Serialization and rollout limits

Apply 066 before deploying this disconnect service. It depends on 024 and 040, not
deferred billing 059–061. No applied migration is edited. Missing 066 or insufficient
RPC rights fail closed with 503; there is no fallback to the old independent
deletion. The existing read-only 065–067 catalog checker does not cover 066; its
scope is deliberately named and is not whole-product readiness. A separately named
[066 catalog check](../schema-readiness/gsc-disconnect-066.md) is now available for
read-only definition/grant inspection; it does not replace these behavioral tests.

Explicit local disconnect intent takes effect at its database transaction. A
later reconnect is allowed. **Provider revocation is not atomic with local state:**
revoking the old Google grant may still invalidate newly issued tokens for that
same Google account. Preserving a local row is not proof that Google accepts its
tokens; the service does not promise that guarantee. Lost acknowledgment can also
mean local disconnection committed without a subsequent provider revoke.

The historical JWT-state OAuth callback does not participate in the agent epoch
protocol. A legacy consent callback can still finish later and reconnect; 066 does
not silently claim to fence it. Addressing that requires a separately reviewed
callback/state contract. Existing agent consent is epoch-fenced by this package.
Token revocation, callback ordering and connection-status observation remain
distinct outcomes rather than one fabricated atomic operation.

This package depends on the preceding refresh fix 46b2bbc. All work is staged in
an isolated clone; there was no hosted schema application, production/customer
operation, live Google call, account-setting change or deployment.
