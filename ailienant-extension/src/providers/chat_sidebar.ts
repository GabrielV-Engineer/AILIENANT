import * as vscode from 'vscode';
import { SessionManager } from '../brain/session';

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
                    const session = SessionManager.getInstance();
                    // Orquestamos la tarea con el prompt que viene de la UI
                    await session.startAITask(data.value);
                    break;
                }
                case 'ABORT_TASK': {
                    SessionManager.getInstance().abortCurrentTask();
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
        // En la siguiente fase, aquí generaremos los URIs de los archivos compilados de React.
        // Por ahora, dejamos un placeholder estructural.
        return `
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>AILIENANT Chat</title>
            </head>
            <body>
                <div id="root"></div>
                <script>
                    const vscode = acquireVsCodeApi();
                    document.getElementById('root').innerHTML = '<h1>AILIENANT 🐜</h1><p>Esperando motor React...</p>';
                </script>
            </body>
            </html>
        `;
    }
}