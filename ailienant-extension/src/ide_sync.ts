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

type LifecycleAction = 'file_saved' | 'file_created' | 'file_renamed';

/** A queued file-lifecycle push awaiting the debounced, privacy-gated flush. */
type PendingPush =
    | { kind: 'telemetry'; action: LifecycleAction; filepath: string; oldPath?: string; documentVersionId?: string }
    | { kind: 'delete'; filepath: string };

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

function loadRulesExcludePatterns(workspaceRoot: string): string[] {
    const cfgPath = path.join(workspaceRoot, '.ailienant', '.ailienant.json');
    try {
        if (!fs.existsSync(cfgPath)) { return []; }
        const raw = JSON.parse(fs.readFileSync(cfgPath, 'utf8')) as Record<string, unknown>;
        const patterns = raw['exclude_patterns'];
        if (!Array.isArray(patterns)) { return []; }
        return patterns.filter((p): p is string => typeof p === 'string');
    } catch {
        return [];
    }
}

export class IdeSync implements vscode.Disposable {
    private readonly _disposables: vscode.Disposable[] = [];
    private _debounceTimer: ReturnType<typeof setTimeout> | undefined;
    private _lifecycleTimer: ReturnType<typeof setTimeout> | undefined;
    private _pendingLifecycle: PendingPush[] = [];
    private static readonly DEBOUNCE_MS = 150;

    private _workspaceRoot: string | undefined;
    private _ignorePatterns: string[] = [];
    private _ignoreWatcher: vscode.FileSystemWatcher | undefined;
    private _rulesExcludePatterns: string[] = [];
    private _rulesConfigWatcher: vscode.FileSystemWatcher | undefined;
    private _incognito: boolean = false;

    constructor(
        private readonly _onFileBlocked: (blocked: boolean, filePath?: string) => void,
    ) {
        const folders = vscode.workspace.workspaceFolders;
        if (folders && folders.length > 0) {
            this._workspaceRoot = folders[0].uri.fsPath;
            this._ignorePatterns = loadIgnorePatterns(this._workspaceRoot);
            this._watchIgnoreFile();
            this._rulesExcludePatterns = loadRulesExcludePatterns(this._workspaceRoot);
            this._watchRulesConfig();
        }

        this._disposables.push(
            vscode.window.onDidChangeActiveTextEditor(() => this._scheduleSync()),
            vscode.window.onDidChangeTextEditorSelection(() => this._scheduleSync()),
            vscode.window.onDidChangeTextEditorVisibleRanges(() => this._scheduleSync()),
            vscode.workspace.onDidChangeTextDocument(() => this._scheduleSync()),
        );

        // File-lifecycle bus: save / create / rename emit the silent
        // client_ide_telemetry channel; delete wires the client_file_delete purge.
        // All four coalesce through the debounced, privacy-gated flush below.
        this._disposables.push(
            vscode.workspace.onDidSaveTextDocument(doc => {
                if (doc.uri.scheme !== 'file') { return; }  // skip untitled/output buffers
                this._enqueueLifecycle({
                    kind: 'telemetry',
                    action: 'file_saved',
                    filepath: doc.uri.fsPath,
                    documentVersionId: String(doc.version),
                });
            }),
            vscode.workspace.onDidCreateFiles(e => {
                for (const f of e.files) {
                    this._enqueueLifecycle({ kind: 'telemetry', action: 'file_created', filepath: f.fsPath });
                }
            }),
            vscode.workspace.onDidRenameFiles(e => {
                for (const { oldUri, newUri } of e.files) {
                    this._enqueueLifecycle({
                        kind: 'telemetry',
                        action: 'file_renamed',
                        filepath: newUri.fsPath,
                        oldPath: oldUri.fsPath,
                    });
                }
            }),
            vscode.workspace.onDidDeleteFiles(e => {
                for (const f of e.files) {
                    this._enqueueLifecycle({ kind: 'delete', filepath: f.fsPath });
                }
            }),
        );
    }

