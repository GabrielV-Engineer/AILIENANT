import { useEffect, useMemo, useState, type ChangeEvent } from 'react';
import {
    ActiveProjectBadge, Badge, type BadgeStatus, Button, Card, EmptyState, SectionHeader, Skeleton, StatTile,
} from '../ui';

interface McpServer { id: string; name: string; uri: string; transport: string; enabled: boolean; }
interface McpTestResult { reachable: boolean; tool_count: number; error?: string; }
interface Skill { id: string; name: string; body: string; }
interface ImportResult {
    ok: boolean;
    imported: string[];
    updated: string[];
    skipped: { name: string; reason: string }[];
    needs_secret: string[];
    error?: string;
}
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
type McpView = 'browse' | 'installed' | 'manual' | 'import';

// The official, source-reviewable ecosystem pages. Discovery opens these in the
// external browser; in-IDE install stays restricted to the curated cards.
const DISCOVERY_LINKS: ReadonlyArray<{ label: string; url: string }> = [
    { label: 'MCP Registry', url: 'https://registry.modelcontextprotocol.io' },
    { label: 'Reference servers', url: 'https://github.com/modelcontextprotocol/servers' },
];

// Severity-coded privilege tiers — the conscious-consent surface shown before a
// user connects a server. Maps onto the reserved status tokens (icon + label,
// never color alone).
function tierStatus(tier: string): BadgeStatus {
    switch (tier) {
        case 'READ_ONLY': return 'good';
        case 'WRITE': return 'warning';
        case 'EXECUTE': return 'serious';
        case 'DANGEROUS': return 'critical';
        default: return 'neutral';
    }
}

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
        <div className="db-ext-row">
            <div className="db-row" style={{ justifyContent: 'space-between', alignItems: 'flex-start', gap: 10 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="db-ext-name">{server.display_name}</div>
                    <div className="db-muted">{server.description}</div>
                    <div className="db-row" style={{ gap: 4, marginTop: 6, flexWrap: 'wrap' }}>
                        {Object.entries(server.tool_tiers).map(([tool, tier]) => (
                            <Badge key={tool} status={tierStatus(tier)} icon="shield">{tool} · {tier}</Badge>
                        ))}
                    </div>
                </div>
                <div className="db-col" style={{ gap: 6, alignItems: 'flex-end', flexShrink: 0 }}>
                    {server.installed
                        ? <Badge status="good" icon="check">Installed</Badge>
                        : <Button variant="primary" icon="plus" disabled={busy} onClick={onInstallClick}>{busy ? 'Installing…' : 'Install'}</Button>}
                    <a href={server.source_url} target="_blank" rel="noopener noreferrer" className="db-ext-link">View source ↗</a>
                </div>
            </div>
            {open && server.secrets.length > 0 && (
                <div className="db-col" style={{ gap: 6, borderTop: '1px solid var(--border-subtle)', paddingTop: 8, marginTop: 8 }}>
                    <div className="db-muted">Installing grants the privileges above. Provide the required credential(s):</div>
                    {server.secrets.map(name => (
                        <input key={name} className="db-input" type="password" placeholder={name}
                            value={secretValues[name] ?? ''}
                            onChange={e => setSecretValues(v => ({ ...v, [name]: e.target.value }))} />
                    ))}
                    {error && <div className="db-ext-error">{error}</div>}
                    <Button variant="primary" icon="plug" disabled={busy} onClick={() => void doInstall()}>{busy ? 'Installing…' : 'Confirm install'}</Button>
                </div>
            )}
            {error && !open && <div className="db-ext-error" style={{ marginTop: 6 }}>{error}</div>}
        </div>
    );
}

