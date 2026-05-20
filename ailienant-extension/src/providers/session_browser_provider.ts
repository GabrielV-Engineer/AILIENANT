import * as vscode from 'vscode';
import type { Session } from '../shared/types';

const SESSIONS_KEY = 'ailienant.sessions';

export class SessionBrowserProvider implements vscode.WebviewViewProvider {
    public static readonly viewType = 'ailienant.sessionBrowser';
    private _view?: vscode.WebviewView;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _workspaceState: vscode.Memento,
        private readonly _onOpenSession: (session: Session) => void,
        private readonly _onNewSession: () => Promise<Session>,
    ) {}

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken,
    ): void {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [this._extensionUri],
        };

        webviewView.webview.html = this._renderHtml(webviewView.webview);

        // Initial state push
        webviewView.onDidChangeVisibility(() => {
            if (webviewView.visible) { this.refresh(); }
        });
        queueMicrotask(() => this.refresh());

        webviewView.webview.onDidReceiveMessage(async (msg) => {
            switch (msg.type) {
                case 'NEW_SESSION': {
                    const session = await this._onNewSession();
                    this._persist((prev) => [session, ...prev]);
                    this._onOpenSession(session);
                    break;
                }
                case 'OPEN_SESSION': {
                    const sessions = this.getSessions();
                    const s = sessions.find(x => x.id === msg.session_id);
                    if (s) {
                        this._touch(s.id);
                        this._onOpenSession(s);
                    }
                    break;
                }
                case 'DELETE_SESSION': {
                    this._persist((prev) => prev.filter(s => s.id !== msg.session_id));
                    break;
                }
                case 'RENAME_SESSION': {
                    this._persist((prev) => prev.map(s =>
                        s.id === msg.session_id ? { ...s, title: msg.title } : s
                    ));
                    break;
                }
            }
        });
    }

    public refresh(): void {
        if (!this._view) { return; }
        this._view.webview.postMessage({ type: 'SESSIONS_UPDATED', sessions: this.getSessions() });
    }

    public getSessions(): Session[] {
        return this._workspaceState.get<Session[]>(SESSIONS_KEY, []);
    }

    public touchSession(id: string): void {
        this._touch(id);
    }

    /** Called by the VS Code command (tab bar button) to add a session created outside the webview. */
    public persistSession(session: Session): void {
        this._persist((prev) => [session, ...prev]);
    }

    public updateSessionTitle(id: string, title: string): void {
        this._persist((prev) => prev.map(s =>
            s.id === id ? { ...s, title } : s
        ));
    }

    public incrementMessageCount(id: string): void {
        this._persist((prev) => prev.map(s =>
            s.id === id ? { ...s, message_count: (s.message_count ?? 0) + 1 } : s
        ));
    }

    private _touch(id: string): void {
        this._persist((prev) => prev.map(s =>
            s.id === id ? { ...s, last_modified: new Date().toISOString() } : s
        ).sort((a, b) => b.last_modified.localeCompare(a.last_modified)));
    }

    private _persist(mutate: (prev: Session[]) => Session[]): void {
        const next = mutate(this.getSessions());
        void this._workspaceState.update(SESSIONS_KEY, next);
        this.refresh();
    }

    private _renderHtml(webview: vscode.Webview): string {
        const scriptUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'sidebar.js')
        );
        const styleUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'dist', 'sidebar.css')
        );
        const logoUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'logo.svg')
        );
        return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src ${webview.cspSource}; script-src ${webview.cspSource}; style-src ${webview.cspSource} 'unsafe-inline';" />
<title>AILIENANT Sessions</title>
<link rel="stylesheet" href="${styleUri}" />
</head>
<body>
<div id="root" data-logo="${logoUri}"></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
    }
}
