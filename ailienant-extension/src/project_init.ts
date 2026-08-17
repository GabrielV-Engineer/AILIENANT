import * as vscode from 'vscode';
import { WSClient } from './api/ws_client';
import { logger } from './shared/logger';

/**
 * /init — explicit, user-owned "draft AILIENANT.md from the workspace" action.
 *
 * Same discipline as Manual Dreaming (`ailienant.triggerDreamingRun`): fires
 * only on an explicit command or a first-provision notification action, never
 * automatically. Registers both halves — the trigger command and the global
 * listener for the backend's one completion event — so `extension.ts` stays a
 * thin wiring point rather than growing another inline handler.
 */

const COMMAND_ID = 'ailienant.initProject';

interface ProjectInitCompleteMessage {
    event_type?: string;
    data?: {
        status?: 'written' | 'refused_budget' | 'aborted_stale' | 'skipped_empty';
        path?: string;
        chars?: number;
    };
}

function triggerProjectInit(): void {
    WSClient.getInstance().send({
        event_type: 'client_project_init',
        data: {},
    });
    vscode.window.setStatusBarMessage('$(sparkle) AILIENANT: analyzing project…', 5000);
}

async function handleCompletion(raw: unknown): Promise<void> {
    const message = raw as ProjectInitCompleteMessage;
    if (message.event_type !== 'server_project_init_complete') {
        return;
    }
    const { status, path, chars } = message.data ?? {};
    switch (status) {
        case 'written':
            if (!path) {
                return;
            }
            vscode.window.showInformationMessage(
                `AILIENANT.md drafted (${chars ?? 0} chars). Review it before you commit.`,
            );
            try {
                const doc = await vscode.workspace.openTextDocument(path);
                await vscode.window.showTextDocument(doc);
            } catch (err) {
                logger.warn('AILIENANT: could not open drafted file:', err);
            }
            return;
        case 'refused_budget':
            vscode.window.showWarningMessage(
                'AILIENANT: project draft skipped — this session already reached its budget ceiling.',
            );
            return;
        case 'skipped_empty':
            vscode.window.showWarningMessage(
                'AILIENANT: could not read enough of the workspace to draft AILIENANT.md.',
            );
            return;
        case 'aborted_stale':
            // Silent: a save landed mid-run — the user's own edit already won.
            return;
        default:
            return;
    }
}

/** Registers the command + completion listener. Call once during activation. */
export function registerProjectInit(context: vscode.ExtensionContext): void {
    const command = vscode.commands.registerCommand(COMMAND_ID, triggerProjectInit);
    context.subscriptions.push(command);

    WSClient.getInstance().onMessageGlobal((raw) => {
        void handleCompletion(raw);
    });
}

/**
 * First-provision suggestion: a non-blocking notification offering to run
 * /init right away. Never auto-executes — mirrors Dreaming's "never wakes on
 * a timer" discipline: the user still has to click through.
 */
export async function suggestProjectInit(): Promise<void> {
    const ANALYZE = 'Analyze';
    const choice = await vscode.window.showInformationMessage(
        'AILIENANT.md was created. Want AILIENANT to draft it from your codebase?',
        ANALYZE,
        'Not now',
    );
    if (choice === ANALYZE) {
        await vscode.commands.executeCommand(COMMAND_ID);
    }
}
