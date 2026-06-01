"ailienant-extension/src/api/api_client.ts"

import { DirtyBuffer } from '../editor/vfs_reader';
import * as vscode from 'vscode';

// Multimodal context the user can attach manually (image or document).
export interface ManualAttachment {
    type: 'image' | 'document';
    data?: string;     // base64-encoded bytes for images
    content?: string;  // raw text for documents
    mime?: string;     // MIME type hint, e.g. 'image/png', 'text/csv'
    name?: string;     // filename hint for the backend
}

// Outgoing payload contract — aligned with FastAPI's TaskPayload model.
// All new fields are optional for backward compatibility during rollout.
export interface TaskPayload {
    task_prompt: string;
    dirty_buffers: DirtyBuffer[];
    project_id?: string;               // SHA-256 of the VS Code workspace root path
    // Phase 7.12.9 (Fix 3) — dynamic IDE context so the Planner anchors on what the
    // user is actually looking at instead of a stale LanceDB/GraphRAG index.
    workspace_root?: string;           // absolute workspaceFolders[0] path (dynamic, authoritative)
    active_file_path?: string;         // absolute path of the focused editor tab
    active_file_content?: string;      // its text, hard-capped to ACTIVE_FILE_CHAR_CAP (OOM guard)
    attachments?: ManualAttachment[];  // user-attached multimodal context
    explicit_mentions?: string[];      // @-referenced file paths — triggers full-file injection
    document_version_id?: string;      // OCC: active document version at submission (Phase 1.5)
    // Per-submit idempotency key. Lets the backend dedup a resubmit (e.g. driven
    // by a WS reconnect) so the same request never spawns two generations.
    request_id?: string;
    // Phase 9 (ADR-707) — Native Thinking. When true (default) the gateway
    // requests native reasoning tokens for capable models; omitted/false → flat
    // text streaming. Optional so pre-Phase-9 payloads keep the backend default.
    enable_native_thinking?: boolean;
    thinking_budget_tokens?: number;   // API-level circuit breaker for the reasoning phase
}

// Phase 1.6.3 — Model discovery response schema (mirrors FastAPI ModelInfo).
export interface ModelInfo {
    id: string;       // LiteLLM alias, e.g. "ailienant/medium"
    name: string;     // Underlying model, e.g. "llama3.1"
    provider: string; // "ollama" | "openai" | "anthropic" | etc.
    is_local: boolean;
}

// 7.9.A.7 — Token usage snapshot (mirrors GET /api/v1/telemetry/tokens).
export interface TokenUsage {
    local_tokens: number;
    cloud_tokens: number;
    estimated_savings_usd: number;
    estimated_invested_usd: number;
}

// Phase 3.4.5 — MCTS Mirror response schema (mirrors FastAPI MergeReport).
export interface MergeReport {
    success: boolean;
    merged_files: number;
    workspace_root: string;
    errors: string[];
    prune_count: number;
    // Phase 3.4.7 — workspace-relative paths actually written; used by the
    // telemetry provider to register Bounding Boxes for rejection detection.
    merged_paths: string[];
}

// Phase 3.4.7 — Silent rejection telemetry payload (mirrors FastAPI RejectTelemetryPayload).
export interface RejectTelemetryPayload {
    uri: string;
    original_ai_code: string;
    current_user_code: string;
    timestamp: number;
    workspace_root: string;
}

export class APIClient {
    private static instance: APIClient;
    // Phase 7.9.A.5.1: mutable so configure() can update after dynamic port is resolved.
    private _baseUrl: string;
    private _token: string = '';

    // Almacenamos los controladores de aborto para poder cancelar peticiones en vuelo
    private activeRequests: Map<string, AbortController> = new Map();

    private constructor() {
        this._baseUrl = 'http://127.0.0.1:8000/api/v1';
    }

    public static getInstance(): APIClient {
        if (!APIClient.instance) {
            APIClient.instance = new APIClient();
        }
        return APIClient.instance;
    }

    /**
     * Phase 7.9.A.5.1 — called from activate() once the CoreProcessManager has a port/token.
     * All subsequent requests will target the new URL and include the auth header.
     */
    public configure(baseUrl: string, token: string): void {
        this._baseUrl = baseUrl;
        this._token = token;
    }

    /** Build auth headers. Health check path (GET /) is excluded by the caller. */
    private _authHeaders(): Record<string, string> {
        return this._token ? { 'X-AILIENANT-TOKEN': this._token } : {};
    }

    /**
     * Envía la entropía del IDE y la tarea al motor LangGraph.
     * Implementa un timeout de seguridad usando AbortController nativo.
     */
    public async submitTask(taskId: string, payload: TaskPayload, timeoutMs: number = 10000): Promise<any> {
        // 1. Prevención de fugas de memoria: Limpiar peticiones previas con el mismo ID
        if (this.activeRequests.has(taskId)) {
            this.cancelTask(taskId);
        }

        // 2. Control de Cancelación y Timeout
        const controller = new AbortController();
        this.activeRequests.set(taskId, controller);

        const timeoutId = setTimeout(() => {
            controller.abort(`Timeout de ${timeoutMs}ms excedido.`);
        }, timeoutMs);

        try {
            const response = await fetch(`${this._baseUrl}/task/submit`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Task-ID': taskId, // Header de trazabilidad
                    ...this._authHeaders(),
                },
                body: JSON.stringify(payload),
                signal: controller.signal // Enlazamos el control de aborto
            });

