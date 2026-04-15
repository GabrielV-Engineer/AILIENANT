"ailienant-extension/src/api/api_client.ts"

import { DirtyBuffer } from '../editor/vfs_reader';
import * as vscode from 'vscode';

// Definimos la interfaz del Payload alineada con el contrato de FastAPI
export interface TaskPayload {
    task_prompt: string;
    dirty_buffers: DirtyBuffer[];
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
}