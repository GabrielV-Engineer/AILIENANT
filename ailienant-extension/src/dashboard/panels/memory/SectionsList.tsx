import { useMemo } from 'react';
import { Icon } from '../../../shared/Icon';
import type { SectionInfo } from './api';

interface SectionsListProps {
    sections: SectionInfo[] | null;
    projectCount: number;
    selected: { project_id: string; abs_prefix: string } | null;
    loading: boolean;
    error: string | null;
    onSelect: (s: SectionInfo) => void;
    onRetry: () => void;
}

function shortHash(id: string): string {
    return id.length > 10 ? `${id.slice(0, 8)}…` : id || '(default)';
}

export function SectionsList({
    sections, projectCount, selected, loading, error, onSelect, onRetry,
}: SectionsListProps): JSX.Element {
    const grouped = useMemo(() => {
        const map = new Map<string, SectionInfo[]>();
        for (const s of sections ?? []) {
            const arr = map.get(s.project_id) ?? [];
            arr.push(s);
            map.set(s.project_id, arr);
        }
        return [...map.entries()];
    }, [sections]);

    if (loading) {
        return (
            <div className="mm-sections">
                {[0, 1, 2, 3].map(i => <div key={i} className="mm-section-skeleton" />)}
            </div>
        );
    }

    if (error) {
        return (
            <div className="mm-sections">
                <div className="mm-error">
                    <Icon name="alert" size={16} />
                    <span>{error}</span>
                    <button className="db-btn db-btn-secondary" onClick={onRetry}>Retry</button>
                </div>
            </div>
        );
    }

    if (!sections || sections.length === 0) {
        return (
            <div className="mm-sections">
                <div className="mm-empty">
                    <Icon name="folder" size={20} />
                    <span>No indexed folders yet. Open a workspace so AILIENANT can index it.</span>
                </div>
            </div>
        );
    }

    return (
        <div className="mm-sections">
            {grouped.map(([projectId, items]) => (
                <div key={projectId} className="mm-section-group">
                    {projectCount > 1 && (
                        <div className="mm-section-project" title={projectId}>
                            <Icon name="brain" size={12} />
                            <span>{shortHash(projectId)}</span>
                        </div>
                    )}
                    {items.map(s => {
                        const active = selected?.project_id === s.project_id
                            && selected?.abs_prefix === s.abs_prefix;
                        return (
                            <button
                                key={`${s.project_id}:${s.abs_prefix}`}
                                className="mm-section-item"
                                data-active={active ? 'true' : 'false'}
                                onClick={() => onSelect(s)}
                                title={s.abs_prefix}
                            >
                                <Icon name="folder" size={14} />
                                <span className="mm-section-name">{s.folder}</span>
                                <span className="mm-section-count">{s.file_count}</span>
                            </button>
                        );
                    })}
                </div>
            ))}
        </div>
    );
}