function ConfigImportView(
    { registry, onChanged }: { registry: RegistryServer[] | null; onChanged: () => void },
): JSX.Element {
    const [result, setResult] = useState<ImportResult | null>(null);
    const [pending, setPending] = useState<string[]>([]);
    const [secretInputs, setSecretInputs] = useState<Record<string, Record<string, string>>>({});
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [fileName, setFileName] = useState<string | null>(null);

    // The export only emits a key_ref for regulated servers, so a server flagged
    // `needs_secret` is present in the curated registry — its declared secret names
    // drive the credential prompt below.
    const secretsFor = (name: string): string[] =>
        registry?.find(r => r.name.toLowerCase() === name.toLowerCase())?.secrets ?? [];

    const onFile = async (e: ChangeEvent<HTMLInputElement>): Promise<void> => {
        const file = e.target.files?.[0];
        e.target.value = '';  // allow re-importing the same file
        if (!file) { return; }
        setFileName(file.name);
        setError(null);
        setResult(null);
        setPending([]);
        setBusy(true);
        let payload: unknown;
        try {
            payload = JSON.parse(await file.text());
        } catch {
            setBusy(false);
            setError('Not a valid JSON file.');
            return;
        }
        const res = await sendJson<ImportResult>('/api/v1/mcp/config/import', 'POST', payload);
        setBusy(false);
        if (!res) {
            setError('Import failed — the file may be malformed or declare an unsupported version.');
            return;
        }
        setResult(res);
        setPending(res.needs_secret ?? []);
        setSecretInputs({});
        onChanged();
    };

    const saveSecret = async (name: string): Promise<void> => {
        setBusy(true);
        setError(null);
        const res = await sendJson<{ ok: boolean; error?: string }>(
            '/api/v1/mcp/registry/install', 'POST',
            { name, secrets: secretInputs[name] ?? {} });
        setBusy(false);
        if (res?.ok) { setPending(p => p.filter(n => n !== name)); onChanged(); }
        else { setError(res?.error ?? `Failed to store credential for ${name}.`); }
    };

    return (
        <Card title="Import server config">
            <div className="db-muted" style={{ marginBottom: 10 }}>
                Import a <code>config.json</code> projection of MCP servers. A server that needs a
                credential is imported but flagged below until you supply the secret — it is never
                silently dropped.
            </div>
            <input type="file" accept="application/json,.json" className="db-input" onChange={e => void onFile(e)} />
            {fileName && <div className="db-muted" style={{ marginTop: 4 }}>{fileName}</div>}
            {busy && <div className="db-muted" style={{ marginTop: 8 }}>Working…</div>}
            {error && <div className="db-ext-error" style={{ marginTop: 8 }}>{error}</div>}

            {result && (
                <div className="db-col" style={{ gap: 6, marginTop: 12 }}>
                    <div className="db-muted">
                        Imported {result.imported.length} · updated {result.updated.length}
                        {result.skipped.length > 0 ? ` · skipped ${result.skipped.length}` : ''}
                    </div>
                    {result.skipped.map(s => (
                        <div key={s.name} className="db-ext-warn">Skipped {s.name || '(unnamed)'}: {s.reason}</div>
                    ))}
                </div>
            )}

            {pending.length > 0 && (
                <div className="db-col" style={{ gap: 10, marginTop: 12 }}>
                    <div className="db-card-title" style={{ marginBottom: 0 }}>Credentials required</div>
                    {pending.map(name => {
                        const secrets = secretsFor(name);
                        return (
                            <div key={name} className="db-ext-row">
                                <div className="db-ext-name">{name}</div>
                                {secrets.length === 0 ? (
                                    <div className="db-muted">
                                        This server needs a credential. Install it from the curated registry to supply the secret.
                                    </div>
                                ) : (
                                    <div className="db-col" style={{ gap: 6, marginTop: 6 }}>
                                        {secrets.map(sn => (
                                            <input key={sn} className="db-input" type="password" placeholder={sn}
                                                value={secretInputs[name]?.[sn] ?? ''}
                                                onChange={e => setSecretInputs(v => ({
                                                    ...v, [name]: { ...(v[name] ?? {}), [sn]: e.target.value },
                                                }))} />
                                        ))}
                                        <Button variant="primary" icon="key" disabled={busy} onClick={() => void saveSecret(name)}>Save credential</Button>
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
        </Card>
    );
}

function McpTab({ onChanged }: { onChanged: () => void }): JSX.Element {
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
    const refreshAll = (): void => { refreshServers(); refreshRegistry(); onChanged(); };
    useEffect(() => { refreshServers(); refreshRegistry(); }, []);

    const addServer = async (): Promise<void> => {
        if (!name.trim() || !uri.trim()) { return; }
        setError(null);
        const res = await sendJson<{ ok: boolean; error?: string; servers?: McpServer[] }>(
            '/api/v1/mcp/servers', 'POST', { name: name.trim(), uri: uri.trim(), enabled: true });
        if (res?.ok) { setServers(res.servers ?? []); setName(''); setUri(''); onChanged(); }
        else { setError(res?.error ?? 'Failed to save server.'); }
    };
    const toggle = async (s: McpServer): Promise<void> => {
        const res = await sendJson<{ ok: boolean; servers?: McpServer[] }>(
            '/api/v1/mcp/servers', 'POST', { ...s, enabled: !s.enabled });
        if (res?.ok) { setServers(res.servers ?? []); onChanged(); }
    };
    const remove = async (id: string): Promise<void> => {
        const res = await sendJson<{ ok: boolean; servers?: McpServer[] }>(`/api/v1/mcp/servers/${id}`, 'DELETE');
        if (res?.ok) { setServers(res.servers ?? []); refreshRegistry(); onChanged(); }
    };
    const test = async (s: McpServer): Promise<void> => {
        setTests(p => ({ ...p, [s.id]: 'loading' }));
        const res = await sendJson<McpTestResult>('/api/v1/mcp/test', 'POST', { uri: s.uri, server_name: s.name });
        setTests(p => ({ ...p, [s.id]: res ?? { reachable: false, tool_count: 0, error: 'no response' } }));
    };

    const q = query.trim().toLowerCase();
    const filtered = (registry ?? []).filter(r =>
        !q || r.display_name.toLowerCase().includes(q) || r.name.includes(q) || r.description.toLowerCase().includes(q));

    const VIEWS: ReadonlyArray<{ id: McpView; label: string }> = [
        { id: 'browse', label: 'Browse registry' },
        { id: 'installed', label: 'Installed' },
        { id: 'manual', label: 'Add manually' },
        { id: 'import', label: 'Import config' },
    ];

    return (
        <div>
            <div className="db-row" style={{ gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
                {VIEWS.map(v => (
                    <Button key={v.id} variant={view === v.id ? 'primary' : 'secondary'} onClick={() => setView(v.id)}>{v.label}</Button>
                ))}
            </div>

            {view === 'browse' && (
                <Card title="Curated registry">
                    <input className="db-input" placeholder="Search servers…" value={query}
                        onChange={e => setQuery(e.target.value)} style={{ marginBottom: 10 }} />
                    {registry === null
                        ? <Skeleton height={64} count={2} />
                        : filtered.length === 0
                            ? <EmptyState icon="search" title="No servers match" hint="Try a different search term." />
                            : filtered.map(r => <RegistryCard key={r.name} server={r} onInstalled={refreshAll} />)}
                    <div className="db-row" style={{ gap: 8, marginTop: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                        <span className="db-muted">Explore the ecosystem:</span>
                        {DISCOVERY_LINKS.map(l => (
                            <a key={l.url} href={l.url} target="_blank" rel="noopener noreferrer" className="db-btn db-btn-secondary" style={{ textDecoration: 'none' }}>{l.label} ↗</a>
                        ))}
                    </div>
                </Card>
            )}

            {view === 'installed' && (
                <Card title="Installed servers">
                    {servers === null
                        ? <Skeleton height={48} count={2} />
                        : servers.length === 0
                            ? <EmptyState icon="plug" title="No MCP servers configured" hint="Install one from the curated registry." action={<Button variant="primary" onClick={() => setView('browse')}>Browse registry</Button>} />
                            : servers.map(s => {
                                const t = tests[s.id];
                                return (
                                    <div key={s.id} className="db-ext-row">
                                        <div className="db-row" style={{ justifyContent: 'space-between', gap: 10, alignItems: 'flex-start' }}>
                                            <div style={{ flex: 1, minWidth: 0 }}>
                                                <div className="db-ext-name">
                                                    {s.name} {!s.enabled && <Badge status="neutral">disabled</Badge>}
                                                </div>
                                                <div className="db-muted" style={{ wordBreak: 'break-all' }}>{s.uri}</div>
                                                {t && t !== 'loading' && (
                                                    <div style={{ marginTop: 6 }}>
                                                        {t.reachable
                                                            ? <Badge status="good" icon="check-circle">{t.tool_count} tools</Badge>
                                                            : <Badge status="critical" icon="x-circle">{t.error ?? 'unreachable'}</Badge>}
                                                    </div>
                                                )}
                                            </div>
                                            <div className="db-row" style={{ gap: 6, flexShrink: 0 }}>
                                                <Button icon="zap" disabled={t === 'loading'} onClick={() => test(s)}>{t === 'loading' ? 'Testing…' : 'Test'}</Button>
                                                <Button onClick={() => toggle(s)}>{s.enabled ? 'Disable' : 'Enable'}</Button>
                                                <Button variant="ghost" icon="trash" onClick={() => remove(s.id)}>Delete</Button>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                </Card>
            )}

            {view === 'manual' && (
                <Card title="Add server manually (advanced)">
                    <div className="db-ext-warn" style={{ marginBottom: 10 }}>
                        Warning: a <code>stdio://</code> server executes a local command on this machine. Only add servers you trust.
                    </div>
                    <div className="db-col">
                        <input className="db-input" placeholder="name" value={name} onChange={e => setName(e.target.value)} />
                        <input className="db-input" placeholder="stdio:///abs/path/to/server?arg=x" value={uri} onChange={e => setUri(e.target.value)} />
                        {error && <div className="db-ext-error">{error}</div>}
                        <Button variant="primary" icon="plus" onClick={addServer}>Add server</Button>
                    </div>
                </Card>
            )}

            {view === 'import' && <ConfigImportView registry={registry} onChanged={refreshAll} />}
        </div>
    );
}

function SkillsTab({ onChanged }: { onChanged: () => void }): JSX.Element {
    const [skills, setSkills] = useState<Skill[] | null>(null);
    const [query, setQuery] = useState('');
    const [creating, setCreating] = useState(false);
    const [name, setName] = useState('');
    const [body, setBody] = useState('');
    const [error, setError] = useState<string | null>(null);

    useEffect(() => { getJson<{ skills: Skill[] }>('/api/v1/skills').then(d => setSkills(d?.skills ?? [])); }, []);

    const create = async (): Promise<void> => {
        if (!name.trim() || !body.trim()) { return; }
        setError(null);
        const res = await sendJson<{ ok: boolean; error?: string; skills?: Skill[] }>(
            '/api/v1/skills', 'POST', { name: name.trim(), body: body.trim() });
        if (res?.ok) { setSkills(res.skills ?? []); setName(''); setBody(''); setCreating(false); onChanged(); }
        else { setError(res?.error ?? 'Failed to save skill.'); }
    };
    const remove = async (id: string): Promise<void> => {
        const res = await sendJson<{ ok: boolean; skills?: Skill[] }>(`/api/v1/skills/${id}`, 'DELETE');
        if (res?.ok) { setSkills(res.skills ?? []); onChanged(); }
    };

    const q = query.trim().toLowerCase();
    const filtered = useMemo(
        () => (skills ?? []).filter(s => !q || s.name.toLowerCase().includes(q) || s.body.toLowerCase().includes(q)),
        [skills, q],
    );

    return (
        <div>
            <Card
                title="Saved skills"
                actions={<Button variant="primary" icon="plus" onClick={() => setCreating(c => !c)}>{creating ? 'Close' : 'New skill'}</Button>}
            >
                {creating && (
                    <div className="db-col" style={{ gap: 8, marginBottom: 14, paddingBottom: 14, borderBottom: '1px solid var(--border-subtle)' }}>
                        <input className="db-input" placeholder="skill name" value={name} onChange={e => setName(e.target.value)} />
                        <textarea className="db-input" rows={5} placeholder="reusable instruction snippet…" value={body} onChange={e => setBody(e.target.value)} />
                        {error && <div className="db-ext-error">{error}</div>}
                        <div className="db-row" style={{ gap: 8 }}>
                            <Button variant="primary" icon="check" onClick={create} disabled={!name.trim() || !body.trim()}>Save skill</Button>
                            <Button onClick={() => { setCreating(false); setError(null); }}>Cancel</Button>
                        </div>
                    </div>
                )}

                {skills && skills.length > 4 && (
                    <input className="db-input" placeholder="Search skills…" value={query}
                        onChange={e => setQuery(e.target.value)} style={{ marginBottom: 10 }} />
                )}

                {skills === null
                    ? <Skeleton height={44} count={2} />
                    : skills.length === 0
                        ? <EmptyState icon="wand" title="No skills yet" hint="Create a reusable instruction snippet with New skill." />
                        : filtered.length === 0
                            ? <EmptyState icon="search" title="No skills match" hint="Try a different search term." />
                            : filtered.map(s => (
                                <div key={s.id} className="db-ext-row">
                                    <div className="db-row" style={{ justifyContent: 'space-between', gap: 10, alignItems: 'flex-start' }}>
                                        <div style={{ flex: 1, minWidth: 0 }}>
                                            <div className="db-ext-name">{s.name}</div>
                                            {/* Plain text node — never dangerouslySetInnerHTML. */}
                                            <div className="db-muted db-ext-clamp">{s.body}</div>
                                        </div>
                                        <Button variant="ghost" icon="trash" onClick={() => remove(s.id)}>Delete</Button>
                                    </div>
                                </div>
                            ))}
            </Card>
        </div>
    );
}

export function ExtensionsPanel(): JSX.Element {
    const [tab, setTab] = useState<SubTab>('mcp');
    const [counts, setCounts] = useState<{ installed: number; available: number; skills: number } | null>(null);

    const refreshCounts = (): void => {
        void Promise.all([
            getJson<{ servers: McpServer[] }>('/api/v1/mcp/servers'),
            getJson<{ servers: RegistryServer[] }>('/api/v1/mcp/registry'),
            getJson<{ skills: Skill[] }>('/api/v1/skills'),
        ]).then(([srv, reg, sk]) => setCounts({
            installed: srv?.servers.length ?? 0,
            available: reg?.servers.length ?? 0,
            skills: sk?.skills.length ?? 0,
        }));
    };
    useEffect(() => { refreshCounts(); }, []);

    const kpi = (v: number | undefined): JSX.Element | number =>
        counts === null ? <Skeleton width={40} height={22} /> : (v ?? 0);

    return (
        <div>
            <SectionHeader
                title="Extensions"
                subtitle="Connect MCP servers and manage reusable skills."
                actions={<ActiveProjectBadge />}
            />

            <div className="db-kpi-grid">
                <StatTile label="MCP installed" value={kpi(counts?.installed)} sub="active servers" />
                <StatTile label="Available in registry" value={kpi(counts?.available)} sub="curated, one-click" />
                <StatTile label="Skills" value={kpi(counts?.skills)} sub="reusable snippets" />
            </div>

            <div className="db-row" style={{ gap: 8, margin: '4px 0 16px' }}>
                <Button variant={tab === 'mcp' ? 'primary' : 'secondary'} icon="plug" onClick={() => setTab('mcp')}>MCP Servers</Button>
                <Button variant={tab === 'skills' ? 'primary' : 'secondary'} icon="wand" onClick={() => setTab('skills')}>Skills</Button>
            </div>

            {tab === 'mcp' ? <McpTab onChanged={refreshCounts} /> : <SkillsTab onChanged={refreshCounts} />}
        </div>
    );
}
