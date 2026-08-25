import * as vscode from 'vscode';
import { logger } from './shared/logger';

/**
 * First-run provisioning of the workspace-local `.ailienant/` home.
 *
 * Mirrors how a tool seeds its dotfolder on first use: it creates the
 * `.ailienant/` skeleton, drops a starter `AILIENANT.md` the user fills in, and
 * appends a marked block to the workspace `.gitignore` so runtime artifacts stay
 * untracked while the user-authored `AILIENANT.md` remains shareable.
 *
 * Every step is idempotent — existing files are never overwritten, and the
 * `.gitignore` block is a no-op once it matches the current managed content —
 * so it is safe to call on every activation. A `workspaceState` flag
 * short-circuits the one-time skeleton/template creation after the first
 * successful run, but the `.gitignore` check itself always runs (see
 * `provisionWorkspaceHome`'s docstring).
 */

const PROVISIONED_FLAG = 'ailienant.provisioned.v1';
export const GITIGNORE_MARKER = '# >>> AILIENANT (managed) >>>';
export const GITIGNORE_END = '# <<< AILIENANT (managed) <<<';

const GITIGNORE_BLOCK_BODY = [
    '# Runtime and cache artifacts — never commit these.',
    '.ailienant_telemetry.log*',
    '.ailienant/AGENTS.md',
    '.ailienant/.ailienant.json',
    '.ailienant/dreams/',
    '.ailienant/plans/',
    // The /init fork written next to a non-empty AILIENANT.md (core/project_init.py's
    // _GENERATED_SUFFIX) — no leading slash, so it matches at either the flat-root
    // or .ailienant/ candidate location.
    '*.generated.md',
    '# Keep .ailienant/AILIENANT.md tracked — it is your shareable project guidance.',
].join('\n');

const MANAGED_BLOCK = [GITIGNORE_MARKER, GITIGNORE_BLOCK_BODY, GITIGNORE_END].join('\n');

/**
 * Pure string transform: given the current `.gitignore` content, return what
 * it should become, or `null` if the managed block is already up to date (no
 * write needed). Exported so its branches — absent block, up-to-date block,
 * and a STALE block from a workspace provisioned before a pattern was added
 * to `GITIGNORE_BLOCK_BODY` (DEBT-173) — are unit-testable without touching
 * the real filesystem, the same pure-logic-module split this codebase already
 * uses for `clarificationLogic.ts`.
 *
 * A stale block is replaced in place (not appended again) so a workspace
 * provisioned under an older `GITIGNORE_BLOCK_BODY` heals to the current one
 * the next time this runs, instead of carrying a permanently outdated block.
 */
export function computeNextGitignore(existing: string): string | null {
    const markerStart = existing.indexOf(GITIGNORE_MARKER);
    if (markerStart !== -1) {
        const markerEnd = existing.indexOf(GITIGNORE_END, markerStart);
        if (markerEnd !== -1) {
            const blockEnd = markerEnd + GITIGNORE_END.length;
            if (existing.slice(markerStart, blockEnd) === MANAGED_BLOCK) {
                return null; // Already up to date — nothing to do.
            }
            return existing.slice(0, markerStart) + MANAGED_BLOCK + existing.slice(blockEnd);
        }
    }
    const separator = existing.length > 0 && !existing.endsWith('\n') ? '\n\n' : existing.length > 0 ? '\n' : '';
    return existing + separator + MANAGED_BLOCK + '\n';
}

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

    const existing = (await pathExists(gitignore))
        ? decoder.decode(await vscode.workspace.fs.readFile(gitignore))
        : '';

    const next = computeNextGitignore(existing);
    if (next === null) {
        return; // Already up to date — nothing to do.
    }
    await vscode.workspace.fs.writeFile(gitignore, encoder.encode(next));
}

/**
 * Provision `<workspace>/.ailienant/` on first run. No-op when no folder is open
 * or when a previous run already completed. Fully non-fatal: any filesystem
 * error is logged and swallowed so activation always proceeds.
 *
 * The managed `.gitignore` block is the one exception to "first run only": it
 * is re-checked on every activation, first-run or not, because
 * `ensureGitignoreBlock`/`computeNextGitignore` are cheap no-ops once the
 * block is current — this is what lets a workspace provisioned before a
 * pattern was added to `GITIGNORE_BLOCK_BODY` (DEBT-173) heal on its next
 * activation instead of carrying a permanently stale block.
 *
 * Returns `true` only when THIS call performed the first-ever provisioning
 * (the starter `AILIENANT.md` was just created) — the signal `extension.ts`
 * uses to offer the /init suggestion. `false` on every later activation, on
 * "no workspace open," and on any provisioning failure.
 */
export async function provisionWorkspaceHome(context: vscode.ExtensionContext): Promise<boolean> {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!root) {
        return false; // No workspace folder — nothing to provision.
    }
    if (context.workspaceState.get<boolean>(PROVISIONED_FLAG)) {
        try {
            await ensureGitignoreBlock(root);
        } catch (err) {
            logger.warn('AILIENANT: .gitignore refresh skipped:', err);
        }
        return false; // Already provisioned in a prior session.
    }

    try {
        const dir = vscode.Uri.joinPath(root, '.ailienant');
        const plansDir = vscode.Uri.joinPath(dir, 'plans');
        // createDirectory is idempotent: it does not error when the target exists.
        await vscode.workspace.fs.createDirectory(dir);
        await vscode.workspace.fs.createDirectory(plansDir);

        const ailienantMd = vscode.Uri.joinPath(dir, 'AILIENANT.md');
        const isFirstProvision = !(await pathExists(ailienantMd));
        if (isFirstProvision) {
            await vscode.workspace.fs.writeFile(ailienantMd, new TextEncoder().encode(AILIENANT_MD_TEMPLATE));
        }

        await ensureGitignoreBlock(root);

        await context.workspaceState.update(PROVISIONED_FLAG, true);
        logger.log('AILIENANT: workspace home provisioned.');
        return isFirstProvision;
    } catch (err) {
        logger.warn('AILIENANT: workspace provisioning skipped:', err);
        return false;
    }
}
