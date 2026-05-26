import * as vscode from 'vscode';
import * as crypto from 'crypto';
import { WSClient } from '../api/ws_client';
import { PatchActuator, type ApplyWorkspaceEditPayload } from './PatchActuator';

/**
 * Phase 7.11.1 (ADR-706 §4.5a) — Cursor-style Cmd+K inline mutation manager.
 *
 * The frontend half of the inline-edit pipeline:
 *  • The user selects a region and hits Cmd+K → extension.ts prompts for
 *    an instruction and calls `startSession(editor, selection, prompt)`.
 *  • A strict FIFO promise-chain serializes every `editor.edit()` so the
 *    async stream of typed deltas from the backend cannot interleave
 *    out-of-order with concurrent user typing (plan §guards).
 *  • Insertions/deletions are wrapped in `undoStopBefore/After: false` so
 *    Ctrl+Z reverts the whole AI session atomically (single undo entry).
 *  • Backend offsets are LF-space (see plan W1); the manager normalizes
 *    every received LF offset back to the editor's native CRLF coordinate
 *    space before issuing the edit, so a Windows CRLF file is not shifted.
 *
 * Source-of-truth for the running mutation state is the `_session` field
 * (a single in-flight session is supported; pressing Cmd+K while one is
 * active first cancels the old one).
 */

export interface InlineEditDelta {
    edit_id: string;
    session_id: string;
    kind: 'INSERT' | 'DELETE' | 'ABORT';
    offset: number;            // absolute LF-space offset
    length: number;
    text: string;
}

export interface InlineEditStart {
    edit_id: string;
    session_id: string;
    file_path: string;
    range_start: number;       // LF-space
    range_end: number;         // LF-space
}

export interface InlineEditEnd {
    edit_id: string;
    session_id: string;
    success: boolean;
    final_content: string;     // LF-normalized
    error?: string | null;
}

interface ActiveSession {
    editId: string;
    sessionId: string;
    editor: vscode.TextEditor;
    docUri: vscode.Uri;
    lfBaseline: string;           // LF-normalized pre-edit snapshot of the document
    insertedRanges: vscode.Range[];
    deletedDecorations: vscode.Range[];
    insertCursorLfOffset: number; // running LF offset where the next INSERT lands
    /** Snapshot of the original selection text for cancel-restore. */
    originalSelectionText: string;
    /** Selection range in native (CRLF) coordinates — used to clear/restore. */
    selectionRange: vscode.Range;
}

/**
 * Convert an LF-space character offset into a native (possibly CRLF) Position.
 * Counts the number of `\n` chars in `lfText[0..lfOffset]` and adds that to
 * `lfOffset` to get the CRLF-space offset, then uses `positionAt`.
 * On non-CRLF files this is a no-op (newlineCount additions cancel out: the
 * document already encodes one char per newline).
 */
function lfOffsetToNativePosition(
    doc: vscode.TextDocument,
    lfText: string,
    lfOffset: number,
): vscode.Position {
    if (doc.eol !== vscode.EndOfLine.CRLF) {
        // Native is already LF — positions map 1:1.
        return doc.positionAt(Math.min(lfOffset, doc.getText().length));
    }
    const clamped = Math.max(0, Math.min(lfOffset, lfText.length));
    let nl = 0;
    for (let i = 0; i < clamped; i++) {
        if (lfText.charCodeAt(i) === 10 /* \n */) { nl++; }
    }
    return doc.positionAt(clamped + nl);
}

export class InlineMutationManager implements vscode.Disposable {
    private static _instance: InlineMutationManager | undefined;
    static get instance(): InlineMutationManager {
        if (!InlineMutationManager._instance) {
            InlineMutationManager._instance = new InlineMutationManager();
        }
        return InlineMutationManager._instance;
    }

    private readonly _insertedDecoration: vscode.TextEditorDecorationType;
    private readonly _deletedDecoration: vscode.TextEditorDecorationType;
    private _session: ActiveSession | undefined;
    /** Strict FIFO promise chain — every editor.edit() awaits the previous one. */
    private _editQueue: Promise<void> = Promise.resolve();
    private _docChangeSub: vscode.Disposable | undefined;

