import { useState, useEffect, useCallback } from 'react';
import { useActiveProject, withProject } from '../hooks/useActiveProject';

// Mirrors core/dead_letter.py::DeadLetterRecord — an unresolved DLQ episode awaiting
// resume (the cross-session complement to in-turn self-healing).
interface DeadLetterEpisode {
    episode_id:        string;
    task_id:           string;
    thread_id:         string;
    failed_node:       string;
    exception_class:   string;
    exception_message: string;
    created_at:        number;  // unix seconds
    resolved_at:       number | null;
}

interface ResumeResult {
    resumed:         boolean;
    reason?:         string;
    from_episode?:   string;
    node_resumed_at?: string;
}

export function RecoveryPanel(): JSX.Element {
    const { projectId } = useActiveProject();
    const [episodes, setEpisodes] = useState<DeadLetterEpisode[]>([]);
    const [loading,  setLoading]  = useState(false);
    const [busy,     setBusy]     = useState<string | null>(null);  // task_id being resumed
    const [notice,   setNotice]   = useState<string | null>(null);

    const load = useCallback(async (): Promise<void> => {
        setLoading(true);
        try {
            const r = await fetch(withProject('/api/v1/dlq/pending', projectId));
            if (!r.ok) { return; }
            const data = await r.json() as { count: number; episodes: DeadLetterEpisode[] };
            setEpisodes(data.episodes ?? []);
        } catch { /* non-blocking */ } finally {
            setLoading(false);
        }
    }, [projectId]);

    useEffect(() => { load(); }, [load]);

    const resume = async (taskId: string): Promise<void> => {
        setBusy(taskId);
        setNotice(null);
        try {
            const r = await fetch(`/api/v1/task/resume/${encodeURIComponent(taskId)}`, { method: 'POST' });
            if (!r.ok) {
                setNotice(`Resume failed — server returned ${r.status}.`);
                return;
            }
            const res = await r.json() as ResumeResult;
            setNotice(res.resumed
                ? `Resumed task ${taskId.slice(0, 8)}… from node "${res.node_resumed_at ?? 'unknown'}".`
                : `Nothing to resume (${res.reason ?? 'no pending episode'}).`);
            await load();  // resolved episodes drop out of the pending list
        } catch {
            setNotice('Resume failed — network error.');
        } finally {
            setBusy(null);
        }
    };

    return (
        <div>
            <div className="db-section-title">Task Recovery</div>

            <div className="db-card">
                <div className="db-row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                        <div className="db-card-title">Dead Letter Queue</div>
                        <div className="db-muted">
                            Crashed tasks that exhausted in-turn self-healing. Re-hydrates the last
                            checkpoint and re-runs the failed node.
                        </div>
                    </div>
                    <button className="db-btn db-btn-secondary" onClick={load} disabled={loading}>
                        {loading ? 'Refreshing…' : 'Refresh'}
                    </button>
                </div>
            </div>

            {notice && (
                <div className="db-card" style={{ borderLeft: '3px solid #58A6FF' }}>
                    <span style={{ fontSize: 12 }}>{notice}</span>
                </div>
            )}

            <div className="db-card">
                <div className="db-card-title">Pending Episodes</div>
                {episodes.length === 0 && !loading && (
                    <div className="db-muted">No tasks awaiting recovery — the queue is clear.</div>
                )}
                {episodes.map(ep => (
                    <div key={ep.episode_id} className="db-audit-row">
                        <span
                            style={{
                                width: 10, height: 10, borderRadius: '50%',
                                background: '#F85149', marginTop: 5,
                            }}
                            title={ep.exception_class}
                        />
                        <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ fontWeight: 600, fontSize: 12 }}>
                                {ep.exception_class} <span className="db-muted">@ {ep.failed_node}</span>
                            </div>
                            <div className="db-muted" style={{
                                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                            }} title={ep.exception_message}>
                                {ep.exception_message || '(no message)'}
                            </div>
                            <div className="db-muted">
                                task {ep.task_id.slice(0, 12)}… · {new Date(ep.created_at * 1000).toLocaleString()}
                            </div>
                        </div>
                        <button
                            className="db-btn db-btn-primary"
                            onClick={() => resume(ep.task_id)}
                            disabled={busy !== null}
                        >
                            {busy === ep.task_id ? 'Resuming…' : 'Resume'}
                        </button>
                    </div>
                ))}
            </div>
        </div>
    );
}
