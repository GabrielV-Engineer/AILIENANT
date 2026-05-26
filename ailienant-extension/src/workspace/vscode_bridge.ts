/**
 * Phase 7.11.2 (ADR-706 §4.5c) — back-compat shim.
 *
 * The typed singleton lives in `../shared/vscodeApi`; this module re-exports
 * the cached instance so existing `import { vscode } from './vscode_bridge'`
 * callers (e.g. Workspace.tsx) keep working without churn. New code should
 * import from `../shared/vscodeApi` directly.
 */
import { vscodeApi, VsCodeApi } from '../shared/vscodeApi';

export const vscode: VsCodeApi = vscodeApi();
export type { VsCodeApi } from '../shared/vscodeApi';
