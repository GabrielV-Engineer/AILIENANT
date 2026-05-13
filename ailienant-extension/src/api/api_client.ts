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
    attachments?: ManualAttachment[];  // user-attached multimodal context
    explicit_mentions?: string[];      // @-referenced file paths — triggers full-file injection
    document_version_id?: string;      // OCC: active document version at submission (Phase 1.5)
}

// Phase 1.6.3 — Model discovery response schema (mirrors FastAPI ModelInfo).
export interface ModelInfo {
    id: string;       // LiteLLM alias, e.g. "ailienant/medium"
    name: string;     // Underlying model, e.g. "llama3.1"
    provider: string; // "ollama" | "openai" | "anthropic" | etc.
    is_local: boolean;
}

export class APIClient {
    private static instance: APIClient;
    private readonly baseUrl: string;

    // Almacenamos los controladores de aborto para poder cancelar peticiones en vuelo
    private activeRequests: Map<string, AbortController> = new Map();

    private constructor() {
        // En el futuro, esto vendrá de las configuraciones de VS Code (settings.json)
        this.baseUrl = 'http://127.0.0.1:8000/api/v1';
    }

    public static getInstance(): APIClient {
        if (!APIClient.instance) {
            APIClient.instance = new APIClient();
        }
        return APIClient.instance;
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
            const response = await fetch(`${this.baseUrl}/task/submit`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Task-ID': taskId // Header de trazabilidad
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
            if (error.name === 'AbortError') {
                vscode.window.showWarningMessage(`AILIENANT: Tarea ${taskId} cancelada por el usuario o por timeout.`);
            } else {
                vscode.window.showErrorMessage(`AILIENANT Error de red: ${error.message}`);
            }
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
            const response = await fetch(`${this.baseUrl}/models/available`, {
                method: 'GET',
                signal: AbortSignal.timeout(3000),
            });
            if (!response.ok) { return []; }
            const data = await response.json();
            return (data.models ?? []) as ModelInfo[];
        } catch {
            return [];
        }
    }
}