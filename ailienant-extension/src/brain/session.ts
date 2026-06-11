import * as vscode from 'vscode';
import { VFSReader } from '../editor/vfs_reader';
import { APIClient, TaskPayload } from '../api/api_client';
import { PathResolver } from '../core/PathResolver';
import { WSClient } from '../api/ws_client';

// Phase 7.12.9 (Fix 3) — hard cap on active-file content forwarded to the Planner.
// ~10k chars ≈ ~2.5k tokens: stays within ADR-703's context budget and prevents an
// OOM token-bomb that would crash local 8k–128k context windows.
const ACTIVE_FILE_CHAR_CAP = 10_000;

export class SessionManager {
    // One instance per session id — each panel drives its own session over the
    // shared WSClient singleton. (Was a process-wide singleton, which collapsed
    // every panel onto one backend session and broke per-session routing.)
    private static readonly _instances = new Map<string, SessionManager>();
    private readonly sessionId: string;

    // OCC — version snapshot captured at task submission (Phase 1.5).
    // Keyed by absolute file path; cleared on new task submission.
    private versionSnapshot: Map<string, number> = new Map();

    private constructor(sessionId: string) {
        this.sessionId = sessionId;
        // React to THIS session's graph mutations only — the WSClient demuxes by
        // session id, so the OCC check fires for this session's writes alone.
        WSClient.getInstance().onMessage(this.sessionId, this._onWSMessage.bind(this));
    }

    /**
     * Factory cache: returns the SessionManager for ``sessionId`` (creating it on
     * first use). All instances share the single WSClient connection; each injects
     * its own session id into submit/abort so the backend inherits the panel's id.
     */
    public static forSession(sessionId: string): SessionManager {
        let inst = SessionManager._instances.get(sessionId);
        if (!inst) {
            inst = new SessionManager(sessionId);
            SessionManager._instances.set(sessionId, inst);
        }
        return inst;
    }

    /**
     * 7.9.A.5 — Open the quantum tunnel (idempotent) and announce the workspace so
     * the backend starts (or resumes) the lazy GraphRAG indexer. Safe to call on
     * every session/panel open as well as before a task.
     */
    public ensureConnected(): void {
        const wsClient = WSClient.getInstance();
        wsClient.ensureConnected();
        // Announce this session on the shared socket so the backend aliases it and
        // routes its events here (re-announced automatically on every reconnect).
        wsClient.registerSession(this.sessionId);

        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (workspaceRoot) {
            wsClient.sendWhenReady({
                event_type: 'client_workspace_init',
                data: {
                    workspace_root: workspaceRoot,
                    project_id: PathResolver.resolveProjectId(),
                    workspace_pid: process.pid,
                },
            });
        }
    }

