import { useCallback, useEffect, useState } from 'react';
import { Icon } from '../../../shared/Icon';
import { Skeleton, EmptyState, ConfirmModal } from '../../ui';
import {
    fetchEmbeddings, purgeEmbedding,
    type EmbeddingRow, type EmbeddingSort, type SortOrder,
} from './api';

const PAGE_SIZE = 25;

interface EmbeddingBrowserProps {
    projectId: string;
    folder: string;
    onSelectRow: (row: EmbeddingRow) => void;
}

const COLUMNS: { key: EmbeddingSort; label: string }[] = [
    { key: 'file_path', label: 'File' },
    { key: 'token_count', label: 'Tokens' },
    { key: 'indexed_at', label: 'Indexed' },
];

/**
 * Paginated, sortable browser over a section's per-file embeddings. Each row can
 * be inspected (click) or purged from the vector store (HITL-confirmed). Pagination
 * is server-side (offset/limit); sort is a whitelisted key resolved by the backend.
 */
export function EmbeddingBrowser({ projectId, folder, onSelectRow }: EmbeddingBrowserProps): JSX.Element {
    const [rows, setRows] = useState<EmbeddingRow[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(0);
    const [sort, setSort] = useState<EmbeddingSort>('indexed_at');
    const [order, setOrder] = useState<SortOrder>('desc');
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [pending, setPending] = useState<EmbeddingRow | null>(null);

    const load = useCallback(() => {
        setLoading(true);
        setError(null);
        fetchEmbeddings(projectId, folder, { sort, order, offset: page * PAGE_SIZE, limit: PAGE_SIZE })
            .then(res => { setRows(res.rows); setTotal(res.total); })
            .catch(() => setError('Failed to load embeddings.'))
            .finally(() => setLoading(false));
    }, [projectId, folder, sort, order, page]);

    useEffect(() => { load(); }, [load]);

    const onSort = (key: EmbeddingSort): void => {
        if (key === sort) {
            setOrder(o => (o === 'asc' ? 'desc' : 'asc'));
        } else {
            setSort(key);
            setOrder(key === 'token_count' || key === 'indexed_at' ? 'desc' : 'asc');
        }
        setPage(0);
    };

    const confirmPurge = (): void => {
        const target = pending;
        if (!target) { return; }
        setPending(null);
        // Optimistic removal with rollback on failure.
        const prev = rows;
        setRows(rs => rs.filter(r => r.file_path !== target.file_path));
        setTotal(t => Math.max(0, t - 1));
        purgeEmbedding(projectId, target.file_path)
            .then(() => load())
            .catch(() => { setRows(prev); setTotal(t => t + 1); setError('Purge failed — restored the row.'); });
    };

    const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

    return (
        <div className="mm-embeddings">
            <table className="mm-emb-table">
                <thead>
                    <tr>
                        {COLUMNS.map(col => (
                            <th key={col.key} className="mm-emb-th" onClick={() => onSort(col.key)}>
                                <span>{col.label}</span>
                                {sort === col.key && (
                                    <span
                                        className="mm-sort-ico"
                                        style={{ transform: order === 'asc' ? 'rotate(180deg)' : undefined, display: 'inline-flex' }}
                                    >
                                        <Icon name="chevron-down" size={12} />
                                    </span>
                                )}
                            </th>
                        ))}
                        <th className="mm-emb-th mm-emb-th--actions" aria-label="Actions" />
                    </tr>
                </thead>
                <tbody>
                    {loading && rows.length === 0 && Array.from({ length: 6 }).map((_, i) => (
                        <tr key={i}><td colSpan={4}><Skeleton height={18} /></td></tr>
                    ))}
                    {!loading && rows.map(row => (
                        <tr key={row.file_path} className="mm-emb-row" onClick={() => onSelectRow(row)}>
                            <td className="mm-emb-file" title={row.file_path}>{row.label}</td>
                            <td className="mm-emb-num">{row.token_count.toLocaleString()}</td>
                            <td className="mm-emb-date">{row.indexed_at.slice(0, 10) || '—'}</td>
                            <td className="mm-emb-actions">
                                <button
                                    className="db-btn db-btn-ghost mm-emb-purge"
                                    onClick={(e) => { e.stopPropagation(); setPending(row); }}
                                    aria-label={`Purge ${row.label}`}
                                >
                                    <Icon name="trash" size={13} />
                                </button>
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>

            {!loading && !error && rows.length === 0 && (
                <EmptyState icon="file" title="No embeddings" hint="This section has no indexed vectors yet." />
            )}
            {error && <div className="mm-empty">{error}</div>}

            {total > PAGE_SIZE && (
                <div className="mm-emb-pager">
                    <button className="db-btn db-btn-ghost" disabled={page === 0} onClick={() => setPage(p => p - 1)}>
                        Prev
                    </button>
                    <span className="mm-emb-pageinfo">{page + 1} / {pageCount} · {total} files</span>
                    <button
                        className="db-btn db-btn-ghost"
                        disabled={page >= pageCount - 1}
                        onClick={() => setPage(p => p + 1)}
                    >
                        Next
                    </button>
                </div>
            )}

            <ConfirmModal
                open={pending !== null}
                title="Purge embedding"
                danger
                confirmLabel="Purge"
                body={<>Remove the vector for <strong>{pending?.label}</strong> from the semantic index? RAG will no longer retrieve this file until it is re-indexed.</>}
                warning="This evicts the stored embedding immediately."
                onConfirm={confirmPurge}
                onCancel={() => setPending(null)}
            />
        </div>
    );
}
