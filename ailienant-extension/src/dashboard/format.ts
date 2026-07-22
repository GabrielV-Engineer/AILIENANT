/**
 * Shared, consistent formatters for dashboard widgets. Centralizing these keeps
 * every panel's numbers, currencies, and sizes rendering identically (and with
 * tabular alignment) instead of drifting via ad-hoc `.toFixed` calls.
 */

/** US dollars, fixed precision. Non-finite → em dash. */
export function formatUsd(v: number, dp = 3): string {
    return Number.isFinite(v) ? `$${v.toFixed(dp)}` : '—';
}

/** Gigabytes with a unit suffix. Non-finite → em dash. */
export function formatGb(v: number, dp = 1): string {
    return Number.isFinite(v) ? `${v.toFixed(dp)} GB` : '—';
}

/** Integer count with thousands separators. Non-finite → em dash. */
export function formatCount(v: number): string {
    return Number.isFinite(v) ? Math.round(v).toLocaleString() : '—';
}

/** A 0..1 fraction as a percentage. Non-finite → em dash. */
export function formatPct(fraction: number, dp = 0): string {
    return Number.isFinite(fraction) ? `${(fraction * 100).toFixed(dp)}%` : '—';
}

/** Compact relative-time label ("just now", "12s ago", "3m ago") for freshness cues. */
export function formatAgo(ms: number, now: number = Date.now()): string {
    const s = Math.max(0, Math.round((now - ms) / 1000));
    if (s < 3) { return 'just now'; }
    if (s < 60) { return `${s}s ago`; }
    const m = Math.round(s / 60);
    if (m < 60) { return `${m}m ago`; }
    return `${Math.round(m / 60)}h ago`;
}
