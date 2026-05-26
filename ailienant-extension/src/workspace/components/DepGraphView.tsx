/**
 * Phase 7.11.6 (ADR-706 §4.5f) — Lightweight CSS/DOM dependency graph.
 *
 * Renders a `{nodes, edges}` graph as a native disclosure tree (`<details>` /
 * `<summary>`) so it's keyboard-accessible for free and contributes zero
 * runtime cost beyond the markup. NO canvas, NO `d3`, NO `reactflow` for this
 * surface — the chip's "graph" tab is meant to be a compact summary, not a
 * full interactive node-link diagram (that's the future
 * "Topological exec tree" feature, ADR-706 §4.5h).
 *
 * Rendering rules:
 *   - Roots = nodes with no incoming edge. If the graph has no roots (it's a
 *     cycle), the lowest-label node is treated as the root.
 *   - Each node renders once. Cycles are broken by tracking visited ids so
 *     deeply-recursive imports don't spin the renderer.
 *   - Labels are sanitized via `sanitizeText` before becoming JSX text —
 *     belt-and-suspenders even though React text nodes are already escaped.
 */
import { memo, useMemo } from 'react';
import type { ToolCallShape } from '../../shared/config';
import { sanitizeText } from '../utils/sanitizer';

type GraphNode = { id: string; label: string };
type GraphEdge = { from: string; to: string };
type Graph = NonNullable<ToolCallShape['dep_graph']>;

interface Props {
    graph: Graph;
}

export const DepGraphView = memo(function DepGraphView({ graph }: Props): JSX.Element {
    const { roots, childrenById, nodeById } = useMemo(() => {
        const incoming = new Map<string, number>();
        const childrenById = new Map<string, string[]>();
        const nodeById = new Map<string, GraphNode>();
        for (const n of graph.nodes) {
            nodeById.set(n.id, n);
            if (!incoming.has(n.id)) { incoming.set(n.id, 0); }
            if (!childrenById.has(n.id)) { childrenById.set(n.id, []); }
        }
        for (const e of graph.edges) {
            incoming.set(e.to, (incoming.get(e.to) ?? 0) + 1);
            const children = childrenById.get(e.from) ?? [];
            children.push(e.to);
            childrenById.set(e.from, children);
        }
        let roots = graph.nodes
            .filter(n => (incoming.get(n.id) ?? 0) === 0)
            .map(n => n.id);
        // Cycle-only graph: fall back to alphabetic root pick so we still
        // render something useful.
        if (roots.length === 0 && graph.nodes.length > 0) {
            roots = [graph.nodes
                .map(n => n.id).sort()[0]];
        }
        return { roots, childrenById, nodeById };
    }, [graph]);

    if (graph.nodes.length === 0) {
        return (
            <div className="ws-dep-graph-empty">
                No dependency information was attached to this tool call.
            </div>
        );
    }

    return (
        <ul className="ws-dep-graph" role="tree">
            {roots.map(rootId => (
                <DepGraphNode
                    key={rootId}
                    nodeId={rootId}
                    childrenById={childrenById}
                    nodeById={nodeById}
                    visited={new Set()}
                    depth={0}
                />
            ))}
        </ul>
    );
});

interface NodeProps {
    nodeId: string;
    childrenById: Map<string, string[]>;
    nodeById: Map<string, GraphNode>;
    visited: Set<string>;
    depth: number;
}

function DepGraphNode({
    nodeId, childrenById, nodeById, visited, depth,
}: NodeProps): JSX.Element {
    const node = nodeById.get(nodeId);
    const label = sanitizeText(node?.label ?? nodeId);
    const kids = childrenById.get(nodeId) ?? [];
    const alreadySeen = visited.has(nodeId);
    const nextVisited = new Set(visited);
    nextVisited.add(nodeId);

    if (alreadySeen || kids.length === 0) {
        return (
            <li className="ws-dep-graph-leaf" role="treeitem" data-depth={depth}>
                <span className="ws-dep-graph-connector" aria-hidden="true">└─</span>
                <span className="ws-dep-graph-label">{label}</span>
                {alreadySeen && (
                    <span className="ws-dep-graph-cycle" aria-label="cyclic reference"> ↻</span>
                )}
            </li>
        );
    }

    return (
        <li className="ws-dep-graph-branch" role="treeitem" data-depth={depth}>
            <details open={depth === 0}>
                <summary>
                    <span className="ws-dep-graph-connector" aria-hidden="true">├─</span>
                    <span className="ws-dep-graph-label">{label}</span>
                    <span className="ws-dep-graph-count" aria-label={`${kids.length} children`}>
                        ({kids.length})
                    </span>
                </summary>
                <ul role="group">
                    {kids.map(kid => (
                        <DepGraphNode
                            key={kid}
                            nodeId={kid}
                            childrenById={childrenById}
                            nodeById={nodeById}
                            visited={nextVisited}
                            depth={depth + 1}
                        />
                    ))}
                </ul>
            </details>
        </li>
    );
}
