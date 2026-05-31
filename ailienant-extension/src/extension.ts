import * as vscode from 'vscode';
import { IntentRouter } from './core/IntentRouter';
import { SessionManager } from './brain/session';
import { SessionBrowserProvider } from './providers/session_browser_provider';
import { WorkspacePanelManager, CoreProcessManager, findFreePort, generateAuthToken } from './providers/workspace_panel';
import { APIClient } from './api/api_client';
import { WSClient } from './api/ws_client';
import type { Session } from './shared/types';
import {
    MIRROR_SCHEME,
    MirrorContentProvider,
    applyMergeCommand,
    showMctsDiff,
} from './providers/mirror';
import { boundingBoxRegistry, installDecayListener } from './providers/telemetry';
import { InlineMutationManager } from './core/InlineMutationManager';
import { IdeSync } from './ide_sync';

function makeSessionId(): string {
    if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
        return crypto.randomUUID();
    }
    return 'ses-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
    console.log('AILIENANT extension activated.');

    // Phase 7.9.A.5.1 — dynamic port selection + ephemeral auth token.
    // findFreePort() is OS-assigned (listen(0)) — no TOCTOU race.
    const port  = await findFreePort();
    const token = generateAuthToken();
    APIClient.getInstance().configure(`http://127.0.0.1:${port}/api/v1`, token);
    WSClient.getInstance().configure(`ws://127.0.0.1:${port}/api/v1/ws`, token);

    const coreManager = new CoreProcessManager(port, token, context.extensionUri.fsPath);

    // ── Workspace panel manager (single-instance editor tab) ─────────
    const workspaceManager = new WorkspacePanelManager(context.extensionUri, context.workspaceState);
    workspaceManager.setCoreManager(coreManager);

    // Auto-start the Core backend when enabled (default: true).
    if (vscode.workspace.getConfiguration('ailienant').get<boolean>('autoStartCore', true)) {
        void coreManager.start();
    }

    // ── Session browser sidebar provider ─────────────────────────────
    const onOpenSession = (s: Session): void => workspaceManager.openSession(s);

    const onNewSession = async (): Promise<Session> => {
        const now = new Date().toISOString();
        return {
            id:            makeSessionId(),
            title:         '',
            created_at:    now,
            last_modified: now,
            message_count: 0,
            model_tier:    'medium',
        };
    };

    const onDeleteSession = (id: string): void => {
        workspaceManager.closeSession(id);
        workspaceManager.clearTranscript(id);  // Phase 7.9.B.20 — drop persisted transcript
    };

    const sessionBrowser = new SessionBrowserProvider(
        context.extensionUri,
        context.workspaceState,
        onOpenSession,
        onNewSession,
        onDeleteSession,
    );

    // Auto-title pipeline: workspace_panel fires the LLM request after the first
    // SUBMIT_TASK for a session whose title is empty; result updates sidebar.
    workspaceManager.setTitleUpdater((sessionId, title) => {
        sessionBrowser.updateSessionTitle(sessionId, title);
    });

    // Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel: when the backend broadcasts
    // `server_session_branched`, workspace_panel.ts mints the new Session and
    // hands it here; we persist it to the sidebar and open the new panel so
    // the user lands on the branched conversation immediately.
    workspaceManager.setSessionBranchedHandler((branched) => {
        sessionBrowser.persistSession(branched);
        workspaceManager.openSession(branched);
    });

    const sidebarRegistration = vscode.window.registerWebviewViewProvider(
        SessionBrowserProvider.viewType,
        sessionBrowser,
        // Phase 7.11.2 (ADR-706 §4.5c) — rehydration via acquireVsCodeApi().setState
        // means the DOM no longer needs to be kept in memory; flipped to false so
        // the rehydration code path is what truly carries the sidebar across show/hide.
        { webviewOptions: { retainContextWhenHidden: false } },
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
            // Persist to sidebar first so the session appears in the browser
            sessionBrowser.persistSession(s);
            workspaceManager.openSession(s);
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

    // ── Phase 7.11.1 (ADR-706 §4.5a) — Cmd+K inline editor mutations ──
    const inlineEditCmd = vscode.commands.registerCommand(
        'ailienant.inlineEdit',
        async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor || editor.selection.isEmpty) {
                void vscode.window.showInformationMessage(
                    'AILIENANT: select code first, then press Cmd+K.',
                );
                return;
            }
            const prompt = await vscode.window.showInputBox({
                prompt: 'Instruction for AILIENANT',
                placeHolder: 'e.g. "type-annotate", "extract helper", "explain inline"',
            });
            if (!prompt) { return; }
            await InlineMutationManager.instance.startSession(editor, editor.selection, prompt);
        },
    );
    const acceptInlineEditCmd = vscode.commands.registerCommand(
        'ailienant.acceptInlineEdit',
        () => InlineMutationManager.instance.accept(),
    );
    const rejectInlineEditCmd = vscode.commands.registerCommand(
        'ailienant.rejectInlineEdit',
        () => InlineMutationManager.instance.cancel(),
    );

    // ── Incognito status bar + IdeSync ───────────────────────────────
    const incognitoBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 99);
    incognitoBar.text = '$(shield) Incógnito (Off)';
    incognitoBar.tooltip = 'Toggle AILIENANT push telemetry (Incognito mode)';
    incognitoBar.command = 'ailienant.toggleIncognito';
    incognitoBar.show();

    const ideSync = new IdeSync((_blocked, _path) => { /* reserved for future blocked-file UX */ });

    let incognitoActive = false;
    const toggleIncognitoCmd = vscode.commands.registerCommand('ailienant.toggleIncognito', () => {
        incognitoActive = !incognitoActive;
        ideSync.setIncognito(incognitoActive);
        incognitoBar.text = incognitoActive ? '$(shield) Incógnito (On)' : '$(shield) Incógnito (Off)';
    });

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
        inlineEditCmd,
        acceptInlineEditCmd,
        rejectInlineEditCmd,
        toggleIncognitoCmd,
        ideSync,
        incognitoBar,
        { dispose: () => InlineMutationManager.instance.dispose() },
        { dispose: () => workspaceManager.dispose() },
    );

    // Phase 3.4.7 — silent Bounding Box decay listener
    installDecayListener(context, boundingBoxRegistry);
}

export function deactivate(): void {}
