interface SkeletonProps {
    width?: number | string;
    height?: number | string;
    radius?: number | string;
    /** Render N stacked bars (e.g. a loading list). Defaults to 1. */
    count?: number;
    className?: string;
}

/**
 * Standard loading placeholder with a subtle shimmer, replacing the ad-hoc
 * opacity divs panels hand-roll. Honors prefers-reduced-motion via CSS.
 */
export function Skeleton({ width = '100%', height = 16, radius = 'var(--radius-sm)', count = 1, className }: SkeletonProps): JSX.Element {
    const bars = Array.from({ length: Math.max(1, count) }, (_, i) => (
        <div
            key={i}
            className={['ui-skeleton', className].filter(Boolean).join(' ')}
            style={{ width, height, borderRadius: radius, marginBottom: count > 1 && i < count - 1 ? 8 : 0 }}
        />
    ));
    return <>{bars}</>;
}
