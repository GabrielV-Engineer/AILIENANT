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

export type Provider =
    | 'ollama' | 'lmstudio' | 'vllm' | 'openai' | 'openrouter' | 'anthropic' | 'custom'
    | 'google' | 'deepseek' | 'mistral' | 'qwen' | 'moonshot' | 'zhipu';

// Registry provider metadata served by GET /providers — drives the dashboard's
// provider dropdown, defaults, hints, key links, and base-URL visibility. Adding
// a provider is a backend-only change; the UI renders whatever this returns.
export interface ProviderSpec {
    id: Provider;
    label: string;
    is_local: boolean;
    needs_key: boolean;
    hides_base_url: boolean;
    default_base_url: string | null;
    key_hint: string;
    help_url: string;
    env_key: string | null;
    // No model lists: available models come from testing a configured endpoint,
    // never from a system-curated preference.
}

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
    discovered: DiscoveredModel[];  // available-model pool (local engines + imported cloud)
    model_cache: Record<string, string[]>;  // endpoint_id → imported canonical model ids
}

export interface TestConnectionRequest {
    url: string;
    api_key: string;
    provider: Provider;
    endpoint_id?: string;  // set → backend restores stored key + imports the catalogue
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

export interface EngineStatus {
    id: string;           // "ollama" | "lmstudio"
    name: string;
    url: string;
    running: boolean;
    model_count: number;
    models: string[];
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

export function fetchEngineStatus(): Promise<EngineStatus[]> {
    return _json<EngineStatus[]>('/engines');
}

export function fetchProviders(): Promise<ProviderSpec[]> {
    return _json<ProviderSpec[]>('/providers');
}

export interface PingRequest {
    model_id?: string;  // canonical pool id, e.g. "google/gemini-2.0-flash"
    tier?: string;      // active-preset tier: small | medium | big | cloud
}

export interface PingResponse {
    ok: boolean;
    model: string;
    reply: string;
    latency_ms: number;
    error: string | null;
}

export function pingModel(req: PingRequest): Promise<PingResponse> {
    return _json<PingResponse>('/ping', { method: 'POST', body: JSON.stringify(req) });
}
