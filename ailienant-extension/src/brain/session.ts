import * as vscode from 'vscode';
import { VFSReader } from '../editor/vfs_reader';
import { APIClient, TaskPayload } from '../api/api_client';
import { WSClient } from '../api/ws_client';

export class SessionManager {
    private static instance: SessionManager;
    private readonly sessionId: string;

    private constructor() {
        // Generamos un ID de sesión criptográficamente seguro o usamos el del IDE
        // Esto es crucial para que FastAPI sepa a quién enviarle los WebSockets
        this.sessionId = vscode.env.sessionId || `ailienant_usr_${Date.now()}`;
    }

    public static getInstance(): SessionManager {
        if (!SessionManager.instance) {
            SessionManager.instance = new SessionManager();
        }
        return SessionManager.instance;
    }

    /**
     * Inicia una misión cognitiva.
     * Orquesta la recolección de entropía, abre el túnel cuántico y despacha el WBS.
     */
    public async startAITask(taskPrompt: string): Promise<void> {
        try {
            // 1. Asegurar el canal de Oídos (WebSockets) ANTES de hablar.
            // Si lo hacemos al revés, podríamos perder los primeros tokens de LangGraph.
            const wsClient = WSClient.getInstance();
            wsClient.connect(this.sessionId);

            // 2. Extraer la Entropía Visual (Ojos) del VFS
            const dirtyBuffers = VFSReader.captureEntropy();

            // 3. Preparar el Contrato (Payload)
            const payload: TaskPayload = {
                task_prompt: taskPrompt,
                dirty_buffers: dirtyBuffers
            };

            // 4. Emitir la Misión (Boca) al API Gateway
            const apiClient = APIClient.getInstance();

            vscode.window.showInformationMessage(`AILIENANT: Analizando directiva con ${dirtyBuffers.length} buffers en contexto... 🐜`);

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
        vscode.window.showWarningMessage("AILIENANT: Misión abortada por el usuario. 🛑");
    }

    // Exponemos el SessionID por si la UI lo necesita para renderizar algo
    public getSessionId(): string {
        return this.sessionId;
    }
}