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

  it('makes first-use guidance discoverable through native initialize and resources without fetching', async () => {
    const { client, close } = await connected();
    try {
      expect(client.getInstructions()).toContain('redirx://guides/first-migration');
      expect(client.getInstructions()).toContain('explicit user approval');
      expect(client.getServerCapabilities()).toHaveProperty('resources');
      const tools = (await client.listTools()).tools;
      const plan = tools.find(tool => tool.name === 'plan_migration')!;
      expect((plan.inputSchema.properties!.old_site as { description: string }).description).toContain('source pages');
      const intake = tools.find(tool => tool.name === 'import_inventory')!;
      expect((intake.inputSchema.properties!.urls as { description: string }).description).toContain('not a sitemap file URL');
      expect((await client.listResources()).resources).toEqual([expect.objectContaining({
        uri: 'redirx://guides/first-migration', mimeType: 'text/markdown',
      })]);
      const guide = await client.readResource({ uri: 'redirx://guides/first-migration' });
      const text = (guide.contents[0] as { text: string }).text;
      expect(text).toContain('not a claim of full site coverage');
      expect(text).toContain('same input and idempotency key');
      expect(text).toContain('sitemap file URL');
      expect(fetchMock).not.toHaveBeenCalled();
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
      expect(client.getInstructions()).toBeUndefined();
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

  it('advertises structured envelopes while retaining byte-compatible JSON text', async () => {
    const expected = envelope({ run_id: ids.run }, { status: 'queued', next_action: 'poll', retry_after_seconds: 10 });
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(expected)));
    const { client, close } = await connected();
    try {
      const tools = (await client.listTools()).tools;
      expect(tools.every(tool => tool.outputSchema?.properties?.data)).toBe(true);
      const result = await client.callTool({ name: 'get_migration', arguments: { migration_id: ids.migration } });
      expect(result.structuredContent).toEqual(expected);
      expect(JSON.parse((result.content[0] as { text: string }).text)).toEqual(result.structuredContent);
    } finally { await close(); }
  });

  it('flags failed business outcomes but retains recovery state and per-row partial outcomes', async () => {
    const failure = envelope({ selection_revision: 3 }, { status: 'failed', next_action: 'resolve_matches', error: {
      code: 'revision_conflict', message: 'Refresh matches.', retryable: false, next_action: 'resolve_matches', details: { mapping_id: ids.mapping },
    } });
    const partial = envelope({ outcomes: [{ mapping_id: ids.mapping, status: 'conflict' }], selection_revision: 4 }, { status: 'partial', next_action: 'resolve_matches' });
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(failure), { status: 409 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(partial)));
    const { client, close } = await connected();
    try {
      const args = { migration_id: ids.migration, run_id: ids.run, idempotency_key: 'decision', decisions: [{ mapping_id: ids.mapping, expected_revision: 2, action: 'defer' }] };
      const failed = await client.callTool({ name: 'resolve_matches', arguments: args });
      expect(failed.isError).toBe(true);
      expect(failed.structuredContent).toEqual(failure);
      const result = await client.callTool({ name: 'resolve_matches', arguments: { ...args, idempotency_key: 'other-decision' } });
      expect(result.isError).not.toBe(true);
      expect(result.structuredContent).toEqual(partial);
    } finally { await close(); }
  });

  it('provides safe unknown-outcome recovery after a lost mutation response', async () => {
    fetchMock.mockRejectedValueOnce(new Error('credential=never-show-this'));
    const { client, close } = await connected();
    try {
      const result = await client.callTool({ name: 'plan_migration', arguments: {
        old_site: 'https://old.test', new_site: 'https://new.test', idempotency_key: 'retain-this-key',
      } });
      expect(result.isError).toBe(true);
      expect(result.structuredContent).toMatchObject({ status: 'failed', next_action: 'retry', error: { retryable: true } });
      expect(JSON.stringify(result)).toContain('same idempotency key');
      expect(JSON.stringify(result)).not.toContain('credential');
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally { await close(); }
  });

  it('carries planning, corrected imports and paginated explicit review into an approved artifact journey', async () => {
    const oldUrl = 'https://old.test/Case%2FPath?x=One&x=two';
    const target = 'https://new.test/Keep%2FCase?x=Two';
    const artifact = '44444444-4444-4444-8444-444444444444';
    const replies = [
      envelope({}, { next_action: 'provide_inventory', status: 'needs_input' }),
      envelope({ inventory: { id: ids.mapping, status: 'partial', input_issues: {
        total_excluded: 1, reason_counts: { undeclared_origin: 1 },
        examples: [{ code: 'undeclared_origin', url_hint: 'https://foreign.test/a' }],
      } } }, { next_action: 'provide_inventory', status: 'partial' }),
      envelope({ inventory: { id: ids.mapping, status: 'complete' } }, { next_action: 'provide_inventory' }),
      envelope({ inventory: { id: artifact, status: 'complete' } }, { next_action: 'run_migration' }),
      envelope({ run_id: ids.run }, { status: 'queued', next_action: 'poll', retry_after_seconds: 10 }),
      envelope({ run_id: ids.run, jev: { seed_revision: 7 } }, { next_action: 'resolve_matches' }),
      envelope({ items: [{ mapping_id: ids.mapping, old_url: oldUrl, revision: 2, new_url: target }], next_cursor: 'opaque+/=', selection_revision: 9 }),
      envelope({ items: [], next_cursor: null, selection_revision: 9 }),
      envelope({ outcomes: [{ mapping_id: ids.mapping, status: 'applied' }], selection_revision: 10 }),
      envelope({ artifact_id: artifact }, { next_action: 'install_artifact' }),
      envelope({ resource_type: 'artifact_download', migration_id: ids.migration, artifact_id: artifact, format: 'json', content: JSON.stringify([{ old_url: oldUrl, new_url: target }]) }),
    ];
    fetchMock.mockImplementation(async () => new Response(JSON.stringify(replies.shift())));
    const { client, close } = await connected();
    try {
      await client.callTool({ name: 'plan_migration', arguments: { old_site: 'https://old.test', new_site: 'https://new.test', idempotency_key: 'plan-stable' } });
      const partial = await client.callTool({ name: 'import_inventory', arguments: {
        migration_id: ids.migration, side: 'old', urls: [oldUrl, 'https://foreign.test/a'], idempotency_key: 'old-original',
      } });
      expect(partial.structuredContent).toMatchObject({ status: 'partial', next_action: 'provide_inventory',
        data: { inventory: { input_issues: { total_excluded: 1 } } } });
      // The user corrects the intended scope; the gateway never drops rows itself.
      await client.callTool({ name: 'import_inventory', arguments: { migration_id: ids.migration,
        side: 'old', urls: [oldUrl], idempotency_key: 'old-corrected' } });
      await client.callTool({ name: 'import_inventory', arguments: { migration_id: ids.migration,
        side: 'new', urls: [target], idempotency_key: 'new-original' } });
      expect(fetchMock).toHaveBeenCalledTimes(4); // imports did not start a run
      const queued = await client.callTool({ name: 'run_migration', arguments: { migration_id: ids.migration,
        old_inventory_id: ids.mapping, new_inventory_id: artifact, idempotency_key: 'run-stable' } });
      expect(queued.structuredContent).toMatchObject({ status: 'queued', retry_after_seconds: 10 });
      expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ side: 'old', rows: [oldUrl], idempotency_key: 'old-corrected' });
      expect(JSON.parse(fetchMock.mock.calls[4][1].body).inventory_ids).toEqual({ old: ids.mapping, new: artifact });
      const state = await client.callTool({ name: 'get_migration', arguments: { migration_id: ids.migration, run_id: ids.run } });
      expect(state.structuredContent).toMatchObject({ next_action: 'resolve_matches', data: { jev: { seed_revision: 7 } } });
      const first = await client.callTool({ name: 'list_matches', arguments: { migration_id: ids.migration, run_id: ids.run, filter: 'needs_review', limit: 1 } });
      expect(first.structuredContent).toMatchObject({ data: { items: [{ old_url: oldUrl }], selection_revision: 9 } });
      const last = await client.callTool({ name: 'list_matches', arguments: { migration_id: ids.migration, run_id: ids.run, filter: 'needs_review', limit: 1, cursor: 'opaque+/=' } });
      expect(last.structuredContent).toMatchObject({ data: { next_cursor: null } });
      expect(new URL(fetchMock.mock.calls[7][0]).searchParams.get('cursor')).toBe('opaque+/=');
      // Fixture represents an explicit user decision; no match confidence is
      // converted into approval by any gateway callback.
      const reviewed = await client.callTool({ name: 'resolve_matches', arguments: { migration_id: ids.migration, run_id: ids.run, idempotency_key: 'explicit-review', decisions: [{ mapping_id: ids.mapping, expected_revision: 2, action: 'set_target', target_url: target }] } });
      expect(reviewed.structuredContent).toMatchObject({ data: { selection_revision: 10 } });
      const exported = await client.callTool({ name: 'export_redirects', arguments: { migration_id: ids.migration, run_id: ids.run, format: 'json', revision: 10, idempotency_key: 'export-reviewed' } });
      expect(exported.structuredContent).toMatchObject({ next_action: 'install_artifact' });
      const downloaded = await client.readResource({ uri: `redirx://migrations/${ids.migration}/artifacts/${artifact}` });
      expect(JSON.parse((downloaded.contents[0] as { text: string }).text)).toEqual([{ old_url: oldUrl, new_url: target }]);
      expect(JSON.parse(fetchMock.mock.calls[8][1].body).decisions[0]).toEqual({ mapping_id: ids.mapping, expected_revision: 2, action: 'set_target', target_url: target });
      expect(JSON.parse(fetchMock.mock.calls[9][1].body)).toMatchObject({ revision: 10, allow_partial: false });
      expect(fetchMock).toHaveBeenCalledTimes(11);
      expect(replies).toHaveLength(0);
    } finally { await close(); }
  });

  it('recovers a committed run after a lost response using status and the original key', async () => {
    const args = { migration_id: ids.migration, old_inventory_id: ids.mapping,
      new_inventory_id: '44444444-4444-4444-8444-444444444444', idempotency_key: 'keep-run-key' };
    const saved = new Map<string, string>();
    let reservations = 0;
    fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
      if (init.method === 'POST') {
        const body = JSON.parse(init.body as string);
        const prior = saved.get(body.idempotency_key);
        if (!prior) {
          saved.set(body.idempotency_key, JSON.stringify(body));
          reservations++;
          throw new Error('fixture response lost after commit');
        }
        expect(JSON.stringify(body)).toBe(prior);
        return new Response(JSON.stringify(envelope({ run_id: ids.run, replayed: true }, { status: 'queued', next_action: 'poll' })));
      }
      expect(url).toBe(`https://backend.test/api/v2/migrations/${ids.migration}`);
      return new Response(JSON.stringify(envelope({ run_id: ids.run }, { status: 'queued', next_action: 'poll', retry_after_seconds: 10 })));
    });
    const { client, close } = await connected();
    try {
      const uncertain = await client.callTool({ name: 'run_migration', arguments: args });
      expect(uncertain.isError).toBe(true);
      expect(JSON.stringify(uncertain)).toContain('The mutation may have completed');
      expect(JSON.stringify(uncertain)).toContain('same idempotency key');
      expect(fetchMock).toHaveBeenCalledTimes(1); // no automatic retry
      const status = await client.callTool({ name: 'get_migration', arguments: { migration_id: ids.migration } });
      expect(status.structuredContent).toMatchObject({ data: { run_id: ids.run } });
      const replay = await client.callTool({ name: 'run_migration', arguments: args });
      expect(replay.structuredContent).toMatchObject({ data: { run_id: ids.run, replayed: true } });
      expect(reservations).toBe(1); // stub authority, not a substitute for SQL idempotency tests
      expect(fetchMock).toHaveBeenCalledTimes(3);
    } finally { await close(); }
  });

});
