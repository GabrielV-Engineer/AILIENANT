import { useEffect, useState } from 'react';

interface McpServer { id: string; name: string; uri: string; transport: string; enabled: boolean; }
interface McpTestResult { reachable: boolean; tool_count: number; error?: string; }
interface Skill { id: string; name: string; body: string; }

type SubTab = 'mcp' | 'skills';

async function getJson<T>(url: string): Promise<T | null> {
    try { const r = await fetch(url); return r.ok ? (await r.json() as T) : null; }
    catch { return null; }
}
async function sendJson<T>(url: string, method: string, body?: unknown): Promise<T | null> {
    try {
        const r = await fetch(url, {
            method,
            headers: body ? { 'Content-Type': 'application/json' } : undefined,
            body: body ? JSON.stringify(body) : undefined,
        });
        return r.ok ? (await r.json() as T) : null;
    } catch { return null; }
}

function McpTab(): JSX.Element {
    const [servers, setServers] = useState<McpServer[] | null>(null);
    const [tests, setTests] = useState<Record<string, McpTestResult | 'loading'>>({});
    const [name, setName] = useState('');
    const [uri, setUri] = useState('');
    const [error, setError] = useState<string | null>(null);

    useEffect(() => { getJson<{ servers: McpServer[] }>('/api/v1/mcp/servers').then(d => setServers(d?.servers ?? [])); }, []);

    const addServer = async (): Promise<void> => {
        if (!name.trim() || !uri.trim()) { return; }
        setError(null);
        const res = await sendJson<{ ok: boolean; error?: string; servers?: McpServer[] }>(
            '/api/v1/mcp/servers', 'POST', { name: name.trim(), uri: uri.trim(), enabled: true });
        if (res?.ok) { setServers(res.servers ?? []); setName(''); setUri(''); }
        else { setError(res?.error ?? 'Failed to save server.'); }
    };
    const toggle = async (s: McpServer): Promise<void> => {
        const res = await sendJson<{ ok: boolean; servers?: McpServer[] }>(
            '/api/v1/mcp/servers', 'POST', { ...s, enabled: !s.enabled });
        if (res?.ok) { setServers(res.servers ?? []); }
    };
    const remove = async (id: string): Promise<void> => {
        const res = await sendJson<{ ok: boolean; servers?: McpServer[] }>(`/api/v1/mcp/servers/${id}`, 'DELETE');
        if (res?.ok) { setServers(res.servers ?? []); }
    };
    const test = async (s: McpServer): Promise<void> => {
        setTests(p => ({ ...p, [s.id]: 'loading' }));
        const res = await sendJson<McpTestResult>('/api/v1/mcp/test', 'POST', { uri: s.uri });
        setTests(p => ({ ...p, [s.id]: res ?? { reachable: false, tool_count: 0, error: 'no response' } }));
    };

    return (
        <div>
            <div className="db-card">
                <div className="db-card-title">MCP servers</div>
                {servers === null
                    ? <div className="db-muted">Loading…</div>
                    : servers.length === 0
                        ? <div className="db-muted">No MCP servers configured.</div>
                        : servers.map(s => {
                            const t = tests[s.id];
                            return (
                                <div key={s.id} className="db-audit-row">
                                    <div style={{ flex: 1 }}>
                                        <div style={{ fontWeight: 600, fontSize: 12 }}>{s.name} {!s.enabled && <span className="db-muted">(disabled)</span>}</div>
                                        <div className="db-muted">{s.uri}</div>
                                        {t && t !== 'loading' && (
                                            <div className="db-traffic-light" style={{ marginTop: 4 }}>
                                                <div className="db-tl-dot" style={{ background: t.reachable ? '#63a583' : '#F85149' }} />
                                                <span style={{ fontSize: 11 }}>{t.reachable ? `${t.tool_count} tools` : (t.error ?? 'unreachable')}</span>
                                            </div>
                                        )}
                                    </div>
                                    <div className="db-row" style={{ gap: 6 }}>
                                        <button className="db-btn db-btn-secondary" disabled={t === 'loading'} onClick={() => test(s)}>{t === 'loading' ? 'Testing…' : 'Test'}</button>
                                        <button className="db-btn db-btn-secondary" onClick={() => toggle(s)}>{s.enabled ? 'Disable' : 'Enable'}</button>
                                        <button className="db-btn db-btn-danger" onClick={() => remove(s.id)}>Delete</button>
                                    </div>
                                </div>
                            );
                        })}
            </div>

            <div className="db-card">
                <div className="db-card-title">Add server</div>
                <div className="db-muted" style={{ marginBottom: 10 }}>
                    Warning: a <code>stdio://</code> server executes a local command on this machine. Only add servers you trust.
                </div>
                <div className="db-col">
                    <input className="db-input" placeholder="name" value={name} onChange={e => setName(e.target.value)} />
                    <input className="db-input" placeholder="stdio:///abs/path/to/server?arg=x" value={uri} onChange={e => setUri(e.target.value)} />
                    {error && <div className="db-muted" style={{ color: '#F85149' }}>{error}</div>}
                    <button className="db-btn db-btn-primary" onClick={addServer}>Add server</button>
                </div>
            </div>
        </div>
    );
}

