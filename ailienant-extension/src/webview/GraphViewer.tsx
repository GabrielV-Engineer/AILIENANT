import { useCallback, useMemo, useState } from 'react';
import ReactFlow, {
    Background,
    Controls,
    MiniMap,
    Node,
    Edge,
    NodeTypes,
    useNodesState,
    useEdgesState,
    useViewport,
    NodeProps,
} from 'reactflow';
import 'reactflow/dist/style.css';

// ── Status colors ───────────────────────────────────────────────────────────
const STATUS_COLORS: Record<string, string> = {
    pending:     '#9EA8B8',
    in_progress: '#E8C43A',
    completed:   '#63a583',
    failed:      '#E85A4F',
};

const STATUS_PULSE: Record<string, boolean> = {
    in_progress: true,
};

// ── LOD-aware node renderers ─────────────────────────────────────────────────
function FullNode({ data }: NodeProps): JSX.Element {
    const color = STATUS_COLORS[data.status] ?? '#9EA8B8';
    return (
        <div style={{
            padding: '8px 12px',
            borderRadius: 6,
            border: `2px solid ${color}`,
            background: 'var(--vscode-editor-background, #1e1e1e)',
            color: 'var(--vscode-foreground, #ccc)',
            fontSize: 11,
            minWidth: 100,
            animation: STATUS_PULSE[data.status] ? 'ai-pulse 1.2s infinite' : 'none',
        }}>
            <div style={{ fontWeight: 700, color, marginBottom: 2 }}>{data.label}</div>
            <div style={{ opacity: 0.7, fontSize: 10 }}>{data.role ?? data.type}</div>
            <div style={{
                marginTop: 4,
                fontSize: 9,
                padding: '1px 5px',
                borderRadius: 3,
                background: color,
                color: '#fff',
                display: 'inline-block',
            }}>
                {data.status}
            </div>
        </div>
    );
}

function MediumNode({ data }: NodeProps): JSX.Element {
    const color = STATUS_COLORS[data.status] ?? '#9EA8B8';
    return (
        <div style={{
            padding: '4px 8px',
            borderRadius: 4,
            border: `1px solid ${color}`,
            background: 'var(--vscode-editor-background, #1e1e1e)',
            color: 'var(--vscode-foreground, #ccc)',
            fontSize: 10,
            opacity: 0.85,
        }}>
            {data.label}
        </div>
    );
}

function DotNode({ data }: NodeProps): JSX.Element {
    const color = STATUS_COLORS[data.status] ?? '#9EA8B8';
    return (
        <div style={{
            width: 10,
            height: 10,
            borderRadius: '50%',
            background: color,
        }} title={data.label} />
    );
}

// ── LOD-switching wrapper ────────────────────────────────────────────────────
function LodNode(props: NodeProps): JSX.Element {
    const { zoom } = useViewport();
    if (zoom > 0.8)  { return <FullNode   {...props} />; }
    if (zoom > 0.4)  { return <MediumNode {...props} />; }
    return <DotNode {...props} />;
}

const NODE_TYPES: NodeTypes = { lodNode: LodNode };

// ── Heatmap overlay (ultra-zoom < 0.4) ──────────────────────────────────────
function HeatmapOverlay({ edgeDensity }: { edgeDensity: number }): JSX.Element {
    const { zoom } = useViewport();
    if (zoom >= 0.4) { return <></>; }
    // Simple intensity square based on edge density
    const intensity = Math.min(edgeDensity / 20, 1);
    return (
        <div style={{
            position: 'absolute',
            inset: 0,
            background: `rgba(99, 165, 131, ${intensity * 0.2})`,
            pointerEvents: 'none',
            zIndex: 1,
        }} />
    );
}

// ── Public graph mutation event ──────────────────────────────────────────────
export interface GraphMutationEvent {
    step_number: number;
    new_status: string;
    agent_name: string;
}

// ── GraphViewer component ────────────────────────────────────────────────────
interface Props {
    mutations: GraphMutationEvent[];
}

function makeInitialNodes(): Node[] {
    return [];
}

export function GraphViewer({ mutations }: Props): JSX.Element {
    const [nodes, setNodes, onNodesChange] = useNodesState(makeInitialNodes());
    const [edges, _setEdges, onEdgesChange] = useEdgesState<Edge[]>([]);
    const [selectedNode, setSelectedNode] = useState<Node | undefined>();

    // Apply mutations: upsert nodes
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
                        type:     'lodNode',
                        position: { x: (m.step_number % 5) * 160, y: Math.floor(m.step_number / 5) * 120 },
                        data:     nodeData,
                    });
                }
            }
            return next;
        });
    }, [mutations, setNodes]);

    const edgeDensity = edges.length;

    const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
        setSelectedNode(node);
    }, []);

    return (
        <div style={{ position: 'relative', width: '100%', height: '100%' }}>
            <ReactFlow
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={onNodeClick}
                nodeTypes={NODE_TYPES}
                onlyRenderVisibleElements
                fitView
                minZoom={0.1}
                maxZoom={2}
                style={{ background: 'var(--vscode-editor-background, #1e1e1e)' }}
            >
                <Background color="rgba(128,128,128,0.15)" gap={24} />
                <Controls />
                <MiniMap
                    nodeColor={n => STATUS_COLORS[n.data?.status ?? 'pending'] ?? '#9EA8B8'}
                    style={{ background: 'var(--vscode-editor-background, #1e1e1e)' }}
                />
            </ReactFlow>

            <HeatmapOverlay edgeDensity={edgeDensity} />

            {/* Node detail panel */}
            {selectedNode && (
                <div style={{
                    position: 'absolute',
                    top: 8,
                    right: 8,
                    width: 200,
                    background: 'var(--vscode-editor-background, #1e1e1e)',
                    border: '1px solid rgba(128,128,128,0.3)',
                    borderRadius: 4,
                    padding: 10,
                    fontSize: 11,
                    zIndex: 10,
                }}>
                    <div style={{ fontWeight: 700, marginBottom: 4 }}>{selectedNode.data.label}</div>
                    <div>Status: <strong>{selectedNode.data.status}</strong></div>
                    <div>Role: {selectedNode.data.role}</div>
                    <button
                        className="ai-btn ai-btn-secondary"
                        style={{ marginTop: 6, width: '100%' }}
                        onClick={() => setSelectedNode(undefined)}
                    >
                        Close
                    </button>
                </div>
            )}
        </div>
    );
}
