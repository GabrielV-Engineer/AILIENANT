import type { ReactNode } from 'react';

interface StatTileProps {
    /** Uppercase micro-label describing the metric. */
    label: string;
    /** The headline figure. Rendered with tabular figures so live-polling
     *  values never shift horizontally between ticks. */
    value: ReactNode;
    /** Optional secondary line under the value. */
    sub?: ReactNode;
    /** Optional action row (e.g. a "Manage" button). */
    footer?: ReactNode;
    /** Tints the value with a status hue for at-a-glance state. */
    tone?: 'default' | 'good' | 'warning' | 'critical';
    title?: string;
}

/**
 * KPI tile — the "big number + label" pattern hand-rolled across panels,
 * standardized here with tabular figures and a consistent hierarchy.
 */
export function StatTile({ label, value, sub, footer, tone = 'default', title }: StatTileProps): JSX.Element {
    return (
        <div className="db-card ui-stat" style={{ marginBottom: 0 }}>
            <div className="db-card-title" style={{ marginBottom: 0 }} title={title}>{label}</div>
            <div className="ui-stat-value" data-tone={tone}>{value}</div>
            {sub && <div className="db-muted ui-stat-sub">{sub}</div>}
            {footer && <div className="ui-stat-footer">{footer}</div>}
        </div>
    );
}
