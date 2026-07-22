import { useEffect, useState } from 'react';
import { Badge } from '../ui';
import { useActiveProject, withProject } from '../hooks/useActiveProject';

interface TokenSnapshot {
    local_tokens:            number;
    cloud_tokens:            number;
    estimated_savings_usd:   number;
    estimated_invested_usd:  number;
}

interface McpServer { id: string; name: string; uri: string; enabled: boolean; }

interface AuditStats {
    total:         number;
    by_resolution: { approved: number; rejected: number; pending: number };
    by_type:       Record<string, number>;
}

interface RoutingDecision { id: number; timestamp: string; }

interface OverviewProps { onNavigate: (id: string) => void; }

const RECENT_BUCKETS = 12; // last 12 hours

function bucketByHour(rows: RoutingDecision[]): number[] {
    const buckets = new Array<number>(RECENT_BUCKETS).fill(0);
    const now = Date.now();
    for (const r of rows) {
        // SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" in UTC.
        const ms = Date.parse(r.timestamp.replace(' ', 'T') + 'Z');
        if (Number.isNaN(ms)) { continue; }
        const hoursAgo = Math.floor((now - ms) / 3_600_000);
        if (hoursAgo >= 0 && hoursAgo < RECENT_BUCKETS) {
            buckets[RECENT_BUCKETS - 1 - hoursAgo] += 1;
        }
    }
    return buckets;
}

export function OverviewPanel({ onNavigate }: OverviewProps): JSX.Element {
    const { projectId } = useActiveProject();
    const [tokens,   setTokens]   = useState<TokenSnapshot | null>(null);
    const [servers,  setServers]  = useState<McpServer[] | null>(null);
    const [stats,    setStats]    = useState<AuditStats | null>(null);
    const [routing,  setRouting]  = useState<RoutingDecision[] | null>(null);

    useEffect(() => {
        const get = async <T,>(url: string): Promise<T | null> => {
            try {
                const r = await fetch(url);
                return r.ok ? (await r.json() as T) : null;
            } catch { return null; }
        };
        // Token usage + MCP servers are process/machine-global; audit stats and
        // routing activity re-scope to the active project.
        get<TokenSnapshot>('/api/v1/telemetry/tokens').then(setTokens);
        get<{ servers: McpServer[] }>('/api/v1/mcp/servers').then(d => setServers(d?.servers ?? []));
        get<AuditStats>(withProject('/api/v1/audit/stats', projectId)).then(setStats);
        get<{ decisions: RoutingDecision[] }>(withProject('/api/v1/telemetry/routing?limit=200', projectId)).then(d => setRouting(d?.decisions ?? []));
    }, [projectId]);

    const enabledServers = servers?.filter(s => s.enabled).length ?? 0;
    const buckets = routing ? bucketByHour(routing) : [];
    const maxBucket = Math.max(1, ...buckets);

    return (
        <div>
            <div className="db-section-title">Overview</div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
                {/* Token usage */}
                <div className="db-card" style={{ marginBottom: 0 }}>
                    <div className="db-row" style={{ justifyContent: 'space-between', gap: 8 }}>
                        <div className="db-card-title" title="Cumulative since the Core process started — not per calendar day">
                            Token usage (since startup)
                        </div>
                        <Badge status="neutral" icon="cpu">Global</Badge>
                    </div>
                    {tokens === null
                        ? <div className="db-muted">No data.</div>
                        : <>
                            <div style={{ fontSize: 22, fontWeight: 700 }}>${tokens.estimated_invested_usd.toFixed(3)}</div>
                            <div className="db-muted" style={{ marginTop: 4 }}>spent · saved ${tokens.estimated_savings_usd.toFixed(3)} via local routing</div>
                            <div className="db-muted" style={{ marginTop: 6 }}>
                                {Math.round(tokens.local_tokens).toLocaleString()} local · {Math.round(tokens.cloud_tokens).toLocaleString()} cloud tokens
                            </div>
                        </>}
                </div>

                {/* MCP servers */}
                <div className="db-card" style={{ marginBottom: 0 }}>
                    <div className="db-row" style={{ justifyContent: 'space-between', gap: 8 }}>
                        <div className="db-card-title">MCP servers</div>
                        <Badge status="neutral" icon="cpu">Global</Badge>
                    </div>
                    {servers === null
                        ? <div className="db-muted">No data.</div>
                        : <>
                            <div style={{ fontSize: 22, fontWeight: 700 }}>{servers.length}</div>
                            <div className="db-muted" style={{ marginTop: 4 }}>{enabledServers} enabled · {servers.length - enabledServers} disabled</div>
                            <button className="db-btn db-btn-secondary" style={{ marginTop: 8 }} onClick={() => onNavigate('extensions')}>Manage</button>
                        </>}
                </div>

                {/* Pending HITL */}
                <div className="db-card" style={{ marginBottom: 0 }}>
                    <div className="db-card-title">Pending HITL</div>
                    {stats === null
                        ? <div className="db-muted">No data.</div>
                        : <>
                            <div style={{ fontSize: 22, fontWeight: 700, color: stats.by_resolution.pending > 0 ? '#E3B341' : undefined }}>
                                {stats.by_resolution.pending}
                            </div>
                            <div className="db-muted" style={{ marginTop: 4 }}>awaiting review · {stats.total} total events</div>
                            <button className="db-btn db-btn-secondary" style={{ marginTop: 8 }} onClick={() => onNavigate('audit')}>Open ledger</button>
                        </>}
                </div>
            </div>

            {/* Recent activity */}
            <div className="db-card" style={{ marginTop: 16 }}>
                <div className="db-card-title">Recent routing activity (last {RECENT_BUCKETS}h)</div>
                {routing === null
                    ? <div className="db-muted">Loading…</div>
                    : routing.length === 0
                        ? <div className="db-muted">No routing telemetry recorded yet.</div>
                        : <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 80 }}>
                            {buckets.map((count, i) => (
                                <div
                                    key={i}
                                    title={`${count} decision(s)`}
                                    style={{
                                        flex: 1,
                                        height: `${(count / maxBucket) * 100}%`,
                                        minHeight: count > 0 ? 4 : 1,
                                        background: count > 0 ? 'var(--accent-primary)' : 'var(--border-subtle)',
                                        borderRadius: 2,
                                        transition: 'height 0.3s ease',
                                    }}
                                />
                            ))}
                        </div>}
            </div>
        </div>
    );
}
