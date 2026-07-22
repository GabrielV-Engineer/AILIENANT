import { useEffect, useMemo, useState } from 'react';
import { Skeleton, StatTile } from '../ui';
import { useActiveProject, withProject } from '../hooks/useActiveProject';
import { formatUsd } from '../format';

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

interface RoutingDecision { id: number; timestamp: string; session_id: string | null; }

interface OverviewProps { onNavigate: (id: string) => void; }

const RECENT_BUCKETS = 12; // last 12 hours
const DAY_MS = 86_400_000;

function parseUtc(ts: string): number {
    // SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" in UTC.
    return Date.parse(ts.replace(' ', 'T') + 'Z');
}

function bucketByHour(rows: RoutingDecision[]): number[] {
    const buckets = new Array<number>(RECENT_BUCKETS).fill(0);
    const now = Date.now();
    for (const r of rows) {
        const ms = parseUtc(r.timestamp);
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

    // Distinct routing sessions seen in the last 24h (bounded to the recent window).
    const activeSessions = useMemo<number>(() => {
        if (!routing) { return 0; }
        const now = Date.now();
        const seen = new Set<string>();
        for (const r of routing) {
            const ms = parseUtc(r.timestamp);
            if (!Number.isNaN(ms) && now - ms < DAY_MS && r.session_id) { seen.add(r.session_id); }
        }
        return seen.size;
    }, [routing]);

    const loadingVal = <Skeleton width={70} height={22} />;

    return (
        <div>
            <div className="db-section-title">Overview</div>

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
                <StatTile
                    label="Token spend · global"
                    title="Cumulative since the Core process started — not per calendar day"
                    value={tokens === null ? loadingVal : formatUsd(tokens.estimated_invested_usd)}
                    sub={tokens === null ? 'No data' : `saved ${formatUsd(tokens.estimated_savings_usd)} via local routing · process-global`}
                />
                <StatTile
                    label="MCP servers · global"
                    value={servers === null ? loadingVal : servers.length}
                    sub={servers === null ? 'No data' : `${enabledServers} enabled · ${servers.length - enabledServers} disabled`}
                    footer={<button className="db-btn db-btn-secondary" onClick={() => onNavigate('extensions')}>Manage</button>}
                />
                <StatTile
                    label="HITL pending"
                    value={stats === null ? loadingVal : stats.by_resolution.pending}
                    tone={stats && stats.by_resolution.pending > 0 ? 'warning' : 'default'}
                    sub={stats === null ? 'No data' : `awaiting review · ${stats.total} total events`}
                    footer={<button className="db-btn db-btn-secondary" onClick={() => onNavigate('audit')}>Open ledger</button>}
                />
                <StatTile
                    label="Active sessions · ≤24h"
                    value={routing === null ? loadingVal : activeSessions}
                    sub="distinct routing sessions in the last 24h"
                />
            </div>

            {/* Recent activity */}
            <div className="db-card" style={{ marginTop: 16 }}>
                <div className="db-card-title">Recent routing activity (last {RECENT_BUCKETS}h)</div>
                {routing === null
                    ? <Skeleton height={80} />
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
                                        transition: 'height var(--dur-base) var(--ease)',
                                    }}
                                />
                            ))}
                        </div>}
            </div>
        </div>
    );
}
