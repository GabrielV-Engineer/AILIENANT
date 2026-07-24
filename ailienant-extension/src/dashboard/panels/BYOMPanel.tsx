import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
    type BYOMConfigResponse,
    type BYOMConfigPayload,
    type DiscoveredModel,
    type EndpointConfig,
    type EngineStatus,
    type ModelPreset,
    type ModelPrice,
    type Provider,
    type ProviderSpec,
    type PingResponse,
    type TestConnectionResponse,
    fetchBYOMConfig,
    fetchEngineStatus,
    fetchProviders,
    pingModel,
    saveBYOMConfig,
    testEndpoint,
} from './byom/api';
import {
    ActiveProjectBadge, Badge, type BadgeStatus, Button, Card, ConfirmModal,
    EmptyState, SectionHeader, Skeleton, StatTile,
} from '../ui';
import { Icon } from '../../shared/Icon';

// ---------------------------------------------------------------------------
// Provider presentation — normalized shape the card renders from. Sourced from
// the backend registry (GET /providers); a small static fallback covers the
// first paint and the offline case so the dropdown is never empty.
// ---------------------------------------------------------------------------

interface ProviderUi {
    label: string;
    url: string;            // default base URL (placeholder + auto-fill)
    needsKey: boolean;
    keyHint: string;
    description: string;
    helpUrl: string;
    hidesBaseUrl: boolean;  // cloud providers: endpoint is fixed/known → hide the field
}

function uiFromSpec(s: ProviderSpec): ProviderUi {
    return {
        label: s.label,
        url: s.default_base_url ?? '',
        needsKey: s.needs_key,
        keyHint: s.key_hint,
        description: s.is_local
            ? 'Local engine — point at your server URL'
            : (s.needs_key ? `${s.label} — paste your API key` : s.label),
        helpUrl: s.help_url,
        hidesBaseUrl: s.hides_base_url,
    };
}

// Static fallback (original 7) — only used until GET /providers resolves.
const FALLBACK_DEFAULTS: Record<string, ProviderUi> = {
    ollama:     { label: 'Ollama',     url: 'http://localhost:11434',    needsKey: false, keyHint: '',          description: 'Local AI runtime — no key needed', helpUrl: '', hidesBaseUrl: false },
    lmstudio:   { label: 'LM Studio',  url: 'http://localhost:1234',     needsKey: false, keyHint: '',          description: 'LM Studio local server — no key needed', helpUrl: '', hidesBaseUrl: false },
    vllm:       { label: 'vLLM',       url: 'http://localhost:8000',     needsKey: false, keyHint: '',          description: 'vLLM OpenAI-compatible server', helpUrl: '', hidesBaseUrl: false },
    openai:     { label: 'OpenAI',     url: 'https://api.openai.com',    needsKey: true,  keyHint: 'sk-…',     description: 'OpenAI official API', helpUrl: '', hidesBaseUrl: true },
    openrouter: { label: 'OpenRouter', url: 'https://openrouter.ai/api/v1', needsKey: true, keyHint: 'sk-or-…', description: 'Multi-provider routing', helpUrl: '', hidesBaseUrl: true },
    anthropic:  { label: 'Anthropic',  url: 'https://api.anthropic.com', needsKey: true,  keyHint: 'sk-ant-…', description: 'Anthropic Claude API', helpUrl: '', hidesBaseUrl: true },
    custom:     { label: 'Custom (OpenAI-compatible)', url: '',          needsKey: false, keyHint: '',          description: 'Any OpenAI-compatible API (/v1/models + /v1/chat/completions).', helpUrl: '', hidesBaseUrl: false },
};

// ---------------------------------------------------------------------------
// Local state extensions (not persisted — ephemeral UI state only)
// ---------------------------------------------------------------------------

interface EndpointUi extends EndpointConfig {
    status: 'unknown' | 'testing' | 'ok' | 'error';
    testError: string | null;
    fieldErrors: { name?: string; url?: string };
    discoveredModels: DiscoveredModel[];
    showModels: boolean;
    showKey: boolean;
    urlAutoFilled: boolean;
}

interface NewPresetForm {
    name: string;
    tiers: Record<string, string>;
}

interface ConfirmState {
    open: boolean;
    title: string;
    body: string;
    warning?: string;
    onConfirm: () => void;
}

const TIER_LABELS: Record<string, string> = {
    small: 'Small',
    medium: 'Medium',
    big: 'Big',
    cloud: 'Cloud',
};

let _nextId = 1;
function makeId(): string { return `ep-${Date.now()}-${_nextId++}`; }

function toUi(ep: EndpointConfig): EndpointUi {
    return { ...ep, status: 'unknown', testError: null, fieldErrors: {}, discoveredModels: [], showModels: false, showKey: false, urlAutoFilled: false };
}

// ---------------------------------------------------------------------------
// Cost presentation — real rates only (litellm cost map); unknown → no badge.
// ---------------------------------------------------------------------------

function fmtRate(n: number): string {
    if (n <= 0) return '$0';
    return `$${n < 1 ? n.toFixed(3) : n.toFixed(2)}`;
}

/** Small money badge for a model, or nothing when the backend couldn't price it. */
function CostBadge({ price }: { price?: ModelPrice }): JSX.Element | null {
    if (!price) return null;
    if (price.local) return <Badge status="good" icon="zap">Local · free</Badge>;
    return (
        <Badge status="neutral" icon="wallet">
            {fmtRate(price.input_per_mtok)} in · {fmtRate(price.output_per_mtok)} out <span className="db-muted">/1M</span>
        </Badge>
    );
}

