import { useState } from 'react';

interface Endpoint {
    id:       string;
    name:     string;
    url:      string;
    apiKey:   string;
    provider: 'ollama' | 'vllm' | 'openrouter' | 'custom';
    status:   'unknown' | 'ok' | 'error';
}

let _nextId = 1;
function makeId(): string { return String(_nextId++); }

export function BYOMPanel(): JSX.Element {
    const [endpoints, setEndpoints] = useState<Endpoint[]>([
        { id: makeId(), name: 'Local Ollama', url: 'http://localhost:11434', apiKey: '', provider: 'ollama', status: 'unknown' },
    ]);
    const [showKey, setShowKey] = useState<Record<string, boolean>>({});

    const updateEndpoint = (id: string, patch: Partial<Endpoint>): void => {
        setEndpoints(prev => prev.map(e => e.id === id ? { ...e, ...patch } : e));
    };

    const testConnection = async (ep: Endpoint): Promise<void> => {
        updateEndpoint(ep.id, { status: 'unknown' });
        try {
            const r = await fetch('/api/v1/models/available');
            updateEndpoint(ep.id, { status: r.ok ? 'ok' : 'error' });
        } catch {
            updateEndpoint(ep.id, { status: 'error' });
        }
    };

    const addEndpoint = (): void => {
        setEndpoints(prev => [
            ...prev,
            { id: makeId(), name: 'New Endpoint', url: '', apiKey: '', provider: 'custom', status: 'unknown' },
        ]);
    };

    const removeEndpoint = (id: string): void => {
        setEndpoints(prev => prev.filter(e => e.id !== id));
    };

    return (
        <div>
            <div className="db-section-title">BYOM — Bring Your Own Model</div>

            {endpoints.map(ep => (
                <div key={ep.id} className="db-card">
                    <div className="db-row" style={{ marginBottom: 12 }}>
                        <input
                            className="db-input"
                            value={ep.name}
                            onChange={e => updateEndpoint(ep.id, { name: e.target.value })}
                            placeholder="Endpoint name"
                            style={{ fontWeight: 600 }}
                        />
                        <span style={{
                            width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
                            background: ep.status === 'ok' ? '#63a583' : ep.status === 'error' ? '#F85149' : '#484F58',
                        }} title={ep.status} />
                        <button className="db-btn db-btn-secondary" style={{ fontSize: 11, padding: '4px 8px' }}
                            onClick={() => removeEndpoint(ep.id)} aria-label="Remove endpoint">Remove</button>
                    </div>

                    <div className="db-grid-2" style={{ marginBottom: 10 }}>
                        <div>
                            <label className="db-label">Provider</label>
                            <select
                                className="db-input"
                                value={ep.provider}
                                onChange={e => updateEndpoint(ep.id, { provider: e.target.value as Endpoint['provider'] })}
                            >
                                <option value="ollama">Ollama</option>
                                <option value="vllm">vLLM</option>
                                <option value="openrouter">OpenRouter</option>
                                <option value="custom">Custom</option>
                            </select>
                        </div>
                        <div>
                            <label className="db-label">Base URL</label>
                            <input
                                className="db-input"
                                value={ep.url}
                                onChange={e => updateEndpoint(ep.id, { url: e.target.value })}
                                placeholder="http://localhost:11434"
                            />
                        </div>
                    </div>

                    <div style={{ marginBottom: 10 }}>
                        <label className="db-label">API Key</label>
                        <div className="db-row">
                            <input
                                className="db-input"
                                type={showKey[ep.id] ? 'text' : 'password'}
                                value={ep.apiKey}
                                onChange={e => updateEndpoint(ep.id, { apiKey: e.target.value })}
                                placeholder="sk-… or leave empty for local"
                            />
                            <button
                                className="db-btn db-btn-secondary"
                                style={{ flexShrink: 0, padding: '5px 10px', fontSize: 11 }}
                                onClick={() => setShowKey(prev => ({ ...prev, [ep.id]: !prev[ep.id] }))}
                                aria-label={showKey[ep.id] ? 'Hide API key' : 'Show API key'}
                            >
                                {showKey[ep.id] ? 'Hide' : 'Show'}
                            </button>
                        </div>
                    </div>

                    <button className="db-btn db-btn-primary" onClick={() => testConnection(ep)}>
                        Test Connection
                    </button>
                </div>
            ))}

            <button className="db-btn db-btn-secondary" onClick={addEndpoint} style={{ width: '100%' }}>
                + Add Endpoint
            </button>
        </div>
    );
}
