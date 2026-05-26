/**
 * Phase 7.11.4 (ADR-706 §4.5d) — host-side workspace path index.
 *
 * In-memory trie of forward-slash-normalised relative paths, kept fresh by a
 * `vscode.workspace.createFileSystemWatcher('**\/\*')` debounced at 500 ms.
 * Used by:
 *   - `WORKSPACE_PATHS_QUERY` panel message — autocomplete top-N matches.
 *   - `SUBMIT_TASK` `@folder:` expansion — enumerate a folder's subtree
 *     (capped to keep the LLM context bounded).
 *
 * Reuses VS Code's `findFiles` exclude rules + `.gitignore` honour so we
 * don't reinvent ignore handling.
 */
import * as vscode from 'vscode';

/** Forward-slash, workspace-relative. */
export type RelPath = string;

export function normalizeRel(uri: vscode.Uri): RelPath {
    return vscode.workspace.asRelativePath(uri, false).replace(/\\/g, '/');
}

interface TrieNode {
    children: Map<string, TrieNode>;
    isFile: boolean;
}

function newNode(): TrieNode {
    return { children: new Map(), isFile: false };
}

export interface MentionItemHit {
    kind: 'file' | 'folder';
    path: RelPath;
}

/** Hard cap on how many files a single `@folder:` expansion contributes to
 *  the prompt — protects the context window from blowups on big trees. */
export const FOLDER_EXPANSION_CAP = 50;

/** Above this folder size we refuse to expand and let the caller emit a
 *  toast warning instead. (W3 in the plan.) */
export const FOLDER_EXPANSION_GIVE_UP = 200;

/** Default exclude glob applied at bootstrap — mirrors the historical
 *  MENTION_FILE quick-pick exclude so behaviour stays consistent. */
export const DEFAULT_EXCLUDE = '**/{node_modules,.git,dist,.venv,venv,__pycache__,.vscode-test}/**';

export class WorkspacePathIndex {
    private root: TrieNode = newNode();
    private size = 0;
    private watcher: vscode.FileSystemWatcher | null = null;

    private pendingAdds = new Set<RelPath>();
    private pendingDels = new Set<RelPath>();
    private flushTimer: NodeJS.Timeout | null = null;

    /** 500 ms debounce per ADR-706 §4.5d. */
    public static readonly DEBOUNCE_MS = 500;

    constructor(private readonly options: { debounceMs?: number } = {}) {}

    /** Insert a forward-slash, relative path. Intermediate folder nodes are
     *  marked as folder hits via `children.size > 0`; only the leaf carries
     *  `isFile = true`. */
    public insert(rel: RelPath): void {
        if (!rel) { return; }
        const parts = rel.split('/').filter(Boolean);
        if (parts.length === 0) { return; }
        let node = this.root;
        for (const p of parts) {
            let child = node.children.get(p);
            if (!child) {
                child = newNode();
                node.children.set(p, child);
            }
            node = child;
        }
        if (!node.isFile) {
            node.isFile = true;
            this.size += 1;
        }
    }

    /** Remove a path. Walks down keeping a stack so we can prune empty
     *  intermediates. Pure O(parts.length). */
    public remove(rel: RelPath): void {
        if (!rel) { return; }
        const parts = rel.split('/').filter(Boolean);
        const stack: { node: TrieNode; key: string }[] = [];
        let node = this.root;
        for (const p of parts) {
            const next = node.children.get(p);
            if (!next) { return; }
            stack.push({ node, key: p });
            node = next;
        }
        if (!node.isFile) { return; }
        node.isFile = false;
        this.size -= 1;
        // Prune empty branches bottom-up.
        for (let i = stack.length - 1; i >= 0; i--) {
            const { node: parent, key } = stack[i];
            const child = parent.children.get(key)!;
            if (!child.isFile && child.children.size === 0) {
                parent.children.delete(key);
            } else {
                break;
            }
        }
    }

    public getSize(): number {
        return this.size;
    }

    /** Returns up to `limit` matches whose path STARTS WITH `prefix`. Files
     *  before folders, alphabetical within each group. Trie descent is
     *  O(prefix.length); collection is O(limit). */
    public query(prefix: string, limit = 12): MentionItemHit[] {
        const norm = prefix.replace(/\\/g, '/').replace(/^\/+/, '');
        const parts = norm.split('/');
        // Walk to the deepest fully-matched node; the last (possibly partial)
        // segment is used as a filter against direct children.
        const lastPartial = parts.pop() ?? '';
        let node = this.root;
        let consumed = '';
        for (const p of parts) {
            const next = node.children.get(p);
            if (!next) { return []; }
            consumed += p + '/';
            node = next;
        }
        // Collect children of `node` whose key startsWith lastPartial.
        const fileHits: MentionItemHit[] = [];
        const folderHits: MentionItemHit[] = [];
        const childKeys = [...node.children.keys()]
            .filter(k => k.startsWith(lastPartial))
            .sort();
        for (const key of childKeys) {
            const child = node.children.get(key)!;
            const path = consumed + key;
            if (child.isFile) {
                fileHits.push({ kind: 'file', path });
            }
            if (child.children.size > 0) {
                folderHits.push({ kind: 'folder', path });
            }
            if (fileHits.length + folderHits.length >= limit) { break; }
        }
        return [...fileHits, ...folderHits].slice(0, limit);
    }

