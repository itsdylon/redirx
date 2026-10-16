import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

const telemetry = vi.hoisted(() => ({ capture: vi.fn() }));
vi.mock('posthog-node', () => ({ PostHog: class { capture(event: unknown) { telemetry.capture(event); } async shutdown() {} } }));

vi.mock('../src/config.js', () => ({
  config: {
    backendBaseUrl: 'https://backend.test', pivotEnabled: true,
    posthog: { apiKey: 'fixture-no-network' },
  },
}));

vi.mock('../src/auth/identity.js', () => ({
  resolveIdentity: vi.fn(async (identity: { subject: string }) => ({
    userId: identity.subject,
    apiKey: `delegation-${identity.subject}`,
    plan: 'free',
    gscConnected: false,
  })),
}));

const { buildMcpServer } = await import('../src/mcpServer.js');
const { config } = await import('../src/config.js');

const ids = {
  migration: '11111111-1111-4111-8111-111111111111',
  run: '22222222-2222-4222-8222-222222222222',
  mapping: '33333333-3333-4333-8333-333333333333',
};

function envelope(data: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  return {
    contract_version: '1.0.0', migration_id: ids.migration, operation_id: null,
    status: 'succeeded', next_action: 'none', data, error: null, ...overrides,
  };
}

async function connected(subject = 'account-a') {
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  const send = clientTransport.send.bind(clientTransport);
  (clientTransport as unknown as { send: typeof send }).send = (message, options) => send(message, {
    ...options,
    authInfo: { token: `provider-${subject}`, clientId: 'test-client', scopes: [], extra: { subject } },
  });
  const server = buildMcpServer();
  const client = new Client({ name: 'pivot-test', version: '1.0.0' }, { capabilities: {} });
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  return { client, close: async () => { await client.close(); await server.close(); } };
}

