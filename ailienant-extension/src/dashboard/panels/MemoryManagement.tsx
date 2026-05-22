import { useState, useEffect, useCallback } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { SectionsList } from './memory/SectionsList';
import { CodeGraphLayer } from './memory/CodeGraphLayer';
import { VectorMapLayer } from './memory/VectorMapLayer';
import {
    fetchSections, fetchGraph, fetchVectors,
    type SectionInfo, type GraphResponse, type GraphNode,
    type VectorMapResponse, type VectorPoint,
} from './memory/api';

type Layer = 'code' | 'vector';

interface SelectedSection {
    project_id: string;
    abs_prefix: string;
    folder: string;
}

type Detail =
    | { kind: 'node'; node: GraphNode }
    | { kind: 'point'; point: VectorPoint }
    | null;

export function MemoryManagement(): JSX.Element {
    const [sections, setSections] = useState<SectionInfo[] | null>(null);
    const [projectCount, setProjectCount] = useState(0);
    const [sectionsLoading, setSectionsLoading] = useState(true);
    const [sectionsError, setSectionsError] = useState<string | null>(null);

    const [selected, setSelected] = useState<SelectedSection | null>(null);
    const [layer, setLayer] = useState<Layer>('code');
    const [search, setSearch] = useState('');

    const [graph, setGraph] = useState<GraphResponse | null>(null);
    const [graphLoading, setGraphLoading] = useState(false);
    const [graphError, setGraphError] = useState<string | null>(null);

    const [vectors, setVectors] = useState<VectorMapResponse | null>(null);
    const [vectorsLoading, setVectorsLoading] = useState(false);
    const [vectorsError, setVectorsError] = useState<string | null>(null);

    const [detail, setDetail] = useState<Detail>(null);

    const loadSections = useCallback(async () => {
        setSectionsLoading(true);
        setSectionsError(null);
        try {
            const res = await fetchSections();
            setSections(res.sections);
            setProjectCount(res.project_count);
        } catch {
            setSectionsError('Backend offline — start the AILIENANT Core.');
            setSections(null);
        } finally {
            setSectionsLoading(false);
        }
    }, []);

    useEffect(() => { void loadSections(); }, [loadSections]);

    // Load a section's two layers — only on click (never bulk-load all sections).
    const onSelectSection = useCallback((s: SectionInfo) => {
        setSelected({ project_id: s.project_id, abs_prefix: s.abs_prefix, folder: s.folder });
        setDetail(null);
        setGraph(null);
        setVectors(null);

        setGraphLoading(true);
        setGraphError(null);
        fetchGraph(s.project_id, s.abs_prefix)
            .then(setGraph)
            .catch(() => setGraphError('Failed to load the dependency graph.'))
            .finally(() => setGraphLoading(false));

        setVectorsLoading(true);
        setVectorsError(null);
        fetchVectors(s.project_id, s.abs_prefix)
            .then(setVectors)
            .catch(() => setVectorsError('Failed to load the vector map.'))
            .finally(() => setVectorsLoading(false));
    }, []);

    const onSelectNode = useCallback((node: GraphNode) => setDetail({ kind: 'node', node }), []);
    const onSelectPoint = useCallback(
        (point: VectorPoint | null) => setDetail(point ? { kind: 'point', point } : null),
        [],
    );

    const renderMain = (): JSX.Element => {
        if (!selected) {
            return (
                <div className="mm-empty mm-empty--center">
                    <Icon name="network" size={28} />
                    <span>Select a section on the left to load its memory graph.</span>
                </div>
            );
        }
        if (layer === 'code') {
            if (graphLoading) { return <div className="mm-overlay">Loading dependency graph…</div>; }
            if (graphError) { return <div className="mm-empty">{graphError}</div>; }
            if (graph) { return <CodeGraphLayer graph={graph} search={search} onSelectNode={onSelectNode} />; }
            return <div className="mm-empty">No graph data.</div>;
        }
        if (vectorsLoading) { return <div className="mm-overlay">Loading vector map…</div>; }
        if (vectorsError) { return <div className="mm-empty">{vectorsError}</div>; }
        if (vectors) { return <VectorMapLayer data={vectors} search={search} onSelectPoint={onSelectPoint} />; }
        return <div className="mm-empty">No vector data.</div>;
    };

    return (
        <div className="mm-layout">
            <aside className="mm-rail">
                <div className="db-section-title mm-rail-title">Sections</div>
                <SectionsList
                    sections={sections}
                    projectCount={projectCount}
                    selected={selected}
                    loading={sectionsLoading}
                    error={sectionsError}
                    onSelect={onSelectSection}
                    onRetry={() => void loadSections()}
                />
            </aside>

            <div className="mm-root">
                <div className="mm-toolbar">
                    <div className="mm-search">
                        <Icon name="search" size={14} />
                        <input
                            className="ai-input"
                            placeholder="Search nodes…"
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                        />
                    </div>
                    <div className="mm-layers">
                        <span className="mm-layer-label">Layer</span>
                        <button
                            className="ai-btn"
                            data-variant={layer === 'vector' ? 'primary' : 'ghost'}
                            onClick={() => setLayer('vector')}
                            aria-pressed={layer === 'vector'}
                        >
                            Vector map
                        </button>
                        <button
                            className="ai-btn"
                            data-variant={layer === 'code' ? 'primary' : 'ghost'}
                            onClick={() => setLayer('code')}
                            aria-pressed={layer === 'code'}
                        >
                            Code graph
                        </button>
                        <Tooltip content="Document chunks are not indexed yet">
                            <button className="ai-btn" data-variant="ghost" disabled aria-disabled>
                                Doc chunks
                            </button>
                        </Tooltip>
                    </div>
                </div>

                <div className="mm-graph">
                    {renderMain()}

                    {detail && (
                        <div className="mm-detail ai-card">
                            <div className="mm-detail-head">
                                <strong>{detail.kind === 'node' ? detail.node.label : detail.point.label}</strong>
                                <Tooltip content="Close detail panel" side="left">
                                    <button
                                        className="ai-btn"
                                        data-variant="ghost"
                                        onClick={() => setDetail(null)}
                                        aria-label="Close"
                                    >
                                        <Icon name="x" size={14} />
                                    </button>
                                </Tooltip>
                            </div>
                            {detail.kind === 'node' ? (
                                <div className="mm-detail-rows">
                                    <div><span className="mm-detail-label">PageRank</span><span>{detail.node.ppr_score.toFixed(5)}</span></div>
                                    <div><span className="mm-detail-label">In / Out</span><span>{detail.node.in_degree} / {detail.node.out_degree}</span></div>
                                    <div><span className="mm-detail-label">Type</span><span>{detail.node.is_external ? 'external module' : 'source file'}</span></div>
                                    <div className="mm-detail-path"><span className="mm-detail-label">Path</span><span>{detail.node.full_path}</span></div>
                                </div>
                            ) : (
                                <div className="mm-detail-rows">
                                    <div><span className="mm-detail-label">Tokens</span><span>{detail.point.token_count}</span></div>
                                    <div className="mm-detail-path"><span className="mm-detail-label">Path</span><span>{detail.point.file_path}</span></div>
                                    <div className="mm-detail-snippet">{detail.point.snippet}</div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
