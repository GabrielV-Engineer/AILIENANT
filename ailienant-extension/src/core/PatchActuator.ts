import * as vscode from 'vscode';
import * as path from 'path';
import * as crypto from 'crypto';
import { applyPatch } from 'diff';
import type { ASTToken } from '../shared/config';

interface WorkspaceEditItem {
    file_path: string;
    // O(Δ) transport (DEBT-024): the HITL preview carries a server-computed unified
    // diff and the host reconstructs the new side via applyPatch. The apply-write
    // path still carries full new_content. Exactly one of the two is populated.
    unified_diff?: string;
    new_content?: string;
    base_hash?: string | null;
}

export interface ApplyWorkspaceEditPayload {
    patch_id: string;
    save?: boolean;
    edits: WorkspaceEditItem[];
}

/**
 * Per-file diff source surfaced to the webview for inline rendering. Built from
 * data the actuator already holds — the document's pre-edit text and the
 * incoming new_content — so no separate backend round-trip is needed. Both
 * sides are EOL-normalized to '\n' so the client-side diff is stable across
 * CRLF / cp1252-origin files.
 */
export interface PatchedFileDiff {
    file_path: string;
    old_content: string;
    new_content: string;
    status: 'edit' | 'create';
    // Column-aligned host-tokenized spans for syntax-highlighted rendering, mirrored
    // from DiffBlockShape. Absent until the host grammar engine populates them; when
    // it does, it must set these explicitly on the pushed diff literal so the own
    // property survives the postMessage structured-clone boundary to the webview.
    old_ast_lines?: ASTToken[][];
    new_ast_lines?: ASTToken[][];
}

export interface PatchAppliedResult {
    patch_id: string;
    ok: boolean;
    applied_files: string[];
    stale_files: string[];
    error?: string;
    // Populated only on the success path. Display-only: the core reads just the
    // applied/stale fields, so this rides along the existing ack harmlessly and
    // also seeds the webview's inline diff.
    diffs?: PatchedFileDiff[];
}

/**
 * Phase 7.9.B.18 — Enterprise Write Pipeline actuator.
 *
 * The ONLY component that writes approved patches to disk. It runs in the
 * extension host (the webview cannot call the VS Code API) and uses
 * vscode.workspace.applyEdit + save() so the change is undoable with native
 * Ctrl+Z and tracked by VS Code Local History. A hash-based stale guard blocks
 * the whole set if any target changed since the patch was proposed — we never
 * clobber the user's edits.
 */
export class PatchActuator {
    /** Collapse CRLF/CR to LF so hashing and client-side diffing are EOL-stable. */
    private static _normalizeEol(text: string): string {
        return text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    }

    /** SHA-256 over EOL-normalized text — must match the Python coder's content_hash. */
    private static _hash(text: string): string {
        return crypto.createHash('sha256').update(PatchActuator._normalizeEol(text), 'utf8').digest('hex');
    }

    private static _resolveUri(filePath: string): vscode.Uri {
        if (path.isAbsolute(filePath)) {
            return vscode.Uri.file(filePath);
        }
        const root = vscode.workspace.workspaceFolders?.[0]?.uri;
        return root ? vscode.Uri.joinPath(root, filePath) : vscode.Uri.file(filePath);
    }

    /**
     * Resolve the post-edit content for an item against its live old side.
     *
     * Returns full new_content verbatim when the backend shipped it (the apply
     * path). Otherwise reconstructs it from the O(Δ) unified diff (the HITL
     * preview): applyPatch returns `false` when the diff's context does not match
     * the host's old side (server-old vs host-old drift), which we surface as
     * `null` so callers degrade to a stale-file notice rather than write/show
     * corrupt content. A normalized old side keeps the match EOL-stable.
     */
    private static _effectiveNewContent(item: WorkspaceEditItem, oldContent: string): string | null {
        if (typeof item.new_content === 'string') {
            return item.new_content;
        }
        if (typeof item.unified_diff === 'string') {
            const patched = applyPatch(PatchActuator._normalizeEol(oldContent), item.unified_diff);
            return patched === false ? null : patched;
        }
        return null;
    }

    private static async _openExisting(uri: vscode.Uri): Promise<vscode.TextDocument | null> {
        try {
            return await vscode.workspace.openTextDocument(uri);
        } catch {
            return null;  // file does not exist yet → treated as a new-file create
        }
    }

