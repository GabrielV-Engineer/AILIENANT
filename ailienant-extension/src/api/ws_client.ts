import WebSocket from 'ws';
import * as vscode from 'vscode';

// Eventos que nuestra extensión necesitará escuchar
export type WSMessageCallback = (data: any) => void;
export type WSErrorCallback = (error: Error) => void;

export class WSClient {
    private static instance: WSClient;
    private ws: WebSocket | null = null;
    private readonly wsUrl: string;

    // Callbacks suscritos (Patrón Observer)
    private onMessageHandlers: Set<WSMessageCallback> = new Set();
    private onErrorHandlers: Set<WSErrorCallback> = new Set();

    // Estado de reconexión (Exponential Backoff)
    private reconnectAttempts: number = 0;
    private maxReconnectAttempts: number = 5;
    private isConnecting: boolean = false;

    private constructor() {
        // En producción, esto se lee de vscode.workspace.getConfiguration()
        this.wsUrl = 'ws://127.0.0.1:8000/api/v1/ws';
    }

    public static getInstance(): WSClient {
        if (!WSClient.instance) {
            WSClient.instance = new WSClient();
        }
        return WSClient.instance;
    }

    /**
     * Inicia el túnel de red.
     * Protegido contra llamadas concurrentes.
     */
    public connect(clientId: string): void {
        if (this.ws?.readyState === WebSocket.OPEN || this.isConnecting) {
            return;
        }

        this.isConnecting = true;
        const urlWithAuth = `${this.wsUrl}/${clientId}`;

        try {
            // 1. PATRÓN DE REFERENCIA LOCAL: Creamos una instancia segura en memoria $O(1)$
            const wsInstance = new WebSocket(urlWithAuth);

            // 2. Acoplamos los eventos a la constante local, no a 'this.ws'
            // TypeScript ahora sabe al 100% que 'wsInstance' no es nulo.
            wsInstance.on('open', () => {
                this.isConnecting = false;
                this.reconnectAttempts = 0;
                vscode.window.showInformationMessage('AILIENANT: Túnel Cuántico [Conectado] 🟢');
            });

            wsInstance.on('message', (data: WebSocket.RawData) => {
                try {
                    const parsedData = JSON.parse(data.toString());
                    this.onMessageHandlers.forEach(handler => handler(parsedData));
                } catch (e) {
                    console.error('Error parseando payload de LangGraph', e);
                }
            });

            wsInstance.on('close', () => {
                this.isConnecting = false;
                this.handleReconnection(clientId);
            });

            wsInstance.on('error', (err: Error) => {
                this.isConnecting = false;
                this.onErrorHandlers.forEach(handler => handler(err));
            });

            // 3. Finalmente, promovemos la instancia local al estado de la clase
            this.ws = wsInstance;

        } catch (error) {
            this.isConnecting = false;
            console.error('Error fatal al crear WebSocket', error);
        }
    }

    /**
     * Cierra el túnel de forma elegante. Evita fugas de memoria.
     */
    public disconnect(): void {
        if (this.ws) {
            // Código 1000 = Cierre Normal
            this.ws.close(1000, "Desconexión iniciada por el IDE");
            this.ws = null;
        }
    }

    /**
     * Algoritmo de Retroceso Exponencial para reconexión.
     */
    private handleReconnection(clientId: string): void {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            vscode.window.showErrorMessage('AILIENANT: Conexión con el núcleo perdida de forma permanente. 🔴');
            return;
        }

        this.reconnectAttempts++;
        // Fórmula de backoff: 2^intentos * 1000ms + Jitter aleatorio
        const backoffTime = Math.pow(2, this.reconnectAttempts) * 1000 + Math.random() * 500;

        console.log(`Reconectando en ${Math.round(backoffTime)}ms... (Intento ${this.reconnectAttempts})`);
        setTimeout(() => this.connect(clientId), backoffTime);
    }

    // Métodos de Suscripción
    public onMessage(callback: WSMessageCallback): void { this.onMessageHandlers.add(callback); }
    public removeMessageHandler(callback: WSMessageCallback): void { this.onMessageHandlers.delete(callback); }

    public send(payload: unknown): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
        } else {
            console.warn('[WSClient] Cannot send: connection not open');
        }
    }
}