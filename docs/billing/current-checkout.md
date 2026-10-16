# October 2026 checkout and paid URL allowances

The current catalog is `redirx_2026_10`. The owner requested both checkout wiring
and paid-run capacity in this PR. Historical quote/grant and test-subscription
contracts remain available for existing records; they are not reinterpreted as
the new plans. New purchases through old checkout endpoints are rejected when
current billing is enabled.

| Purchase | Price before tax | Allowance |
| --- | --- | --- |
| Separate free trial | $0 | One migration, up to 500 old URLs |
| Pay as you go | `max(10, ceil(old_urls / 50))` dollars | Exact purchased old/new inventory pair |
| Studio | $79/month | 5,000 old URLs pooled across migrations |
| Agency | $199/month | 15,000 old URLs pooled across migrations |
| Approved monthly extras | `ceil(total_extra_urls / 50)` dollars | Beyond the monthly allowance |

New and destination URLs are not billed. Free trial usage does not subtract from
a purchase or monthly allowance. The application sends Stripe the original URL
quantity (at least 500 for PAYG); Stripe's $1/50 package transformation rounds
once. It never sends a pre-rounded package count as a URL quantity. For example,
501 URLs costs $11; two additions of 25 and 26 extra URLs cost $2 in total.

`docs/billing/stripe-catalog-2026-10.json` records the verified live catalog IDs.
Test mode requires four separately configured sandbox price IDs; a test key
cannot use a live price. Each checkout retrieves the account and configured price
and verifies environment, amount, currency, period and quantity transformation.

## Purchase and admission

The authenticated companion uses `/api/v2/billing/current` for its catalog,
`/usage` for account usage, `/checkouts` to reserve a purchase and create a hosted
Stripe session, and `/checkouts/:id` for server-confirmed state. Return URLs are
fixed to the configured companion origin. The browser cannot supply a customer,
owner, price, amount or URL quantity. Recurring plans require explicit consent.

The server counts owned, complete, immutable inventories in PostgreSQL. Purchase
reservations survive request retries. A signed webhook plus independently
retrieved Checkout/Subscription/Invoice/PaymentIntent/Charge objects establishes
payment. Success-page query parameters grant nothing. Out-of-order callbacks can
bind an already-reserved purchase before its create request returns.

`run_migration` accepts `checkout_id` for PAYG or `subscription_id` (an internal
checkout UUID, not a Stripe ID) for a monthly plan. Retrying the original request
after payment automatically finds an unused purchase for the exact inventory pair;
after the separate trial is used, an active monthly allowance is selected when
no purchase is specified. The database atomically
reserves URLs and the run together. Monthly allowances are shared across all
migrations owned by that account. Extra usage requires `approve_extra_urls=true`
and `max_extra_amount_cents`, a ceiling on the **total monthly extra charge after
this run**. Concurrent admissions recheck that ceiling under the account lock.
Repeat the same run idempotency key after transient errors; use `refine_matches`
for the existing run rather than purchasing the same URLs again.

Paid runs admit at most 15,000 old URLs, 20,000 new URLs and 16 MiB of raw URL text.
The separate trial retains 500/2,000/2 MiB limits. Paid work includes up to three
passes during its 30-day work window. Manual review and artifact access do not
consume passes. Cancellation or a failed next renewal blocks new subscription
admissions, while already-paid work keeps its window. Payment reversals revoke
the affected purchase/period. Historical exports remain readable.

This does not raise the global $1/day provider budget, remove lease fencing,
change matching quality, or increase worker concurrency. Large jobs can pause on
the existing provider budget and resume with saved progress. The larger admission
ceiling is covered with fixture execution; it is not a live throughput benchmark.

## Monthly settlement

On `invoice.created` for a subscription cycle, the webhook freezes the ended
period's approved usage and attaches one invoice item to that exact draft invoice.
Zero extras create no item. The frozen period and a stable Stripe idempotency key
prevent duplicate charges. Existing invoice items are reconciled before any new
creation, including when the invoice has already finalized. A missing item after
the safe retry window fails for reconciliation instead of risking a duplicate.

Renewal verification checks the fixed monthly line and any recorded extra-usage
line, including total payment and absence of refunds/disputes. Delivery of a
specific older paid invoice may reconstruct its historical period without
resetting an existing allowance. No worker emits per-run Stripe meter events.

Cancellation freezes the final approved extra usage under the same account lock.
A period is assigned to either the renewal invoice or a separate final invoice,
so concurrent deliveries cannot bill it twice. The final invoice contains only
that period's approved extras and grants no new monthly allowance. Lost provider
responses are recovered through saved IDs and exact metadata before retrying a
write. An ambiguous or expired retry window requires reconciliation instead of
creating another charge. If cancellation races a renewal invoice that already
owns the usage but is not verifiably paid, delivery fails for reconciliation;
it never creates a second invoice or silently treats paused collection as paid.

## Deployment is owner-applied

This PR creates no charges, subscriptions, webhook destinations, account settings,
or production schema changes. Billing defaults off. Before activating:

1. Apply additive 063 and 064 to the intended database after the existing schema
   through 062. Do not apply historical deferred migrations 059–061.
2. Configure the `REDIRX_STRIPE_*` environment in `.env.example` and the trusted
   `MCP_CHECKOUT_COMPANION_ORIGIN`. Start with test keys and sandbox products.
   For hosted account management, configure `REDIRX_STRIPE_PORTAL_CONFIGURATION_ID`
   with subscription changes disabled and cancellation at period end. The adapter
   verifies this configuration; it never changes account settings.
3. Configure a signed webhook destination for `/api/v2/billing/current/webhook`
   with `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `invoice.created`, `invoice.paid`, `invoice.payment_failed`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `charge.refunded`, and `charge.dispute.created`. The adapter pins
   `2024-12-18.acacia` for retrieved objects. Payloads are re-retrieved.
4. Exercise real sandbox payment, monthly renewal with extras, cancellation,
   refund, duplicate delivery and an interrupted provider response before live
   activation. Automated fixtures do not prove deployed Stripe/PostgREST setup.
5. Enable the current billing flag together with the existing MCP/Jev runtime
   switches only after sandbox acceptance. Disabling the billing flag pauses
   marked paid runs; it must not send them into a historical engine.

Prices are USD and exclusive of tax. The current adapter requests no automatic
tax or discounts and verifies exact agreed totals. Tax collection changes require
a separate owner-reviewed configuration and matching verification tests.

References: [Stripe Checkout sessions](https://docs.stripe.com/api/checkout/sessions/create),
[package pricing](https://docs.stripe.com/products-prices/manage-prices),
[subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks),
[invoice items](https://docs.stripe.com/api/invoiceitems/create),
and [subscription cancellation](https://docs.stripe.com/billing/subscriptions/cancel).
