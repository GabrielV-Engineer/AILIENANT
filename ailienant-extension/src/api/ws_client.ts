import WebSocket from 'ws';
import * as vscode from 'vscode';
import { WsConnectionStatus } from '../shared/config';

// Eventos que nuestra extensión necesitará escuchar
export type WSMessageCallback = (data: unknown) => void;
export type WSErrorCallback = (error: Error) => void;
export type WSStatusCallback = (status: WsConnectionStatus) => void;

export class WSClient {
    private static instance: WSClient;
    private ws: WebSocket | null = null;
    private readonly wsUrl: string;

    // Callbacks suscritos (Patrón Observer)
    private onMessageHandlers: Set<WSMessageCallback> = new Set();
    private onErrorHandlers: Set<WSErrorCallback> = new Set();
    private onStatusHandlers: Set<WSStatusCallback> = new Set();

    // Estado de reconexión (Exponential Backoff)
    private reconnectAttempts: number = 0;
    private maxReconnectAttempts: number = 10;
    private isConnecting: boolean = false;
    private _clientId: string = '';

    // Current connection status — replayed to new subscribers so a panel opened
    // after the tunnel is already up still learns it is connected.
    private _status: WsConnectionStatus = 'disconnected';
    // Payloads queued while the socket was not yet OPEN; flushed on 'open'.
    private _pendingSends: unknown[] = [];

    // Delta sync: track current document_version_id per file
    // Used to detect stale state before Dashboard approvals
    private _fileVersions: Map<string, string> = new Map();
    private _dashboardBc: BroadcastChannel | undefined;

    private constructor() {
        this.wsUrl = 'ws://127.0.0.1:8000/api/v1/ws';
        // BroadcastChannel to push WS events to Dashboard SPA in the browser
        if (typeof BroadcastChannel !== 'undefined') {
            this._dashboardBc = new BroadcastChannel('ailienant_ws');
        }
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
        this._clientId = clientId;
        this.isConnecting = true;
        const urlWithAuth = `${this.wsUrl}/${clientId}`;

        try {
            const wsInstance = new WebSocket(urlWithAuth);

            wsInstance.on('open', () => {
                this.isConnecting = false;
                this.reconnectAttempts = 0;
                this._emitStatus('connected');
                this._flushPending();
            });

            wsInstance.on('message', (data: WebSocket.RawData) => {
                try {
                    const parsedData = JSON.parse(data.toString()) as { event_type?: string; data?: unknown };
                    this.onMessageHandlers.forEach(handler => handler(parsedData));

                    // Delta sync: track file version changes and flag stale Dashboard patches
                    if (parsedData.event_type === 'client_file_update') {
                        const d = parsedData.data as { filepath: string; document_version_id: string } | undefined;
                        if (d?.filepath) {
                            const prev = this._fileVersions.get(d.filepath);
                            if (prev && prev !== d.document_version_id) {
                                // File changed while Dashboard may have an open patch — emit stale event
                                this._dashboardBc?.postMessage({
                                    type: 'FILE_VERSION_CHANGED',
                                    filepath: d.filepath,
                                    new_version_id: d.document_version_id,
                                });
                            }
                            this._fileVersions.set(d.filepath, d.document_version_id);
                        }
                    }

                    // Forward all server events to Dashboard browser SPA
                    if (parsedData.event_type?.startsWith('server_')) {
                        this._dashboardBc?.postMessage(parsedData);
                    }
                } catch (e) {
                    console.error('Error parseando payload de LangGraph', e);
                }
            });

            wsInstance.on('close', () => {
                this.isConnecting = false;
                this._emitStatus('reconnecting');
                this._handleReconnection(clientId);
            });

            wsInstance.on('error', (err: Error) => {
                this.isConnecting = false;
                this.onErrorHandlers.forEach(handler => handler(err));
                this._emitStatus('disconnected');
            });

            this.ws = wsInstance;

        } catch (error) {
            this.isConnecting = false;
            this._emitStatus('disconnected');
            console.error('Error fatal al crear WebSocket', error);
        }
    }

    private _emitStatus(status: WsConnectionStatus): void {
        this._status = status;
        this.onStatusHandlers.forEach(h => h(status));
        if (status === 'connected') {
            vscode.window.showInformationMessage('AILIENANT: Quantum tunnel connected');
        } else if (status === 'disconnected') {
            vscode.window.showErrorMessage('AILIENANT: Connection lost');
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

    private _handleReconnection(clientId: string): void {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            this._emitStatus('disconnected');
            return;
        }
        this.reconnectAttempts++;
        // Exponential backoff: 2^n * 1000ms + jitter, capped at 30s
        const backoffTime = Math.min(Math.pow(2, this.reconnectAttempts) * 1000 + Math.random() * 500, 30000);
        console.log(`[WSClient] Reconecting in ${Math.round(backoffTime)}ms (attempt ${this.reconnectAttempts})`);
        setTimeout(() => this.connect(clientId), backoffTime);
    }

    // ── Subscription methods ─────────────────────────────────────────────────
    public onMessage(callback: WSMessageCallback): void      { this.onMessageHandlers.add(callback); }
    public removeMessageHandler(callback: WSMessageCallback): void { this.onMessageHandlers.delete(callback); }
    public onStatus(callback: WSStatusCallback): void {
        this.onStatusHandlers.add(callback);
        // Replay the latest status so subscribers registering after connect are accurate.
        callback(this._status);
    }
    public removeStatusHandler(callback: WSStatusCallback): void  { this.onStatusHandlers.delete(callback); }

    /** Current connection status (without subscribing). */
    public getStatus(): WsConnectionStatus { return this._status; }

    /** Record a document_version_id for OCC delta sync tracking. */
    public trackFileVersion(filepath: string, versionId: string): void {
        this._fileVersions.set(filepath, versionId);
    }

    public send(payload: unknown): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
        } else {
            console.warn('[WSClient] Cannot send: connection not open');
        }
    }

    /**
     * Send once the socket is OPEN. If already open, sends immediately;
     * otherwise queues the payload and flushes it on the next 'open'.
     * Used for client_workspace_init, which must land right after connect.
     */
    public sendWhenReady(payload: unknown): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
        } else {
            this._pendingSends.push(payload);
        }
    }

    private _flushPending(): void {
        if (this._pendingSends.length === 0) { return; }
        const queued = this._pendingSends;
        this._pendingSends = [];
        for (const payload of queued) {
            this.send(payload);
        }
    }
}