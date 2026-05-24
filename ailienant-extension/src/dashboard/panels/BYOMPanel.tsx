import { useCallback, useEffect, useRef, useState } from 'react';
import {
    type BYOMConfigResponse,
    type BYOMConfigPayload,
    type DiscoveredModel,
    type EndpointConfig,
    type EngineStatus,
    type ModelPreset,
    type Provider,
    type TestConnectionResponse,
    fetchBYOMConfig,
    fetchEngineStatus,
    saveBYOMConfig,
    testEndpoint,
} from './byom/api';

// ---------------------------------------------------------------------------
// Provider defaults — auto-fill Base URL and guidance per provider
// ---------------------------------------------------------------------------

const PROVIDER_DEFAULTS: Record<Provider, { url: string; needsKey: boolean; keyHint: string; description: string }> = {
    ollama:     { url: 'http://localhost:11434',    needsKey: false, keyHint: '',          description: 'Local AI runtime — no key needed' },
    lmstudio:   { url: 'http://localhost:1234',     needsKey: false, keyHint: '',          description: 'LM Studio local server — no key needed' },
    vllm:       { url: 'http://localhost:8000',     needsKey: false, keyHint: '',          description: 'vLLM OpenAI-compatible server' },
    openai:     { url: 'https://api.openai.com',    needsKey: true,  keyHint: 'sk-…',     description: 'OpenAI official API' },
    openrouter: { url: 'https://openrouter.ai/api', needsKey: true,  keyHint: 'sk-or-…',  description: 'Multi-provider routing' },
    anthropic:  { url: 'https://api.anthropic.com', needsKey: true,  keyHint: 'sk-ant-…', description: 'Anthropic Claude API' },
    custom:     { url: '',                          needsKey: false, keyHint: '',          description: 'Any OpenAI-compatible API (/v1/models + /v1/chat/completions). Works with LocalAI, kobold.cpp, TabbyAPI.' },
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
// Component
// ---------------------------------------------------------------------------

export function BYOMPanel(): JSX.Element {
    const [endpoints, setEndpoints] = useState<EndpointUi[]>([]);
    const [presets, setPresets] = useState<ModelPreset[]>([]);
    const [activePresetId, setActivePresetId] = useState<string | null>(null);
    const [discovered, setDiscovered] = useState<DiscoveredModel[]>([]);

    const [loading, setLoading] = useState(true);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const [saveError, setSaveError] = useState<string | null>(null);

    const [engines, setEngines] = useState<EngineStatus[]>([]);
    const [enginesLoading, setEnginesLoading] = useState(true);

    const [newPreset, setNewPreset] = useState<NewPresetForm | null>(null);
    const [activating, setActivating] = useState<string | null>(null);
    const [editingPresetId, setEditingPresetId] = useState<string | null>(null);
    const [editForm, setEditForm] = useState<NewPresetForm | null>(null);
    const [showDiscovered, setShowDiscovered] = useState(false);

    const [confirm, setConfirm] = useState<ConfirmState>({ open: false, title: '', body: '', onConfirm: () => {} });
    const closeConfirm = () => setConfirm(s => ({ ...s, open: false }));

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
                setLoadError(null);
            })
            .catch(err => setLoadError(String(err)))
            .finally(() => setLoading(false));

        fetchEngineStatus()
            .then(setEngines)
            .catch(() => setEngines([]))
            .finally(() => setEnginesLoading(false));
    }, []);

    // ---- Endpoint helpers ----
    const updateEndpoint = useCallback((id: string, patch: Partial<EndpointUi>): void => {
        setEndpoints(prev => prev.map(e => e.id === id ? { ...e, ...patch } : e));
    }, []);

    const handleProviderChange = useCallback((ep: EndpointUi, provider: Provider): void => {
        const def = PROVIDER_DEFAULTS[provider];
        const shouldFill = !ep.url || ep.urlAutoFilled;
        updateEndpoint(ep.id, {
            provider,
            url: shouldFill ? def.url : ep.url,
            urlAutoFilled: shouldFill,
        });
    }, [updateEndpoint]);

    const handleTest = useCallback(async (ep: EndpointUi): Promise<void> => {
        const errs: EndpointUi['fieldErrors'] = {};
        if (!ep.name.trim()) errs.name = 'Name is required';
        if (!ep.url.trim())  errs.url  = 'URL is required';
        if (Object.keys(errs).length) {
            updateEndpoint(ep.id, { fieldErrors: errs });
            return;
        }
        updateEndpoint(ep.id, { status: 'testing', testError: null, fieldErrors: {}, discoveredModels: [] });
        let result: TestConnectionResponse;
        try {
            result = await testEndpoint({ url: ep.url, api_key: ep.api_key, provider: ep.provider as Provider });
        } catch (err) {
            updateEndpoint(ep.id, { status: 'error', testError: String(err) });
            return;
        }
        if (result.ok) {
            updateEndpoint(ep.id, { status: 'ok', discoveredModels: result.models, showModels: result.models.length > 0 });
        } else {
            updateEndpoint(ep.id, { status: 'error', testError: result.error ?? 'Connection failed' });
        }
    }, [updateEndpoint]);

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
            setNewPreset(null);
        } catch { /* ignore */ }
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
            setEditingPresetId(null);
            setEditForm(null);
        } catch { /* leave edit open on failure */ }
    }, [editingPresetId, editForm, presets]);

    // ---- URL scheme soft warning ----
    const urlWarning = (url: string): string | null =>
        url && !url.startsWith('http://') && !url.startsWith('https://')
            ? 'URL should start with http:// or https://'
            : null;

    // ---- Group discovered models by provider prefix ----
    const modelsByGroup = discovered.reduce<Record<string, DiscoveredModel[]>>((acc, m) => {
        const group = m.id.includes('/') ? m.id.split('/')[0] : 'other';
        (acc[group] = acc[group] ?? []).push(m);
        return acc;
    }, {});

    // ====================================================================
    // Render
    // ====================================================================

    if (loading) {
        return <div className="db-card" style={{ color: 'var(--vscode-descriptionForeground)' }}>Loading BYOM config…</div>;
    }
    if (loadError) {
        return (
            <div className="db-card byom-error">
                Failed to load config: {loadError}
                <br />
                <button className="db-btn db-btn-primary" style={{ marginTop: 8 }}
                    onClick={() => { setLoading(true); setLoadError(null); fetchBYOMConfig().then(cfg => { setEndpoints(cfg.endpoints.map(toUi)); setPresets(cfg.presets); setActivePresetId(cfg.active_preset_id); setDiscovered(cfg.discovered); }).catch(e => setLoadError(String(e))).finally(() => setLoading(false)); }}>
                    Retry
                </button>
            </div>
        );
    }

    return (
        <div>
            {/* ===== ENGINE HEALTH BAR ===== */}
            {!enginesLoading && engines.length > 0 && (
                <div className="byom-engine-bar">
                    <span className="byom-engine-bar-label">Local Engines</span>
                    {engines.map(eng => (
                        <div key={eng.id} className={`byom-engine-chip${eng.running ? ' byom-engine-chip--running' : ''}`}>
                            <span className={`byom-engine-dot${eng.running ? ' byom-engine-dot--on' : ''}`} />
                            <span className="byom-engine-name">{eng.name}</span>
                            {eng.running
                                ? <span className="byom-engine-count">{eng.model_count} model{eng.model_count !== 1 ? 's' : ''}</span>
                                : <span className="byom-engine-offline">Not running</span>
                            }
                            {eng.running && (
                                <button className="db-btn db-btn-secondary byom-engine-add"
                                    onClick={() => setEndpoints(prev => [...prev, toUi({
                                        id: makeId(),
                                        name: eng.name,
                                        url: eng.url,
                                        api_key: '',
                                        provider: eng.id as Provider,
                                    })])}>
                                    + Add
                                </button>
                            )}
                        </div>
                    ))}
                </div>
            )}

            {/* ===== ENDPOINTS SECTION ===== */}
            <div className="db-row" style={{ marginBottom: 12, alignItems: 'center' }}>
                <span className="db-section-title" style={{ marginBottom: 0 }}>Endpoints</span>
                <button className="db-btn db-btn-secondary" style={{ marginLeft: 'auto' }}
                    onClick={() => setEndpoints(prev => [...prev, toUi({ id: makeId(), name: '', url: '', api_key: '', provider: 'custom' })])}>
                    + Add Endpoint
                </button>
            </div>

            {endpoints.length === 0 && (
                <div className="db-card" style={{ color: 'var(--vscode-descriptionForeground)', textAlign: 'center' }}>
                    No endpoints configured. Add one above or click <strong>+ Add</strong> next to a running local engine.
                </div>
            )}

            {endpoints.map(ep => {
                const warn = urlWarning(ep.url);
                const dotColor = ep.status === 'ok' ? '#63a583' : ep.status === 'error' ? '#F85149' : ep.status === 'testing' ? '#E3B341' : '#484F58';
                const def = PROVIDER_DEFAULTS[ep.provider];
                return (
                    <div key={ep.id} className="db-card" style={{ marginBottom: 12 }}>
                        {/* Name row */}
                        <div className="db-row" style={{ marginBottom: 10 }}>
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
                            <span
                                className={`byom-status-dot${ep.status === 'testing' ? ' byom-status-dot--pulse' : ''}`}
                                style={{ background: dotColor }}
                                title={ep.status}
                            />
                            <button className="db-btn db-btn-secondary" style={{ fontSize: 11, padding: '4px 8px' }}
                                onClick={() => setConfirm({
                                    open: true,
                                    title: 'Remove endpoint?',
                                    body: `"${ep.name || 'This endpoint'}" will be removed. Click Save Endpoints to persist.`,
                                    onConfirm: () => { setEndpoints(prev => prev.filter(e => e.id !== ep.id)); closeConfirm(); },
                                })}>
                                Remove
                            </button>
                        </div>

                        {/* Provider + URL */}
                        <div className="db-grid-2" style={{ marginBottom: 10 }}>
                            <div>
                                <label className="db-label">Provider</label>
                                <select className="db-input" value={ep.provider}
                                    onChange={e => handleProviderChange(ep, e.target.value as Provider)}>
                                    <option value="ollama">Ollama</option>
                                    <option value="lmstudio">LM Studio</option>
                                    <option value="vllm">vLLM</option>
                                    <option value="openai">OpenAI</option>
                                    <option value="openrouter">OpenRouter</option>
                                    <option value="anthropic">Anthropic</option>
                                    <option value="custom">Custom</option>
                                </select>
                                <div className="byom-provider-hint">{def.description}</div>
                            </div>
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
                        </div>

                        {/* API Key */}
                        <div style={{ marginBottom: 10 }}>
                            <label className="db-label">
                                API Key
                                {!def.needsKey && <span className="byom-api-key-hint"> — not required for local engines</span>}
                            </label>
                            <div className="db-row">
                                <input
                                    className="db-input"
                                    type={ep.showKey ? 'text' : 'password'}
                                    value={ep.api_key}
                                    onChange={e => updateEndpoint(ep.id, { api_key: e.target.value })}
                                    placeholder={def.keyHint || (def.needsKey ? 'sk-…' : 'Leave empty for local')}
                                    style={{ flex: 1 }}
                                />
                                <button
                                    className="db-btn db-btn-secondary"
                                    style={{ flexShrink: 0, padding: '5px 10px', fontSize: 11 }}
                                    onClick={() => updateEndpoint(ep.id, { showKey: !ep.showKey })}
                                    aria-label={ep.showKey ? 'Hide key' : 'Show key'}>
                                    {ep.showKey ? 'Hide' : 'Show'}
                                </button>
                            </div>
                        </div>

                        {/* Test */}
                        <button
                            className="db-btn db-btn-primary"
                            disabled={ep.status === 'testing'}
                            onClick={() => handleTest(ep)}>
                            {ep.status === 'testing' ? 'Testing…' : 'Test Connection'}
                        </button>
                        {ep.testError && <div className="byom-field-error" style={{ marginTop: 6 }}>{ep.testError}</div>}

                        {/* Discovered models */}
                        {ep.discoveredModels.length > 0 && (
                            <div style={{ marginTop: 8 }}>
                                <button
                                    className="db-btn db-btn-secondary"
                                    style={{ fontSize: 11, padding: '3px 8px' }}
                                    onClick={() => updateEndpoint(ep.id, { showModels: !ep.showModels })}>
                                    {ep.showModels ? '▾' : '▸'} {ep.discoveredModels.length} model{ep.discoveredModels.length !== 1 ? 's' : ''} found
                                </button>
                                {ep.showModels && (
                                    <ul className="byom-model-list">
                                        {ep.discoveredModels.map(m => (
                                            <li key={m.id}>{m.name}</li>
                                        ))}
                                    </ul>
                                )}
                            </div>
                        )}
                    </div>
                );
            })}

            <div className="db-row" style={{ marginBottom: 20 }}>
                <button className="db-btn db-btn-primary" onClick={handleSave} disabled={saving}>
                    {saving ? 'Saving…' : 'Save Endpoints'}
                </button>
                {saveError && <span className="byom-field-error">{saveError}</span>}
            </div>

            {/* ===== DETECTED MODELS (collapsible) ===== */}
            {discovered.length > 0 && (
                <div className="db-card byom-discovered-section" style={{ marginBottom: 20 }}>
                    <button className="byom-discovered-toggle"
                        onClick={() => setShowDiscovered(v => !v)}>
                        {showDiscovered ? '▾' : '▸'} Detected Models ({discovered.length} total)
                    </button>
                    {showDiscovered && Object.entries(modelsByGroup).map(([group, models]) => (
                        <div key={group} className="byom-discovered-group">
                            <div className="byom-discovered-group-label">{group}</div>
                            {models.map(m => (
                                <div key={m.id} className="byom-discovered-model-row">
                                    <span className="byom-discovered-model-name">{m.name}</span>
                                    <span className="byom-discovered-model-id">{m.id}</span>
                                </div>
                            ))}
                        </div>
                    ))}
                </div>
            )}

            {/* ===== MODEL PRESETS SECTION ===== */}
            <div className="db-row" style={{ marginBottom: 12, alignItems: 'center' }}>
                <span className="db-section-title" style={{ marginBottom: 0 }}>Model Presets</span>
                <button className="db-btn db-btn-secondary" style={{ marginLeft: 'auto' }}
                    onClick={() => { setNewPreset({ name: '', tiers: { small: '', medium: '', big: '', cloud: '' } }); setEditingPresetId(null); setEditForm(null); }}>
                    + New Preset
                </button>
            </div>

            <datalist id="byom-models-datalist">
                {discovered.map(m => (
                    <option key={m.id} value={m.id}>{m.name}</option>
                ))}
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
                                    <div className="db-section-title" style={{ fontSize: 13, marginBottom: 10 }}>Edit Preset</div>
                                    <div style={{ marginBottom: 10 }}>
                                        <label className="db-label">Preset Name</label>
                                        <input className="db-input"
                                            value={editForm.name}
                                            onChange={e => setEditForm(f => f && ({ ...f, name: e.target.value }))}
                                            placeholder="Preset name" />
                                    </div>
                                    {Object.entries(TIER_LABELS).map(([key, label]) => (
                                        <div key={key} style={{ marginBottom: 8 }}>
                                            <label className="db-label">{label} model</label>
                                            <input
                                                className="db-input byom-tier-combobox"
                                                list="byom-models-datalist"
                                                value={editForm.tiers[key] ?? ''}
                                                onChange={e => setEditForm(f => f && ({ ...f, tiers: { ...f.tiers, [key]: e.target.value } }))}
                                                placeholder="— inherit from config.yaml —" />
                                        </div>
                                    ))}
                                    <div className="db-row" style={{ gap: 8, marginTop: 12 }}>
                                        <button className="db-btn db-btn-primary"
                                            style={{ fontSize: 11, padding: '4px 10px' }}
                                            disabled={!editForm.name.trim()}
                                            onClick={handleSaveEdit}>Save</button>
                                        <button className="db-btn db-btn-secondary"
                                            style={{ fontSize: 11, padding: '4px 8px' }}
                                            onClick={() => { setEditingPresetId(null); setEditForm(null); }}>Cancel</button>
                                    </div>
                                </div>
                            ) : (
                                <>
                                    <div className="byom-preset-name">{preset.name}</div>
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
                                            <span style={{ fontSize: 11, color: 'var(--accent-primary, #63a583)', fontWeight: 600 }}>✓ Active</span>
                                        ) : (
                                            <button className="db-btn db-btn-primary" style={{ fontSize: 11, padding: '4px 10px' }}
                                                disabled={isBusy}
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
                                            </button>
                                        )}
                                        <button className="db-btn db-btn-secondary" style={{ fontSize: 11, padding: '4px 8px' }}
                                            onClick={() => { setEditingPresetId(preset.id); setEditForm({ name: preset.name, tiers: { ...preset.tiers } }); setNewPreset(null); }}>
                                            Edit
                                        </button>
                                        {!preset.is_builtin && (
                                            <button className="db-btn db-btn-secondary" style={{ fontSize: 11, padding: '4px 8px' }}
                                                onClick={() => setConfirm({
                                                    open: true,
                                                    title: 'Delete preset?',
                                                    body: `"${preset.name}" will be permanently deleted.`,
                                                    warning: preset.id === activePresetId ? 'This is your currently active preset.' : undefined,
                                                    onConfirm: () => { void handleDeletePreset(preset.id); closeConfirm(); },
                                                })}>
                                                Delete
                                            </button>
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
                <div className="db-card" style={{ marginTop: 12 }}>
                    <div className="db-section-title" style={{ fontSize: 13, marginBottom: 10 }}>New Preset</div>
                    <div style={{ marginBottom: 10 }}>
                        <label className="db-label">Preset Name</label>
                        <input
                            className="db-input"
                            value={newPreset.name}
                            onChange={e => setNewPreset(p => p && ({ ...p, name: e.target.value }))}
                            placeholder="My Custom Preset"
                        />
                    </div>
                    {Object.entries(TIER_LABELS).map(([key, label]) => (
                        <div key={key} style={{ marginBottom: 8 }}>
                            <label className="db-label">{label} model</label>
                            <input
                                className="db-input byom-tier-combobox"
                                list="byom-models-datalist"
                                value={newPreset.tiers[key] ?? ''}
                                onChange={e => setNewPreset(p => p && ({ ...p, tiers: { ...p.tiers, [key]: e.target.value } }))}
                                placeholder="— inherit from config.yaml —"
                            />
                        </div>
                    ))}
                    <div className="db-row" style={{ gap: 8, marginTop: 12 }}>
                        <button className="db-btn db-btn-primary" onClick={handleCreatePreset}
                            disabled={!newPreset.name.trim()}>
                            Create
                        </button>
                        <button className="db-btn db-btn-secondary" onClick={() => setNewPreset(null)}>Cancel</button>
                    </div>
                </div>
            )}

            {/* ===== CONFIRMATION MODAL ===== */}
            {confirm.open && (
                <div className="byom-confirm-overlay" onClick={closeConfirm}>
                    <div className="byom-confirm-modal" onClick={e => e.stopPropagation()}>
                        <div className="byom-confirm-title">{confirm.title}</div>
                        <div className="byom-confirm-body">{confirm.body}</div>
                        {confirm.warning && <div className="byom-confirm-warning">{confirm.warning}</div>}
                        <div className="db-row" style={{ gap: 8, marginTop: 16, justifyContent: 'flex-end' }}>
                            <button className="db-btn db-btn-secondary" onClick={closeConfirm}>Cancel</button>
                            <button className="db-btn db-btn-danger" onClick={confirm.onConfirm}>Confirm</button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
