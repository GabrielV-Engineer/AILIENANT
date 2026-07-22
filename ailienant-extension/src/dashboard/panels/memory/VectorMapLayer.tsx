import { useEffect, useRef, useState, useMemo } from 'react';
import * as Slider from '@radix-ui/react-slider';
import type { VectorMapResponse, VectorPoint } from './api';
import type { VectorMapController } from './vectormap/engine';

type Status = 'loading' | 'ready' | 'unsupported' | 'error';

interface VectorMapLayerProps {
    data: VectorMapResponse;
    search: string;
    onSelectPoint: (p: VectorPoint | null) => void;
}

// Sequential density ramp (theme --seq), ordered sparse→dense. On the dark surface
// higher density reads brighter, so dense embedding regions glow like a heatmap.
const DENSITY_COLORS = ['#184f95', '#256abf', '#3987e5', '#6da7ec', '#9ec5f4', '#cde2fb', '#eaf4ff'];

// Grid-bin the points and return a per-point density level (0..levels-1). Log-scaled
// so a few very dense cells don't wash out the mid-range. O(n) — cheap at any scale.
function densityLevels(points: VectorPoint[], bins = 36, levels = DENSITY_COLORS.length): number[] {
    if (points.length === 0) { return []; }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of points) {
        if (p.x < minX) { minX = p.x; } if (p.x > maxX) { maxX = p.x; }
        if (p.y < minY) { minY = p.y; } if (p.y > maxY) { maxY = p.y; }
    }
    const rx = (maxX - minX) || 1;
    const ry = (maxY - minY) || 1;
    const cellOf = (p: VectorPoint): number => {
        const gx = Math.min(bins - 1, Math.floor(((p.x - minX) / rx) * bins));
        const gy = Math.min(bins - 1, Math.floor(((p.y - minY) / ry) * bins));
        return gy * bins + gx;
    };
    const grid = new Map<number, number>();
    for (const p of points) { const c = cellOf(p); grid.set(c, (grid.get(c) ?? 0) + 1); }
    let maxCount = 1;
    for (const v of grid.values()) { if (v > maxCount) { maxCount = v; } }
    const denom = Math.log(1 + maxCount);
    return points.map(p => {
        const count = grid.get(cellOf(p)) ?? 1;
        const norm = denom > 0 ? Math.log(1 + count) / denom : 0;
        return Math.min(levels - 1, Math.floor(norm * levels));
    });
}

/**
 * 2D embedding scatter, rendered with three.js (no `eval`, unlike the old
 * regl-scatterplot which the dashboard CSP forbids). Points are density-colored via
 * the sequential ramp; hovering shows a label tooltip, clicking selects a point, and
 * the Neighbors slider dims points beyond a similarity radius of the selection.
 */
export function VectorMapLayer({ data, search, onSelectPoint }: VectorMapLayerProps): JSX.Element {
    const containerRef = useRef<HTMLDivElement>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const ctrlRef = useRef<VectorMapController | null>(null);
    const pointsRef = useRef<VectorPoint[]>(data.points);
    const selectedIdxRef = useRef<number | null>(null);

    const [status, setStatus] = useState<Status>('loading');
    const [errMsg, setErrMsg] = useState<string | null>(null);
    const [tooltip, setTooltip] = useState<{ left: number; top: number; text: string } | null>(null);
    const [threshold, setThreshold] = useState(1);
    const [hasSelection, setHasSelection] = useState(false);

    pointsRef.current = data.points;

    const colors = useMemo(() => {
        const lv = densityLevels(data.points);
        return data.points.map((_, i) => DENSITY_COLORS[lv[i] ?? 0]);
    }, [data]);

    // Build (and rebuild on section change) the three.js renderer.
    useEffect(() => {
        let cancelled = false;
        setStatus('loading');
        setErrMsg(null);
        setTooltip(null);
        selectedIdxRef.current = null;
        setHasSelection(false);
        setThreshold(1);

        const probe = document.createElement('canvas');
        if (!probe.getContext('webgl2') && !probe.getContext('webgl')) {
            setStatus('unsupported');
            return;
        }

        void (async () => {
            try {
                const canvas = canvasRef.current;
                if (!canvas) { return; }
                const { createVectorMap } = await import('./vectormap/engine');
                if (cancelled) { return; }
                ctrlRef.current = createVectorMap(
                    canvas,
                    pointsRef.current.map(p => ({ x: p.x, y: p.y })),
                    {
                        colors,
                        onHover: (idx, lx, ly) => {
                            if (idx === null) { setTooltip(null); return; }
                            const p = pointsRef.current[idx];
                            if (p) { setTooltip({ left: lx, top: ly, text: `${p.label} · ${p.token_count} tok` }); }
                        },
                        onSelect: (idx) => {
                            selectedIdxRef.current = idx;
                            setHasSelection(idx !== null);
                            setThreshold(1);
                            onSelectPoint(idx !== null ? (pointsRef.current[idx] ?? null) : null);
                        },
                    },
                );
                const rect = containerRef.current?.getBoundingClientRect();
                if (rect) { ctrlRef.current.resize(rect.width, rect.height); }
                setStatus('ready');
            } catch (err) {
                if (!cancelled) {
                    console.error('[VectorMap] three.js renderer failed', err);
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
    }, [data, colors, onSelectPoint]);

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

    // Similarity-threshold filter (client-side, on the 2D coords).
    useEffect(() => {
        if (status === 'ready') { ctrlRef.current?.setFilter(selectedIdxRef.current, threshold); }
    }, [threshold, hasSelection, status]);

    // Highlight search matches.
    useEffect(() => {
        if (status !== 'ready') { return; }
        const q = search.trim().toLowerCase();
        if (!q) { ctrlRef.current?.setSearch([]); return; }
        const matches = pointsRef.current
            .map((p, i) => ((p.label.toLowerCase().includes(q) || p.file_path.toLowerCase().includes(q)) ? i : -1))
            .filter(i => i >= 0);
        ctrlRef.current?.setSearch(matches);
    }, [search, status]);

    if (data.point_count === 0) {
        return <div className="mm-empty">No embeddings indexed yet for this section.</div>;
    }

    const pctVar = data.variance_explained.length >= 2
        ? Math.min(100, Math.max(0, Math.round((data.variance_explained[0] + data.variance_explained[1]) * 100)))
        : null;

    return (
        <div ref={containerRef} className="mm-scatter">
            <canvas ref={canvasRef} className="mm-scatter-canvas" />

            {status === 'ready' && (
                <>
                    <div className="mm-vec-caption">
                        PCA projection{pctVar !== null ? ` · ${pctVar}% variance` : ''} · {data.point_count} files
                    </div>
                    <div className="mm-vec-legend" aria-hidden="true">
                        <span className="mm-vec-legend-label">sparse</span>
                        <span className="mm-vec-legend-bar" />
                        <span className="mm-vec-legend-label">dense</span>
                    </div>
                </>
            )}

            {status === 'loading' && <div className="mm-overlay">Loading vector map…</div>}
            {status === 'unsupported' && <div className="mm-overlay">WebGL is not available in this browser.</div>}
            {status === 'error' && (
                <div className="mm-overlay">
                    Failed to load the vector renderer.{errMsg ? ` (${errMsg})` : ''}
                </div>
            )}
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