describe('opt-in pivot MCP tools', () => {
  const fetchMock = vi.fn();
  beforeEach(() => { vi.stubGlobal('fetch', fetchMock); fetchMock.mockReset(); telemetry.capture.mockReset(); });
  afterEach(() => vi.unstubAllGlobals());

  it('exposes exactly the nine pinned Jev tools through the native MCP SDK', async () => {
    const { client, close } = await connected();
    try {
      const result = await client.listTools();
      expect(result.tools.map(tool => tool.name).sort()).toEqual([
        'export_redirects', 'get_migration', 'import_inventory', 'list_matches', 'plan_migration',
        'refine_matches', 'resolve_matches', 'run_migration', 'verify_redirects',
      ]);
      const run = result.tools.find(tool => tool.name === 'run_migration')!;
      expect(run.inputSchema.properties).not.toHaveProperty('grant_id');
      expect(run.inputSchema.properties).toHaveProperty('subscription_id');
      expect(run.inputSchema.properties).toHaveProperty('checkout_id');
      expect(run.inputSchema.properties).toHaveProperty('max_extra_amount_cents');
      expect(run.inputSchema.properties).not.toHaveProperty('rerun_of');
      expect(run.inputSchema.properties).toHaveProperty('quote_id');
    } finally { await close(); }
  });

  it('imports explicit URL strings through the owned durable API without a crawler or URL fetch', async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify(envelope({ inventory: { id: ids.run, status: 'complete' } })), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const args = { migration_id: ids.migration, side: 'old', urls: ['https://old.test/a?x=1', 'https://old.test/b'], idempotency_key: 'import-stable' };
      for (let i = 0; i < 2; i++) {
        const result = await client.callTool({ name: 'import_inventory', arguments: args });
        expect(JSON.parse((result.content[0] as { text: string }).text).data.inventory).toEqual({ id: ids.run, status: 'complete' });
      }
      expect(fetchMock.mock.calls.map(call => call[0])).toEqual(Array(2).fill(`https://backend.test/api/v2/migrations/${ids.migration}/inventories`));
      for (const [, init] of fetchMock.mock.calls) {
        expect(JSON.parse(init.body)).toEqual({ side: 'old', rows: args.urls, idempotency_key: 'import-stable' });
        expect(init.headers.Authorization).toBe('Bearer delegation-account-a');
      }
    } finally { await close(); }
  });

  it('rejects malformed imports and oversized text before backend or provider work', async () => {
    const { client, close } = await connected();
    try {
      const base = { migration_id: ids.migration, side: 'old', urls: ['https://old.test/a'], idempotency_key: 'import' };
      for (const change of [{ side: 'other' }, { urls: [] }, { urls: ['file:///private/file'] },
        { urls: Array(20001).fill('https://old.test/a') }, { urls: Array(6000).fill('https://old.test/' + 'x'.repeat(3000)) }]) {
        expect((await client.callTool({ name: 'import_inventory', arguments: { ...base, ...change } })).isError).toBe(true);
      }
      expect(fetchMock).not.toHaveBeenCalled();
    } finally { await close(); }
  });

  it('preserves confirmed pairs and revision-bound refinement without approving model results', async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify(envelope({ run_id: ids.run }, { status: 'queued', next_action: 'poll' })), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const confirmed = [{ old_url: 'https://old.test/a', new_url: 'https://new.test/a' }];
      await client.callTool({ name: 'run_migration', arguments: { migration_id: ids.migration, old_inventory_id: ids.run, new_inventory_id: ids.mapping, confirmed_pairs: confirmed, idempotency_key: 'run' } });
      expect(JSON.parse(fetchMock.mock.calls[0][1].body).confirmed_pairs).toEqual(confirmed);
      const args = { migration_id: ids.migration, run_id: ids.run, expected_seed_revision: 4, idempotency_key: 'refine-stable' };
      for (let i = 0; i < 2; i++) await client.callTool({ name: 'refine_matches', arguments: args });
      for (const [url, init] of fetchMock.mock.calls.slice(1)) {
        expect(url).toBe(`https://backend.test/api/v2/migrations/${ids.migration}/runs/${ids.run}/refine`);
        expect(JSON.parse(init.body)).toEqual({ expected_seed_revision: 4, idempotency_key: 'refine-stable' });
      }
      const count = fetchMock.mock.calls.length;
      expect((await client.callTool({ name: 'refine_matches', arguments: { ...args, expected_seed_revision: -1 } })).isError).toBe(true);
      expect(fetchMock).toHaveBeenCalledTimes(count);
    } finally { await close(); }
  });

  it('reads artifact resources through the current account delegation without following download URLs', async () => {
    const artifact = ids.mapping;
    const uri = `redirx://migrations/${ids.migration}/artifacts/${artifact}`;
    fetchMock.mockResolvedValue(new Response(JSON.stringify(envelope({
      resource_type: 'artifact_download', artifact_id: artifact, migration_id: ids.migration,
      format: 'nginx', content: 'location = /old { return 301 https://new.test/new; }',
      download_url: 'http://169.254.169.254/never-follow',
    })), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const templates = await client.listResourceTemplates();
      expect(templates.resourceTemplates[0].uriTemplate).toBe('redirx://migrations/{migration_id}/artifacts/{artifact_id}');
      const result = await client.readResource({ uri });
      expect(result.contents).toEqual([{ uri, mimeType: 'text/plain', text: 'location = /old { return 301 https://new.test/new; }' }]);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toBe(`https://backend.test/api/v2/migrations/${ids.migration}/artifacts/${artifact}`);
      expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer delegation-account-a');
    } finally { await close(); }
  });

  it('rejects malformed, foreign and mismatched artifact resources', async () => {
    const { client, close } = await connected();
    const uri = `redirx://migrations/${ids.migration}/artifacts/${ids.mapping}`;
    try {
      await expect(client.readResource({ uri: 'redirx://migrations/not-a-uuid/artifacts/../../private' })).rejects.toThrow();
      expect(fetchMock).not.toHaveBeenCalled();
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(envelope({}, {status: 'failed', error: {code:'not_found'}})),
        {status:404,headers:{'content-type':'application/json'}}));
      await expect(client.readResource({ uri })).rejects.toThrow('Artifact unavailable');
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(envelope({resource_type:'artifact_download',
        artifact_id:ids.run,migration_id:ids.migration,content:'wrong artifact'})), {headers:{'content-type':'application/json'}}));
      await expect(client.readResource({ uri })).rejects.toThrow('Invalid artifact response');
    } finally { await close(); }
  });

  it('keeps the existing four tools when MCP_PIVOT_ENABLED is false', async () => {
    config.pivotEnabled = false;
    const { client, close } = await connected();
    try {
      expect((await client.listTools()).tools.map(tool => tool.name).sort()).toEqual(['deep_match', 'discover', 'export', 'get_more_tools', 'preview']);
    } finally {
      config.pivotEnabled = true;
      await close();
    }
  });

  it('uses separate delegated identities for two accounts and never sends provider tokens', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(envelope({})), { headers: { 'content-type': 'application/json' } }));
    const a = await connected('account-a');
    const b = await connected('account-b');
    try {
      await a.client.callTool({ name: 'get_migration', arguments: { migration_id: ids.migration } });
      await b.client.callTool({ name: 'get_migration', arguments: { migration_id: ids.migration } });
      expect((fetchMock.mock.calls[0][1].headers as Record<string, string>).Authorization).toBe('Bearer delegation-account-a');
      expect((fetchMock.mock.calls[1][1].headers as Record<string, string>).Authorization).toBe('Bearer delegation-account-b');
      expect(JSON.stringify(fetchMock.mock.calls)).not.toContain('provider-account-a');
    } finally { await a.close(); await b.close(); }
  });

  it('rejects malformed bounded decision input before contacting the backend', async () => {
    const { client, close } = await connected();
    try {
      const result = await client.callTool({ name: 'resolve_matches', arguments: {
        migration_id: ids.migration, run_id: ids.run, idempotency_key: 'key',
        decisions: Array.from({ length: 101 }, () => ({ mapping_id: ids.mapping, expected_revision: 0, action: 'approve' })),
      } });
      expect(result.isError).toBe(true);
      expect(fetchMock).not.toHaveBeenCalled();
    } finally { await close(); }
  });

  it('preserves side-qualified aliases for the authoritative planning validator', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(envelope({})), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      await client.callTool({ name: 'plan_migration', arguments: {
        old_site: 'https://old.test', new_site: 'https://new.test', idempotency_key: 'plan-1',
        site_aliases: { old: ['https://www.old.test'], new: ['https://preview.new.test'] },
      } });
      expect(JSON.parse(fetchMock.mock.calls[0][1].body).site_aliases).toEqual({
        old: ['https://www.old.test'], new: ['https://preview.new.test'],
      });
    } finally { await close(); }
  });

  it('forwards opaque pagination and returns backend async envelopes as stable JSON', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(envelope({ items: [], next_cursor: 'opaque-next' })), { headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(envelope({ run_id: ids.run }, { operation_id: '44444444-4444-4444-8444-444444444444', status: 'queued', next_action: 'poll', retry_after_seconds: 10 })), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const listed = await client.callTool({ name: 'list_matches', arguments: { migration_id: ids.migration, run_id: ids.run, cursor: 'opaque-old', limit: 2 } });
      expect(fetchMock.mock.calls[0][0]).toContain('cursor=opaque-old');
      expect(JSON.parse((listed.content[0] as { text: string }).text)).toMatchObject({ data: { next_cursor: 'opaque-next' } });
      const run = await client.callTool({ name: 'run_migration', arguments: { migration_id: ids.migration, old_inventory_id: ids.run, new_inventory_id: ids.mapping, idempotency_key: 'run-1' } });
      expect(JSON.parse((run.content[0] as { text: string }).text)).toMatchObject({ status: 'queued', next_action: 'poll', retry_after_seconds: 10 });
    } finally { await close(); }
  });

  it('forwards current paid consent while dropping legacy grant and rerun fields', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(envelope({ run_id: ids.run }, { status: 'queued', next_action: 'poll' })), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      await client.callTool({ name: 'run_migration', arguments: { migration_id: ids.migration,
        old_inventory_id: ids.run, new_inventory_id: ids.mapping, quote_id: ids.mapping,
        // @ts-expect-error legacy fields are no longer part of the advertised schema
        grant_id: ids.run, subscription_id: ids.mapping, checkout_id: ids.run, approve_extra_urls: true, max_extra_amount_cents: 1200, rerun_of: ids.run, idempotency_key: 'jev-run' } });
      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body).toMatchObject({ quote_id: ids.mapping, inventory_ids: { old: ids.run, new: ids.mapping }, idempotency_key: 'jev-run' });
      expect(body).not.toHaveProperty('grant_id');
      expect(body).toMatchObject({ subscription_id: ids.mapping, checkout_id: ids.run, approve_extra_urls: true, max_extra_amount_cents: 1200 });
      expect(body).not.toHaveProperty('rerun_of');
    } finally { await close(); }
  });

  it('preserves recoverable payment and consent envelopes even when the backend uses 4xx', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(envelope({
      quote_id: '55555555-5555-4555-8555-555555555555', checkout_url: 'https://backend.test/authorized/checkout',
    }, { status: 'payment_required', next_action: 'complete_payment', error: { code: 'payment_required', message: 'Complete payment in a browser.', retryable: false, next_action: 'complete_payment' } })), { status: 402, headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const result = await client.callTool({ name: 'run_migration', arguments: { migration_id: ids.migration, old_inventory_id: ids.run, new_inventory_id: ids.mapping, idempotency_key: 'pay-1' } });
      const parsed = JSON.parse((result.content[0] as { text: string }).text);
      expect(parsed).toMatchObject({ status: 'payment_required', next_action: 'complete_payment', data: { checkout_url: 'https://backend.test/authorized/checkout' } });
      expect(result.isError).not.toBe(true);
    } finally { await close(); }
  });

  it('forwards both verification modes without implicit origin rewrites', async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify(envelope({})), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const common = { migration_id: ids.migration, artifact_id: ids.run, idempotency_key: 'verify' };
      const cases = [
        { deployment_id: ids.mapping },
        { deployment_confirmation: true, live_origin: 'https://live.test', origin_rewrites: {} },
        { deployment_confirmation: true, live_origin: 'https://live.test', origin_rewrites: { 'https://preview.test': 'https://live.test' } },
      ];
      for (const fields of cases) {
        const result = await client.callTool({ name: 'verify_redirects', arguments: { ...common, ...fields } });
        expect(result.isError).not.toBe(true);
      }
      expect(fetchMock.mock.calls.map(call => call[0])).toEqual(cases.map(() => `https://backend.test/api/v2/migrations/${ids.migration}/verifications`));
      expect(fetchMock.mock.calls.map(call => JSON.parse(call[1].body))).toEqual(cases.map(fields => ({ artifact_id: ids.run, idempotency_key: 'verify', ...fields })));
    } finally { await close(); }
  });

  it('rejects incomplete, ambiguous and legacy verification inputs without HTTP calls', async () => {
    const { client, close } = await connected();
    try {
      const common = { migration_id: ids.migration, artifact_id: ids.run, idempotency_key: 'verify' };
      const explicit = { deployment_confirmation: true, live_origin: 'https://live.test', origin_rewrites: {} };
      const cases = [
        {}, { deployment_confirmation: true }, { deployment_confirmation: true, live_origin: 'https://live.test' },
        { deployment_id: ids.mapping, origin_rewrites: {} }, { deployment_id: ids.mapping, ...explicit },
        { ...explicit, deployment_confirmation: false }, { ...explicit, live_origin: 'https://live.test/path' },
        { ...explicit, live_origin: 'https://user:pass@live.test' },
        { ...explicit, origin_rewrites: { 'https://preview.test/path': 'https://live.test' } },
        { ...explicit, origin_rewrites: { 'https://preview.test': 'file:///private' } },
        { deployment_id: ids.mapping, live_origins: ['https://live.test'] },
      ];
      for (const fields of cases) {
        expect((await client.callTool({ name: 'verify_redirects', arguments: { ...common, ...fields } })).isError, JSON.stringify(fields)).toBe(true);
      }
      const invalid = await client.callTool({ name: 'verify_redirects', arguments: common });
      expect(JSON.parse((invalid.content[0] as { text: string }).text)).toMatchObject({
        contract_version: '1.0.0', status: 'failed', error: { code: 'invalid_input', retryable: false },
      });
      expect(fetchMock).not.toHaveBeenCalled();
    } finally { await close(); }
  });

  it('preserves telemetry intent on strict outcome schemas without forwarding context to the backend', async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify(envelope({})), { headers: { 'content-type': 'application/json' } }));
    const { client, close } = await connected();
    try {
      const context = 'Checking the installed migration artifact against expected redirects before evaluating optional monitoring coverage for the completed website migration.';
      const tools = (await client.listTools()).tools;
      expect(tools).toHaveLength(9);
      expect(tools.find(tool => tool.name === 'verify_redirects')!.inputSchema.properties).toHaveProperty('context');
      const result = await client.callTool({ name: 'verify_redirects', arguments: {
        migration_id: ids.migration, artifact_id: ids.run, deployment_id: ids.mapping, idempotency_key: 'telemetry', context,
      } });
      expect(result.isError).not.toBe(true);
      expect(JSON.parse(fetchMock.mock.calls[0][1].body)).not.toHaveProperty('context');
      await vi.waitFor(() => expect(telemetry.capture).toHaveBeenCalledWith(expect.objectContaining({
        event: '$mcp_tool_call', distinctId: 'account-a', properties: expect.objectContaining({ $mcp_intent: context, $mcp_tool_name: 'verify_redirects' }),
      })));
    } finally { await close(); }
  });

});
