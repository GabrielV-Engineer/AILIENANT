// ailienant-extension/src/providers/mirror.ts
// Phase 3.4.5 — VS Code Mirror integration for MCTS "parallel universes".
//
// Registers the `ailienant-vision://` URI scheme so VS Code's native diff view
// can render `<workspace_file> ↔ <MCTS dreamed file>`. Also exposes commands:
//   * ailienant.showMctsDiff(nodeId, relPath) — opens the diff view
//   * ailienant.applyMerge(nodeId)            — confirmation → POST /merge

import * as vscode from 'vscode';
import { APIClient } from '../api/api_client';

export const MIRROR_SCHEME = 'ailienant-vision';

export class MirrorContentProvider implements vscode.TextDocumentContentProvider {
    private readonly _onDidChange = new vscode.EventEmitter<vscode.Uri>();
    public readonly onDidChange = this._onDidChange.event;

    public async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
        // URI form: ailienant-vision://{node_id}/{relative_path}
        const nodeId = uri.authority;
        const relPath = uri.path.replace(/^\//, '');
        try {
            return await APIClient.getInstance().fetchVirtualFile(nodeId, relPath);
        } catch (e: any) {
            return `// [ailienant-vision] failed to fetch '${relPath}' from node '${nodeId}':\n// ${e?.message ?? e}\n`;
        }
    }

    /**
     * Caller can fire this when the backend MCTS state changes so VS Code
     * re-renders any open diff views. No backend hook wires this yet (3.4.5+).
     */
    public invalidate(uri: vscode.Uri): void {
        this._onDidChange.fire(uri);
    }
}

export function buildMirrorUri(nodeId: string, relPath: string): vscode.Uri {
    return vscode.Uri.from({
        scheme: MIRROR_SCHEME,
        authority: nodeId,
        path: '/' + relPath.replace(/^\/+/, ''),
    });
}

export async function showMctsDiff(nodeId: string, relPath: string): Promise<void> {
    const ws = vscode.workspace.workspaceFolders?.[0];
    if (!ws) {
        vscode.window.showErrorMessage('AILIENANT: no workspace folder open');
        return;
    }
    const local = vscode.Uri.joinPath(ws.uri, relPath);
    const virtual = buildMirrorUri(nodeId, relPath);
    const title = `${relPath} ↔ Dream ${nodeId.slice(0, 8)}`;
    await vscode.commands.executeCommand('vscode.diff', local, virtual, title);
}

export async function applyMergeCommand(nodeId: string): Promise<void> {
    const ws = vscode.workspace.workspaceFolders?.[0];
    if (!ws) {
        vscode.window.showErrorMessage('AILIENANT: no workspace folder open');
        return;
    }
    const confirm = await vscode.window.showWarningMessage(
        `Apply MCTS dream ${nodeId.slice(0, 8)} to disk? Files will be overwritten.`,
        { modal: true },
        'Apply',
    );
    if (confirm !== 'Apply') {
        return;
    }
    try {
        const report = await APIClient.getInstance().applyMerge(nodeId, ws.uri.fsPath);
        if (report.success) {
            vscode.window.showInformationMessage(
                `AILIENANT: merged ${report.merged_files} file(s); pruned ${report.prune_count} node(s).`,
            );
        } else {
            vscode.window.showErrorMessage(
                `AILIENANT merge failed: ${report.errors.join('; ')}`,
            );
        }
    } catch (e: any) {
        vscode.window.showErrorMessage(`AILIENANT applyMerge call failed: ${e?.message ?? e}`);
    }
}
