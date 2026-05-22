import { useEffect, useRef, useState, useCallback } from 'react';
import * as Slider from '@radix-ui/react-slider';
import type { VectorMapResponse, VectorPoint } from './api';

// Minimal structural type for the methods we use. Avoids importing types from
// regl-scatterplot (an ESM-only package) into this CommonJS-compiled file.
interface Scatterplot {
    isSupported: boolean;
    draw(points: number[][]): Promise<void>;
    filter(idxs: number[]): Promise<unknown>;
    unfilter(): Promise<unknown>;
    select(idxs: number[]): void;
    deselect(): void;
    destroy(): void;
    set(props: { width: number; height: number }): Promise<unknown>;
    getScreenPosition(idx: number): [number, number] | undefined;
    subscribe<T = unknown>(event: string, handler: (payload: T) => void): unknown;
}
type CreateScatterplot = (props: Record<string, unknown>) => Scatterplot;

type Status = 'loading' | 'ready' | 'lost' | 'unsupported' | 'error';

interface VectorMapLayerProps {
    data: VectorMapResponse;
    search: string;
    onSelectPoint: (p: VectorPoint | null) => void;
}

const MAX_DIST = Math.SQRT2 * 2; // diagonal span of the [-1,1]² domain

export function VectorMapLayer({ data, search, onSelectPoint }: VectorMapLayerProps): JSX.Element {
    const containerRef = useRef<HTMLDivElement>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const scatterRef = useRef<Scatterplot | null>(null);
    const createRef = useRef<CreateScatterplot | null>(null);
    const pointsRef = useRef<VectorPoint[]>(data.points);
    const selectedIdxRef = useRef<number | null>(null);

    const [status, setStatus] = useState<Status>('loading');
    const [tooltip, setTooltip] = useState<{ left: number; top: number; text: string } | null>(null);
    const [threshold, setThreshold] = useState(1); // 1 = show all
    const [hasSelection, setHasSelection] = useState(false);

    pointsRef.current = data.points;

    const drawPoints = useCallback(async () => {
        const sp = scatterRef.current;
        if (!sp) { return; }
        const coords = pointsRef.current.map(p => [p.x, p.y]);
        await sp.draw(coords);
    }, []);

    const initScatter = useCallback(async () => {
        const canvas = canvasRef.current;
        const create = createRef.current;
        if (!canvas || !create) { return; }
        const rect = containerRef.current?.getBoundingClientRect();
        const sp = create({
            canvas,
            width: rect?.width ?? 600,
            height: rect?.height ?? 400,
            pointSize: 5,
            pointColor: '#63a583',
            pointColorActive: '#E3B341',
            pointColorHover: '#E3B341',
            backgroundColor: '#161B22',
            lassoColor: '#63a583',
        });
        if (!sp.isSupported) {
            setStatus('unsupported');
            sp.destroy();
            return;
        }
        scatterRef.current = sp;

        sp.subscribe('pointOver', (idx: number) => {
            const p = pointsRef.current[idx];
            const pos = sp.getScreenPosition(idx);
            if (!p || !pos) { return; }
            setTooltip({ left: pos[0], top: pos[1], text: `${p.label} · ${p.token_count} tok` });
        });
        sp.subscribe('pointOut', () => setTooltip(null));
        sp.subscribe('select', ({ points }: { points: number[] }) => {
            const idx = points[0];
            selectedIdxRef.current = idx ?? null;
            setHasSelection(idx != null);
            onSelectPoint(idx != null ? (pointsRef.current[idx] ?? null) : null);
        });
        sp.subscribe('deselect', () => {
            selectedIdxRef.current = null;
            setHasSelection(false);
            onSelectPoint(null);
        });

        await drawPoints();
        setStatus('ready');
    }, [drawPoints, onSelectPoint]);

    // Mount: lazy-load the WebGL lib (own chunk), init, and wire context-loss.
    useEffect(() => {
        let cancelled = false;
        const canvas = canvasRef.current;

        const onLost = (e: Event): void => {
            e.preventDefault(); // allow the browser to restore the context
            setStatus('lost');
            scatterRef.current = null;
        };
        const onRestored = (): void => {
            // Re-create the renderer and redraw from in-memory points (no refetch).
            void initScatter();
        };

        void (async () => {
            try {
                const mod = await import('regl-scatterplot');
                if (cancelled) { return; }
                createRef.current = mod.default as unknown as CreateScatterplot;
                await initScatter();
            } catch {
                if (!cancelled) { setStatus('error'); }
            }
        })();

        canvas?.addEventListener('webglcontextlost', onLost);
        canvas?.addEventListener('webglcontextrestored', onRestored);

        return () => {
            cancelled = true;
            canvas?.removeEventListener('webglcontextlost', onLost);
            canvas?.removeEventListener('webglcontextrestored', onRestored);
            scatterRef.current?.destroy();
            scatterRef.current = null;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Redraw when the section's points change.
    useEffect(() => {
        if (status === 'ready') {
            selectedIdxRef.current = null;
            setHasSelection(false);
            void drawPoints();
        }
    }, [data, status, drawPoints]);

    // Resize the renderer with its container.
    useEffect(() => {
        const el = containerRef.current;
        if (!el) { return; }
        const ro = new ResizeObserver(() => {
            const sp = scatterRef.current;
            const rect = el.getBoundingClientRect();
            if (sp && rect.width > 0 && rect.height > 0) {
                void sp.set({ width: rect.width, height: rect.height });
            }
        });
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    // Similarity-threshold filter: show only points within a radius of the
    // selected point (client-side, from the 2D coords — no backend call).
    useEffect(() => {
        const sp = scatterRef.current;
        if (!sp || status !== 'ready') { return; }
        const sel = selectedIdxRef.current;
        if (sel == null || threshold >= 1) {
            void sp.unfilter();
            return;
        }
        const pts = pointsRef.current;
        const origin = pts[sel];
        if (!origin) { return; }
        const radius = threshold * MAX_DIST;
        const keep: number[] = [];
        pts.forEach((p, i) => {
            const dx = p.x - origin.x;
            const dy = p.y - origin.y;
            if (Math.sqrt(dx * dx + dy * dy) <= radius) { keep.push(i); }
        });
        void sp.filter(keep);
    }, [threshold, hasSelection, status]);

    // Highlight search matches by selecting them in the scatter.
    useEffect(() => {
        const sp = scatterRef.current;
        if (!sp || status !== 'ready') { return; }
        const q = search.trim().toLowerCase();
        if (!q) { sp.deselect(); return; }
        const matches = pointsRef.current
            .map((p, i) => ((p.label.toLowerCase().includes(q) || p.file_path.toLowerCase().includes(q)) ? i : -1))
            .filter(i => i >= 0);
        if (matches.length) { sp.select(matches); }
    }, [search, status]);

    if (data.point_count === 0) {
        return <div className="mm-empty">No embeddings indexed yet for this section.</div>;
    }

    return (
        <div ref={containerRef} className="mm-scatter">
            <canvas ref={canvasRef} className="mm-scatter-canvas" />

            {status === 'loading' && <div className="mm-overlay">Loading vector map…</div>}
            {status === 'lost' && <div className="mm-overlay">GPU context lost — restoring…</div>}
            {status === 'unsupported' && <div className="mm-overlay">WebGL is not available in this browser.</div>}
            {status === 'error' && <div className="mm-overlay">Failed to load the vector renderer.</div>}
            {data.degenerate && status === 'ready' && (
                <div className="mm-overlay mm-overlay--soft">Embeddings too similar to project a meaningful map.</div>
            )}

            {tooltip && (
                <div className="mm-tooltip" style={{ left: tooltip.left, top: tooltip.top }}>
                    {tooltip.text}
                </div>
            )}

            {hasSelection && status === 'ready' && (
                <div className="mm-threshold">
                    <span className="mm-threshold-label">Neighbors</span>
                    <Slider.Root
                        className="mm-slider"
                        min={0.02} max={1} step={0.02}
                        value={[threshold]}
                        onValueChange={(v) => setThreshold(v[0])}
                    >
                        <Slider.Track className="mm-slider-track">
                            <Slider.Range className="mm-slider-range" />
                        </Slider.Track>
                        <Slider.Thumb className="mm-slider-thumb" aria-label="Similarity threshold" />
                    </Slider.Root>
                    <span className="mm-threshold-val">{threshold >= 1 ? 'all' : `${Math.round(threshold * 100)}%`}</span>
                </div>
            )}
        </div>
    );
}