    public setIncognito(value: boolean): void {
        this._incognito = value;
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

    private _watchRulesConfig(): void {
        if (!this._workspaceRoot) { return; }
        const pattern = new vscode.RelativePattern(this._workspaceRoot, '.ailienant/.ailienant.json');
        this._rulesConfigWatcher = vscode.workspace.createFileSystemWatcher(pattern);
        this._rulesConfigWatcher.onDidChange(() => this._reloadRulesConfig());
        this._rulesConfigWatcher.onDidCreate(() => this._reloadRulesConfig());
        this._rulesConfigWatcher.onDidDelete(() => { this._rulesExcludePatterns = []; });
    }

    private _reloadRulesConfig(): void {
        if (this._workspaceRoot) {
            this._rulesExcludePatterns = loadRulesExcludePatterns(this._workspaceRoot);
            this._scheduleSync();
        }
    }

    private _scheduleSync(): void {
        if (this._debounceTimer !== undefined) {
            clearTimeout(this._debounceTimer);
        }
        this._debounceTimer = setTimeout(() => this._doSync(), IdeSync.DEBOUNCE_MS);
    }

    /**
     * Privacy Gate (shared with _doSync): a path is allowed only if it matches
     * neither the .ailienantignore patterns nor the resolved .ailienant.json
     * exclusion patterns. The single source of truth for "may this leave the IDE".
     */
    private _isPathAllowed(filePath: string): boolean {
        if (this._ignorePatterns.length > 0 && isFileBlocked(filePath, this._ignorePatterns)) {
            return false;
        }
        if (this._rulesExcludePatterns.length > 0 && isFileBlocked(filePath, this._rulesExcludePatterns)) {
            return false;
        }
        return true;
    }

    private _enqueueLifecycle(ev: PendingPush): void {
        this._pendingLifecycle.push(ev);
        if (this._lifecycleTimer !== undefined) {
            clearTimeout(this._lifecycleTimer);
        }
        this._lifecycleTimer = setTimeout(() => this._flushLifecycle(), IdeSync.DEBOUNCE_MS);
    }

    /**
     * Flush coalesced lifecycle events onto the silent bus. Each push is gated
     * by the Privacy Gate before leaving the extension; Incognito pauses the
     * whole bus. No toast, no chat side-effect — telemetry is invisible.
     */
    private _flushLifecycle(): void {
        const pending = this._pendingLifecycle;
        this._pendingLifecycle = [];
        if (this._incognito) { return; }  // bus paused — drop silently

        const client = WSClient.getInstance();
        for (const ev of pending) {
            if (ev.kind === 'delete') {
                // A deleted excluded file was never indexed and announcing it
                // would leak that the path existed — gate it like any push.
                if (!this._isPathAllowed(ev.filepath)) { continue; }
                client.sendTelemetry({
                    event_type: 'client_file_delete',
                    data: { filepath: ev.filepath, project_id: '' },
                });
                continue;
            }
            // Telemetry: gate the (new) path; a rename must also clear the OLD
            // path — if EITHER side is excluded, discard the whole event so a
            // rename across the privacy boundary never leaks the excluded path.
            if (!this._isPathAllowed(ev.filepath)) { continue; }
            if (ev.action === 'file_renamed' && (!ev.oldPath || !this._isPathAllowed(ev.oldPath))) {
                continue;
            }
            client.sendTelemetry({
                event_type: 'client_ide_telemetry',
                data: {
                    action: ev.action,
                    filepath: ev.filepath,
                    old_path: ev.oldPath ?? null,
                    document_version_id: ev.documentVersionId ?? '',
                },
            });
        }
    }

    private _doSync(): void {
        if (this._incognito) { return; }

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

        // Privacy gate: check exclude_patterns from .ailienant/.ailienant.json
        if (this._rulesExcludePatterns.length > 0 && isFileBlocked(filePath, this._rulesExcludePatterns)) {
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
        if (this._lifecycleTimer !== undefined) {
            clearTimeout(this._lifecycleTimer);
        }
        this._ignoreWatcher?.dispose();
        this._rulesConfigWatcher?.dispose();
        this._disposables.forEach(d => d.dispose());
    }
}
