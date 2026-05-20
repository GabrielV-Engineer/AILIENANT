import { useState } from 'react';

interface DirectoryRule {
    id:        string;
    directory: string;
    rules:     string;
}

let _ruleId = 1;
function makeId(): string { return String(_ruleId++); }

export function RulesPanel(): JSX.Element {
    const [globalInstructions, setGlobalInstructions] = useState('');
    const [dirRules, setDirRules] = useState<DirectoryRule[]>([
        { id: makeId(), directory: '/src/auth', rules: 'All mutations must use strict typing.\nNo external dependencies.' },
    ]);
    const [saved, setSaved] = useState(false);

    const addRule = (): void => {
        setDirRules(prev => [...prev, { id: makeId(), directory: '', rules: '' }]);
    };

    const updateRule = (id: string, patch: Partial<DirectoryRule>): void => {
        setDirRules(prev => prev.map(r => r.id === id ? { ...r, ...patch } : r));
    };

    const removeRule = (id: string): void => {
        setDirRules(prev => prev.filter(r => r.id !== id));
    };

    const saveAll = async (): Promise<void> => {
        setSaved(false);
        try {
            // Persist global instructions
            await fetch('/api/v1/system/soul', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: globalInstructions }),
            });
            // Persist directory rules
            for (const r of dirRules) {
                if (!r.directory) { continue; }
                await fetch('/api/v1/telemetry/reject', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ directory: r.directory, rule: r.rules }),
                });
            }
            setSaved(true);
            setTimeout(() => setSaved(false), 3000);
        } catch { /* no-op */ }
    };

    return (
        <div>
            <div className="db-section-title">Rules & Governance</div>

            {/* Global instructions */}
            <div className="db-card">
                <div className="db-card-title">Global Custom Instructions (SOUL.md)</div>
                <textarea
                    className="db-input"
                    rows={6}
                    value={globalInstructions}
                    onChange={e => setGlobalInstructions(e.target.value)}
                    placeholder="You are AILIENANT, an autonomous engineering AI…"
                    style={{ resize: 'vertical', fontFamily: 'monospace', fontSize: 12 }}
                />
            </div>

            {/* Directory-scoped rules */}
            <div className="db-card">
                <div className="db-card-title">Directory Rules</div>
                {dirRules.map(r => (
                    <div key={r.id} style={{ marginBottom: 12, padding: '10px', background: 'var(--bg-main)', borderRadius: 4, border: '1px solid var(--border-subtle)' }}>
                        <div className="db-row" style={{ marginBottom: 6 }}>
                            <input
                                className="db-input"
                                value={r.directory}
                                onChange={e => updateRule(r.id, { directory: e.target.value })}
                                placeholder="/src/module"
                                style={{ fontFamily: 'monospace', fontSize: 12 }}
                            />
                            <button
                                className="db-btn db-btn-secondary"
                                style={{ flexShrink: 0, padding: '4px 10px', fontSize: 11 }}
                                onClick={() => removeRule(r.id)}
                                aria-label="Remove directory rule"
                            >Remove</button>
                        </div>
                        <textarea
                            className="db-input"
                            rows={3}
                            value={r.rules}
                            onChange={e => updateRule(r.id, { rules: e.target.value })}
                            placeholder="Rule 1: All mutations must…&#10;Rule 2: No external deps…"
                            style={{ resize: 'vertical', fontSize: 12 }}
                        />
                    </div>
                ))}
                <button className="db-btn db-btn-secondary" onClick={addRule} style={{ width: '100%', marginBottom: 10 }}>
                    + Add Directory Rule
                </button>
                <div className="db-row">
                    <button className="db-btn db-btn-primary" onClick={saveAll}>Save All Rules</button>
                    {saved && <span style={{ color: '#63a583', fontSize: 12 }}>Saved</span>}
                </div>
            </div>
        </div>
    );
}