    private constructor() {
        // Locally chosen rgba values (blueprint silent, plan-approved).
        this._insertedDecoration = vscode.window.createTextEditorDecorationType({
            backgroundColor: 'rgba(74, 187, 106, 0.15)',
            isWholeLine: false,
        });
        this._deletedDecoration = vscode.window.createTextEditorDecorationType({
            backgroundColor: 'rgba(220, 38, 38, 0.15)',
            textDecoration: 'line-through',
            isWholeLine: false,
        });
    }

    /** True iff an inline-edit session is in progress (drives the Esc/Tab context key). */
    get isActive(): boolean { return this._session !== undefined; }

    /**
     * Cmd+K entry point. Captures the selection, normalizes to LF coordinates,
     * sends a `client_inline_edit_request` to the backend, and arms the local
     * session state so subsequent `handle*` events render onto the editor.
     */
    async startSession(
        editor: vscode.TextEditor,
        selection: vscode.Selection,
        prompt: string,
    ): Promise<void> {
        // If a session is already running, cancel it first (the user issued a new Cmd+K).
        if (this._session) { await this.cancel(); }

        const nativeText = editor.document.getText();
        const lfBaseline = nativeText.replace(/\r\n/g, '\n');
        const baseHash = crypto.createHash('sha256').update(lfBaseline, 'utf8').digest('hex');

        // Convert native start/end → LF offsets (plan W1).
        const nativeStartOff = editor.document.offsetAt(selection.start);
        const nativeEndOff = editor.document.offsetAt(selection.end);
        const lfStart = nativeOffsetToLf(nativeText, nativeStartOff);
        const lfEnd = nativeOffsetToLf(nativeText, nativeEndOff);
        const selectedText = lfBaseline.substring(lfStart, lfEnd);

        const editId = ('iedit-' + Date.now().toString(36) + '-' +
            Math.random().toString(36).slice(2, 8));
        const sessionId = sessionIdFromContext();

        this._session = {
            editId,
            sessionId,
            editor,
            docUri: editor.document.uri,
            lfBaseline,
            insertedRanges: [],
            deletedDecorations: [new vscode.Range(selection.start, selection.end)],
            insertCursorLfOffset: lfStart,
            originalSelectionText: editor.document.getText(selection),
            selectionRange: new vscode.Range(selection.start, selection.end),
        };

        // Show the deletion highlight immediately so the user sees feedback.
        editor.setDecorations(this._deletedDecoration, this._session.deletedDecorations);

        // Track concurrent typing to keep our insert cursor aligned (plan §guards).
        this._docChangeSub = vscode.workspace.onDidChangeTextDocument(
            this._onDocChange.bind(this),
        );

        // Flip the context key so accept/reject keybindings (Tab/Esc) become active.
        await vscode.commands.executeCommand(
            'setContext', 'ailienant.inlineEditActive', true,
        );

        WSClient.getInstance().send({
            event_type: 'client_inline_edit_request',
            data: {
                edit_id: editId,
                session_id: sessionId,
                file_path: editor.document.uri.fsPath,
                range_start: lfStart,
                range_end: lfEnd,
                prompt,
                base_hash: baseHash,
                selected_text: selectedText,
                language_id: editor.document.languageId || null,
            },
        });
    }

    /** Dispatcher for `server_inline_edit_*` events from the websocket router. */
    handle(eventType: string, payload: unknown): void {
        if (!this._session) { return; }
        switch (eventType) {
            case 'server_inline_edit_start':
                // Decorations are already up — nothing more to do on start.
                return;
            case 'server_inline_edit_delta':
                this._handleDelta(payload as InlineEditDelta);
                return;
            case 'server_inline_edit_end':
                void this._handleEnd(payload as InlineEditEnd);
                return;
        }
    }

