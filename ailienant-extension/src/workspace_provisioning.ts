import * as vscode from 'vscode';

/**
 * First-run provisioning of the workspace-local `.ailienant/` home.
 *
 * Mirrors how a tool seeds its dotfolder on first use: it creates the
 * `.ailienant/` skeleton, drops a starter `AILIENANT.md` the user fills in, and
 * appends a marked block to the workspace `.gitignore` so runtime artifacts stay
 * untracked while the user-authored `AILIENANT.md` remains shareable.
 *
 * Every step is idempotent — existing files are never overwritten and the
 * `.gitignore` block is appended only when its marker is absent — so it is safe
 * to call on every activation. A `workspaceState` flag short-circuits the common
 * case after the first successful run.
 */

const PROVISIONED_FLAG = 'ailienant.provisioned.v1';
const GITIGNORE_MARKER = '# >>> AILIENANT (managed) >>>';
const GITIGNORE_END = '# <<< AILIENANT (managed) <<<';

const GITIGNORE_BLOCK = [
    GITIGNORE_MARKER,
    '# Runtime and cache artifacts — never commit these.',
    '.ailienant_telemetry.log*',
    '.ailienant/AGENTS.md',
    '.ailienant/.ailienant.json',
    '.ailienant/dreams/',
    '.ailienant/plans/',
    '# Keep .ailienant/AILIENANT.md tracked — it is your shareable project guidance.',
    GITIGNORE_END,
    '',
].join('\n');

const AILIENANT_MD_TEMPLATE = [
    '# AILIENANT Project Instructions',
    '',
    '<!--',
    'Freeform, standing guidance AILIENANT reads on every task in this project.',
    'Use it for conventions, domain vocabulary, and "always / never" notes that do',
    'not fit the machine-checkable rules in .ailienant/.ailienant.json.',
    'This file is meant to be committed and shared with your team.',
    '-->',
    '',
    '## Stack & Conventions',
    '',
    '- ',
    '',
    '## Always',
    '',
    '- ',
    '',
    '## Never',
    '',
    '- ',
    '',
].join('\n');

async function pathExists(uri: vscode.Uri): Promise<boolean> {
    try {
        await vscode.workspace.fs.stat(uri);
        return true;
    } catch {
        return false;
    }
}

async function ensureGitignoreBlock(root: vscode.Uri): Promise<void> {
    const gitignore = vscode.Uri.joinPath(root, '.gitignore');
    const decoder = new TextDecoder('utf-8');
    const encoder = new TextEncoder();

    let existing = '';
    if (await pathExists(gitignore)) {
        existing = decoder.decode(await vscode.workspace.fs.readFile(gitignore));
        if (existing.includes(GITIGNORE_MARKER)) {
            return; // Block already present — nothing to do.
        }
    }

    const separator = existing.length > 0 && !existing.endsWith('\n') ? '\n\n' : existing.length > 0 ? '\n' : '';
    const next = existing + separator + GITIGNORE_BLOCK;
    await vscode.workspace.fs.writeFile(gitignore, encoder.encode(next));
}

/**
 * Provision `<workspace>/.ailienant/` on first run. No-op when no folder is open
 * or when a previous run already completed. Fully non-fatal: any filesystem
 * error is logged and swallowed so activation always proceeds.
 */
export async function provisionWorkspaceHome(context: vscode.ExtensionContext): Promise<void> {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!root) {
        return; // No workspace folder — nothing to provision.
    }
    if (context.workspaceState.get<boolean>(PROVISIONED_FLAG)) {
        return; // Already provisioned in a prior session.
    }

    try {
        const dir = vscode.Uri.joinPath(root, '.ailienant');
        const plansDir = vscode.Uri.joinPath(dir, 'plans');
        // createDirectory is idempotent: it does not error when the target exists.
        await vscode.workspace.fs.createDirectory(dir);
        await vscode.workspace.fs.createDirectory(plansDir);

        const ailienantMd = vscode.Uri.joinPath(dir, 'AILIENANT.md');
        if (!(await pathExists(ailienantMd))) {
            await vscode.workspace.fs.writeFile(ailienantMd, new TextEncoder().encode(AILIENANT_MD_TEMPLATE));
        }

        await ensureGitignoreBlock(root);

        await context.workspaceState.update(PROVISIONED_FLAG, true);
        console.log('AILIENANT: workspace home provisioned.');
    } catch (err) {
        console.warn('AILIENANT: workspace provisioning skipped:', err);
    }
}
