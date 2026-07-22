import { memo } from 'react';

interface RadialGaugeProps {
    /** Current reading, in the same unit as `max`. */
    value: number;
    /** Full-scale value (absolute domain — the arc never auto-zooms). */
    max: number;
    /** Short metric name shown under the value. */
    label: string;
    /** Optional unit suffix (e.g. "GB"). */
    unit?: string;
    /** Warn threshold as a 0..1 fraction of `max`. */
    warnPct?: number;
    /** Critical threshold as a 0..1 fraction of `max`. */
    critPct?: number;
    /** Override for the centered value text; defaults to the numeric `value`. */
    valueText?: string;
}

// Semicircular arc geometry, expressed in viewBox units.
const CX = 60;
const CY = 60;
const R = 50;
const SW = 10;

/** Point on the 180° arc for a 0..1 fraction (0 = left, 1 = right). */
function arcPoint(frac: number, radius: number): [number, number] {
    const theta = Math.PI - frac * Math.PI;
    return [CX + radius * Math.cos(theta), CY - radius * Math.sin(theta)];
}

/**
 * Semicircular gauge on an absolute 0..`max` domain. The fill is a single arc
 * animated via `stroke-dashoffset` (robustly transition-able and GPU-composited,
 * unlike animating SVG geometry attributes), recolored by the status palette as
 * it crosses the warn/crit thresholds — which are also marked as ticks on the arc.
 */
export const RadialGauge = memo(function RadialGauge({
    value, max, label, unit, warnPct = 0.7, critPct = 0.9, valueText,
}: RadialGaugeProps): JSX.Element {
    const frac = max > 0 ? Math.max(0, Math.min(value / max, 1)) : 0;
    const tone = frac >= critPct ? 'critical' : frac >= warnPct ? 'warning' : 'good';

    const [sx, sy] = arcPoint(0, R);
    const [ex, ey] = arcPoint(1, R);
    const arc = `M ${sx} ${sy} A ${R} ${R} 0 0 1 ${ex} ${ey}`;

    const ticks = [warnPct, critPct].map((f) => {
        const theta = Math.PI - f * Math.PI;
        return (
            <line
                key={f}
                x1={CX + (R - SW / 2) * Math.cos(theta)}
                y1={CY - (R - SW / 2) * Math.sin(theta)}
                x2={CX + (R + SW / 2) * Math.cos(theta)}
                y2={CY - (R + SW / 2) * Math.sin(theta)}
                stroke="var(--bg-main)"
                strokeWidth={1.5}
            />
        );
    });

    const display = valueText ?? value.toFixed(unit === 'GB' ? 1 : 0);
    const ariaUnit = unit ? ` ${unit}` : '';

    return (
        <div
            className="db-gauge-radial"
            role="img"
            aria-label={`${label}: ${display}${ariaUnit} of ${max}${ariaUnit} (${Math.round(frac * 100)}%)`}
        >
            <svg viewBox="0 0 120 72" width="100%" preserveAspectRatio="xMidYMid meet" aria-hidden>
                <path
                    d={arc}
                    fill="none"
                    stroke="var(--border-subtle)"
                    strokeWidth={SW}
                    strokeLinecap="round"
                    pathLength={100}
                />
                <path
                    className="db-gauge-radial-fill"
                    d={arc}
                    fill="none"
                    stroke={`var(--status-${tone})`}
                    strokeWidth={SW}
                    strokeLinecap="round"
                    pathLength={100}
                    strokeDasharray={100}
                    strokeDashoffset={100 - frac * 100}
                />
                {ticks}
            </svg>
            <div className="db-gauge-radial-center">
                <div className="db-gauge-radial-value" data-tone={tone}>
                    {display}
                    {unit && <span className="db-gauge-radial-unit">{unit}</span>}
                </div>
                <div className="db-gauge-radial-label">{label}</div>
            </div>
        </div>
    );
});
