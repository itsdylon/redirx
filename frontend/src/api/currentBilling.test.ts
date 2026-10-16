import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { billingRedirectUrl, createCurrentCheckout, createCurrentPortal, getCurrentCheckout, getCurrentPricing, getCurrentQuote, getCurrentUsage } from './currentBilling';
const fetchMock = vi.fn();
const checkout = { checkout_id: 'checkout-1', status: 'open', sku: 'payg', amount_cents: 1100, currency: 'usd', checkout_url: 'https://checkout.stripe.com/c/pay/fixture' };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock); localStorage.setItem('access_token', 'fixture-token'); });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });
describe('current billing transport', () => {
  it('sends exact inventory identity, consent, auth and stable key without legacy quotes', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(response(checkout)));
    const body = { sku: 'payg' as const, migration_id: 'm', inventory_ids: { old: 'o', new: 'n' }, recurring_consent: false, idempotency_key: 'stable' };
    await createCurrentCheckout(body); await createCurrentCheckout(body);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetchMock.mock.calls) {
      expect(url).toContain('/api/v2/billing/current/checkouts');
      expect(init).toMatchObject({ method: 'POST', headers: { Authorization: 'Bearer fixture-token' }, cache: 'no-store' });
      expect(JSON.parse(init.body)).toEqual(body);
    }
  });
  it('refuses recurring checkout without consent and PAYG without inventories', async () => {
    await expect(createCurrentCheckout({ sku: 'studio', recurring_consent: false, idempotency_key: 'key' })).rejects.toThrow('Approve monthly');
    await expect(createCurrentCheckout({ sku: 'payg', recurring_consent: false, idempotency_key: 'key', migration_id: 'm' })).rejects.toThrow('both URL inventories');
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it('verifies checkout with an authenticated GET and no success claim', async () => {
    fetchMock.mockResolvedValue(response({ ...checkout, checkout_id: 'c/1', status: 'paid' }));
    const signal = new AbortController().signal;
    expect((await getCurrentCheckout('c/1', signal)).status).toBe('paid');
    expect(fetchMock.mock.calls[0][0]).toContain('/checkouts/c%2F1');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal, cache: 'no-store' });
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
  });
  it.each([401, 403, 404, 500])('rejects HTTP %s even with paid JSON', async status => {
    fetchMock.mockResolvedValue(response({ ...checkout, status: 'paid' }, status));
    await expect(getCurrentCheckout('checkout-1')).rejects.toThrow();
  });
  it.each([{ ...checkout, status: 'success' }, { ...checkout, checkout_id: 'other' }, {}])('rejects unverifiable checkout data', async data => {
    fetchMock.mockResolvedValue(response(data));
    await expect(getCurrentCheckout('checkout-1')).rejects.toThrow('incomplete');
  });
  it('rejects malformed catalog and JSON rather than enabling checkout', async () => {
    fetchMock.mockResolvedValueOnce(response({ enabled: 'true', plans: [] })).mockResolvedValueOnce(new Response('bad'));
    await expect(getCurrentPricing()).rejects.toThrow();
    await expect(getCurrentPricing()).rejects.toThrow();
  });
  it('reads exact quote identities and usage; opens portal via POST', async () => {
    fetchMock.mockResolvedValueOnce(response({ old_urls: 501, amount_cents: 1100, stripe_quantity: 11 }))
      .mockResolvedValueOnce(response({ sku: 'free', included_old_urls: 0, used_old_urls: 0, extra_old_urls: 0, extra_amount_cents: 0, portal_available: false }))
      .mockResolvedValueOnce(response({ url: 'https://billing.stripe.com/p/session/fixture' }));
    await getCurrentQuote('m/1', 'old/1', 'new/2'); await getCurrentUsage();
    expect(await createCurrentPortal()).toBe('https://billing.stripe.com/p/session/fixture');
    const url = new URL(fetchMock.mock.calls[0][0], 'https://fixture.test');
    expect(url.pathname).toContain('/migrations/m%2F1/quote');
    expect(Object.fromEntries(url.searchParams)).toEqual({ old_inventory_id: 'old/1', new_inventory_id: 'new/2' });
    expect(fetchMock.mock.calls[2][1].method).toBe('POST');
  });
  it.each(['javascript:alert(1)', 'https://evil.test', 'https://checkout.stripe.com.evil.test', 'http://checkout.stripe.com', 'https://user@checkout.stripe.com'])('refuses unsafe checkout redirect %s', url => {
    expect(() => billingRedirectUrl(url)).toThrow();
  });
});
