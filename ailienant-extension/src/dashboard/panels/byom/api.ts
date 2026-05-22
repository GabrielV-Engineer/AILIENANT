// BYOM Models dashboard REST client — Phase 7.9.B.2.
// Same-origin fetch: the dashboard SPA is served by the same FastAPI server.
// Types mirror api/byom.py pydantic models.

const BASE = '/api/v1/byom';

async function _json<T>(path: string, init?: RequestInit): Promise<T> {
    const r = await fetch(BASE + path, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
    });
    if (!r.ok) {
        const text = await r.text().catch(() => r.statusText);
        throw new Error(`${r.status} ${text}`);
    }
    return r.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type Provider = 'ollama' | 'vllm' | 'openai' | 'openrouter' | 'anthropic' | 'custom';

export interface EndpointConfig {
    id: string;
    name: string;
    url: string;
    api_key: string;    // masked ("sk-••••…") in GET responses
    provider: Provider;
}

export interface ModelPreset {
    id: string;
    name: string;
    description: string;
    is_builtin: boolean;
    tiers: Record<string, string>;  // { small: "ollama/phi3", big: "gpt-4o", ... }
}

export interface DiscoveredModel {
    id: string;   // model ID to use in preset tiers
    name: string; // human-readable label
}

export interface BYOMConfigResponse {
    endpoints: EndpointConfig[];
    presets: ModelPreset[];         // built-ins first, then user-defined
    active_preset_id: string | null;
    discovered: DiscoveredModel[];  // live models for preset-tier dropdowns
}

export interface TestConnectionRequest {
    url: string;
    api_key: string;
    provider: Provider;
}

export interface TestConnectionResponse {
    ok: boolean;
    models: DiscoveredModel[];
    latency_ms: number;
    error: string | null;
}

// Payload for PUT /config — all fields optional (server merges onto existing).
export interface BYOMConfigPayload {
    endpoints?: EndpointConfig[];
    presets?: ModelPreset[];
    active_preset_id?: string | null;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export function fetchBYOMConfig(): Promise<BYOMConfigResponse> {
    return _json<BYOMConfigResponse>('/config');
}

export function saveBYOMConfig(payload: BYOMConfigPayload): Promise<BYOMConfigResponse> {
    return _json<BYOMConfigResponse>('/config', {
        method: 'PUT',
        body: JSON.stringify(payload),
    });
}

export function testEndpoint(req: TestConnectionRequest): Promise<TestConnectionResponse> {
    return _json<TestConnectionResponse>('/test', {
        method: 'POST',
        body: JSON.stringify(req),
    });
}
