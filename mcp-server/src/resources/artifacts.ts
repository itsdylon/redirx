import { McpServer, ResourceTemplate } from '@modelcontextprotocol/sdk/server/mcp.js';
import { McpError } from '@modelcontextprotocol/sdk/types.js';
import { clientForCall, type ToolExtra } from '../tools/context.js';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Artifact IDs are resolved through the caller's delegation on every read. */
export function registerArtifactResources(server: McpServer): void {
  server.registerResource('redirect-artifact', new ResourceTemplate(
    'redirx://migrations/{migration_id}/artifacts/{artifact_id}', { list: undefined },
  ), { description: 'Read an owned immutable redirect artifact after export.' }, async (uri, variables, extra) => {
    const migration = variables.migration_id;
    const artifact = variables.artifact_id;
    if (typeof migration !== 'string' || typeof artifact !== 'string'
        || !UUID.test(migration) || !UUID.test(artifact) || uri.search || uri.hash) {
      throw new McpError(-32602, 'Invalid artifact resource.');
    }
    const { client } = await clientForCall(extra as ToolExtra);
    const response = await client.pivot('GET', `/migrations/${migration}/artifacts/${artifact}`, undefined, (extra as ToolExtra).signal);
    if (!response.ok || response.data.error || response.data.status !== 'succeeded') {
      throw new McpError(-32002, 'Artifact unavailable for this account.');
    }
    const data = response.data.data;
    if (data.artifact_id !== artifact || data.migration_id !== migration
        || data.resource_type !== 'artifact_download' || typeof data.content !== 'string') {
      throw new McpError(-32603, 'Invalid artifact response.');
    }
    return { contents: [{ uri: uri.href,
      mimeType: data.format === 'json' ? 'application/json' : data.format === 'csv' ? 'text/csv' : 'text/plain',
      text: data.content }] };
  });
}
