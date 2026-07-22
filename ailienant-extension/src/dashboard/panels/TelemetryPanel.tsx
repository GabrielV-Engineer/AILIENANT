import { useEffect, useMemo, useRef, useState } from 'react';
import { Badge, Donut, EmptyState, Skeleton, Sparkline, type DonutSlice } from '../ui';
import { Icon } from '../../shared/Icon';
import { useActiveProject, withProject } from '../hooks/useActiveProject';
import { usePollingWhileVisible } from '../hooks/usePollingWhileVisible';
import { useRingBuffer } from '../hooks/useRingBuffer';
import { formatAgo, formatUsd } from '../format';

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
const COST_POLL_MS = 5_000;

/** Freshness cue: a pulsing dot + "updated Ns ago". */
function LiveTag({ at }: { at: number | null }): JSX.Element | null {
    if (at === null) { return null; }
    return (
        <span className="db-live"><span className="db-live-dot" />updated {formatAgo(at)}</span>
    );
}

function CostCard(): JSX.Element {
    const [snap, setSnap] = useState<TokenSnapshot | null>(null);
    const [updatedAt, setUpdatedAt] = useState<number | null>(null);
    const prevInvested = useRef<number | null>(null);
    const velocity = useRingBuffer<number>(40);

    // Poll the process-global token ledger while visible; derive a spend-velocity
    // (Δ invested per interval) series — a monotonic cumulative total is useless
    // as a live line, but its rate of change is the signal a monitor wants.
    usePollingWhileVisible(() => {
        void (async (): Promise<void> => {
            try {
                const r = await fetch('/api/v1/telemetry/tokens');
                if (!r.ok) { return; }
                const data = await r.json() as TokenSnapshot;
                const now = Date.now();
                if (prevInvested.current !== null) {
                    velocity.push(Math.max(0, data.estimated_invested_usd - prevInvested.current), now);
                }
                prevInvested.current = data.estimated_invested_usd;
                setSnap(data);
                setUpdatedAt(now);
            } catch { /* backend offline */ }
        })();
    }, COST_POLL_MS);

    const total = snap ? snap.local_tokens + snap.cloud_tokens : 0;
    const localPct = total > 0 ? (snap!.local_tokens / total) * 100 : 0;
    const cloudPct = total > 0 ? (snap!.cloud_tokens / total) * 100 : 0;
    const lastVel = velocity.samples.length > 0 ? velocity.samples[velocity.samples.length - 1].v : 0;

    return (
        <div className="db-card">
            <div className="db-row" style={{ justifyContent: 'space-between' }}>
                <div className="db-row" style={{ gap: 8 }}>
                    <div className="db-card-title">Cost &amp; Budget snapshot</div>
                    <Badge status="neutral" icon="cpu">Process-global</Badge>
                </div>
                <LiveTag at={updatedAt} />
            </div>
            {snap === null
                ? <Skeleton height={72} />
                : <>
                    <div className="db-row" style={{ gap: 24, marginBottom: 12 }}>
                        <div>
                            <div className="db-label" style={{ marginBottom: 2 }}>Spent</div>
                            <div style={{ fontSize: 20, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>{formatUsd(snap.estimated_invested_usd)}</div>
                        </div>
                        <div>
                            <div className="db-label" style={{ marginBottom: 2 }}>Saved via local routing</div>
                            <div style={{ fontSize: 20, fontWeight: 700, color: 'var(--status-good)', fontVariantNumeric: 'tabular-nums' }}>{formatUsd(snap.estimated_savings_usd)}</div>
                        </div>
                    </div>

                    <div className="db-row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
                        <span className="db-label" style={{ marginBottom: 0 }}>Spend velocity</span>
                        <span className="db-muted" style={{ fontVariantNumeric: 'tabular-nums' }}>{formatUsd(lastVel)} / interval</span>
                    </div>
                    <Sparkline
                        samples={velocity.samples}
                        ariaLabel={`Spend velocity, latest ${formatUsd(lastVel)} per ${COST_POLL_MS / 1000}-second interval`}
                        warmingHint="Warming up — spend velocity appears after two polls…"
                    />

                    <div className="db-label" style={{ marginTop: 12 }}>Local tokens · {Math.round(snap.local_tokens).toLocaleString()}</div>
                    <div className="db-gauge-track"><div className="db-gauge-fill" style={{ width: `${localPct}%` }} /></div>
                    <div className="db-label" style={{ marginTop: 8 }}>Cloud tokens · {Math.round(snap.cloud_tokens).toLocaleString()}</div>
                    <div className="db-gauge-track"><div className="db-gauge-fill" data-warn="true" style={{ width: `${cloudPct}%` }} /></div>
                </>}
        </div>
    );
}

function RoutingDonutCard(): JSX.Element {
    const { projectId } = useActiveProject();
    const [rows, setRows] = useState<RoutingDecision[] | null>(null);

    useEffect(() => {
        let cancelled = false;
        setRows(null);
        void (async (): Promise<void> => {
            try {
                const r = await fetch(withProject('/api/v1/telemetry/routing?limit=200', projectId));
                if (!r.ok) { if (!cancelled) { setRows([]); } return; }
                const data = (await r.json() as { decisions: RoutingDecision[] }).decisions ?? [];
                if (!cancelled) { setRows(data); }
            } catch { if (!cancelled) { setRows([]); } }
        })();
        return () => { cancelled = true; };
    }, [projectId]);

    // Aggregate by target node (a bounded set) rather than the free-text reason.
    const slices = useMemo<DonutSlice[]>(() => {
        if (!rows) { return []; }
        const counts = new Map<string, number>();
        for (const d of rows) {
            const key = d.target_node ?? 'unknown';
            counts.set(key, (counts.get(key) ?? 0) + 1);
        }
        return [...counts.entries()].map(([label, value]) => ({ label, value }));
    }, [rows]);

    return (
        <div className="db-card">
            <div className="db-card-title">Routing distribution (last 200)</div>
            {rows === null
                ? <Skeleton height={120} />
                : rows.length === 0
                    ? <EmptyState icon="network" title="No routing telemetry yet" hint="Decisions appear here as the engine routes tasks." />
                    : <Donut slices={slices} ariaLabel="Routing decisions by target node" centerLabel="decisions" />}
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
            <RoutingDonutCard />
            <div className="db-card">
                <div className="db-card-title">Request latency (P50 / P95)</div>
                <div className="db-deferred">
                    <Icon name="clock" size={12} />
                    Coming in 11.3.B — per-request latency is not captured yet.
                </div>
            </div>
            <RoutingLogCard />
        </div>
    );
}