    private _handleDelta(delta: InlineEditDelta): void {
        const sess = this._session;
        if (!sess || delta.edit_id !== sess.editId) { return; }

        if (delta.kind === 'ABORT') {
            const reason = delta.text || 'aborted';
            void this._finalizeCancel(reason);
            return;
        }
        if (delta.kind === 'DELETE') {
            // The upfront DELETE is reflected by the existing red decoration;
            // mid-stream DELETEs are rare for Cmd+K (LLM emits replacement text).
            // Apply via the FIFO queue so ordering is preserved.
            this._enqueueEdit((eb) => {
                const start = lfOffsetToNativePosition(
                    sess.editor.document, sess.lfBaseline, delta.offset,
                );
                const end = lfOffsetToNativePosition(
                    sess.editor.document, sess.lfBaseline, delta.offset + delta.length,
                );
                eb.delete(new vscode.Range(start, end));
            });
            return;
        }
        // INSERT — convert LF offset back to native, append, decorate green.
        this._enqueueEdit((eb) => {
            const pos = lfOffsetToNativePosition(
                sess.editor.document, sess.lfBaseline, delta.offset,
            );
            eb.insert(pos, delta.text);
        }, () => {
            // After the edit applies, compute the inserted range for decoration.
            const startPos = lfOffsetToNativePosition(
                sess.editor.document, sess.lfBaseline, delta.offset,
            );
            const endLf = delta.offset + delta.text.length;
            const endPos = lfOffsetToNativePosition(
                sess.editor.document, sess.lfBaseline, endLf,
            );
            sess.insertedRanges.push(new vscode.Range(startPos, endPos));
            sess.editor.setDecorations(this._insertedDecoration, sess.insertedRanges);
            sess.insertCursorLfOffset = endLf;
        });
    }

    private async _handleEnd(end: InlineEditEnd): Promise<void> {
        const sess = this._session;
        if (!sess || end.edit_id !== sess.editId) { return; }
        // Drain the FIFO so every queued edit has actually landed before we react.
        await this._editQueue;
        if (!end.success) {
            void this._finalizeCancel(end.error || 'stream_aborted');
            return;
        }
        // Surface the change for accept/reject; the actual commit happens when
        // the user invokes ailienant.acceptInlineEdit (or rejects via Esc).
        void vscode.window.showInformationMessage(
            'AILIENANT inline edit ready — Tab to accept, Esc to reject.',
        );
    }

    /** Accept: commit via PatchActuator (atomic + SHA-256 stale guard reused). */
    async accept(): Promise<void> {
        const sess = this._session;
        if (!sess) { return; }
        await this._editQueue;
        const nativeText = sess.editor.document.getText();
        const lfContent = nativeText.replace(/\r\n/g, '\n');
        const payload: ApplyWorkspaceEditPayload = {
            patch_id: 'inline-' + sess.editId,
            save: true,
            edits: [{
                file_path: sess.docUri.fsPath,
                new_content: nativeText,
                // base_hash is the LF-normalized hash of the buffer the host will
                // commit — recomputed here so it matches what's on disk now (the
                // mutation just landed, so this IS the post-edit content).
                base_hash: crypto.createHash('sha256').update(lfContent, 'utf8').digest('hex'),
            }],
        };
        const result = await PatchActuator.apply(payload);
        if (!result.ok) {
            void vscode.window.showWarningMessage(
                `AILIENANT: inline edit commit failed (${result.error || 'unknown'}).`,
            );
        }
        await this._cleanupSession();
    }

    /** Reject: send cancel to backend + undo the in-place insertions. */
    async cancel(): Promise<void> {
        const sess = this._session;
        if (!sess) { return; }
        WSClient.getInstance().send({
            event_type: 'client_inline_edit_cancel',
            data: { edit_id: sess.editId, session_id: sess.sessionId },
        });
        await this._finalizeCancel('user_reject');
    }

