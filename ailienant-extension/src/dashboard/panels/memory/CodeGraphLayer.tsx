import { useEffect, useMemo, useCallback } from 'react';
import ReactFlow, {
    Background, Controls, MiniMap, Handle, Position, type Node, type Edge, type NodeTypes,
    useNodesState, useEdgesState, useViewport, type NodeProps,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { forceSimulation, forceManyBody, forceLink, forceCenter, type SimNode } from 'd3-force-3d';
import type { GraphResponse, GraphNode } from './api';

const EXTERNAL_COLOR = '#6E7681';
const GOD_NODE_SCALE = 1.5;

// Green ramp from muted to bright by normalized PPR (0..1). Fallback coloring
// when a node has no community id yet (analytics pass not run).
function pprColor(norm: number): string {
    const lo = [40, 70, 55];   // dim green
    const hi = [99, 165, 131]; // --accent-primary
    const mix = lo.map((c, i) => Math.round(c + (hi[i] - c) * norm));
    return `rgb(${mix[0]}, ${mix[1]}, ${mix[2]})`;
}

// Stable categorical palette indexed by Louvain community id. Golden-angle hue
// rotation gives well-separated, deterministic colors for any community count.
function communityColor(id: number): string {
    const hue = (id * 137.508) % 360;
    return `hsl(${hue.toFixed(1)}, 60%, 62%)`;
}

// Confidence → edge stroke style: EXTRACTED solid, INFERRED dashed, AMBIGUOUS
// amber. Colors are chosen to read clearly against the dark canvas (the old
// near-black strokes were effectively invisible).
function edgeStyle(confidence?: string | null): React.CSSProperties {
    if (confidence === 'AMBIGUOUS') { return { stroke: '#f0883e', strokeWidth: 1.4 }; }
    if (confidence === 'INFERRED') { return { stroke: '#5b6785', strokeWidth: 1.2, strokeDasharray: '6 4' }; }
    return { stroke: '#7c8bb0', strokeWidth: 1.2 };  // EXTRACTED or unknown
}

interface NodeData {
    label: string;
    color: string;
    size: number;
    is_external: boolean;
    full_path: string;
    ppr_score: number;
    in_degree: number;
    out_degree: number;
    is_god_node: boolean;
}

function FullNode({ data }: NodeProps<NodeData>): JSX.Element {
    const scale = data.is_god_node ? GOD_NODE_SCALE : 1;
    return (
        <div
            className="mm-node mm-node--full"
            style={{
                borderColor: data.color,
                minWidth: (100 + data.size * 60) * scale,
                borderWidth: data.is_god_node ? 2 : undefined,
            }}
        >
            <div className="mm-node-title" style={{ color: data.color }}>{data.label}</div>
            <div className="mm-node-role">
                {data.is_external ? 'external module' : `in ${data.in_degree} · out ${data.out_degree}`}
            </div>
        </div>
    );
}

function MediumNode({ data }: NodeProps<NodeData>): JSX.Element {
    return (
        <div className="mm-node mm-node--medium" style={{ borderColor: data.color }}>
            {data.label}
        </div>
    );
}

function DotNode({ data }: NodeProps<NodeData>): JSX.Element {
    const d = (8 + data.size * 14) * (data.is_god_node ? GOD_NODE_SCALE : 1);
    return (
        <div
            className="mm-node-dot"
            style={{ background: data.color, width: d, height: d }}
            title={data.label}
        />
    );
}

function LodNode(props: NodeProps<NodeData>): JSX.Element {
    const { zoom } = useViewport();
    const inner = zoom > 0.8 ? <FullNode {...props} /> : zoom > 0.4 ? <MediumNode {...props} /> : <DotNode {...props} />;
    // Custom ReactFlow nodes must expose handles or edges have no anchor and never
    // render. Both are pinned to the node centre (via CSS) and hidden, so edges
    // draw straight centre-to-centre — this graph is undirected, so every node is
    // both a source and a target.
    return (
        <>
            <Handle type="target" position={Position.Top} className="mm-handle" isConnectable={false} />
            {inner}
            <Handle type="source" position={Position.Bottom} className="mm-handle" isConnectable={false} />
        </>
    );
}

const NODE_TYPES: NodeTypes = { lodNode: LodNode };

// Force-directed placement: a one-shot d3-force pass in 2D, then frozen — nodes
// cluster by their actual connectivity (unlike the old phyllotaxis spiral, which
// ignored edges). Bounded iterations keep it off the render loop.
function layout(nodes: GraphNode[], edges: GraphResponse['edges']): Map<string, { x: number; y: number }> {
    const simNodes: SimNode[] = nodes.map(n => ({ id: n.id }));
    const links = edges.map(e => ({ source: e.source, target: e.target }));
    const sim = forceSimulation(simNodes, 2)
        .force('charge', forceManyBody().strength(-120).distanceMax(700))
        .force('link', forceLink(links).id((d: SimNode) => d.id).distance(70).strength(0.4))
        .force('center', forceCenter(0, 0).strength(0.05))
        .alphaDecay(0.05)
        .stop();
    sim.tick(Math.min(240, Math.max(90, Math.round(1200 / Math.sqrt(nodes.length + 1)))));
    const pos = new Map<string, { x: number; y: number }>();
    simNodes.forEach(sn => pos.set(sn.id, { x: (sn.x ?? 0) * 2.2, y: (sn.y ?? 0) * 2.2 }));
    return pos;
}

function toFlow(graph: GraphResponse): { nodes: Node<NodeData>[]; edges: Edge[] } {
    const maxPpr = Math.max(1e-9, ...graph.nodes.map(n => n.ppr_score));
    const pos = layout(graph.nodes, graph.edges);
    const nodes: Node<NodeData>[] = graph.nodes.map(n => {
        const norm = Math.min(1, n.ppr_score / maxPpr);
        const hasCommunity = n.leiden_community_id !== null && n.leiden_community_id !== undefined;
        const color = n.is_external
            ? EXTERNAL_COLOR
            : hasCommunity
                ? communityColor(n.leiden_community_id as number)
                : pprColor(norm);   // fallback until the analytics pass assigns a community
        return {
            id: n.id,
            type: 'lodNode',
            position: pos.get(n.id) ?? { x: 0, y: 0 },
            data: {
                label: n.label,
                color,
                size: norm,
                is_external: n.is_external,
                full_path: n.full_path,
                ppr_score: n.ppr_score,
                in_degree: n.in_degree,
                out_degree: n.out_degree,
                is_god_node: n.is_god_node ?? false,
            },
        };
    });
    const edges: Edge[] = graph.edges.map((e, i) => ({
        id: `e${i}`,
        source: e.source,
        target: e.target,
        style: edgeStyle(e.confidence),
    }));
    return { nodes, edges };
}

interface CodeGraphLayerProps {
    graph: GraphResponse;
    search: string;
    onSelectNode: (node: GraphNode) => void;
}

export function CodeGraphLayer({ graph, search, onSelectNode }: CodeGraphLayerProps): JSX.Element {
    const built = useMemo(() => toFlow(graph), [graph]);
    const [nodes, setNodes, onNodesChange] = useNodesState(built.nodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(built.edges);

    // Reset graph contents whenever a new section is loaded.
    useEffect(() => {
        setNodes(built.nodes);
        setEdges(built.edges);
    }, [built, setNodes, setEdges]);

    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) { return nodes.map(n => ({ ...n, className: undefined })); }
        // Pulse matches, dim the rest — a highlight, not a hide, so the graph's
        // shape stays legible while the search result stands out.
        return nodes.map(n => {
            const hit = n.data.label.toLowerCase().includes(q) || n.data.full_path.toLowerCase().includes(q);
            return { ...n, className: hit ? 'mm-node-hit' : 'mm-node-dim' };
        });
    }, [nodes, search]);

    const onNodeClick = useCallback((_: React.MouseEvent, node: Node<NodeData>) => {
        onSelectNode({
            id: node.id,
            label: node.data.label,
            ppr_score: node.data.ppr_score,
            in_degree: node.data.in_degree,
            out_degree: node.data.out_degree,
            is_external: node.data.is_external,
            full_path: node.data.full_path,
        });
    }, [onSelectNode]);

    if (graph.nodes.length === 0) {
        return <div className="mm-empty">No code dependencies indexed for this section yet.</div>;
    }

    return (
        <>
            {graph.capped && (
                <div className="mm-banner">
                    Showing top {graph.nodes.length} of {graph.total_nodes} nodes by centrality.
                </div>
            )}
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
                    nodeColor={n => (n.data as NodeData)?.color ?? '#8B949E'}
                    maskColor="rgba(13, 17, 23, 0.6)"
                    style={{ background: '#161B22', border: '1px solid #30363D' }}
                />
            </ReactFlow>
        </>
    );
}
