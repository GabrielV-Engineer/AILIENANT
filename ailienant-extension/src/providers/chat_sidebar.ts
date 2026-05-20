import * as vscode from 'vscode';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { WSClient } from '../api/ws_client';
import {
    DEFAULT_PROFILE,
    IntelligenceProfile,
    ReasoningPreset,
    InferenceTier,
    DreamingProfile,
    WORKSPACE_STATE_KEYS,
} from '../shared/config';

interface InitialState {
    masterEnabled:   boolean;
    profile:         IntelligenceProfile;
    reasoningPreset: ReasoningPreset;
    inferenceTier:   InferenceTier;
    dreamingEnabled: boolean;
    dreamingProfile: DreamingProfile;
}

export class AilienantChatProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'ailienant.chatView';
    private _view?: vscode.WebviewView;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _workspaceState: vscode.Memento,
    ) { }

    /**
     * Se ejecuta cuando el usuario abre el panel lateral por primera vez.
     */
    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri]
        };

        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        // Forward WS status changes to the webview
        WSClient.getInstance().onStatus(status => {
            this.sendMessageToWebview('WS_STATUS', status);
        });

        // Forward incoming WS server events to webview
        WSClient.getInstance().onMessage((raw) => {
            const msg = raw as { event_type?: string; data?: unknown };
            if (msg.event_type) {
                this.sendMessageToWebview(msg.event_type, msg.data);
            }
        });

        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'SUBMIT_TASK': {
                    const activeDoc = vscode.window.activeTextEditor?.document;
                    const intercepted = await IntentRouter.intercept(data.value, activeDoc);
                    if (!intercepted) {
                        const session = SessionManager.getInstance();
                        // Preset/tier params are injected into the WSClient payload by the webview;
                        // SessionManager.startAITask handles the full task submission pipeline.
                        await session.startAITask(data.value as string);
                    }
                    break;
                }
                case 'ABORT_TASK': {
                    SessionManager.getInstance().abortCurrentTask();
                    break;
                }
                case 'HITL_RESPONSE': {
                    WSClient.getInstance().send({
                        event_type: 'client_hitl_response',
                        data: {
                            approval_id: data.approval_id,
                            approved:    data.approved,
                            comment:     data.comment,
                        },
                    });
                    break;
                }
                case 'FORCE_AGENT': {
                    const session = SessionManager.getInstance();
                    // Force-invokes agent via blank prompt; role is encoded in the task prompt
                    await session.startAITask(`/agent ${data.role as string}`);
                    break;
                }
                case 'togglePlannerMode': {
                    WSClient.getInstance().send({
                        event_type: 'client_planner_mode_toggle',
                        data: { active: data.value as boolean },
                    });
                    break;
                }
                case 'dreaming_toggle': {
                    const dreamingEnabled = data.value as boolean;
                    const dreamingProfile = data.profile as DreamingProfile;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.dreamingEnabled, dreamingEnabled);
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.dreamingProfile, dreamingProfile);
                    WSClient.getInstance().send({
                        event_type: 'client_planner_mode_toggle',
                        data: { active: dreamingEnabled, dreaming_profile: dreamingProfile },
                    });
                    break;
                }
                case 'master_toggle': {
                    const enabled = data.value as boolean;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.masterEnabled, enabled);
                    WSClient.getInstance().send({
                        event_type: 'client_master_toggle',
                        data: { enabled },
                    });
                    break;
                }
                case 'profile_change': {
                    const profile = data.value as IntelligenceProfile;
                    await this._workspaceState.update(WORKSPACE_STATE_KEYS.profile, profile);
                    WSClient.getInstance().send({
                        event_type: 'client_profile_change',
                        data: { profile },
                    });
                    break;
                }
            }
        });
    }

    /**
     * Envía datos DESDE la extensión HACIA la UI (React).
     */
    public sendMessageToWebview(type: string, payload: unknown) {
        if (this._view) {
            this._view.webview.postMessage({ type, payload });
        }
    }

    private _readInitialState(): InitialState {
        return {
            masterEnabled:   this._workspaceState.get<boolean>(WORKSPACE_STATE_KEYS.masterEnabled, false),
            profile:         this._workspaceState.get<IntelligenceProfile>(WORKSPACE_STATE_KEYS.profile, DEFAULT_PROFILE),
            reasoningPreset: this._workspaceState.get<ReasoningPreset>(WORKSPACE_STATE_KEYS.reasoningPreset, 'architect'),
            inferenceTier:   this._workspaceState.get<InferenceTier>(WORKSPACE_STATE_KEYS.inferenceTier, 'HYBRID'),
            dreamingEnabled: this._workspaceState.get<boolean>(WORKSPACE_STATE_KEYS.dreamingEnabled, false),
            dreamingProfile: this._workspaceState.get<DreamingProfile>(WORKSPACE_STATE_KEYS.dreamingProfile, 'Hybrid'),
        };
    }

    private _getHtmlForWebview(webview: vscode.Webview) {
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview.js')
        );
        const styleUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview.css')
        );
        const logoUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'logo.svg')
        );
        // Encode initial state on a data-* attribute (CSP-safe — no inline <script>).
        const initialAttr = JSON.stringify(this._readInitialState())
            .replace(/&/g, '&amp;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;');
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none'; img-src ${webview.cspSource}; script-src ${webview.cspSource}; style-src ${webview.cspSource} 'unsafe-inline';">
    <title>AILIENANT</title>
    <link rel="stylesheet" href="${styleUri}">
</head>
<body>
    <div id="root" data-initial='${initialAttr}' data-logo='${logoUri}'></div>
    <script src="${scriptUri}"></script>
</body>
</html>`;
    }
}
