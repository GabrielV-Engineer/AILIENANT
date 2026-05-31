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
    // Phase 7.9.A.5.1: mutable so configure() can update after dynamic port is resolved.
    private _wsUrl: string;
    private _token: string = '';

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
    // Hard-capped to bound memory across a long reconnect outage (see sendWhenReady).
    private _pendingSends: unknown[] = [];
    private static readonly MAX_PENDING = 256;

    // Delta sync: track current document_version_id per file
    // Used to detect stale state before Dashboard approvals
    private _fileVersions: Map<string, string> = new Map();
    private _dashboardBc: BroadcastChannel | undefined;

    private constructor() {
        this._wsUrl = 'ws://127.0.0.1:8000/api/v1/ws';
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
     * Phase 7.9.A.5.1 — called from activate() once the CoreProcessManager has a port/token.
     * All subsequent connections will target the new URL and send the auth handshake.
     */
    public configure(wsUrl: string, token: string): void {
        this._wsUrl = wsUrl;
        this._token = token;
    }

    /**
     * Phase 7.11.8 — derive the HTTP API base URL from the configured WS URL,
     * so callers can hit REST endpoints (e.g. `/api/v1/sessions/{id}/checkpoints`)
     * on the same backend without duplicating port discovery. Accepts
     * `ws://host:port/...`, returns `http://host:port`.
     */
    public getHttpBaseUrl(): string {
        try {
            const u = new URL(this._wsUrl);
            const proto = u.protocol === 'wss:' ? 'https:' : 'http:';
            return `${proto}//${u.host}`;
        } catch {
            // Defensive: configure() may not have been called; fall back to default.
            return 'http://127.0.0.1:8000';
        }
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
        const urlWithAuth = `${this._wsUrl}/${clientId}`;

        try {
            const wsInstance = new WebSocket(urlWithAuth);

            wsInstance.on('open', () => {
                this.isConnecting = false;
                this.reconnectAttempts = 0;
                // Phase 7.9.A.5.1 — send auth handshake before flushing any queued payloads.
                // Token in URL query params would appear in server logs; first-message is portable.
                if (this._token) {
                    wsInstance.send(JSON.stringify({ event_type: 'auth', token: this._token }));
                }
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

            wsInstance.on('close', (code: number) => {
                this.isConnecting = false;
                // Phase 7.9.A.5.1 — close code 4001 = server rejected auth token.
                // Do not retry: the token is wrong for this server instance.
                if (code === 4001) {
                    this._emitStatus('disconnected');
                    vscode.window.showErrorMessage('AILIENANT: WebSocket auth rejected (token mismatch). Restart the extension.');
                    return;
                }
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
        // Phase 7.12 — connection status is surfaced asynchronously in the webview
        // via the WS_STATUS indicator (workspace_panel.ts:459). No host toasts on
        // normal connect/reconnect cycles (they spam the VS Code UI event loop).
        if (status === 'disconnected') {
            console.error('[WSClient] Connection lost (status=disconnected)');
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
     * Phase 7.12.9 (Fix 1) — idempotent re-assert of the tunnel, called when a
     * webview panel becomes visible again. The singleton survives the webview
     * teardown, but if the socket was closed (or exhausted its backoff budget)
     * while the panel was hidden, the remounted UI would stay "disconnected"
     * forever. Reset the backoff counter and reconnect when not OPEN; the
     * existing connect() guards make an already-open socket a no-op.
     */
    public ensureConnected(clientId?: string): void {
        const id = clientId || this._clientId;
        if (!id) { return; }
        if (this.ws?.readyState === WebSocket.OPEN || this.isConnecting) { return; }
        this.reconnectAttempts = 0;
        this.connect(id);
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
     * Droppable send for the telemetry bus (silent IDE lifecycle pushes).
     *
     * Telemetry has NO absolute priority: if the socket is not OPEN the frame is
     * dropped rather than queued. A file-save event flushed minutes later on
     * reconnect is stale noise, and the reactive index re-derives current state
     * from the next live save — so queueing it would only risk Head-of-Line
     * blocking the chat/answer stream it shares the socket with. Interactive
     * traffic keeps using send()/sendWhenReady().
     */
    public sendTelemetry(payload: unknown): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
        }
        // else: intentionally dropped — see method contract.
    }

    /**
     * Send once the socket is OPEN. If already open, sends immediately;
     * otherwise queues the payload and flushes it on the next 'open'.
     * Used for client_workspace_init, which must land right after connect.
     *
     * The queue is hard-capped: across a long reconnect outage an unbounded
     * backlog would be a memory leak. Critical-init traffic is the only user
     * today, so the cap is a defensive bound — when hit, the oldest entry is
     * evicted (FIFO) before enqueuing the newest.
     */
    public sendWhenReady(payload: unknown): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
        } else {
            if (this._pendingSends.length >= WSClient.MAX_PENDING) {
                this._pendingSends.shift();
            }
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