    /**
     * Build the both-sides diff for a set of proposed edits WITHOUT touching the
     * workspace — used to render the inline diff in-chat while a HITL approval is
     * pending. Reads each file's current text (the old side) and pairs it with the
     * incoming new_content; a file that does not exist yet is a 'create'. No
     * WorkspaceEdit, no save: this is read-only. The actual write still goes
     * through `apply` once the user authorizes.
     */
    static async preview(edits: WorkspaceEditItem[]): Promise<PatchedFileDiff[]> {
        const diffs: PatchedFileDiff[] = [];
        for (const item of edits) {
            // Defensive per file: a single unreadable target (or a malformed
            // payload field) must not sink the whole preview — that would strand
            // the pending approval. Coerce to strings up front and degrade a read
            // failure to an empty old side rather than throwing.
            const filePath = typeof item.file_path === 'string' ? item.file_path : String(item.file_path ?? '');
            try {
                const uri = PatchActuator._resolveUri(filePath);
                const doc = await PatchActuator._openExisting(uri);
                const oldContent = doc ? doc.getText() : '';
                const newContent = PatchActuator._effectiveNewContent(item, oldContent);
                if (newContent === null) {
                    // Diff could not be reconstructed against the live old side
                    // (drift). Skip this file's inline render rather than show a
                    // corrupt diff — the approval card still appears, and the
                    // apply path's base_hash guard catches the drift definitively.
                    continue;
                }
                diffs.push({
                    file_path: filePath,
                    old_content: PatchActuator._normalizeEol(oldContent),
                    new_content: PatchActuator._normalizeEol(newContent),
                    status: doc ? 'edit' : 'create',
                });
            } catch {
                // Could not resolve/read the file — reconstruct against an empty
                // base and show it as a create so the user still sees (and can
                // authorize) the change. If even that fails to apply, skip its render.
                const newContent = PatchActuator._effectiveNewContent(item, '');
                if (newContent === null) {
                    continue;
                }
                diffs.push({
                    file_path: filePath,
                    old_content: '',
                    new_content: PatchActuator._normalizeEol(newContent),
                    status: 'create',
                });
            }
        }
        return diffs;
    }

    static async apply(payload: ApplyWorkspaceEditPayload): Promise<PatchAppliedResult> {
        const result: PatchAppliedResult = {
            patch_id: payload.patch_id,
            ok: false,
            applied_files: [],
            stale_files: [],
        };

        try {
            const edit = new vscode.WorkspaceEdit();
            const toSave: vscode.Uri[] = [];

            // Pass 1 — resolve + stale guard for every file (atomic: any stale aborts all).
            // The effective new side is resolved here too: full new_content when the
            // backend shipped it, else reconstructed from the unified diff. A diff that
            // cannot be reconstructed against the live old side is a drift — treated as
            // stale so the whole set aborts rather than writing corrupt content.
            const resolved: Array<{ uri: vscode.Uri; doc: vscode.TextDocument | null; item: WorkspaceEditItem; newContent: string }> = [];
            for (const item of payload.edits) {
                const uri = PatchActuator._resolveUri(item.file_path);
                const doc = await PatchActuator._openExisting(uri);
                const oldContent = doc ? doc.getText() : '';
                const currentHash = doc ? PatchActuator._hash(doc.getText()) : PatchActuator._hash('');
                // A real base_hash is always a 64-char sha256 (never empty); skip the
                // guard only when it's genuinely absent.
                if (item.base_hash && currentHash !== item.base_hash) {
                    result.stale_files.push(item.file_path);
                    resolved.push({ uri, doc, item, newContent: '' });
                    continue;
                }
                const newContent = PatchActuator._effectiveNewContent(item, oldContent);
                if (newContent === null) {
                    result.stale_files.push(item.file_path);
                    resolved.push({ uri, doc, item, newContent: '' });
                    continue;
                }
                resolved.push({ uri, doc, item, newContent });
            }

            if (result.stale_files.length > 0) {
                void vscode.window.showWarningMessage(
                    `AILIENANT: not applied — ${result.stale_files.length} file(s) changed since the patch was proposed: ${result.stale_files.join(', ')}.`
                );
                return result;  // ok stays false
            }

            // Pass 2 — build one atomic WorkspaceEdit (create new files, full-range replace existing).
            // The pre-edit text (doc.getText()) is captured here for the inline diff the
            // webview renders — the actuator is the only place that holds both sides.
            const diffs: PatchedFileDiff[] = [];
            for (const { uri, doc, item, newContent } of resolved) {
                const oldContent = doc ? doc.getText() : '';
                if (!doc) {
                    edit.createFile(uri, { ignoreIfExists: true });
                    edit.insert(uri, new vscode.Position(0, 0), newContent);
                } else {
                    const fullRange = new vscode.Range(
                        new vscode.Position(0, 0),
                        doc.lineAt(Math.max(doc.lineCount - 1, 0)).range.end
                    );
                    edit.replace(uri, fullRange, newContent);
                }
                diffs.push({
                    file_path: item.file_path,
                    old_content: PatchActuator._normalizeEol(oldContent),
                    new_content: PatchActuator._normalizeEol(newContent),
                    status: doc ? 'edit' : 'create',
                });
                toSave.push(uri);
                result.applied_files.push(item.file_path);
            }

            const applied = await vscode.workspace.applyEdit(edit);
            if (!applied) {
                result.applied_files = [];
                result.error = 'VS Code rejected the workspace edit.';
                return result;
            }

            // Save to disk (true physical write) when requested.
            if (payload.save !== false) {
                for (const uri of toSave) {
                    const d = await vscode.workspace.openTextDocument(uri);
                    if (d.isDirty) { await d.save(); }
                }
            }

            result.ok = true;
            result.diffs = diffs;
            return result;
        } catch (err: unknown) {
            result.ok = false;
            result.applied_files = [];
            result.error = err instanceof Error ? err.message : String(err);
            return result;
        }
    }
}
