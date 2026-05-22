import { useState, useEffect } from 'react';

interface AuditEntry {
    id:           number;
    session_id:   string;
    action_type:  string;
    approved:     boolean | null;
    chain_hash:   string;
    prev_hash:    string | null;
    timestamp:    string;
}

interface ChainStatus {
    valid:   boolean;
    checked: number;
    error?:  string;
}

interface AuditStats {
    total:          number;
    by_resolution:  { approved: number; rejected: number; pending: number };
    by_type:        Record<string, number>;
}

export function AuditPanel(): JSX.Element {
    const [entries,     setEntries]     = useState<AuditEntry[]>([]);
    const [chainStatus, setChainStatus] = useState<ChainStatus | undefined>();
    const [stats,       setStats]       = useState<AuditStats | null>(null);
    const [loading,     setLoading]     = useState(false);
    const [page,        setPage]        = useState(0);
    const PAGE_SIZE = 20;

    const loadEntries = async (): Promise<void> => {
        setLoading(true);
        try {
            const r = await fetch(`/api/v1/audit/log?offset=${page * PAGE_SIZE}&limit=${PAGE_SIZE}`);
            if (!r.ok) { return; }
            const data = await r.json() as AuditEntry[];
            setEntries(prev => page === 0 ? data : [...prev, ...data]);
        } catch { /* no-op */ } finally {
            setLoading(false);
        }
    };

    const verifyChain = async (): Promise<void> => {
        setChainStatus(undefined);
        try {
            const r = await fetch('/api/v1/audit/verify');
            const data = await r.json() as ChainStatus;
            setChainStatus(data);
        } catch {
            setChainStatus({ valid: false, checked: 0, error: 'Network error' });
        }
    };

    useEffect(() => { loadEntries(); }, [page]);

    useEffect(() => {
        fetch('/api/v1/audit/stats')
            .then(r => r.ok ? r.json() : null)
            .then((d: AuditStats | null) => { if (d) setStats(d); })
            .catch(() => {});
    }, []);

    return (
        <div>
            <div className="db-section-title">Approval Ledger</div>

            {/* Metrics row */}
            {stats && (
                <div className="db-row" style={{ gap: 12, marginBottom: 16, alignItems: 'stretch' }}>
                    <div className="db-card" style={{ flex: 1, marginBottom: 0 }}>
                        <div className="db-card-title">Total Events</div>
                        <div style={{ fontSize: 24, fontWeight: 700 }}>{stats.total}</div>
                    </div>
                    <div className="db-card" style={{ flex: 2, marginBottom: 0 }}>
                        <div className="db-card-title">Resolutions</div>
                        <div className="db-row" style={{ gap: 16, flexWrap: 'wrap' }}>
                            <span style={{ color: '#63a583', fontWeight: 600 }}>&#10003; {stats.by_resolution.approved} approved</span>
                            <span style={{ color: '#F85149', fontWeight: 600 }}>&#10007; {stats.by_resolution.rejected} rejected</span>
                            <span style={{ color: '#E3B341', fontWeight: 600 }}>&#8987; {stats.by_resolution.pending} pending</span>
                        </div>
                    </div>
                </div>
            )}

            {/* Chain verification */}
            <div className="db-card">
                <div className="db-card-title" title="Secured with Blake2b cryptographic chain">Tamper-Evident Seal</div>
                <div className="db-row">
                    <button className="db-btn db-btn-primary" onClick={verifyChain}>
                        Verify Seal
                    </button>
                </div>
                {chainStatus && (
                    <div className="db-traffic-light" style={{ marginTop: 10 }}>
                        <div className="db-tl-dot"
                            style={{ background: chainStatus.valid ? '#63a583' : '#F85149' }} />
                        <span style={{ fontWeight: 600, fontSize: 12 }}>
                            {chainStatus.valid
                                ? `Seal intact — ${chainStatus.checked} sessions verified`
                                : `Seal broken — ${chainStatus.error ?? 'tamper detected'}`}
                        </span>
                    </div>
                )}
            </div>

            {/* Event-type breakdown */}
            {stats && Object.keys(stats.by_type).length > 0 && (
                <div className="db-card">
                    <div className="db-card-title">Event Types</div>
                    {Object.entries(stats.by_type)
                        .sort((a, b) => b[1] - a[1])
                        .map(([kind, count]) => (
                            <div key={kind} style={{ marginBottom: 8 }}>
                                <div className="db-row" style={{ justifyContent: 'space-between', marginBottom: 3 }}>
                                    <span className="db-label" style={{ marginBottom: 0 }}>{kind.replace(/_/g, ' ')}</span>
                                    <span className="db-muted">{count}</span>
                                </div>
                                <div className="db-gauge-track">
                                    <div className="db-gauge-fill"
                                        style={{ width: `${stats.total > 0 ? (count / stats.total) * 100 : 0}%` }} />
                                </div>
                            </div>
                        ))
                    }
                </div>
            )}

            {/* Audit log */}
            <div className="db-card">
                <div className="db-card-title">Audit Log</div>
                {entries.length === 0 && !loading && (
                    <div className="db-muted">No audit entries found.</div>
                )}
                {entries.map(e => (
                    <div key={e.id} className="db-audit-row">
                        <span
                            className="db-audit-status"
                            data-state={e.approved === true ? 'approved' : e.approved === false ? 'rejected' : 'pending'}
                            style={{
                                width: 10, height: 10, borderRadius: '50%',
                                background: e.approved === true ? '#63a583' : e.approved === false ? '#F85149' : '#E3B341',
                                marginTop: 5,
                            }}
                            title={e.approved === true ? 'Approved' : e.approved === false ? 'Rejected' : 'Pending'}
                        />
                        <div style={{ flex: 1 }}>
                            <div style={{ fontWeight: 600, fontSize: 12 }}>{e.action_type}</div>
                            <div className="db-muted">{e.session_id} · {new Date(e.timestamp).toLocaleString()}</div>
                        </div>
                        <div className="db-col" style={{ gap: 0, alignItems: 'flex-end' }}>
                            <span className="db-audit-hash" title={e.chain_hash}>{e.chain_hash.slice(0, 12)}…</span>
                            {e.prev_hash && (
                                <span className="db-audit-hash" title={e.prev_hash} style={{ opacity: 0.5 }}>
                                    ← {e.prev_hash.slice(0, 8)}…
                                </span>
                            )}
                        </div>
                    </div>
                ))}
                {entries.length > 0 && entries.length % PAGE_SIZE === 0 && (
                    <button
                        className="db-btn db-btn-secondary"
                        style={{ marginTop: 10, width: '100%' }}
                        onClick={() => setPage(p => p + 1)}
                        disabled={loading}
                    >
                        {loading ? 'Loading…' : 'Load more'}
                    </button>
                )}
            </div>
        </div>
    );
}
