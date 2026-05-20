import { useState, useEffect, useMemo, useCallback } from 'react';
import ReactFlow, {
    Background, Controls, MiniMap, type Node, type Edge, type NodeTypes,
    useNodesState, useEdgesState, useViewport, type NodeProps,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';

const STATUS_COLORS: Record<string, string> = {
    pending:     '#8B949E',
    in_progress: '#E3B341',
    completed:   '#63a583',
    failed:      '#F85149',
};

function FullNode({ data }: NodeProps): JSX.Element {
    const color = STATUS_COLORS[data.status] ?? '#8B949E';
    return (
        <div className="mm-node mm-node--full" style={{ borderColor: color }}>
            <div className="mm-node-title" style={{ color }}>{data.label}</div>
            <div className="mm-node-role">{data.role ?? data.type}</div>
            <span className="mm-node-pill" style={{ background: color }}>{data.status}</span>
        </div>
    );
}

function MediumNode({ data }: NodeProps): JSX.Element {
    const color = STATUS_COLORS[data.status] ?? '#8B949E';
    return (
        <div className="mm-node mm-node--medium" style={{ borderColor: color }}>{data.label}</div>
    );
}

function DotNode({ data }: NodeProps): JSX.Element {
    const color = STATUS_COLORS[data.status] ?? '#8B949E';
    return <div className="mm-node-dot" style={{ background: color }} title={data.label} />;
}

function LodNode(props: NodeProps): JSX.Element {
    const { zoom } = useViewport();
    if (zoom > 0.8) { return <FullNode {...props} />; }
    if (zoom > 0.4) { return <MediumNode {...props} />; }
    return <DotNode {...props} />;
}

const NODE_TYPES: NodeTypes = { lodNode: LodNode };

function HeatmapOverlay({ edgeDensity }: { edgeDensity: number }): JSX.Element | null {
    const { zoom } = useViewport();
    if (zoom >= 0.4) { return null; }
    const intensity = Math.min(edgeDensity / 20, 1);
    return (
        <div className="mm-heatmap" style={{ background: `rgba(99, 165, 131, ${intensity * 0.22})` }} />
    );
}

interface GraphMutationEvent {
    step_number: number;
    new_status: string;
    agent_name: string;
}

interface LayerToggles {
    vector: boolean;
    code:   boolean;
    docs:   boolean;
}

export function MemoryManagement(): JSX.Element {
    const [nodes, setNodes, onNodesChange] = useNodesState([]);
    const [edges, _setEdges, onEdgesChange] = useEdgesState([]);
    const [selected, setSelected] = useState<Node | null>(null);
    const [search, setSearch] = useState('');
    const [layers, setLayers] = useState<LayerToggles>({ vector: true, code: true, docs: true });
    const [mutations, setMutations] = useState<GraphMutationEvent[]>([]);

    // BroadcastChannel listener for live graph mutations
    useEffect(() => {
        const ch = new BroadcastChannel('ailienant_graph');
        ch.onmessage = (e) => {
            const data = e.data as GraphMutationEvent;
            if (typeof data?.step_number === 'number') {
                setMutations(prev => [...prev, data]);
            }
        };
        return () => ch.close();
    }, []);

    useMemo(() => {
        setNodes(prev => {
            const next = [...prev];
            for (const m of mutations) {
                const id = String(m.step_number);
                const idx = next.findIndex(n => n.id === id);
                const nodeData = {
                    label:  `Step ${m.step_number}: ${m.agent_name}`,
                    status: m.new_status,
                    role:   m.agent_name,
                };
                if (idx >= 0) {
                    next[idx] = { ...next[idx], data: nodeData };
                } else {
                    next.push({
                        id,
                        type: 'lodNode',
                        position: { x: (m.step_number % 5) * 180, y: Math.floor(m.step_number / 5) * 130 },
                        data: nodeData,
                    });
                }
            }
            return next;
        });
    }, [mutations, setNodes]);

    const filtered = useMemo(() => {
        if (!search.trim()) { return nodes; }
        const q = search.toLowerCase();
        return nodes.map(n => ({
            ...n,
            hidden: !((n.data?.label ?? '').toLowerCase().includes(q)),
        }));
    }, [nodes, search]);

    const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
        setSelected(node);
    }, []);

    return (
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
                    <span className="mm-layer-label">Layers</span>
                    {([
                        { key: 'vector' as const, label: 'Vector index' },
                        { key: 'code' as const,   label: 'Code entities' },
                        { key: 'docs' as const,   label: 'Doc chunks' },
                    ]).map(l => (
                        <Tooltip key={l.key} content={`Toggle ${l.label} layer`}>
                            <button
                                className="ai-btn"
                                data-variant={layers[l.key] ? 'primary' : 'ghost'}
                                onClick={() => setLayers(p => ({ ...p, [l.key]: !p[l.key] }))}
                                aria-pressed={layers[l.key]}
                            >
                                {l.label}
                            </button>
                        </Tooltip>
                    ))}
                </div>
            </div>

            <div className="mm-graph">
                <ReactFlow
                    nodes={filtered}
                    edges={edges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onNodeClick={onNodeClick}
                    nodeTypes={NODE_TYPES}
                    onlyRenderVisibleElements
                    fitView
                    minZoom={0.1}
                    maxZoom={2}
                >
                    <Background color="#30363D" gap={26} />
                    <Controls className="mm-controls" />
                    <MiniMap
                        nodeColor={n => STATUS_COLORS[n.data?.status ?? 'pending'] ?? '#8B949E'}
                        maskColor="rgba(13, 17, 23, 0.6)"
                        style={{ background: '#161B22', border: '1px solid #30363D' }}
                    />
                </ReactFlow>
                <HeatmapOverlay edgeDensity={edges.length} />

                {selected && (
                    <div className="mm-detail ai-card">
                        <div className="mm-detail-head">
                            <strong>{selected.data?.label}</strong>
                            <Tooltip content="Close detail panel" side="left">
                                <button
                                    className="ai-btn"
                                    data-variant="ghost"
                                    onClick={() => setSelected(null)}
                                    aria-label="Close"
                                >
                                    <Icon name="x" size={14} />
                                </button>
                            </Tooltip>
                        </div>
                        <div className="mm-detail-rows">
                            <div><span className="mm-detail-label">Status</span><span>{selected.data?.status}</span></div>
                            <div><span className="mm-detail-label">Role</span><span>{selected.data?.role}</span></div>
                            <div><span className="mm-detail-label">Node ID</span><span>{selected.id}</span></div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
