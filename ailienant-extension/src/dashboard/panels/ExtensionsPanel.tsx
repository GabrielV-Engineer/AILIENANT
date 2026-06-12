import { useEffect, useState } from 'react';

interface McpServer { id: string; name: string; uri: string; transport: string; enabled: boolean; }
interface McpTestResult { reachable: boolean; tool_count: number; error?: string; }
interface Skill { id: string; name: string; body: string; }
interface RegistryServer {
    name: string;
    display_name: string;
    description: string;
    source_url: string;
    command: string;
    args: string[];
    secrets: string[];
    tool_tiers: Record<string, string>;
    installed: boolean;
}

type SubTab = 'mcp' | 'skills';
type McpView = 'browse' | 'installed' | 'manual';

// The official, source-reviewable ecosystem pages. Discovery opens these in the
// external browser; in-IDE install stays restricted to the curated cards.
const DISCOVERY_LINKS: ReadonlyArray<{ label: string; url: string }> = [
    { label: 'MCP Registry', url: 'https://registry.modelcontextprotocol.io' },
    { label: 'Reference servers', url: 'https://github.com/modelcontextprotocol/servers' },
];

// Severity-coded privilege tiers — the conscious-consent surface shown before
// a user connects a server.
const TIER_COLORS: Record<string, string> = {
    READ_ONLY: '#63a583',
    WRITE: '#d6a534',
    EXECUTE: '#d98b3a',
    DANGEROUS: '#F85149',
};
function tierColor(tier: string): string { return TIER_COLORS[tier] ?? '#888'; }

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

function RegistryCard(
    { server, onInstalled }: { server: RegistryServer; onInstalled: () => void },
): JSX.Element {
    const [open, setOpen] = useState(false);
    const [secretValues, setSecretValues] = useState<Record<string, string>>({});
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const doInstall = async (): Promise<void> => {
        setBusy(true);
        setError(null);
        const res = await sendJson<{ ok: boolean; error?: string }>(
            '/api/v1/mcp/registry/install', 'POST',
            { name: server.name, secrets: server.secrets.length ? secretValues : undefined });
        setBusy(false);
        if (res?.ok) { setOpen(false); setSecretValues({}); onInstalled(); }
        else { setError(res?.error ?? 'Install failed.'); }
    };
    const onInstallClick = (): void => {
        if (server.secrets.length === 0) { void doInstall(); }
        else { setOpen(o => !o); }
    };

    return (
        <div className="db-audit-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
            <div className="db-row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, fontSize: 12 }}>{server.display_name}</div>
                    <div className="db-muted">{server.description}</div>
                    <div className="db-row" style={{ gap: 4, marginTop: 4, flexWrap: 'wrap' }}>
                        {Object.entries(server.tool_tiers).map(([tool, tier]) => (
                            <span key={tool} title={`${tool}: ${tier}`} style={{ fontSize: 10, padding: '1px 5px', borderRadius: 3, color: '#fff', background: tierColor(tier) }}>
                                {tool} · {tier}
                            </span>
                        ))}
                    </div>
                </div>
                <div className="db-col" style={{ gap: 4, alignItems: 'flex-end', flexShrink: 0 }}>
                    {server.installed
                        ? <span className="db-muted" style={{ fontSize: 11 }}>Installed ✓</span>
                        : <button className="db-btn db-btn-primary" disabled={busy} onClick={onInstallClick}>{busy ? 'Installing…' : 'Install'}</button>}
                    <a href={server.source_url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 11 }}>View source ↗</a>
                </div>
            </div>
            {open && server.secrets.length > 0 && (
                <div className="db-col" style={{ gap: 6, borderTop: '1px solid var(--vscode-panel-border)', paddingTop: 6 }}>
                    <div className="db-muted" style={{ fontSize: 11 }}>
                        Installing grants the privileges above. Provide the required credential(s):
                    </div>
                    {server.secrets.map(name => (
                        <input
                            key={name}
                            className="db-input"
                            type="password"
                            placeholder={name}
                            value={secretValues[name] ?? ''}
                            onChange={e => setSecretValues(v => ({ ...v, [name]: e.target.value }))}
                        />
                    ))}
                    {error && <div className="db-muted" style={{ color: '#F85149' }}>{error}</div>}
                    <button className="db-btn db-btn-primary" disabled={busy} onClick={() => void doInstall()}>{busy ? 'Installing…' : 'Confirm install'}</button>
                </div>
            )}
            {error && !open && <div className="db-muted" style={{ color: '#F85149' }}>{error}</div>}
        </div>
    );
}

