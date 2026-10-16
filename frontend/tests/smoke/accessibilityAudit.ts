import type { Page, TestInfo } from '@playwright/test';

type Violation = { id: string; impact: string; nodes: Array<{ target: string[]; failureSummary: string }> };

// Optional supplemental evidence: no runtime dependency or claim of full WCAG
// conformance. Install audit tooling separately and pass its local script path.
export async function captureAccessibilityAudit(page: Page, testInfo: TestInfo, name: string) {
  if (!process.env.REDIRX_AXE_PATH) return;
  await page.addScriptTag({ path: process.env.REDIRX_AXE_PATH });
  const result = await page.evaluate(async () => {
    const axe = (window as typeof window & { axe: { run: (document: Document) => Promise<{ violations: Violation[] }> } }).axe;
    return axe.run(document);
  });
  await testInfo.attach(`${name}-axe`, { body: JSON.stringify(result.violations, null, 2), contentType: 'application/json' });
  console.log(name, JSON.stringify(result.violations.map(item => ({ id: item.id, impact: item.impact, nodes: item.nodes.map(node => node.target) }))));
  return result.violations;
}