    /**
     * Inicia una misión cognitiva.
     * Orquesta la recolección de entropía, abre el túnel cuántico y despacha el WBS.
     *
     * @param taskPrompt The user prompt text.
     * @param opts Optional UI-layer injections.
     *   - `explicit_mentions` (Phase 7.11.4 — ADR-706 §4.5d) hard-context file
     *     paths extracted from `@file:` / `@folder:` tokens. The backend
     *     researcher reads these verbatim via VFSMiddleware and skips the
     *     GraphRAG retrieval entirely for this turn.
     */
    public async startAITask(
        taskPrompt: string,
        opts?: { explicit_mentions?: string[]; enable_native_thinking?: boolean; planner_mode_active?: boolean; execution_mode?: string; invoked_skill_id?: string },
    ): Promise<number | undefined> {
        try {
            // 1. Asegurar el canal de Oídos (WebSockets) ANTES de hablar.
            // Si lo hacemos al revés, podríamos perder los primeros tokens de LangGraph.
            // ensureConnected also (re)announces the workspace to keep the indexer alive.
            this.ensureConnected();

            // 2. Extraer la Entropía Visual (Ojos) del VFS
            const dirtyBuffers = VFSReader.captureEntropy();

            // 3. OCC: snapshot active document version before sending.
            this.versionSnapshot.clear();
            const activeDoc = vscode.window.activeTextEditor?.document;
            if (activeDoc) {
                this.versionSnapshot.set(activeDoc.uri.fsPath, activeDoc.version);
            }

            // 3b. Phase 7.12.9 (Fix 3) — dynamic IDE context. The active tab may be
            // SAVED (so it never appears in dirty_buffers), yet it is exactly what
            // the user is looking at. Forward the real workspace root + the focused
            // file (truncated to the hard char cap) so the Planner anchors on it
            // instead of hallucinating from the stale RAG index.
            const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
            let activeFilePath: string | undefined;
            let activeFileContent: string | undefined;
            if (activeDoc && activeDoc.uri.scheme === 'file') {
                activeFilePath = activeDoc.uri.fsPath;
                const fullText = activeDoc.getText();
                activeFileContent = fullText.length > ACTIVE_FILE_CHAR_CAP
                    ? fullText.substring(0, ACTIVE_FILE_CHAR_CAP) + '\n...[CONTENT TRUNCATED DUE TO CONTEXT LIMITS]...'
                    : fullText;
            }

            // 4. Preparar el Contrato (Payload)
            const payload: TaskPayload = {
                task_prompt: taskPrompt,
                dirty_buffers: dirtyBuffers,
                project_id: PathResolver.resolveProjectId(),
                workspace_root: workspaceRoot,
                active_file_path: activeFilePath,
                active_file_content: activeFileContent,
                document_version_id: activeDoc ? String(activeDoc.version) : undefined,
                // Per-submit idempotency key so the backend dedups a reconnect-driven
                // resubmit (never two generations for one user action).
                request_id: (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
                    ? crypto.randomUUID()
                    : `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`,
                // Phase 7.11.4 — wires the host-extracted @mentions through to the
                // researcher's existing RAG-bypass path (agents/researcher.py:78).
                explicit_mentions:
                    opts?.explicit_mentions && opts.explicit_mentions.length > 0
                        ? opts.explicit_mentions
                        : undefined,
                // Phase 9 (ADR-707) — forward the persisted Native Thinking
                // toggle so the backend gateway appends/omits the thinking config.
                enable_native_thinking: opts?.enable_native_thinking,
                // When the Planner surface is active, route this turn into the
                // backend Socratic ideation loop instead of autonomous planning.
                planner_mode_active: opts?.planner_mode_active,
                // The mode selector maps to the backend session permission policy
                // (Auto → auto-apply, Ask → HITL card, Plan → block mutations).
                execution_mode: opts?.execution_mode,
                // Snake_case end-to-end — must not be camelCased at the call site.
                invoked_skill_id: opts?.invoked_skill_id,
            };

            // 5. Emitir la Misión (Boca) al API Gateway
            const apiClient = APIClient.getInstance();

            // Phase 7.12 — analysis start is already signalled by the streaming UI;
            // a host toast on every directive spams the VS Code event loop.
            console.debug(`[SessionManager] Analyzing directive with ${dirtyBuffers.length} buffers in context...`);

            // Disparamos la petición. Si falla, el catch local lo maneja.
            // The 202 carries `stream_watchdog_ms` — the backend-governed timeout the
            // UI arms its stall watchdog with (longer for slow local engines).
            const ack = await apiClient.submitTask(this.sessionId, payload) as
                { stream_watchdog_ms?: number } | undefined;
            const ms = ack?.stream_watchdog_ms;
            return typeof ms === 'number' ? ms : undefined;

        } catch (error: any) {
            if (error.name !== 'AbortError') {
                vscode.window.showErrorMessage(`AILIENANT: Colapso en la red neuronal. Verifica la conexión con el core.`);
                console.error("[SessionManager] Error de orquestación:", error);
            }
        }
    }

    /**
     * Botón de Pánico (Human-In-The-Loop)
     * Aborta la petición HTTP en vuelo.
     */
    public abortCurrentTask(): void {
        APIClient.getInstance().cancelTask(this.sessionId);
        vscode.window.showWarningMessage("AILIENANT: Mission aborted by user.");
    }

    // Exponemos el SessionID por si la UI lo necesita para renderizar algo
    public getSessionId(): string {
        return this.sessionId;
    }

    // -------------------------------------------------------------------------
    // OCC — Optimistic Concurrency Control (Phase 1.5)
    // -------------------------------------------------------------------------

    private _onWSMessage(msg: any): void {
        if (msg?.event_type === 'server_graph_mutation') {
            this._checkOCC();
        }
    }

    private _checkOCC(): void {
        const editor = vscode.window.activeTextEditor;
        if (!editor) { return; }
        const doc = editor.document;
        const storedVersion = this.versionSnapshot.get(doc.uri.fsPath);

        if (storedVersion !== undefined && doc.version !== storedVersion) {
            vscode.window.showWarningMessage(
                `AILIENANT: Concurrency conflict on ${doc.fileName} — file was edited during inference. Aborting write.`
            );
            WSClient.getInstance().send({
                event_type: 'client_concurrency_conflict',
                data: {
                    filepath: doc.uri.fsPath,
                    expected_version: storedVersion,
                    actual_version: doc.version,
                },
            });
            // Clear snapshot so the conflict fires only once per mutation event
            this.versionSnapshot.delete(doc.uri.fsPath);
        }
    }
}