function McpTab(): JSX.Element {
    const [view, setView] = useState<McpView>('browse');
    const [servers, setServers] = useState<McpServer[] | null>(null);
    const [registry, setRegistry] = useState<RegistryServer[] | null>(null);
    const [query, setQuery] = useState('');
    const [tests, setTests] = useState<Record<string, McpTestResult | 'loading'>>({});
    const [name, setName] = useState('');
    const [uri, setUri] = useState('');
    const [error, setError] = useState<string | null>(null);

    const refreshServers = (): void => { void getJson<{ servers: McpServer[] }>('/api/v1/mcp/servers').then(d => setServers(d?.servers ?? [])); };
    const refreshRegistry = (): void => { void getJson<{ servers: RegistryServer[] }>('/api/v1/mcp/registry').then(d => setRegistry(d?.servers ?? [])); };
    const refreshAll = (): void => { refreshServers(); refreshRegistry(); };
    useEffect(() => { refreshAll(); }, []);

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
        if (res?.ok) { setServers(res.servers ?? []); refreshRegistry(); }
    };
    const test = async (s: McpServer): Promise<void> => {
        setTests(p => ({ ...p, [s.id]: 'loading' }));
        const res = await sendJson<McpTestResult>('/api/v1/mcp/test', 'POST', { uri: s.uri, server_name: s.name });
        setTests(p => ({ ...p, [s.id]: res ?? { reachable: false, tool_count: 0, error: 'no response' } }));
    };

    const q = query.trim().toLowerCase();
    const filtered = (registry ?? []).filter(r =>
        !q || r.display_name.toLowerCase().includes(q) || r.name.includes(q) || r.description.toLowerCase().includes(q));

    return (
        <div>
            <div className="db-row" style={{ gap: 6, marginBottom: 12 }}>
                <button className={view === 'browse' ? 'db-btn db-btn-primary' : 'db-btn db-btn-secondary'} onClick={() => setView('browse')}>Browse registry</button>
                <button className={view === 'installed' ? 'db-btn db-btn-primary' : 'db-btn db-btn-secondary'} onClick={() => setView('installed')}>Installed</button>
                <button className={view === 'manual' ? 'db-btn db-btn-primary' : 'db-btn db-btn-secondary'} onClick={() => setView('manual')}>Add manually</button>
            </div>

            {view === 'browse' && (
                <div className="db-card">
                    <div className="db-card-title">Curated registry</div>
                    <input className="db-input" placeholder="Search servers…" value={query} onChange={e => setQuery(e.target.value)} style={{ marginBottom: 10 }} />
                    {registry === null
                        ? <div className="db-muted">Loading…</div>
                        : filtered.length === 0
                            ? <div className="db-muted">No servers match.</div>
                            : filtered.map(r => <RegistryCard key={r.name} server={r} onInstalled={refreshAll} />)}
                    <div className="db-row" style={{ gap: 8, marginTop: 12, alignItems: 'center' }}>
                        <span className="db-muted" style={{ fontSize: 11 }}>Explore the ecosystem:</span>
                        {DISCOVERY_LINKS.map(l => (
                            <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer" className="db-btn db-btn-secondary" style={{ fontSize: 11, textDecoration: 'none' }}>{l.label} ↗</a>
                        ))}
                    </div>
                </div>
            )}

            {view === 'installed' && (
                <div className="db-card">
                    <div className="db-card-title">Installed servers</div>
                    {servers === null
                        ? <div className="db-muted">Loading…</div>
                        : servers.length === 0
                            ? <div className="db-muted">No MCP servers configured. Install one from the registry.</div>
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
            )}

            {view === 'manual' && (
                <div className="db-card">
                    <div className="db-card-title">Add server manually (advanced)</div>
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
            )}
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
