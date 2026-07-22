import { useState, useEffect, useCallback, useMemo } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { Badge, StatTile, EmptyState } from '../ui';
import { SectionsList } from './memory/SectionsList';
import { CodeGraphLayer } from './memory/CodeGraphLayer';
import { VectorMapLayer } from './memory/VectorMapLayer';
import { NebulaLayer } from './memory/nebula/NebulaLayer';
import { EmbeddingBrowser } from './memory/EmbeddingBrowser';
import {
    fetchSections, fetchGraph, fetchVectors,
    type SectionInfo, type GraphResponse, type GraphNode,
    type VectorMapResponse, type VectorPoint, type EmbeddingRow,
} from './memory/api';

type Layer = 'nebula' | 'graph2d' | 'vector' | 'embeddings';

interface SelectedSection {
    project_id: string;
    abs_prefix: string;
    folder: string;
}

type Detail =
    | { kind: 'node'; node: GraphNode }
    | { kind: 'point'; point: VectorPoint }
    | { kind: 'embedding'; row: EmbeddingRow }
    | null;

function hasWebGL(): boolean {
    try {
        const c = document.createElement('canvas');
        return !!(c.getContext('webgl2') || c.getContext('webgl'));
    } catch { return false; }
}

export function MemoryManagement(): JSX.Element {
    const [sections, setSections] = useState<SectionInfo[] | null>(null);
    const [projectCount, setProjectCount] = useState(0);
    const [sectionsLoading, setSectionsLoading] = useState(true);
    const [sectionsError, setSectionsError] = useState<string | null>(null);

    const [selected, setSelected] = useState<SelectedSection | null>(null);
    const webglOk = useMemo(() => hasWebGL(), []);
    // Default to the 3D nebula; fall back to the 2D graph only when WebGL is absent.
    // Reduced-motion does NOT force 2D — the nebula simply renders static.
    const [layer, setLayer] = useState<Layer>(webglOk ? 'nebula' : 'graph2d');
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

    const onSelectNode = useCallback((node: GraphNode | null) => {
        setDetail(node ? { kind: 'node', node } : null);
    }, []);
    const onSelectPoint = useCallback(
        (point: VectorPoint | null) => setDetail(point ? { kind: 'point', point } : null),
        [],
    );
    const onSelectRow = useCallback((row: EmbeddingRow) => setDetail({ kind: 'embedding', row }), []);

    const renderMain = (): JSX.Element => {
        if (!selected) {
            return (
                <EmptyState
                    icon="network"
                    title="Select a section"
                    hint="Choose a section on the left to load its knowledge nebula."
                />
            );
        }
        if (layer === 'embeddings') {
            return (
                <EmbeddingBrowser
                    projectId={selected.project_id}
                    folder={selected.abs_prefix}
                    onSelectRow={onSelectRow}
                />
            );
        }
        if (layer === 'vector') {
            if (vectorsLoading) { return <div className="mm-overlay">Loading vector map…</div>; }
            if (vectorsError) { return <div className="mm-empty">{vectorsError}</div>; }
            if (vectors) { return <VectorMapLayer data={vectors} search={search} onSelectPoint={onSelectPoint} />; }
            return <div className="mm-empty">No vector data.</div>;
        }
        // nebula / graph2d both consume the graph payload.
        if (graphLoading) { return <div className="mm-overlay">Loading dependency graph…</div>; }
        if (graphError) { return <div className="mm-empty">{graphError}</div>; }
        if (!graph) { return <div className="mm-empty">No graph data.</div>; }
        return layer === 'nebula'
            ? <NebulaLayer graph={graph} search={search} onSelectNode={onSelectNode} />
            : <CodeGraphLayer graph={graph} search={search} onSelectNode={(n) => onSelectNode(n)} />;
    };

    const layerBtn = (id: Layer, label: string): JSX.Element => (
        <button
            className="ai-btn"
            data-variant={layer === id ? 'primary' : 'ghost'}
            onClick={() => setLayer(id)}
            aria-pressed={layer === id}
        >
            {label}
        </button>
    );

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
                        {webglOk ? layerBtn('nebula', 'Graph 3D') : (
                            <Tooltip content="WebGL unavailable — using the 2D graph">
                                <button className="ai-btn" data-variant="ghost" disabled aria-disabled>Graph 3D</button>
                            </Tooltip>
                        )}
                        {layerBtn('graph2d', 'Graph 2D')}
                        {layerBtn('vector', 'Vector map')}
                        {layerBtn('embeddings', 'Embeddings')}
                    </div>
                </div>

                <div className="mm-graph">
                    {renderMain()}
                    {detail && <DetailCard detail={detail} onClose={() => setDetail(null)} />}
                </div>
            </div>
        </div>
    );
}

function DetailCard({ detail, onClose }: { detail: NonNullable<Detail>; onClose: () => void }): JSX.Element {
    const title =
        detail.kind === 'node' ? detail.node.label
            : detail.kind === 'point' ? detail.point.label
                : detail.row.label;

    return (
        <div className="mm-detail mm-glass">
            <div className="mm-detail-head">
                <strong>{title}</strong>
                <Tooltip content="Close detail panel" side="left">
                    <button className="ai-btn" data-variant="ghost" onClick={onClose} aria-label="Close">
                        <Icon name="x" size={14} />
                    </button>
                </Tooltip>
            </div>

            {detail.kind === 'node' && (
                <>
                    <div className="mm-detail-badges">
                        <Badge status={detail.node.is_external ? 'neutral' : 'info'}>
                            {detail.node.is_external ? 'external module' : 'source file'}
                        </Badge>
                        {detail.node.is_god_node && <Badge status="warning" icon="zap">hub</Badge>}
                        {detail.node.leiden_community_id != null && (
                            <Badge status="neutral">community {detail.node.leiden_community_id}</Badge>
                        )}
                    </div>
                    <div className="mm-detail-stats">
                        <StatTile label="Centrality" value={detail.node.ppr_score.toFixed(4)} />
                        <StatTile label="In / Out" value={`${detail.node.in_degree} / ${detail.node.out_degree}`} />
                    </div>
                    <div className="mm-detail-path"><span className="mm-detail-label">Path</span><span>{detail.node.full_path}</span></div>
                </>
            )}

            {detail.kind === 'point' && (
                <>
                    <div className="mm-detail-stats">
                        <StatTile label="Tokens" value={detail.point.token_count.toLocaleString()} />
                    </div>
                    <div className="mm-detail-path"><span className="mm-detail-label">Path</span><span>{detail.point.file_path}</span></div>
                    <div className="mm-detail-snippet">{detail.point.snippet}</div>
                </>
            )}

            {detail.kind === 'embedding' && (
                <>
                    <div className="mm-detail-stats">
                        <StatTile label="Tokens" value={detail.row.token_count.toLocaleString()} />
                        <StatTile label="Indexed" value={detail.row.indexed_at.slice(0, 10) || '—'} />
                    </div>
                    <div className="mm-detail-path"><span className="mm-detail-label">Path</span><span>{detail.row.file_path}</span></div>
                    <div className="mm-detail-snippet">{detail.row.snippet}</div>
                </>
            )}
        </div>
    );
}
