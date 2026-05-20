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

export function AuditPanel(): JSX.Element {
    const [entries,     setEntries]     = useState<AuditEntry[]>([]);
    const [chainStatus, setChainStatus] = useState<ChainStatus | undefined>();
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

    return (
        <div>
            <div className="db-section-title">HITL Audit Ledger</div>

            {/* Chain verification */}
            <div className="db-card">
                <div className="db-card-title">Blake2b Chain Integrity</div>
                <div className="db-row" style={{ marginBottom: chainStatus ? 10 : 0 }}>
                    <button className="db-btn db-btn-primary" onClick={verifyChain}>
                        Verify Chain
                    </button>
                    {chainStatus && (
                        <div className="db-row" style={{ gap: 6 }}>
                            <span style={{ fontSize: 18 }}>{chainStatus.valid ? '✅' : '❌'}</span>
                            <span style={{ fontWeight: 600, color: chainStatus.valid ? '#63a583' : '#E85A4F' }}>
                                {chainStatus.valid
                                    ? `Chain intact — ${chainStatus.checked} events verified`
                                    : `Tamper detected — ${chainStatus.error ?? 'unknown error'}`
                                }
                            </span>
                        </div>
                    )}
                </div>
            </div>

            {/* Audit log */}
            <div className="db-card">
                <div className="db-card-title">Audit Log</div>
                {entries.length === 0 && !loading && (
                    <div className="db-muted">No audit entries found.</div>
                )}
                {entries.map(e => (
                    <div key={e.id} className="db-audit-row">
                        <span className="db-audit-status">{e.approved === true ? '✅' : e.approved === false ? '❌' : '⏳'}</span>
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
