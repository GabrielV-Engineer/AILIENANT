import * as vscode from 'vscode';
import { IntentRouter } from './core/IntentRouter';
import { SessionManager } from './brain/session';
import { SessionBrowserProvider } from './providers/session_browser_provider';
import { WorkspacePanelManager } from './providers/workspace_panel';
import type { Session } from './shared/types';
import {
    MIRROR_SCHEME,
    MirrorContentProvider,
    applyMergeCommand,
    showMctsDiff,
} from './providers/mirror';
import { boundingBoxRegistry, installDecayListener } from './providers/telemetry';

function makeSessionId(): string {
    if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
        return crypto.randomUUID();
    }
    return 'ses-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

export function activate(context: vscode.ExtensionContext): void {
    console.log('AILIENANT extension activated.');

    // ── Workspace panel manager (single-instance editor tab) ─────────
    const workspaceManager = new WorkspacePanelManager(context.extensionUri, context.workspaceState);

    // ── Session browser sidebar provider ─────────────────────────────
    const onOpenSession = (s: Session): void => workspaceManager.openSession(s);

    const onNewSession = async (): Promise<Session> => {
        const now = new Date().toISOString();
        return {
            id:            makeSessionId(),
            title:         `Session ${new Date().toLocaleString()}`,
            created_at:    now,
            last_modified: now,
            message_count: 0,
            model_tier:    'medium',
        };
    };

    const sessionBrowser = new SessionBrowserProvider(
        context.extensionUri,
        context.workspaceState,
        onOpenSession,
        onNewSession,
    );

    const sidebarRegistration = vscode.window.registerWebviewViewProvider(
        SessionBrowserProvider.viewType, sessionBrowser,
    );

    // ── Commands ──────────────────────────────────────────────────────
    const openWorkspaceCmd = vscode.commands.registerCommand(
        'ailienant.openWorkspace',
        async () => {
            const sessions = sessionBrowser.getSessions();
            const target = sessions[0] ?? await onNewSession();
            if (!sessions.find(s => s.id === target.id)) {
                // newly created — persist via sidebar provider's NEW_SESSION path
                // (the provider auto-persists on its NEW_SESSION message; here we
                //  call the open path directly for the command form)
            }
            workspaceManager.openSession(target);
        },
    );

    const newSessionCmd = vscode.commands.registerCommand(
        'ailienant.newSession',
        async () => {
            const s = await onNewSession();
            workspaceManager.openSession(s);
            sessionBrowser.refresh();
        },
    );

    const runTaskCmd = vscode.commands.registerCommand(
        'ailienant-extension.runTask',
        async () => {
            const prompt = await vscode.window.showInputBox({
                prompt: 'Enter your directive for AILIENANT',
                placeHolder: 'e.g. "format", "constify", or describe a complex task',
            });
            if (!prompt) { return; }
            const doc = vscode.window.activeTextEditor?.document;
            const intercepted = await IntentRouter.intercept(prompt, doc);
            if (!intercepted) {
                await SessionManager.getInstance().startAITask(prompt);
            }
        },
    );

    // ── MCTS Mirror (Phase 3.4.5) ─────────────────────────────────────
    const mirrorProvider = new MirrorContentProvider();
    const mirrorRegistration = vscode.workspace.registerTextDocumentContentProvider(
        MIRROR_SCHEME, mirrorProvider,
    );
    const showDiffCmd = vscode.commands.registerCommand(
        'ailienant.showMctsDiff',
        (nodeId: string, filePath: string) => showMctsDiff(nodeId, filePath),
    );
    const applyMergeCmd = vscode.commands.registerCommand(
        'ailienant.applyMerge',
        (nodeId: string) => applyMergeCommand(nodeId),
    );

    context.subscriptions.push(
        sidebarRegistration,
        openWorkspaceCmd,
        newSessionCmd,
        runTaskCmd,
        mirrorRegistration,
        showDiffCmd,
        applyMergeCmd,
        { dispose: () => workspaceManager.dispose() },
    );

    // Phase 3.4.7 — silent Bounding Box decay listener
    installDecayListener(context, boundingBoxRegistry);
}

export function deactivate(): void {}
