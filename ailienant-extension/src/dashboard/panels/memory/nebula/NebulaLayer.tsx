import { useEffect, useRef, useState } from 'react';
import type { GraphResponse, GraphNode } from '../api';
import type { NebulaController } from './engine';

type Status = 'loading' | 'ready' | 'error';

interface NebulaLayerProps {
    graph: GraphResponse;
    search: string;
    onSelectNode: (node: GraphNode | null) => void;
}

function prefersReducedMotion(): boolean {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; }
}

/**
 * React host for the 3D "Neural Nebula". Lazy-imports the three.js engine (its own
 * split chunk) on mount, forwards search + reduced-motion, and guarantees teardown
 * of the WebGL context on unmount or graph change. Errors are surfaced, never
 * swallowed. The 2D layer remains the fallback when this fails or WebGL is absent.
 */
export function NebulaLayer({ graph, search, onSelectNode }: NebulaLayerProps): JSX.Element {
    const containerRef = useRef<HTMLDivElement>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const ctrlRef = useRef<NebulaController | null>(null);
    const [status, setStatus] = useState<Status>('loading');
    const [errMsg, setErrMsg] = useState<string | null>(null);

    // Build (and rebuild on graph change) the engine.
    useEffect(() => {
        let cancelled = false;
        setStatus('loading');
        setErrMsg(null);

        void (async () => {
            try {
                const canvas = canvasRef.current;
                if (!canvas) { return; }
                const { createNebula } = await import('./engine');
                if (cancelled) { return; }
                ctrlRef.current = createNebula(canvas, graph, {
                    onSelect: onSelectNode,
                    reducedMotion: prefersReducedMotion(),
                });
                const rect = containerRef.current?.getBoundingClientRect();
                if (rect) { ctrlRef.current.resize(rect.width, rect.height); }
                ctrlRef.current.setSearch(search);
                setStatus('ready');
            } catch (err) {
                if (!cancelled) {
                    console.error('[Nebula] engine failed to initialize', err);
                    setErrMsg(err instanceof Error ? err.message : String(err));
                    setStatus('error');
                }
            }
        })();

        return () => {
            cancelled = true;
            ctrlRef.current?.dispose();
            ctrlRef.current = null;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [graph]);

    // Forward search changes without rebuilding the scene.
    useEffect(() => {
        if (status === 'ready') { ctrlRef.current?.setSearch(search); }
    }, [search, status]);

    // Track reduced-motion changes live.
    useEffect(() => {
        const mql = window.matchMedia('(prefers-reduced-motion: reduce)');
        const onChange = (): void => ctrlRef.current?.setReducedMotion(mql.matches);
        mql.addEventListener('change', onChange);
        return () => mql.removeEventListener('change', onChange);
    }, []);

    // Resize with the container.
    useEffect(() => {
        const el = containerRef.current;
        if (!el) { return; }
        const ro = new ResizeObserver(() => {
            const rect = el.getBoundingClientRect();
            ctrlRef.current?.resize(rect.width, rect.height);
        });
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    if (graph.nodes.length === 0) {
        return <div className="mm-empty">No code dependencies indexed for this section yet.</div>;
    }

    return (
        <div ref={containerRef} className="mm-nebula">
            <canvas ref={canvasRef} className="mm-nebula-canvas" />
            {graph.capped && (
                <div className="mm-banner">
                    Showing top {graph.nodes.length} of {graph.total_nodes} nodes by centrality.
                </div>
            )}
            {status === 'loading' && <div className="mm-overlay">Rendering the 3D graph…</div>}
            {status === 'error' && (
                <div className="mm-overlay">
                    3D view unavailable{errMsg ? ` (${errMsg})` : ''} — switch to the 2D graph.
                </div>
            )}
            <div className="mm-nebula-legend">
                <span className="mm-legend-item"><i className="mm-legend-dot mm-legend-file" />file</span>
                <span className="mm-legend-item"><i className="mm-legend-dot mm-legend-ext" />external</span>
                <span className="mm-legend-item"><i className="mm-legend-dot mm-legend-god" />hub (god node)</span>
            </div>
        </div>
    );
}
