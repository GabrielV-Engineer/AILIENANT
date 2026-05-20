import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { WSClient } from './api/ws_client';

export interface CursorPosition {
    line: number;
    col: number;
}

export interface IdeSyncPayload {
    active_file: string;
    cursor_position: CursorPosition;
    selected_text: string;
    document_version_id: string;
}

/**
 * Parses .ailienantignore lines into an array of path patterns.
 * Supports simple glob prefixes: a plain "path" is treated as a prefix match.
 */
function loadIgnorePatterns(workspaceRoot: string): string[] {
    const ignorePath = path.join(workspaceRoot, '.ailienantignore');
    try {
        if (!fs.existsSync(ignorePath)) { return []; }
        return fs.readFileSync(ignorePath, 'utf8')
            .split('\n')
            .map(l => l.trim())
            .filter(l => l.length > 0 && !l.startsWith('#'));
    } catch {
        return [];
    }
}

function isFileBlocked(filePath: string, patterns: string[]): boolean {
    for (const pattern of patterns) {
        // Support leading ** glob and simple path prefix match
        const normalized = pattern.replace(/\\/g, '/');
        const normalizedFile = filePath.replace(/\\/g, '/');
        if (normalizedFile.includes(normalized)) {
            return true;
        }
    }
    return false;
}

export class IdeSync implements vscode.Disposable {
    private readonly _disposables: vscode.Disposable[] = [];
    private _debounceTimer: ReturnType<typeof setTimeout> | undefined;
    private static readonly DEBOUNCE_MS = 150;

    private _workspaceRoot: string | undefined;
    private _ignorePatterns: string[] = [];
    private _ignoreWatcher: vscode.FileSystemWatcher | undefined;

    constructor(
        private readonly _onFileBlocked: (blocked: boolean, filePath?: string) => void,
    ) {
        const folders = vscode.workspace.workspaceFolders;
        if (folders && folders.length > 0) {
            this._workspaceRoot = folders[0].uri.fsPath;
            this._ignorePatterns = loadIgnorePatterns(this._workspaceRoot);
            this._watchIgnoreFile();
        }

        this._disposables.push(
            vscode.window.onDidChangeActiveTextEditor(() => this._scheduleSync()),
            vscode.window.onDidChangeTextEditorSelection(() => this._scheduleSync()),
            vscode.window.onDidChangeTextEditorVisibleRanges(() => this._scheduleSync()),
            vscode.workspace.onDidChangeTextDocument(() => this._scheduleSync()),
        );
    }

    private _watchIgnoreFile(): void {
        if (!this._workspaceRoot) { return; }
        const pattern = new vscode.RelativePattern(this._workspaceRoot, '.ailienantignore');
        this._ignoreWatcher = vscode.workspace.createFileSystemWatcher(pattern);
        this._ignoreWatcher.onDidChange(() => this._reloadIgnore());
        this._ignoreWatcher.onDidCreate(() => this._reloadIgnore());
        this._ignoreWatcher.onDidDelete(() => { this._ignorePatterns = []; });
    }

    private _reloadIgnore(): void {
        if (this._workspaceRoot) {
            this._ignorePatterns = loadIgnorePatterns(this._workspaceRoot);
            this._scheduleSync();
        }
    }

    private _scheduleSync(): void {
        if (this._debounceTimer !== undefined) {
            clearTimeout(this._debounceTimer);
        }
        this._debounceTimer = setTimeout(() => this._doSync(), IdeSync.DEBOUNCE_MS);
    }

    private _doSync(): void {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            this._onFileBlocked(false);
            return;
        }

        const filePath = editor.document.uri.fsPath;

        // Privacy gate: check .ailienantignore
        if (this._ignorePatterns.length > 0 && isFileBlocked(filePath, this._ignorePatterns)) {
            this._onFileBlocked(true, filePath);
            return;
        }
        this._onFileBlocked(false);

        const selection = editor.selection;
        const payload: IdeSyncPayload = {
            active_file:        filePath,
            cursor_position:    { line: selection.active.line, col: selection.active.character },
            selected_text:      editor.document.getText(selection.isEmpty ? undefined : selection),
            document_version_id: String(editor.document.version),
        };

        WSClient.getInstance().send({
            event_type: 'client_file_update',
            data: {
                filepath:            payload.active_file,
                content:             '',
                document_version_id: payload.document_version_id,
            },
        });

        // Also broadcast to webview via a lightweight context event
        // (handled in chat_sidebar.ts via sendMessageToWebview)
        this._lastPayload = payload;
    }

    public getLastPayload(): IdeSyncPayload | undefined {
        return this._lastPayload;
    }

    private _lastPayload: IdeSyncPayload | undefined;

    public dispose(): void {
        if (this._debounceTimer !== undefined) {
            clearTimeout(this._debounceTimer);
        }
        this._ignoreWatcher?.dispose();
        this._disposables.forEach(d => d.dispose());
    }
}