function SkillsTab(): JSX.Element {
    const [skills, setSkills] = useState<Skill[] | null>(null);
    const [name, setName] = useState('');
    const [body, setBody] = useState('');
    const [error, setError] = useState<string | null>(null);

    useEffect(() => { getJson<{ skills: Skill[] }>('/api/v1/skills').then(d => setSkills(d?.skills ?? [])); }, []);

    const create = async (): Promise<void> => {
        if (!name.trim() || !body.trim()) { return; }
        setError(null);
        const res = await sendJson<{ ok: boolean; error?: string; skills?: Skill[] }>(
            '/api/v1/skills', 'POST', { name: name.trim(), body: body.trim() });
        if (res?.ok) { setSkills(res.skills ?? []); setName(''); setBody(''); }
        else { setError(res?.error ?? 'Failed to save skill.'); }
    };
    const remove = async (id: string): Promise<void> => {
        const res = await sendJson<{ ok: boolean; skills?: Skill[] }>(`/api/v1/skills/${id}`, 'DELETE');
        if (res?.ok) { setSkills(res.skills ?? []); }
    };

    return (
        <div>
            <div className="db-card">
                <div className="db-card-title">Saved skills</div>
                {skills === null
                    ? <div className="db-muted">Loading…</div>
                    : skills.length === 0
                        ? <div className="db-muted">No skills yet. Create one below.</div>
                        : skills.map(s => (
                            <div key={s.id} className="db-audit-row">
                                <div style={{ flex: 1, minWidth: 0 }}>
                                    <div style={{ fontWeight: 600, fontSize: 12 }}>{s.name}</div>
                                    {/* Plain text node — never dangerouslySetInnerHTML (S3). */}
                                    <div className="db-muted" style={{ whiteSpace: 'pre-wrap', overflow: 'hidden', textOverflow: 'ellipsis', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                                        {s.body}
                                    </div>
                                </div>
                                <button className="db-btn db-btn-danger" onClick={() => remove(s.id)}>Delete</button>
                            </div>
                        ))}
            </div>

            <div className="db-card">
                <div className="db-card-title">Create skill</div>
                <div className="db-col">
                    <input className="db-input" placeholder="skill name" value={name} onChange={e => setName(e.target.value)} />
                    <textarea className="db-input" rows={5} placeholder="reusable instruction snippet…" value={body} onChange={e => setBody(e.target.value)} />
                    {error && <div className="db-muted" style={{ color: '#F85149' }}>{error}</div>}
                    <button className="db-btn db-btn-primary" onClick={create}>Save skill</button>
                </div>
            </div>
        </div>
    );
}

export function ExtensionsPanel(): JSX.Element {
    const [tab, setTab] = useState<SubTab>('mcp');
    return (
        <div>
            <div className="db-section-title">Extensions</div>
            <div className="db-row" style={{ gap: 8, marginBottom: 16 }}>
                <button className={tab === 'mcp' ? 'db-btn db-btn-primary' : 'db-btn db-btn-secondary'} onClick={() => setTab('mcp')}>MCP Servers</button>
                <button className={tab === 'skills' ? 'db-btn db-btn-primary' : 'db-btn db-btn-secondary'} onClick={() => setTab('skills')}>Skills</button>
            </div>
            {tab === 'mcp' ? <McpTab /> : <SkillsTab />}
        </div>
    );
}
