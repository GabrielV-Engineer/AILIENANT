import { memo } from 'react';

export interface DonutSlice {
    label: string;
    value: number;
}

interface DonutProps {
    slices: DonutSlice[];
    /** Keep the largest `topN` slices; the remainder collapse into "Other". */
    topN?: number;
    /** Accessible summary of the distribution. */
    ariaLabel: string;
    /** Caption under the center total. */
    centerLabel?: string;
}

const CX = 60;
const CY = 60;
const R = 42;
const SW = 14;

/**
 * Category-distribution donut. High-cardinality inputs are bounded to the top-N
 * slices plus an aggregated "Other", so the ring never fragments into an
 * unreadable confetti of hairline arcs. Segments use the fixed categorical
 * palette (never the status hues) and are drawn via `stroke-dasharray` on a
 * single circle. A direct legend carries the exact value + percent per slice,
 * and a visually-hidden table exposes the same data to assistive tech.
 */
export const Donut = memo(function Donut({ slices, topN = 6, ariaLabel, centerLabel }: DonutProps): JSX.Element {
    const total = slices.reduce((acc, s) => acc + s.value, 0);
    if (total <= 0) {
        return <div className="db-muted db-chart-donut-empty">No data to chart yet.</div>;
    }

    const sorted = [...slices].sort((a, b) => b.value - a.value);
    const head = sorted.slice(0, topN);
    const tail = sorted.slice(topN).reduce((acc, s) => acc + s.value, 0);
    const shown: DonutSlice[] = tail > 0 ? [...head, { label: 'Other', value: tail }] : head;

    let cumulative = 0;

    return (
        <div className="db-chart-donut" role="img" aria-label={ariaLabel}>
            <div className="db-chart-donut-ring">
                <svg viewBox="0 0 120 120" width="120" height="120" aria-hidden>
                    <g transform="rotate(-90 60 60)">
                        <circle cx={CX} cy={CY} r={R} fill="none" stroke="var(--border-subtle)" strokeWidth={SW} pathLength={100} />
                        {shown.map((s, i) => {
                            const frac = s.value / total;
                            const seg = (
                                <circle
                                    key={s.label}
                                    className="db-chart-donut-seg"
                                    cx={CX}
                                    cy={CY}
                                    r={R}
                                    fill="none"
                                    stroke={`var(--cat-${(i % 8) + 1})`}
                                    strokeWidth={SW}
                                    pathLength={100}
                                    strokeDasharray={`${(frac * 100).toFixed(2)} ${(100 - frac * 100).toFixed(2)}`}
                                    strokeDashoffset={`${(-cumulative * 100).toFixed(2)}`}
                                />
                            );
                            cumulative += frac;
                            return seg;
                        })}
                    </g>
                </svg>
                <div className="db-chart-donut-center">
                    <div className="db-chart-donut-total">{total.toLocaleString()}</div>
                    {centerLabel && <div className="db-chart-donut-cap">{centerLabel}</div>}
                </div>
            </div>

            <ul className="db-chart-legend">
                {shown.map((s, i) => (
                    <li key={s.label} className="db-chart-legend-item">
                        <span className="db-chart-legend-swatch" style={{ background: `var(--cat-${(i % 8) + 1})` }} />
                        <span className="db-chart-legend-label">{s.label}</span>
                        <span className="db-chart-legend-val">{s.value.toLocaleString()} · {Math.round((s.value / total) * 100)}%</span>
                    </li>
                ))}
            </ul>

            <table className="db-sr-only">
                <caption>{ariaLabel}</caption>
                <thead><tr><th>Category</th><th>Count</th><th>Share</th></tr></thead>
                <tbody>
                    {shown.map(s => (
                        <tr key={s.label}>
                            <td>{s.label}</td>
                            <td>{s.value}</td>
                            <td>{Math.round((s.value / total) * 100)}%</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
});