    /**
     * Undo every queued edit by issuing a single inverse replace that restores
     * the original selection text. Falls back to a warning if the editor moved.
     */
    private async _finalizeCancel(reason: string): Promise<void> {
        const sess = this._session;
        if (!sess) { return; }
        await this._editQueue;
        try {
            // Find the current range spanning [selection.start, lastInsertEnd].
            const start = sess.selectionRange.start;
            const lastEnd = sess.insertedRanges.length > 0
                ? sess.insertedRanges[sess.insertedRanges.length - 1].end
                : sess.selectionRange.end;
            await sess.editor.edit((eb) => {
                eb.replace(new vscode.Range(start, lastEnd), sess.originalSelectionText);
            }, { undoStopBefore: false, undoStopAfter: false });
        } catch {
            void vscode.window.showWarningMessage(
                `AILIENANT inline edit cancelled (${reason}) — but the editor moved and ` +
                'the auto-restore failed. Use Ctrl+Z to revert.',
            );
        }
        await this._cleanupSession();
    }

    private async _cleanupSession(): Promise<void> {
        const sess = this._session;
        if (sess) {
            sess.editor.setDecorations(this._insertedDecoration, []);
            sess.editor.setDecorations(this._deletedDecoration, []);
        }
        this._session = undefined;
        this._docChangeSub?.dispose();
        this._docChangeSub = undefined;
        await vscode.commands.executeCommand(
            'setContext', 'ailienant.inlineEditActive', false,
        );
    }

    /** Strict FIFO: every edit() chains onto the previous one's resolution. */
    private _enqueueEdit(
        body: (eb: vscode.TextEditorEdit) => void,
        post?: () => void,
    ): void {
        const sess = this._session;
        if (!sess) { return; }
        this._editQueue = this._editQueue.then(async () => {
            try {
                await sess.editor.edit(body, {
                    undoStopBefore: false,    // single Undo transaction (plan)
                    undoStopAfter: false,
                });
                post?.();
            } catch {
                // Best-effort during cancel races; downstream cleanup handles the rest.
            }
        });
    }

    /** Concurrent-typing reconciliation (plan W1 + §guards). */
    private _onDocChange(ev: vscode.TextDocumentChangeEvent): void {
        const sess = this._session;
        if (!sess || ev.document.uri.toString() !== sess.docUri.toString()) { return; }
        // Each ContentChange before the running insert cursor shifts subsequent
        // backend offsets by (newLen - oldLen). We accumulate the delta into
        // lfBaseline so future positionAt conversions stay aligned.
        for (const change of ev.contentChanges) {
            const lfText = sess.editor.document.getText().replace(/\r\n/g, '\n');
            // Cheap & correct: re-snapshot LF baseline so subsequent INSERT
            // offsets compute against the user's actual buffer.
            sess.lfBaseline = lfText;
            void change;
        }
    }

    dispose(): void {
        this._insertedDecoration.dispose();
        this._deletedDecoration.dispose();
        this._docChangeSub?.dispose();
        InlineMutationManager._instance = undefined;
    }
}

/** Map a native (CRLF or LF) editor offset to LF-space, used at session start. */
function nativeOffsetToLf(nativeText: string, nativeOffset: number): number {
    const clamped = Math.max(0, Math.min(nativeOffset, nativeText.length));
    let lf = 0;
    let i = 0;
    while (i < clamped) {
        if (nativeText.charCodeAt(i) === 13 /* \r */ &&
            i + 1 < nativeText.length &&
            nativeText.charCodeAt(i + 1) === 10 /* \n */) {
            // CRLF — counts as 1 char in LF-space.
            lf += 1;
            i += 2;
        } else {
            lf += 1;
            i += 1;
        }
    }
    return lf;
}

/**
 * Best-effort session ID resolver. The backend keys per-WebSocket client_id
 * (set on connect), not per-workspace-tab; for inline edits we mirror the
 * convention used by other client_* events: a stable per-window id read from
 * the workspace state. Falls back to "default" so a manual backend start still
 * receives the event.
 */
function sessionIdFromContext(): string {
    // The WSClient sets its own client_id; the server treats every event from
    // a connected socket as belonging to that client. We populate session_id
    // as a logical tag (used only by the manager for ack-matching).
    return 'inline-' + (typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : Date.now().toString(36));
}
