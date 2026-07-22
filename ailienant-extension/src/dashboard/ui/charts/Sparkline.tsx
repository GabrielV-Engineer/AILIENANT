import { memo } from 'react';
import type { Sample } from '../../hooks/useRingBuffer';

interface SparklineProps {
    /** Timestamped readings, oldest first (from `useRingBuffer`). */
    samples: Sample<number>[];
    /** Absolute lower bound of the value axis; omit for auto-scale. */
    domainMin?: number;
    /** Absolute upper bound of the value axis; omit for auto-scale. */
    domainMax?: number;
    /** Pixel height of the chart. */
    height?: number;
    /** Draw a soft area under the line. */
    area?: boolean;
    /** Accessible description carrying the live values. */
    ariaLabel: string;
    /** Shown until at least two samples exist. */
    warmingHint?: string;
}

// viewBox units; the line uses a non-scaling stroke so it stays crisp when the
// SVG is stretched to the container width.
const VW = 200;
const VH = 48;

/**
 * Live sparkline plotted against a real wall-clock time axis. A backgrounded tab
 * pauses its feed, so the buffer simply shows a shorter window rather than a
 * fabricated continuous line. Renders a "warming up" hint until it has ≥2 points.
 */
export const Sparkline = memo(function Sparkline({
    samples, domainMin, domainMax, height = 44, area = true, ariaLabel, warmingHint = 'Warming up…',
}: SparklineProps): JSX.Element {
    if (samples.length < 2) {
        return <div className="db-chart-warming db-muted" style={{ height }}>{warmingHint}</div>;
    }

    const ts = samples.map(s => s.t);
    const vs = samples.map(s => s.v);
    const tMin = ts[0];
    const tMax = ts[ts.length - 1];
    const tSpan = Math.max(1, tMax - tMin);

    const vLo = domainMin ?? Math.min(...vs);
    const vHiRaw = domainMax ?? Math.max(...vs);
    const vHi = vHiRaw > vLo ? vHiRaw : vLo + 1;

    const x = (t: number): number => ((t - tMin) / tSpan) * VW;
    const y = (v: number): number => VH - ((Math.max(vLo, Math.min(v, vHi)) - vLo) / (vHi - vLo)) * VH;

    const pts = samples.map(s => `${x(s.t).toFixed(2)},${y(s.v).toFixed(2)}`).join(' ');
    const areaPts = `${x(tMin).toFixed(2)},${VH} ${pts} ${x(tMax).toFixed(2)},${VH}`;
    const last = samples[samples.length - 1];

    return (
        <div className="db-chart-spark" role="img" aria-label={ariaLabel} style={{ height }}>
            <svg viewBox={`0 0 ${VW} ${VH}`} width="100%" height="100%" preserveAspectRatio="none" aria-hidden>
                {area && <polygon className="db-chart-spark-area" points={areaPts} />}
                <polyline
                    className="db-chart-spark-line"
                    points={pts}
                    fill="none"
                    vectorEffect="non-scaling-stroke"
                />
                <circle className="db-chart-spark-dot" cx={x(last.t)} cy={y(last.v)} r={2.5} />
            </svg>
        </div>
    );
});