    /** Enumerate every file under `folderPrefix`. Returns up to `cap` paths.
     *  If the subtree exceeds `FOLDER_EXPANSION_GIVE_UP` files, returns
     *  `null` so the caller can emit a budget-warning toast instead of
     *  silently truncating. */
    public enumerateFolder(
        folderPrefix: string,
        cap = FOLDER_EXPANSION_CAP,
        giveUp = FOLDER_EXPANSION_GIVE_UP,
    ): RelPath[] | null {
        const norm = folderPrefix.replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
        const parts = norm.split('/').filter(Boolean);
        let node = this.root;
        for (const p of parts) {
            const next = node.children.get(p);
            if (!next) { return []; }
            node = next;
        }
        // BFS the subtree; stop early at `giveUp` to bail out.
        const out: RelPath[] = [];
        const stack: { node: TrieNode; path: string }[] = [{ node, path: norm }];
        let total = 0;
        while (stack.length > 0) {
            const { node: cur, path } = stack.pop()!;
            for (const [key, child] of cur.children) {
                const childPath = path ? `${path}/${key}` : key;
                if (child.isFile) {
                    total += 1;
                    if (total > giveUp) { return null; }
                    if (out.length < cap) { out.push(childPath); }
                }
                if (child.children.size > 0) {
                    stack.push({ node: child, path: childPath });
                }
            }
        }
        return out;
    }

    // ── File-system watcher integration ──────────────────────────────────

    /** Bootstrap from a one-shot `findFiles` scan + register a watcher. */
    public async bootstrap(): Promise<void> {
        // Snapshot up to 5,000 paths — caps memory at ~500 KB worst-case.
        const uris = await vscode.workspace.findFiles('**/*', DEFAULT_EXCLUDE, 5_000);
        for (const uri of uris) {
            this.insert(normalizeRel(uri));
        }
        if (this.watcher === null) {
            this.watcher = vscode.workspace.createFileSystemWatcher('**/*');
            this.watcher.onDidCreate(uri => this.enqueueAdd(normalizeRel(uri)));
            this.watcher.onDidDelete(uri => this.enqueueDel(normalizeRel(uri)));
            // VS Code surfaces renames as delete-then-create on this watcher.
        }
    }

    /** Wire a custom watcher (used by the test rig). */
    public attachWatcher(watcher: vscode.FileSystemWatcher): void {
        this.watcher = watcher;
        watcher.onDidCreate(uri => this.enqueueAdd(normalizeRel(uri)));
        watcher.onDidDelete(uri => this.enqueueDel(normalizeRel(uri)));
    }

    public enqueueAdd(rel: RelPath): void {
        this.pendingDels.delete(rel);
        this.pendingAdds.add(rel);
        this.scheduleFlush();
    }

    public enqueueDel(rel: RelPath): void {
        this.pendingAdds.delete(rel);
        this.pendingDels.add(rel);
        this.scheduleFlush();
    }

    private scheduleFlush(): void {
        if (this.flushTimer !== null) { return; }
        const delay = this.options.debounceMs ?? WorkspacePathIndex.DEBOUNCE_MS;
        this.flushTimer = setTimeout(() => this.flush(), delay);
    }

    /** Apply all pending updates atomically. Exposed for tests. */
    public flush(): void {
        if (this.flushTimer !== null) {
            clearTimeout(this.flushTimer);
            this.flushTimer = null;
        }
        for (const rel of this.pendingDels) { this.remove(rel); }
        for (const rel of this.pendingAdds) { this.insert(rel); }
        this.pendingAdds.clear();
        this.pendingDels.clear();
    }

    public dispose(): void {
        this.watcher?.dispose();
        this.watcher = null;
        if (this.flushTimer !== null) {
            clearTimeout(this.flushTimer);
            this.flushTimer = null;
        }
        this.pendingAdds.clear();
        this.pendingDels.clear();
    }
}

// ── Pure helpers used by SUBMIT_TASK extraction ────────────────────────────

const MENTION_RE = /@(file|folder|terminal)(?::([^\s]+))?/g;

/**
 * Extract `@file:`, `@folder:`, `@terminal` tokens from the prompt text and
 * resolve them to a flat list of forward-slash, relative file paths the
 * backend researcher can read directly.
 *
 * Behaviour:
 *   - `@file:path/to/x.ts`  → push `path/to/x.ts`.
 *   - `@folder:src/`        → enumerate the folder via the trie (capped at
 *                             `FOLDER_EXPANSION_CAP`); if the folder has more
 *                             than `FOLDER_EXPANSION_GIVE_UP` entries, emit a
 *                             warning toast and skip expansion.
 *   - `@terminal` (no path) → ignored here; the host opens the existing
 *                             ContextOverlay terminal tab via a separate
 *                             message flow.
 *
 * Deduplicated, order-preserving, returns an empty array when no mentions.
 */
export function extractMentions(
    text: string,
    index: WorkspacePathIndex,
    warnOversizeFolder?: (folder: string) => void,
): RelPath[] {
    const seen = new Set<RelPath>();
    const out: RelPath[] = [];
    const re = new RegExp(MENTION_RE.source, MENTION_RE.flags);
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
        const kind = m[1] as 'file' | 'folder' | 'terminal';
        const raw = m[2];
        if (kind === 'terminal') { continue; }
        if (!raw) { continue; }
        const rel = raw.replace(/\\/g, '/').replace(/\/+$/, '');
        if (kind === 'file') {
            if (!seen.has(rel)) { seen.add(rel); out.push(rel); }
            continue;
        }
        // folder
        const expanded = index.enumerateFolder(rel);
        if (expanded === null) {
            warnOversizeFolder?.(rel);
            continue;
        }
        for (const p of expanded) {
            if (!seen.has(p)) { seen.add(p); out.push(p); }
        }
    }
    return out;
}
