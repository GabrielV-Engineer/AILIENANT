import * as vscode from 'vscode';
import { VFSReader } from '../editor/vfs_reader';
import { APIClient, TaskPayload } from '../api/api_client';
import { PathResolver } from '../core/PathResolver';
import { WSClient } from '../api/ws_client';

export class SessionManager {
    private static instance: SessionManager;
    private readonly sessionId: string;

    // OCC — version snapshot captured at task submission (Phase 1.5).
    // Keyed by absolute file path; cleared on new task submission.
    private versionSnapshot: Map<string, number> = new Map();

    private constructor() {
        // vscode.env.sessionId returns a non-standard concatenated string in some VS Code builds.
        // NOTE: Backend also requires `pip install 'uvicorn[standard]'` for WS support.
        this.sessionId = (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function')
            ? crypto.randomUUID()
            : `ailienant_${Date.now().toString(36)}`;

        // Register OCC handler once — checks file version on every incoming graph mutation.
        WSClient.getInstance().onMessage(this._onWSMessage.bind(this));
    }

    public static getInstance(): SessionManager {
        if (!SessionManager.instance) {
            SessionManager.instance = new SessionManager();
        }
        return SessionManager.instance;
    }

    /**
     * 7.9.A.5 — Open the quantum tunnel (idempotent) and announce the workspace so
     * the backend starts (or resumes) the lazy GraphRAG indexer. Safe to call on
     * every session/panel open as well as before a task.
     */
    public ensureConnected(): void {
        const wsClient = WSClient.getInstance();
        wsClient.connect(this.sessionId);

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
        opts?: { explicit_mentions?: string[] },
    ): Promise<void> {
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

            // 4. Preparar el Contrato (Payload)
            const payload: TaskPayload = {
                task_prompt: taskPrompt,
                dirty_buffers: dirtyBuffers,
                project_id: PathResolver.resolveProjectId(),
                document_version_id: activeDoc ? String(activeDoc.version) : undefined,
                // Phase 7.11.4 — wires the host-extracted @mentions through to the
                // researcher's existing RAG-bypass path (agents/researcher.py:78).
                explicit_mentions:
                    opts?.explicit_mentions && opts.explicit_mentions.length > 0
                        ? opts.explicit_mentions
                        : undefined,
            };

            // 5. Emitir la Misión (Boca) al API Gateway
            const apiClient = APIClient.getInstance();

            vscode.window.showInformationMessage(`AILIENANT: Analyzing directive with ${dirtyBuffers.length} buffers in context...`);

            // Disparamos la petición. Si falla, el catch local lo maneja.
            await apiClient.submitTask(this.sessionId, payload);

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
