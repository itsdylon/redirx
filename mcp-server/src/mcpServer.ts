import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerDeepMatchTool } from './tools/deepMatch.js';
import { registerDiscoverTool } from './tools/discover.js';
import { registerExportTool } from './tools/export.js';
import { registerPreviewTool } from './tools/preview.js';
import { registerPivotTools } from './tools/pivot.js';
import { registerArtifactResources } from './resources/artifacts.js';
import { FIRST_MIGRATION_INSTRUCTIONS, registerFirstMigrationResource } from './resources/firstMigration.js';
import { setupTelemetry } from './telemetry/posthog.js';
import { config } from './config.js';

/**
 * A fresh McpServer per HTTP request (stateless transport, see index.ts) —
 * cheap, since registration below is just attaching closures, no I/O. This
 * is also literally the pattern the SDK's own stateless example uses
 * (examples/server/simpleStatelessStreamableHttp.js): construct-and-discard,
 * not a long-lived singleton.
 */
export function buildMcpServer(): McpServer {
  const server = new McpServer({ name: 'redirx', version: '0.1.0' }, {
    capabilities: {},
    ...(config.pivotEnabled ? { instructions: FIRST_MIGRATION_INSTRUCTIONS } : {}),
  });

  if (config.pivotEnabled) {
    registerPivotTools(server);
    registerArtifactResources(server);
    registerFirstMigrationResource(server);
  } else {
    registerDiscoverTool(server);
    registerDeepMatchTool(server);
    registerPreviewTool(server);
    registerExportTool(server);
  }

  setupTelemetry(server);

  return server;
}
