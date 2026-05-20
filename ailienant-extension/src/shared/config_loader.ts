import * as vscode from 'vscode';
import type { AilienantConfig } from './types';

const CONFIG_FILENAME = 'ailienant-config.json';

export class ConfigLoader implements vscode.Disposable {
    private _config: AilienantConfig | null = null;
    private _watcher: vscode.FileSystemWatcher | undefined;
    private readonly _onChange = new vscode.EventEmitter<AilienantConfig | null>();
    readonly onChange = this._onChange.event;

    constructor() {
        this._load();
        const root = this._workspaceRoot();
        if (root) {
            this._watcher = vscode.workspace.createFileSystemWatcher(
                new vscode.RelativePattern(root, CONFIG_FILENAME)
            );
            this._watcher.onDidChange(() => this._load());
            this._watcher.onDidCreate(() => this._load());
            this._watcher.onDidDelete(() => this._set(null));
        }
    }

    get current(): AilienantConfig | null {
        return this._config;
    }

    dispose(): void {
        this._watcher?.dispose();
        this._onChange.dispose();
    }

    private _workspaceRoot(): vscode.Uri | undefined {
        const folders = vscode.workspace.workspaceFolders;
        return folders && folders.length > 0 ? folders[0].uri : undefined;
    }

    private async _load(): Promise<void> {
        const root = this._workspaceRoot();
        if (!root) { this._set(null); return; }
        const uri = vscode.Uri.joinPath(root, CONFIG_FILENAME);
        try {
            const bytes = await vscode.workspace.fs.readFile(uri);
            const text = new TextDecoder('utf-8').decode(bytes);
            const parsed = JSON.parse(text) as AilienantConfig;
            if (this._isValid(parsed)) {
                this._set(parsed);
            } else {
                this._set(null);
            }
        } catch {
            this._set(null);
        }
    }

    private _isValid(c: unknown): c is AilienantConfig {
        if (!c || typeof c !== 'object') { return false; }
        const obj = c as Record<string, unknown>;
        if (typeof obj.engine_endpoint !== 'string') { return false; }
        if (!obj.agent_settings || typeof (obj.agent_settings as Record<string, unknown>).analyst_name !== 'string') {
            return false;
        }
        if (!obj.tiers) { return false; }
        const t = obj.tiers as Record<string, unknown>;
        return typeof t.small === 'string' && typeof t.medium === 'string' && typeof t.big === 'string';
    }

    private _set(c: AilienantConfig | null): void {
        this._config = c;
        this._onChange.fire(c);
    }
}
