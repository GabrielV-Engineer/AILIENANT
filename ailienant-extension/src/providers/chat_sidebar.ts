import * as vscode from 'vscode';
import { SessionManager } from '../brain/session';
import { IntentRouter } from '../core/IntentRouter';
import { WSClient } from '../api/ws_client';

export class AilienantChatProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'ailienant.chatView';
    private _view?: vscode.WebviewView;

    constructor(private readonly _extensionUri: vscode.Uri) { }

    /**
     * Se ejecuta cuando el usuario abre el panel lateral por primera vez.
     */
    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ) {
        this._view = webviewView;

        // Configuración de seguridad y capacidades del Webview
        webviewView.webview.options = {
            enableScripts: true, // Vital para React
            localResourceRoots: [this._extensionUri]
        };

        // Inyectamos el HTML base (que luego cargará nuestro JS de React)
        webviewView.webview.html = this._getHtmlForWebview(webviewView.webview);

        // OÍDOS: Escuchamos mensajes que vienen DESDE la UI (React)
        webviewView.webview.onDidReceiveMessage(async (data) => {
            switch (data.type) {
                case 'SUBMIT_TASK': {
                    const activeDoc = vscode.window.activeTextEditor?.document;
                    const intercepted = await IntentRouter.intercept(data.value, activeDoc);
                    if (!intercepted) {
                        const session = SessionManager.getInstance();
                        await session.startAITask(data.value);
                    }
                    break;
                }
                case 'ABORT_TASK': {
                    SessionManager.getInstance().abortCurrentTask();
                    break;
                }
                case 'togglePlannerMode': {
                    WSClient.getInstance().send({
                        event_type: 'client_planner_mode_toggle',
                        data: { active: data.value as boolean },
                    });
                    break;
                }
            }
        });
    }

    /**
     * Envía datos DESDE la extensión HACIA la UI (React).
     * Se usará para el streaming de tokens de los WebSockets.
     */
    public sendMessageToWebview(type: string, payload: any) {
        if (this._view) {
            this._view.webview.postMessage({ type, payload });
        }
    }

    private _getHtmlForWebview(webview: vscode.Webview) {
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'webview.js')
        );
        return `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Content-Security-Policy"
          content="default-src 'none'; script-src ${webview.cspSource};">
    <title>AILIENANT Chat</title>
</head>
<body>
    <div id="root"></div>
    <script src="${scriptUri}"></script>
</body>
</html>`;
    }
}