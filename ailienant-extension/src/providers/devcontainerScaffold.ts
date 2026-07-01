import * as vscode from 'vscode';
import { logger } from '../shared/logger';

// A minimal, user-owned starter devcontainer. Kept intentionally small: the user
// edits it to declare their real environment. The base image ships common tooling
// and lets the host's Docker daemon build/cache it — AILIENANT builds nothing.
const STARTER_DEVCONTAINER = JSON.stringify(
    {
        name: 'AILIENANT project',
        image: 'mcr.microsoft.com/devcontainers/base:ubuntu',
        // Add features / postCreateCommand / forwardPorts to match your project.
    },
    null,
    4,
) + '\n';

async function pathExists(uri: vscode.Uri): Promise<boolean> {
    try {
        await vscode.workspace.fs.stat(uri);
        return true;
    } catch {
        return false;
    }
}

/**
 * Write a starter `.devcontainer/devcontainer.json` when the workspace has none.
 * Idempotent — an existing config is never overwritten — so it is safe to invoke
 * repeatedly. Opens the file for editing on first creation.
 */
export async function scaffoldDevcontainer(): Promise<void> {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!root) {
        void vscode.window.showWarningMessage('AILIENANT: open a folder before scaffolding a devcontainer.');
        return;
    }

    const dir = vscode.Uri.joinPath(root, '.devcontainer');
    const nested = vscode.Uri.joinPath(dir, 'devcontainer.json');
    const flat = vscode.Uri.joinPath(root, '.devcontainer.json');

    if (await pathExists(nested) || await pathExists(flat)) {
        void vscode.window.showInformationMessage('AILIENANT: a devcontainer.json already exists.');
        return;
    }

    try {
        await vscode.workspace.fs.createDirectory(dir);
        await vscode.workspace.fs.writeFile(nested, new TextEncoder().encode(STARTER_DEVCONTAINER));
        logger.log('AILIENANT: scaffolded .devcontainer/devcontainer.json');
        const doc = await vscode.workspace.openTextDocument(nested);
        await vscode.window.showTextDocument(doc);
    } catch (err) {
        logger.warn('AILIENANT: devcontainer scaffold failed:', err);
        void vscode.window.showErrorMessage('AILIENANT: could not create devcontainer.json (see logs).');
    }
}