            if (!response.ok) {
                throw new Error(`AILIENANT API Error: ${response.status} - ${response.statusText}`);
            }

            // Parseo JSON nativo (Cero dependencias)
            return await response.json();

        } catch (error: any) {
            // Detect abort via the signal, not error.name: when abort() is given a
            // string reason the rejection IS that string (no .name/.message), which
            // previously rendered "Error de red: undefined".
            if (controller.signal.aborted) {
                vscode.window.showWarningMessage(`AILIENANT: Tarea ${taskId} cancelada por el usuario o por timeout.`);
                // Normalize so SessionManager's AbortError suppression keeps quiet.
                throw Object.assign(new Error('aborted'), { name: 'AbortError' });
            }
            const detail = error?.message ?? String(error);
            vscode.window.showErrorMessage(`AILIENANT Error de red: ${detail}`);
            throw error; // Re-lanzamos para que la UI lo maneje
        } finally {
            // 3. Limpieza: Liberar memoria y evitar bloqueos en el Event Loop
            clearTimeout(timeoutId);
            this.activeRequests.delete(taskId);
        }
    }

    /**
     * Cancela una petición HTTP en vuelo.
     * Crucial para el botón "Stop Generation" en la UI.
     */
    public cancelTask(taskId: string): void {
        const controller = this.activeRequests.get(taskId);
        if (controller) {
            controller.abort("Operación cancelada por el usuario.");
            this.activeRequests.delete(taskId);
        }
    }

    /**
     * Phase 1.6.3 — Fetch available models from the discovery endpoint.
     * Tries LiteLLM proxy first; falls back to direct Ollama scan if proxy is down.
     * Returns empty array on any network error (non-blocking).
     */
    public async fetchAvailableModels(): Promise<ModelInfo[]> {
        try {
            const response = await fetch(`${this._baseUrl}/models/available`, {
                method: 'GET',
                headers: { ...this._authHeaders() },
                signal: AbortSignal.timeout(3000),
            });
            if (!response.ok) { return []; }
            const data = await response.json();
            return (data.models ?? []) as ModelInfo[];
        } catch {
            return [];
        }
    }

    /**
     * 7.9.A.7 — Fetch the per-session token-usage snapshot for the Account & Usage view.
     * Returns null on any network error (non-blocking).
     */
    public async fetchTokenUsage(): Promise<TokenUsage | null> {
        try {
            const response = await fetch(`${this._baseUrl}/telemetry/tokens`, {
                method: 'GET',
                headers: { ...this._authHeaders() },
                signal: AbortSignal.timeout(3000),
            });
            if (!response.ok) { return null; }
            return (await response.json()) as TokenUsage;
        } catch {
            return null;
        }
    }

    /**
     * 7.9.B.2 — Fetch BYOM config (endpoints + presets + active preset).
     */
    public async fetchBYOMConfig(): Promise<{ presets: unknown[]; active_preset_id: string | null } | null> {
        try {
            const response = await fetch(`${this._baseUrl}/byom/config`, {
                method: 'GET',
                headers: { ...this._authHeaders() },
                signal: AbortSignal.timeout(5000),
            });
            if (!response.ok) { return null; }
            return response.json() as Promise<{ presets: unknown[]; active_preset_id: string | null }>;
        } catch {
            return null;
        }
    }

    /**
     * 7.9.B.2 — Merge-save BYOM config (partial patch; server preserves unset fields).
     */
    public async saveBYOMConfig(payload: Record<string, unknown>): Promise<{ presets: unknown[]; active_preset_id: string | null } | null> {
        try {
            const response = await fetch(`${this._baseUrl}/byom/config`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', ...this._authHeaders() },
                body: JSON.stringify(payload),
                signal: AbortSignal.timeout(10000),
            });
            if (!response.ok) { return null; }
            return response.json() as Promise<{ presets: unknown[]; active_preset_id: string | null }>;
        } catch {
            return null;
        }
    }

    /**
     * 7.9.A.5 — Probe whether the Core backend is reachable.
     * The health route lives at the server origin root ("/"), not under /api/v1.
     */
    public async checkHealth(): Promise<boolean> {
        try {
            const origin = new URL(this._baseUrl).origin;
            const response = await fetch(`${origin}/`, {
                method: 'GET',
                signal: AbortSignal.timeout(2000),
            });
            return response.ok;
        } catch {
            return false;
        }
    }

    /**
     * Phase 3.4.5 — Read a virtual file from an MCTS "dream" node.
     * Backs the `ailienant-vision://{node_id}/{path}` URI scheme.
     */
    public async fetchVirtualFile(nodeId: string, filePath: string): Promise<string> {
        const url = `${this._baseUrl}/mcts/${encodeURIComponent(nodeId)}/vfs?path=${encodeURIComponent(filePath)}`;
        const response = await fetch(url, { headers: { ...this._authHeaders() }, signal: AbortSignal.timeout(5000) });
        if (!response.ok) {
            throw new Error(`AILIENANT Mirror fetch failed: ${response.status} ${response.statusText}`);
        }
        return await response.text();
    }

    /**
     * Phase 3.4.5 — One-Click Merge: apply a stable MCTS node's vfs_view to disk.
     */
    public async applyMerge(nodeId: string, workspaceRoot: string): Promise<MergeReport> {
        const url = `${this._baseUrl}/mcts/${encodeURIComponent(nodeId)}/merge`;
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...this._authHeaders() },
            body: JSON.stringify({ workspace_root: workspaceRoot }),
            signal: AbortSignal.timeout(15000),
        });
        if (!response.ok) {
            throw new Error(`AILIENANT applyMerge failed: ${response.status} ${response.statusText}`);
        }
        return (await response.json()) as MergeReport;
    }

    // ── Phase 7.9.A.7 — Command-menu config endpoints ────────────────────────

    /** Generic JSON request helper. Returns null on any network/parse error (non-blocking). */
    private async _json<T>(path: string, init?: RequestInit, timeoutMs = 5000): Promise<T | null> {
        try {
            const response = await fetch(`${this._baseUrl}${path}`, {
                headers: { 'Content-Type': 'application/json', ...this._authHeaders() },
                signal: AbortSignal.timeout(timeoutMs),
                ...init,
            });
            if (!response.ok) { return null; }
            return (await response.json()) as T;
        } catch {
            return null;
        }
    }

    // System settings (scalars: analyst_name, output_style, permission_mode)
    public getSystemSettings(): Promise<Record<string, unknown> | null> {
        return this._json('/system/settings', { method: 'GET' });
    }
    public saveSystemSettings(patch: Record<string, unknown>): Promise<Record<string, unknown> | null> {
        return this._json('/system/settings', { method: 'POST', body: JSON.stringify(patch) });
    }

    // Hooks (config-capture only)
    public getHooks(): Promise<{ hooks: unknown[] } | null> {
        return this._json('/system/hooks', { method: 'GET' });
    }
    public saveHook(body: Record<string, unknown>): Promise<{ hooks: unknown[] } | null> {
        return this._json('/system/hooks', { method: 'POST', body: JSON.stringify(body) });
    }
    public deleteHook(id: string): Promise<{ hooks: unknown[] } | null> {
        return this._json(`/system/hooks/${encodeURIComponent(id)}`, { method: 'DELETE' });
    }

    // Agent role overrides
    public getAgentRoles(): Promise<{ base_coder_prompt: string; roles: unknown[] } | null> {
        return this._json('/agents/roles', { method: 'GET' });
    }
    public saveAgentRole(role: string, systemPrompt: string): Promise<unknown> {
        return this._json(`/agents/roles/${encodeURIComponent(role)}`, {
            method: 'POST', body: JSON.stringify({ system_prompt: systemPrompt }),
        });
    }

    // MCP server registry + zombie-safe connection probe
    public getMcpServers(): Promise<{ servers: unknown[] } | null> {
        return this._json('/mcp/servers', { method: 'GET' });
    }
    public saveMcpServer(body: Record<string, unknown>): Promise<{ servers: unknown[] } | null> {
        return this._json('/mcp/servers', { method: 'POST', body: JSON.stringify(body) });
    }
    public deleteMcpServer(id: string): Promise<{ servers: unknown[] } | null> {
        return this._json(`/mcp/servers/${encodeURIComponent(id)}`, { method: 'DELETE' });
    }
    public testMcpServer(uri: string): Promise<{ reachable: boolean; tool_count: number; error?: string } | null> {
        // Generous timeout: probe spawns a subprocess + handshake before reaping.
        return this._json('/mcp/test', { method: 'POST', body: JSON.stringify({ uri }) }, 15000);
    }

    // Skills (prompt templates)
    public getSkills(): Promise<{ skills: unknown[] } | null> {
        return this._json('/skills', { method: 'GET' });
    }
    public saveSkill(body: Record<string, unknown>): Promise<{ skills: unknown[] } | null> {
        return this._json('/skills', { method: 'POST', body: JSON.stringify(body) });
    }
    public deleteSkill(id: string): Promise<{ skills: unknown[] } | null> {
        return this._json(`/skills/${encodeURIComponent(id)}`, { method: 'DELETE' });
    }

    /**
     * Phase 3.4.7 — Fire-and-forget rejection telemetry.
     * Errors are swallowed: silent telemetry must NEVER surface to the user.
     */
    public async reportRejection(payload: RejectTelemetryPayload): Promise<void> {
        try {
            await fetch(`${this._baseUrl}/telemetry/reject`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...this._authHeaders() },
                body: JSON.stringify(payload),
                signal: AbortSignal.timeout(10000),
            });
        } catch (e) {
            console.warn('[ailienant] telemetry/reject failed:', e);
        }
    }
}