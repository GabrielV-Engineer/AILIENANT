import { useEffect, useState } from 'react';
import { Badge } from '../ui';
import { useActiveProject, withProject } from '../hooks/useActiveProject';

interface TokenSnapshot {
    local_tokens:           number;
    cloud_tokens:           number;
    estimated_savings_usd:  number;
    estimated_invested_usd: number;
}

interface RoutingDecision {
    id:                  number;
    timestamp:          string;
    session_id:         string | null;
    source_node:        string | null;
    target_node:        string | null;
    reason:             string | null;
    css_score:          number | null;
    tci_score:          number | null;
    hardware_constraint: string | null;
}

const PAGE_SIZE = 25;
const REASON_CLAMP = 80;

function CostCard(): JSX.Element {
    const [snap, setSnap] = useState<TokenSnapshot | null>(null);
    const [loading, setLoading] = useState(false);

    const load = async (): Promise<void> => {
        setLoading(true);
        try {
            const r = await fetch('/api/v1/telemetry/tokens');
            if (r.ok) { setSnap(await r.json() as TokenSnapshot); }
        } catch { /* no-op */ } finally { setLoading(false); }
    };
    useEffect(() => { load(); }, []);

    const total = snap ? snap.local_tokens + snap.cloud_tokens : 0;
    const localPct = total > 0 ? (snap!.local_tokens / total) * 100 : 0;
    const cloudPct = total > 0 ? (snap!.cloud_tokens / total) * 100 : 0;

    return (
        <div className="db-card">
            <div className="db-row" style={{ justifyContent: 'space-between' }}>
                <div className="db-row" style={{ gap: 8 }}>
                    <div className="db-card-title">Cost &amp; Budget snapshot</div>
                    <Badge status="neutral" icon="cpu">Process-global</Badge>
                </div>
                <button className="db-btn db-btn-secondary" onClick={load} disabled={loading}>{loading ? 'Refreshing…' : 'Refresh'}</button>
            </div>
            {snap === null
                ? <div className="db-muted">No telemetry yet.</div>
                : <>
                    <div className="db-row" style={{ gap: 24, marginBottom: 12 }}>
                        <div>
                            <div className="db-label" style={{ marginBottom: 2 }}>Spent</div>
                            <div style={{ fontSize: 20, fontWeight: 700 }}>${snap.estimated_invested_usd.toFixed(3)}</div>
                        </div>
                        <div>
                            <div className="db-label" style={{ marginBottom: 2 }}>Saved via local routing</div>
                            <div style={{ fontSize: 20, fontWeight: 700, color: '#63a583' }}>${snap.estimated_savings_usd.toFixed(3)}</div>
                        </div>
                    </div>
                    <div className="db-label">Local tokens · {Math.round(snap.local_tokens).toLocaleString()}</div>
                    <div className="db-gauge-track"><div className="db-gauge-fill" style={{ width: `${localPct}%` }} /></div>
                    <div className="db-label" style={{ marginTop: 8 }}>Cloud tokens · {Math.round(snap.cloud_tokens).toLocaleString()}</div>
                    <div className="db-gauge-track"><div className="db-gauge-fill" data-warn="true" style={{ width: `${cloudPct}%` }} /></div>
                </>}
        </div>
    );
}

function RoutingLogCard(): JSX.Element {
    const { projectId } = useActiveProject();
    const [rows, setRows] = useState<RoutingDecision[]>([]);
    const [page, setPage] = useState(0);
    const [loading, setLoading] = useState(false);
    const [done, setDone] = useState(false);
    const [revealed, setRevealed] = useState<Record<number, boolean>>({});

    // A project switch re-scopes the log: reset pagination so the next load
    // replaces (rather than appends to) rows from the previous project.
    useEffect(() => { setPage(0); setDone(false); }, [projectId]);

    useEffect(() => {
        let cancelled = false;
        const load = async (): Promise<void> => {
            setLoading(true);
            try {
                const url = withProject(`/api/v1/telemetry/routing?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`, projectId);
                const r = await fetch(url);
                if (!r.ok) { return; }
                const data = (await r.json() as { decisions: RoutingDecision[] }).decisions ?? [];
                if (cancelled) { return; }
                setRows(prev => page === 0 ? data : [...prev, ...data]);
                if (data.length < PAGE_SIZE) { setDone(true); }
            } catch { /* no-op */ } finally { if (!cancelled) { setLoading(false); } }
        };
        load();
        return () => { cancelled = true; };
    }, [page, projectId]);

    return (
        <div className="db-card">
            <div className="db-card-title">Routing log</div>
            {rows.length === 0 && !loading && <div className="db-muted">No routing decisions recorded yet.</div>}
            {rows.map(d => {
                const reason = d.reason ?? '';
                const long = reason.length > REASON_CLAMP;
                const show = revealed[d.id] || !long;
                return (
                    <div key={d.id} className="db-audit-row">
                        <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ fontWeight: 600, fontSize: 12 }}>
                                {d.source_node ?? '?'} → {d.target_node ?? '?'}
                            </div>
                            <div className="db-muted">{new Date(d.timestamp.replace(' ', 'T') + 'Z').toLocaleString()}</div>
                            {reason && (
                                <div className="db-muted" style={{ whiteSpace: 'pre-wrap', marginTop: 2 }}>
                                    {show ? reason : `${reason.slice(0, REASON_CLAMP)}…`}
                                    {long && (
                                        <button
                                            className="db-btn db-btn-secondary"
                                            style={{ marginLeft: 8, padding: '0 6px', fontSize: 11 }}
                                            onClick={() => setRevealed(p => ({ ...p, [d.id]: !p[d.id] }))}
                                        >{show ? 'Hide' : 'Reveal'}</button>
                                    )}
                                </div>
                            )}
                        </div>
                        <div className="db-col" style={{ gap: 0, alignItems: 'flex-end' }}>
                            {d.css_score != null && <span className="db-muted">CSS {d.css_score.toFixed(2)}</span>}
                            {d.tci_score != null && <span className="db-muted">TCI {d.tci_score.toFixed(2)}</span>}
                        </div>
                    </div>
                );
            })}
            {!done && rows.length > 0 && (
                <button className="db-btn db-btn-secondary" style={{ marginTop: 10, width: '100%' }} disabled={loading} onClick={() => setPage(p => p + 1)}>
                    {loading ? 'Loading…' : 'Load more'}
                </button>
            )}
        </div>
    );
}

export function TelemetryPanel(): JSX.Element {
    return (
        <div>
            <div className="db-section-title">Telemetry</div>
            <CostCard />
            <RoutingLogCard />
        </div>
    );
}