const STATUS_META: Record<EndpointUi['status'], { badge: BadgeStatus; icon: 'check-circle' | 'x-circle' | 'loader' | 'circle'; label: string }> = {
    ok:      { badge: 'good',     icon: 'check-circle', label: 'Connected' },
    error:   { badge: 'critical', icon: 'x-circle',     label: 'Error' },
    testing: { badge: 'warning',  icon: 'loader',       label: 'Testing…' },
    unknown: { badge: 'neutral',  icon: 'circle',       label: 'Untested' },
};

// A numbered step wrapper — the Connect → Configure → Verify spine.
function Step({ n, title, subtitle, actions, children }: {
    n: number; title: string; subtitle?: string; actions?: ReactNode; children: ReactNode;
}): JSX.Element {
    return (
        <section className="db-step">
            <div className="db-step-head">
                <span className="db-step-num">{n}</span>
                <div className="db-step-heading">
                    <div className="db-step-title">{title}</div>
                    {subtitle && <div className="db-muted">{subtitle}</div>}
                </div>
                {actions && <div className="db-step-actions">{actions}</div>}
            </div>
            {children}
        </section>
    );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BYOMPanel(): JSX.Element {
    const [endpoints, setEndpoints] = useState<EndpointUi[]>([]);
    const [presets, setPresets] = useState<ModelPreset[]>([]);
    const [activePresetId, setActivePresetId] = useState<string | null>(null);
    const [discovered, setDiscovered] = useState<DiscoveredModel[]>([]);
    const [pricing, setPricing] = useState<Record<string, ModelPrice>>({});
    const [providers, setProviders] = useState<ProviderSpec[]>([]);

    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);

    const [engines, setEngines] = useState<EngineStatus[]>([]);
    const [enginesLoading, setEnginesLoading] = useState(true);

    // Quick-connect strip (add + test + import a provider in one action).
    const [quickProvider, setQuickProvider] = useState<string>('openai');
    const [quickKey, setQuickKey] = useState('');
    const [quickShowKey, setQuickShowKey] = useState(false);
    const [quickBusy, setQuickBusy] = useState(false);
    const [quickError, setQuickError] = useState<string | null>(null);

    const [newPreset, setNewPreset] = useState<NewPresetForm | null>(null);
    const [activating, setActivating] = useState<string | null>(null);
    const [editingPresetId, setEditingPresetId] = useState<string | null>(null);
    const [editForm, setEditForm] = useState<NewPresetForm | null>(null);
    const [showDiscovered, setShowDiscovered] = useState(false);

    const [confirm, setConfirm] = useState<ConfirmState>({ open: false, title: '', body: '', onConfirm: () => {} });
    const closeConfirm = () => setConfirm(s => ({ ...s, open: false }));

    const [endpointSavedAt, setEndpointSavedAt] = useState<number | null>(null);
    const [presetSavedAt, setPresetSavedAt] = useState<number | null>(null);
    const [presetSaveError, setPresetSaveError] = useState<string | null>(null);

    // Model browser modal (pick from the available pool into a tier field).
    const [browseFor, setBrowseFor] = useState<{ tier: string; target: 'new' | 'edit' } | null>(null);
    const [browseQuery, setBrowseQuery] = useState('');
    // Model health check (low-token ping).
    const [pingState, setPingState] = useState<Record<string, { busy: boolean; result: PingResponse | null }>>({});
    const [detectedFilter, setDetectedFilter] = useState('');

    const endpointsRef = useRef(endpoints);
    endpointsRef.current = endpoints;

    // ---- Load on mount ----
    useEffect(() => {
        fetchBYOMConfig()
            .then(cfg => {
                setEndpoints(cfg.endpoints.map(toUi));
                setPresets(cfg.presets);
                setActivePresetId(cfg.active_preset_id);
                setDiscovered(cfg.discovered);
                setPricing(cfg.model_pricing ?? {});
                setLoadError(null);
            })
            .catch(err => setLoadError(String(err)))
            .finally(() => setLoading(false));

        fetchEngineStatus()
            .then(setEngines)
            .catch(() => setEngines([]))
            .finally(() => setEnginesLoading(false));

        fetchProviders()
            .then(setProviders)
            .catch(() => setProviders([]));  // fall back to FALLBACK_DEFAULTS
    }, []);

    // Normalized provider presentation, registry-first with a static fallback.
    const providerMap = useMemo<Record<string, ProviderUi>>(() => {
        if (providers.length === 0) return FALLBACK_DEFAULTS;
        const m: Record<string, ProviderUi> = {};
        for (const s of providers) m[s.id] = uiFromSpec(s);
        return m;
    }, [providers]);
    const specFor = useCallback(
        (p: string): ProviderUi => providerMap[p] ?? FALLBACK_DEFAULTS[p] ?? {
            label: p, url: '', needsKey: true, keyHint: '', description: '',
            helpUrl: '', hidesBaseUrl: false,
        },
        [providerMap],
    );
    const providerOptions = useMemo(
        () => (providers.length > 0
            ? providers.map(s => ({ id: s.id, label: s.label }))
            : Object.keys(FALLBACK_DEFAULTS).map(id => ({ id, label: FALLBACK_DEFAULTS[id].label }))),
        [providers],
    );

    // ---- Endpoint helpers ----
    const updateEndpoint = useCallback((id: string, patch: Partial<EndpointUi>): void => {
        setEndpoints(prev => prev.map(e => e.id === id ? { ...e, ...patch } : e));
    }, []);

    const handleProviderChange = useCallback((ep: EndpointUi, provider: Provider): void => {
        const def = specFor(provider);
        const shouldFill = !ep.url || ep.urlAutoFilled;
        updateEndpoint(ep.id, {
            provider,
            url: shouldFill ? def.url : ep.url,
            urlAutoFilled: shouldFill,
        });
    }, [updateEndpoint, specFor]);

    const handleTest = useCallback(async (ep: EndpointUi): Promise<void> => {
        const errs: EndpointUi['fieldErrors'] = {};
        if (!ep.name.trim()) errs.name = 'Name is required';
        // Cloud providers hide the Base URL (the backend probes the registry's fixed
        // endpoint), so only local providers require a user-supplied URL.
        if (!specFor(ep.provider).hidesBaseUrl && !ep.url.trim()) errs.url = 'URL is required';
        if (Object.keys(errs).length) {
            updateEndpoint(ep.id, { fieldErrors: errs });
            return;
        }
        updateEndpoint(ep.id, { status: 'testing', testError: null, fieldErrors: {}, discoveredModels: [] });
        // One-click import: persist the endpoint first (stores id + real key), then
        // test (the backend caches the catalogue by endpoint_id), then refresh the
        // pool so the imported models appear in presets + Detected Models.
        try {
            await saveBYOMConfig({
                endpoints: endpointsRef.current.map(({ id, name, url, api_key, provider }) => ({ id, name, url, api_key, provider })),
            });
        } catch { /* non-fatal — test can still run with the in-form key */ }

        let result: TestConnectionResponse;
        try {
            result = await testEndpoint({ url: ep.url, api_key: ep.api_key, provider: ep.provider as Provider, endpoint_id: ep.id });
        } catch (err) {
            updateEndpoint(ep.id, { status: 'error', testError: String(err) });
            return;
        }
        if (result.ok) {
            updateEndpoint(ep.id, { status: 'ok', discoveredModels: result.models, showModels: result.models.length > 0 });
            // Pull the refreshed POOL (now includes this endpoint's imported catalogue).
            // Deliberately do NOT reset the endpoint cards — that would wipe the
            // just-set status/discoveredModels on the card the user is looking at.
            try {
                const cfg = await fetchBYOMConfig();
                setPresets(cfg.presets);
                setActivePresetId(cfg.active_preset_id);
                setDiscovered(cfg.discovered);
                setPricing(cfg.model_pricing ?? {});
            } catch { /* keep current state */ }
        } else {
            updateEndpoint(ep.id, { status: 'error', testError: result.error ?? 'Connection failed' });
        }
    }, [updateEndpoint, specFor]);

    // Quick-connect: build an endpoint from the strip, add it, and run the same
    // save→test→import path. Sync the ref so the pre-test save already sees it.
    const handleQuickConnect = useCallback(async (): Promise<void> => {
        const def = specFor(quickProvider);
        const ep = toUi({
            id: makeId(),
            name: def.label,
            url: def.url,
            api_key: quickKey,
            provider: quickProvider as Provider,
        });
        const next = [...endpointsRef.current, ep];
        endpointsRef.current = next;
        setEndpoints(next);
        setQuickBusy(true);
        setQuickError(null);
        try {
            await handleTest(ep);
        } finally {
            setQuickBusy(false);
            setQuickKey('');
        }
    }, [quickProvider, quickKey, specFor, handleTest]);

    const handleSave = useCallback(async (): Promise<void> => {
        setSaving(true);
        setSaveError(null);
        const payload: BYOMConfigPayload = {
            endpoints: endpointsRef.current.map(({ id, name, url, api_key, provider }) => ({ id, name, url, api_key, provider })),
        };
        try {
            const cfg = await saveBYOMConfig(payload);
            setEndpoints(cfg.endpoints.map(toUi));
            setPresets(cfg.presets);
            setActivePresetId(cfg.active_preset_id);
            setDiscovered(cfg.discovered);
            setPricing(cfg.model_pricing ?? {});
            setEndpointSavedAt(Date.now());
            setTimeout(() => setEndpointSavedAt(null), 2000);
        } catch (err) {
            setSaveError(String(err));
        } finally {
            setSaving(false);
        }
    }, []);

    const handleActivatePreset = useCallback(async (presetId: string): Promise<void> => {
        setActivating(presetId);
        try {
            const cfg = await saveBYOMConfig({ active_preset_id: presetId });
            setActivePresetId(cfg.active_preset_id);
            setPresets(cfg.presets);
            setDiscovered(cfg.discovered);
            setPricing(cfg.model_pricing ?? {});
        } catch {
            // keep existing state
        } finally {
            setActivating(null);
        }
    }, []);

    // ---- Preset creation ----
    const handleCreatePreset = useCallback(async (): Promise<void> => {
        if (!newPreset || !newPreset.name.trim()) return;
        const preset: ModelPreset = {
            id: `user-${Date.now()}`,
            name: newPreset.name.trim(),
            description: '',
            is_builtin: false,
            tiers: newPreset.tiers,
        };
        const userPresets = presets.filter(p => !p.is_builtin);
        try {
            const cfg = await saveBYOMConfig({ presets: [...userPresets, preset] });
            setPresets(cfg.presets);
            setDiscovered(cfg.discovered);
            setPricing(cfg.model_pricing ?? {});
            setNewPreset(null);
            setPresetSavedAt(Date.now());
            setTimeout(() => setPresetSavedAt(null), 2000);
        } catch (err) { setPresetSaveError(String(err)); }
    }, [newPreset, presets]);

    const handleDeletePreset = useCallback(async (presetId: string): Promise<void> => {
        const userPresets = presets.filter(p => !p.is_builtin && p.id !== presetId);
        try {
            const cfg = await saveBYOMConfig({ presets: userPresets });
            setPresets(cfg.presets);
            if (activePresetId === presetId) setActivePresetId(null);
        } catch { /* ignore */ }
    }, [presets, activePresetId]);

    const handleSaveEdit = useCallback(async (): Promise<void> => {
        if (!editingPresetId || !editForm || !editForm.name.trim()) return;
        const original = presets.find(p => p.id === editingPresetId);
        if (!original) return;
        const updated: ModelPreset = { ...original, name: editForm.name.trim(), tiers: editForm.tiers };
        const userPresets = presets.map(p => p.id === editingPresetId ? updated : p).filter(p => !p.is_builtin);
        try {
            const cfg = await saveBYOMConfig({ presets: userPresets });
            setPresets(cfg.presets);
            setDiscovered(cfg.discovered);
            setPricing(cfg.model_pricing ?? {});
            setEditingPresetId(null);
            setEditForm(null);
            setPresetSavedAt(Date.now());
            setTimeout(() => setPresetSavedAt(null), 2000);
        } catch (err) { setPresetSaveError(String(err)); }
    }, [editingPresetId, editForm, presets]);

    // Editing a built-in opens the New Preset form pre-filled — a built-in is an
    // editable example, customized and saved as your own preset.
    const startEditFromBuiltin = useCallback((preset: ModelPreset): void => {
        setNewPreset({ name: `${preset.name} (Custom)`, tiers: { ...preset.tiers } });
        setEditingPresetId(null);
        setEditForm(null);
    }, []);

    // ---- URL scheme soft warning ----
    const urlWarning = (url: string): string | null =>
        url && !url.startsWith('http://') && !url.startsWith('https://')
            ? 'URL should start with http:// or https://'
            : null;

    // ---- Group discovered models by provider prefix (with a filter) ----
    const modelsByGroup = discovered
        .filter(m => !detectedFilter || m.id.toLowerCase().includes(detectedFilter.toLowerCase()))
        .reduce<Record<string, DiscoveredModel[]>>((acc, m) => {
            const group = m.id.includes('/') ? m.id.split('/')[0] : 'other';
            (acc[group] = acc[group] ?? []).push(m);
            return acc;
        }, {});

    // ---- Models the user actually wired into a preset (any preset, active or not).
    //      This is the set the Health Check can ping. ----
    const configuredModels = useMemo(() => {
        const set = new Set<string>();
        for (const p of presets) {
            for (const v of Object.values(p.tiers)) {
                if (v && v.trim()) set.add(v.trim());
            }
        }
        return Array.from(set).sort();
    }, [presets]);

    const activePreset = useMemo(
        () => presets.find(p => p.id === activePresetId) ?? null,
        [presets, activePresetId],
    );
    const runningEngines = engines.filter(e => e.running);

    // ---- Model browser: select a pool model into the open tier field ----
    const pickModel = useCallback((modelId: string): void => {
        if (!browseFor) return;
        if (browseFor.target === 'new') {
            setNewPreset(p => p && ({ ...p, tiers: { ...p.tiers, [browseFor.tier]: modelId } }));
        } else {
            setEditForm(f => f && ({ ...f, tiers: { ...f.tiers, [browseFor.tier]: modelId } }));
        }
        setBrowseFor(null);
        setBrowseQuery('');
    }, [browseFor]);

    // ---- Health check: minimal completion to verify a configured model works ----
    const handlePing = useCallback(async (modelId: string): Promise<void> => {
        setPingState(s => ({ ...s, [modelId]: { busy: true, result: null } }));
        try {
            const res = await pingModel({ model_id: modelId });
            setPingState(s => ({ ...s, [modelId]: { busy: false, result: res } }));
        } catch (err) {
            setPingState(s => ({ ...s, [modelId]: { busy: false, result: { ok: false, model: modelId, reply: '', latency_ms: 0, error: String(err) } } }));
        }
    }, []);

    // ====================================================================
    // Render
    // ====================================================================

    if (loading) {
        return (
            <div>
                <SectionHeader title="Bring Your Own Model" subtitle="Loading configuration…" />
                <Card><Skeleton height={22} count={4} /></Card>
            </div>
        );
    }
    if (loadError) {
        return (
            <div>
                <SectionHeader title="Bring Your Own Model" />
                <Card>
                    <EmptyState
                        icon="alert"
                        title="Failed to load config"
                        hint={loadError}
                        action={
                            <Button variant="primary" icon="loader"
                                onClick={() => { setLoading(true); setLoadError(null); fetchBYOMConfig().then(cfg => { setEndpoints(cfg.endpoints.map(toUi)); setPresets(cfg.presets); setActivePresetId(cfg.active_preset_id); setDiscovered(cfg.discovered); setPricing(cfg.model_pricing ?? {}); }).catch(e => setLoadError(String(e))).finally(() => setLoading(false)); }}>
                                Retry
                            </Button>
                        }
                    />
                </Card>
            </div>
        );
    }

    const quickDef = specFor(quickProvider);

    return (
        <div>
            <SectionHeader
                title="Bring Your Own Model"
                subtitle="Connect providers, choose which models run at each tier, and verify they respond."
                actions={<ActiveProjectBadge />}
            />

            {/* ===== KPI SUMMARY ===== */}
            <div className="db-kpi-grid">
                <StatTile label="Endpoints" value={endpoints.length}
                    sub={endpoints.length === 0 ? 'none configured' : `${endpoints.filter(e => e.status === 'ok').length} connected`} />
                <StatTile label="Models available" value={discovered.length}
                    sub="local + imported cloud" />
                <StatTile label="Active preset" value={activePreset?.name ?? 'None'}
                    tone={activePreset ? 'good' : 'default'}
                    sub={activePreset ? 'live now' : 'pick one below'} />
                <StatTile label="Local engines"
                    value={enginesLoading ? <Skeleton width={48} height={22} /> : `${runningEngines.length}/${engines.length}`}
                    sub={enginesLoading ? 'probing…' : (engines.length === 0 ? 'none detected' : 'running / detected')} />
            </div>

            {/* ============================================================ */}
            {/* STEP 1 — CONNECT                                            */}
            {/* ============================================================ */}
            <Step n={1} title="Connect a provider"
                subtitle="Point at a local engine or paste a cloud API key, then Connect to import its models."
                actions={
                    <Button icon="plus"
                        onClick={() => setEndpoints(prev => [...prev, toUi({ id: makeId(), name: '', url: '', api_key: '', provider: 'custom' })])}>
                        Add manually
                    </Button>
                }>
                {/* Quick-connect strip */}
                <Card className="byom-quick">
                    <div className="byom-quick-row">
                        <div className="byom-quick-field">
                            <label className="db-label">Provider</label>
                            <select className="db-input" value={quickProvider} onChange={e => setQuickProvider(e.target.value)}>
                                {providerOptions.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
                            </select>
                        </div>
                        <div className="byom-quick-field byom-quick-key">
                            <label className="db-label">
                                API key{!quickDef.needsKey && <span className="db-muted"> — not needed for local</span>}
                            </label>
                            <div className="db-row" style={{ gap: 6 }}>
                                <input className="db-input" style={{ flex: 1 }}
                                    type={quickShowKey ? 'text' : 'password'}
                                    value={quickKey}
                                    onChange={e => setQuickKey(e.target.value)}
                                    placeholder={quickDef.keyHint || (quickDef.needsKey ? 'sk-…' : 'Leave empty for local')} />
                                <Button variant="ghost" icon={quickShowKey ? 'eye-off' : 'eye'}
                                    aria-label={quickShowKey ? 'Hide key' : 'Show key'}
                                    onClick={() => setQuickShowKey(v => !v)} />
                            </div>
                        </div>
                        <Button variant="primary" icon="plug" disabled={quickBusy} onClick={() => void handleQuickConnect()}>
                            {quickBusy ? 'Connecting…' : 'Connect'}
                        </Button>
                    </div>
                    {quickDef.helpUrl && (
                        <div className="db-muted" style={{ marginTop: 6 }}>
                            {quickDef.description}{' '}
                            <a href={quickDef.helpUrl} target="_blank" rel="noreferrer" className="byom-link">Get your key →</a>
                        </div>
                    )}
                    {quickError && <div className="byom-field-error" style={{ marginTop: 6 }}>{quickError}</div>}
                </Card>

                {/* Running local engines */}
                {runningEngines.length > 0 && (
                    <div className="byom-engine-row">
                        <span className="db-muted" style={{ fontSize: 11 }}>Detected locally:</span>
                        {runningEngines.map(eng => (
                            <span key={eng.id} className="byom-engine-item">
                                <Badge status="good" icon="cpu">{eng.name} · {eng.model_count} model{eng.model_count !== 1 ? 's' : ''}</Badge>
                                <Button variant="ghost" icon="plus"
                                    onClick={() => setEndpoints(prev => [...prev, toUi({ id: makeId(), name: eng.name, url: eng.url, api_key: '', provider: eng.id as Provider })])}>
                                    Add
                                </Button>
                            </span>
                        ))}
                    </div>
                )}

                {/* Endpoint cards */}
                {endpoints.length === 0 ? (
                    <EmptyState icon="plug" title="No endpoints yet"
                        hint="Use Quick connect above, add a detected local engine, or Add manually." />
                ) : endpoints.map(ep => {
                    const warn = urlWarning(ep.url);
                    const def = specFor(ep.provider);
                    const meta = STATUS_META[ep.status];
                    return (
                        <Card key={ep.id} className="byom-endpoint">
                            {/* Name row + status */}
                            <div className="db-row" style={{ marginBottom: 10, gap: 10 }}>
                                <div style={{ flex: 1 }}>
                                    <input
                                        className={`db-input${ep.fieldErrors.name ? ' byom-input-error' : ''}`}
                                        value={ep.name}
                                        onChange={e => updateEndpoint(ep.id, { name: e.target.value, fieldErrors: { ...ep.fieldErrors, name: undefined } })}
                                        placeholder="Endpoint name"
                                        style={{ fontWeight: 600, width: '100%' }}
                                    />
                                    {ep.fieldErrors.name && <div className="byom-field-error">{ep.fieldErrors.name}</div>}
                                </div>
                                <Badge status={meta.badge} icon={meta.icon}>{meta.label}</Badge>
                                <Button variant="ghost" icon="trash"
                                    onClick={() => setConfirm({
                                        open: true,
                                        title: 'Remove endpoint?',
                                        body: `"${ep.name || 'This endpoint'}" will be removed. Click Save to persist.`,
                                        onConfirm: () => { setEndpoints(prev => prev.filter(e => e.id !== ep.id)); closeConfirm(); },
                                    })}>
                                    Remove
                                </Button>
                            </div>

                            {/* Provider + (conditional) Base URL */}
                            <div className={def.hidesBaseUrl ? '' : 'db-grid-2'} style={{ marginBottom: 10 }}>
                                <div>
                                    <label className="db-label">Provider</label>
                                    <select className="db-input" value={ep.provider}
                                        onChange={e => handleProviderChange(ep, e.target.value as Provider)}>
                                        {providerOptions.map(o => <option key={o.id} value={o.id}>{o.label}</option>)}
                                    </select>
                                    <div className="byom-provider-hint">
                                        {def.description}
                                        {def.helpUrl && (
                                            <>{' '}<a href={def.helpUrl} target="_blank" rel="noreferrer" className="byom-link">Get your key →</a></>
                                        )}
                                    </div>
                                </div>
                                {!def.hidesBaseUrl && (
                                    <div>
                                        <label className="db-label">Base URL</label>
                                        <input
                                            className={`db-input${ep.fieldErrors.url ? ' byom-input-error' : ''}`}
                                            value={ep.url}
                                            onChange={e => updateEndpoint(ep.id, { url: e.target.value, urlAutoFilled: false, fieldErrors: { ...ep.fieldErrors, url: undefined } })}
                                            placeholder={def.url || 'https://…'}
                                        />
                                        {ep.fieldErrors.url && <div className="byom-field-error">{ep.fieldErrors.url}</div>}
                                        {!ep.fieldErrors.url && warn && <div className="byom-field-warn">{warn}</div>}
                                    </div>
                                )}
                            </div>

                            {/* API Key */}
                            <div style={{ marginBottom: 10 }}>
                                <label className="db-label">
                                    API Key
                                    {!def.needsKey && <span className="db-muted"> — not required for local engines</span>}
                                </label>
                                <div className="db-row" style={{ gap: 6 }}>
                                    <input
                                        className="db-input"
                                        type={ep.showKey ? 'text' : 'password'}
                                        value={ep.api_key}
                                        onChange={e => updateEndpoint(ep.id, { api_key: e.target.value })}
                                        placeholder={def.keyHint || (def.needsKey ? 'sk-…' : 'Leave empty for local')}
                                        style={{ flex: 1 }}
                                    />
                                    <Button variant="ghost" icon={ep.showKey ? 'eye-off' : 'eye'}
                                        aria-label={ep.showKey ? 'Hide key' : 'Show key'}
                                        onClick={() => updateEndpoint(ep.id, { showKey: !ep.showKey })} />
                                </div>
                            </div>

                            {/* Test + result */}
                            <div className="db-row" style={{ gap: 10, flexWrap: 'wrap' }}>
                                <Button variant="primary" icon="zap" disabled={ep.status === 'testing'} onClick={() => handleTest(ep)}>
                                    {ep.status === 'testing' ? 'Testing…' : 'Test connection'}
                                </Button>
                                {ep.discoveredModels.length > 0 && (
                                    <Button variant="ghost" icon={ep.showModels ? 'chevron-down' : 'chevron-right'}
                                        onClick={() => updateEndpoint(ep.id, { showModels: !ep.showModels })}>
                                        {ep.discoveredModels.length} model{ep.discoveredModels.length !== 1 ? 's' : ''} found
                                    </Button>
                                )}
                            </div>
                            {ep.testError && <div className="byom-field-error" style={{ marginTop: 6 }}>{ep.testError}</div>}
                            {ep.discoveredModels.length > 0 && ep.showModels && (
                                <ul className="byom-model-list">
                                    {ep.discoveredModels.map(m => <li key={m.id}>{m.name}</li>)}
                                </ul>
                            )}
                        </Card>
                    );
                })}

                {endpoints.length > 0 && (
                    <div className="db-row" style={{ marginTop: 4, gap: 10 }}>
                        <Button variant="primary" icon="check" onClick={handleSave} disabled={saving}>
                            {saving ? 'Saving…' : 'Save endpoints'}
                        </Button>
                        {saveError && <span className="byom-field-error">{saveError}</span>}
                        {endpointSavedAt && <Badge status="good" icon="check">Saved</Badge>}
                    </div>
                )}

                {/* Detected models — compact reference (a byproduct of connecting) */}
                {discovered.length > 0 && (
                    <Card className="byom-discovered-section" style={{ marginTop: 14 }}>
                        <button className="byom-discovered-toggle" onClick={() => setShowDiscovered(v => !v)}>
                            <Icon name={showDiscovered ? 'chevron-down' : 'chevron-right'} size={13} />
                            {' '}Detected models ({discovered.length} total)
                        </button>
                        {showDiscovered && discovered.length > 12 && (
                            <input className="db-input byom-detected-filter" value={detectedFilter}
                                onChange={e => setDetectedFilter(e.target.value)} placeholder="Filter models…" />
                        )}
                        {showDiscovered && Object.entries(modelsByGroup).map(([group, models]) => (
                            <div key={group} className="byom-discovered-group">
                                <div className="byom-discovered-group-label">{group}</div>
                                {models.map(m => (
                                    <div key={m.id} className="byom-discovered-model-row">
                                        <span className="byom-discovered-model-name">{m.name}</span>
                                        <span className="db-row" style={{ gap: 8 }}>
                                            <CostBadge price={pricing[m.id]} />
                                            <span className="byom-discovered-model-id">{m.id}</span>
                                        </span>
                                    </div>
                                ))}
                            </div>
                        ))}
                    </Card>
                )}
            </Step>

            {/* ============================================================ */}
            {/* STEP 2 — CONFIGURE PRESET                                   */}
            {/* ============================================================ */}
            <Step n={2} title="Choose which models run at each tier"
                subtitle="A preset maps each routing tier to a model. Activate one to make it live."
                actions={
                    <Button icon="plus"
                        onClick={() => { setNewPreset({ name: '', tiers: { small: '', medium: '', big: '', cloud: '' } }); setEditingPresetId(null); setEditForm(null); }}>
                        New preset
                    </Button>
                }>
                {presetSavedAt && <Badge status="good" icon="check">Saved</Badge>}

                {/* Prominent active-preset summary with cost badges */}
                {activePreset && (
                    <Card className="byom-active-preset">
                        <div className="db-row" style={{ marginBottom: 8, gap: 8 }}>
                            <Badge status="good" icon="check-circle">Active</Badge>
                            <span style={{ fontWeight: 600 }}>{activePreset.name}</span>
                            <span className="db-muted" style={{ marginLeft: 'auto', fontSize: 11 }}>live routing</span>
                        </div>
                        <div className="byom-active-tiers">
                            {Object.entries(TIER_LABELS).map(([key, label]) => {
                                const mid = activePreset.tiers[key];
                                return (
                                    <div key={key} className="byom-active-tier">
                                        <span className="byom-active-tier-label">{label}</span>
                                        <span className="byom-active-tier-model">{mid || '—'}</span>
                                        {mid && <CostBadge price={pricing[mid]} />}
                                    </div>
                                );
                            })}
                        </div>
                    </Card>
                )}

                {/* Available-model pool for the tier comboboxes */}
                <datalist id="byom-models-datalist">
                    {discovered.map(m => <option key={m.id} value={m.id}>{m.id}</option>)}
                </datalist>

                <div className="byom-preset-grid">
                    {presets.map(preset => {
                        const isActive = preset.id === activePresetId;
                        const isBusy = activating === preset.id;
                        const isEditing = editingPresetId === preset.id && editForm !== null;
                        return (
                            <div key={preset.id} className={`byom-preset-card${isActive ? ' byom-preset-card--active' : ''}`}>
                                {isEditing && editForm ? (
                                    <div className="byom-preset-edit-form">
                                        <div className="db-step-title" style={{ fontSize: 13, marginBottom: 10 }}>Edit preset</div>
                                        <div style={{ marginBottom: 10 }}>
                                            <label className="db-label">Preset name</label>
                                            <input className="db-input" value={editForm.name}
                                                onChange={e => setEditForm(f => f && ({ ...f, name: e.target.value }))}
                                                placeholder="Preset name" />
                                        </div>
                                        {Object.entries(TIER_LABELS).map(([key, label]) => (
                                            <div key={key} style={{ marginBottom: 8 }}>
                                                <label className="db-label">{label} model</label>
                                                <div className="byom-tier-row">
                                                    <input className="db-input byom-tier-combobox" list="byom-models-datalist"
                                                        value={editForm.tiers[key] ?? ''}
                                                        onChange={e => setEditForm(f => f && ({ ...f, tiers: { ...f.tiers, [key]: e.target.value } }))}
                                                        placeholder="— inherit from config.yaml —" />
                                                    <button type="button" className="byom-tier-browse" title="Browse available models"
                                                        onClick={() => { setBrowseFor({ tier: key, target: 'edit' }); setBrowseQuery(''); }}>Browse</button>
                                                    {editForm.tiers[key] && (
                                                        <button type="button" className="byom-tier-clear" title="Clear"
                                                            onClick={() => setEditForm(f => f && ({ ...f, tiers: { ...f.tiers, [key]: '' } }))}>×</button>
                                                    )}
                                                </div>
                                            </div>
                                        ))}
                                        <div className="db-row" style={{ gap: 8, marginTop: 12 }}>
                                            <Button variant="primary" disabled={!editForm.name.trim()} onClick={handleSaveEdit}>Save</Button>
                                            <Button onClick={() => { setEditingPresetId(null); setEditForm(null); setPresetSaveError(null); }}>Cancel</Button>
                                        </div>
                                        {presetSaveError && <div className="byom-field-warn" style={{ marginTop: 4 }}>{presetSaveError}</div>}
                                    </div>
                                ) : (
                                    <>
                                        <div className="byom-preset-name">
                                            {preset.name}
                                            {preset.is_builtin && (
                                                <span className="byom-preset-builtin-badge" title="Auto-generated from connected engines. Edit to save as your own.">Built-in</span>
                                            )}
                                        </div>
                                        {preset.description && <div className="byom-preset-desc">{preset.description}</div>}
                                        <div className="byom-preset-tiers">
                                            {Object.entries(TIER_LABELS).map(([key, label]) => (
                                                <div key={key} className="byom-preset-tier-row">
                                                    <span className="byom-preset-tier-label">{label}</span>
                                                    <span className="byom-preset-tier-val">{preset.tiers[key] || '—'}</span>
                                                </div>
                                            ))}
                                        </div>
                                        <div className="db-row" style={{ marginTop: 10, gap: 6 }}>
                                            {isActive ? (
                                                <Badge status="good" icon="check">Active</Badge>
                                            ) : (
                                                <Button variant="primary" disabled={isBusy}
                                                    onClick={() => {
                                                        if (activePresetId && activePresetId !== preset.id) {
                                                            setConfirm({
                                                                open: true,
                                                                title: 'Switch active preset?',
                                                                body: `This will rewrite config.yaml and reload LiteLLM to use "${preset.name}".`,
                                                                onConfirm: () => { void handleActivatePreset(preset.id); closeConfirm(); },
                                                            });
                                                        } else {
                                                            void handleActivatePreset(preset.id);
                                                        }
                                                    }}>
                                                    {isBusy ? 'Applying…' : 'Activate'}
                                                </Button>
                                            )}
                                            {preset.is_builtin ? (
                                                <Button onClick={() => startEditFromBuiltin(preset)} title="Edit this example and save it as your own preset">Edit</Button>
                                            ) : (
                                                <Button onClick={() => { setEditingPresetId(preset.id); setEditForm({ name: preset.name, tiers: { ...preset.tiers } }); setNewPreset(null); }}>Edit</Button>
                                            )}
                                            {!preset.is_builtin && (
                                                <Button variant="ghost" icon="trash"
                                                    onClick={() => setConfirm({
                                                        open: true,
                                                        title: 'Delete preset?',
                                                        body: `"${preset.name}" will be permanently deleted.`,
                                                        warning: preset.id === activePresetId ? 'This is your currently active preset.' : undefined,
                                                        onConfirm: () => { void handleDeletePreset(preset.id); closeConfirm(); },
                                                    })}>
                                                    Delete
                                                </Button>
                                            )}
                                        </div>
                                    </>
                                )}
                            </div>
                        );
                    })}
                </div>

                {/* New Preset form */}
                {newPreset && (
                    <Card style={{ marginTop: 12 }}>
                        <div className="db-step-title" style={{ fontSize: 13, marginBottom: 10 }}>New preset</div>
                        <div style={{ marginBottom: 10 }}>
                            <label className="db-label">Preset name</label>
                            <input className="db-input" value={newPreset.name}
                                onChange={e => setNewPreset(p => p && ({ ...p, name: e.target.value }))}
                                placeholder="My Custom Preset" />
                        </div>
                        {Object.entries(TIER_LABELS).map(([key, label]) => (
                            <div key={key} style={{ marginBottom: 8 }}>
                                <label className="db-label">{label} model</label>
                                <div className="byom-tier-row">
                                    <input className="db-input byom-tier-combobox" list="byom-models-datalist"
                                        value={newPreset.tiers[key] ?? ''}
                                        onChange={e => setNewPreset(p => p && ({ ...p, tiers: { ...p.tiers, [key]: e.target.value } }))}
                                        placeholder="— inherit from config.yaml —" />
                                    <button type="button" className="byom-tier-browse" title="Browse available models"
                                        onClick={() => { setBrowseFor({ tier: key, target: 'new' }); setBrowseQuery(''); }}>Browse</button>
                                    {newPreset.tiers[key] && (
                                        <button type="button" className="byom-tier-clear" title="Clear"
                                            onClick={() => setNewPreset(p => p && ({ ...p, tiers: { ...p.tiers, [key]: '' } }))}>×</button>
                                    )}
                                </div>
                            </div>
                        ))}
                        <div className="db-row" style={{ gap: 8, marginTop: 12 }}>
                            <Button variant="primary" onClick={handleCreatePreset} disabled={!newPreset.name.trim()}>Create</Button>
                            <Button onClick={() => setNewPreset(null)}>Cancel</Button>
                        </div>
                    </Card>
                )}
            </Step>

            {/* ============================================================ */}
            {/* STEP 3 — VERIFY                                             */}
            {/* ============================================================ */}
            <Step n={3} title="Verify a model responds"
                subtitle="Sends a tiny one-word prompt (≈5 tokens) to each model wired into a preset. Near-zero cost.">
                <Card>
                    {configuredModels.length === 0 ? (
                        <EmptyState icon="zap" title="Nothing to verify yet"
                            hint="Wire a model into a preset tier above, then health-check it here." />
                    ) : (
                        <ul className="byom-health-list">
                            {configuredModels.map(mid => {
                                const st = pingState[mid];
                                const ok = st?.result?.ok;
                                return (
                                    <li key={mid} className="byom-health-row">
                                        <span className="byom-health-model">{mid}</span>
                                        <CostBadge price={pricing[mid]} />
                                        <Button variant="secondary" icon="send" disabled={st?.busy} onClick={() => void handlePing(mid)}>
                                            {st?.busy ? 'Pinging…' : 'Ping'}
                                        </Button>
                                        {st?.result && (ok
                                            ? <Badge status="good" icon="check-circle">{st.result.reply || 'OK'} · {st.result.latency_ms} ms</Badge>
                                            : <Badge status="critical" icon="x-circle">{st.result.error || 'failed'}</Badge>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </Card>
            </Step>

            {/* ===== MODEL BROWSER MODAL ===== */}
            {browseFor && (
                <div className="ui-overlay" role="dialog" aria-modal="true" aria-label="Select a model" onClick={() => setBrowseFor(null)}>
                    <div className="ui-overlay-panel byom-model-browser" onClick={e => e.stopPropagation()}>
                        <div className="db-card-title" style={{ marginBottom: 0 }}>
                            Select a model · {TIER_LABELS[browseFor.tier] ?? browseFor.tier} tier
                        </div>
                        <input className="db-input byom-browser-search" autoFocus value={browseQuery}
                            onChange={e => setBrowseQuery(e.target.value)} placeholder="Search available models…" />
                        <div className="byom-browser-list">
                            {discovered.length === 0 && (
                                <div className="db-muted">No models available yet. Connect an endpoint in Step 1 to import its models.</div>
                            )}
                            {Object.entries(
                                discovered
                                    .filter(m => !browseQuery || m.id.toLowerCase().includes(browseQuery.toLowerCase()))
                                    .reduce<Record<string, DiscoveredModel[]>>((acc, m) => {
                                        const g = m.id.includes('/') ? m.id.split('/')[0] : 'other';
                                        (acc[g] = acc[g] ?? []).push(m); return acc;
                                    }, {})
                            ).map(([group, models]) => (
                                <div key={group} className="byom-browser-group">
                                    <div className="byom-discovered-group-label">{group}</div>
                                    {models.map(m => (
                                        <button key={m.id} className="byom-browser-item" onClick={() => pickModel(m.id)}>
                                            <span>{m.id}</span>
                                            <CostBadge price={pricing[m.id]} />
                                        </button>
                                    ))}
                                </div>
                            ))}
                        </div>
                        <div className="db-row" style={{ justifyContent: 'flex-end', marginTop: 12 }}>
                            <Button onClick={() => setBrowseFor(null)}>Close</Button>
                        </div>
                    </div>
                </div>
            )}

            {/* ===== CONFIRMATION MODAL ===== */}
            <ConfirmModal
                open={confirm.open}
                title={confirm.title}
                body={confirm.body}
                warning={confirm.warning}
                danger
                onConfirm={confirm.onConfirm}
                onCancel={closeConfirm}
            />
        </div>
    );
}
