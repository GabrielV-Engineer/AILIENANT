import { useEffect, useRef, useState } from 'react';
import { OccStatus, TelemetryFrame, TokenSnapshot } from '../../shared/config';

// ── OCC Ring ────────────────────────────────────────────────────────────────
interface OccRingProps {
    status: OccStatus;
    lockedFiles: number;
}

const OCC_COLORS: Record<OccStatus, string> = {
    clear:         '#63a583',
    soft_conflict: '#E8C43A',
    hard_conflict: '#E85A4F',
};

export function OccRing({ status, lockedFiles }: OccRingProps): JSX.Element {
    const r = 10;
    const circ = 2 * Math.PI * r;
    const fill = status === 'clear' ? circ : status === 'soft_conflict' ? circ * 0.6 : circ * 0.3;

    return (
        <div className="ai-row" style={{ gap: 5 }} title={`OCC: ${status} — ${lockedFiles} file(s) locked`}>
            <svg width="28" height="28" viewBox="0 0 28 28">
                <circle cx="14" cy="14" r={r} fill="none"
                    stroke="rgba(128,128,128,0.2)" strokeWidth="3" />
                <circle cx="14" cy="14" r={r} fill="none"
                    stroke={OCC_COLORS[status]} strokeWidth="3"
                    strokeDasharray={`${fill} ${circ}`}
                    strokeLinecap="round"
                    transform="rotate(-90 14 14)"
                    style={{ transition: 'stroke-dasharray 0.4s ease, stroke 0.3s ease' }}
                />
            </svg>
            <span className="ai-muted" style={{ fontSize: 10 }}>OCC</span>
        </div>
    );
}

// ── Inference Speedometer ───────────────────────────────────────────────────
interface SpeedometerProps {
    tps: number;
    maxTps?: number;
}

export function Speedometer({ tps, maxTps = 150 }: SpeedometerProps): JSX.Element {
    const pct = Math.min(tps / maxTps, 1);
    const r = 11;
    // Semi-circle arc: start at 180deg (left), sweep to 0deg (right)
    const startX = 14 - r;
    const startY = 14;
    const endX   = 14 + r * Math.cos(Math.PI - pct * Math.PI);
    const endY   = 14 - r * Math.sin(pct * Math.PI);
    const largeArc = pct > 0.5 ? 1 : 0;

    return (
        <div className="ai-row" style={{ gap: 5 }} title={`${tps.toFixed(1)} tok/s`}>
            <svg width="28" height="18" viewBox="0 0 28 18">
                {/* Track */}
                <path
                    d={`M ${14 - r} 14 A ${r} ${r} 0 0 1 ${14 + r} 14`}
                    fill="none" stroke="rgba(128,128,128,0.2)" strokeWidth="3"
                    strokeLinecap="round"
                />
                {/* Fill */}
                {pct > 0 && (
                    <path
                        d={`M ${startX} ${startY} A ${r} ${r} 0 ${largeArc} 1 ${endX} ${endY}`}
                        fill="none" stroke="#63a583" strokeWidth="3"
                        strokeLinecap="round"
                        style={{ transition: 'd 0.3s ease' }}
                    />
                )}
            </svg>
            <div className="ai-col" style={{ gap: 0 }}>
                <span style={{ fontSize: 11, fontWeight: 600, lineHeight: 1 }}>{tps.toFixed(0)}</span>
                <span className="ai-muted" style={{ fontSize: 9 }}>tok/s</span>
            </div>
        </div>
    );
}

// ── TPS Sparkline (60-point SVG polyline) ──────────────────────────────────
interface SparklineProps {
    points: number[];
    maxVal?: number;
}

export function TpsSparkline({ points, maxVal = 150 }: SparklineProps): JSX.Element {
    const W = 80;
    const H = 16;
    if (points.length < 2) { return <div style={{ width: W, height: H }} />; }

    const step = W / (points.length - 1);
    const pts = points.map((v, i) =>
        `${i * step},${H - (Math.min(v, maxVal) / maxVal) * H}`
    ).join(' ');

    return (
        <svg width={W} height={H} style={{ display: 'block' }}>
            <polyline points={pts} fill="none" stroke="#63a583" strokeWidth="1.5"
                strokeLinejoin="round" strokeLinecap="round" />
        </svg>
    );
}

// ── FinOps Bar ──────────────────────────────────────────────────────────────
interface FinOpsBarProps {
    costUsd: number;
    budgetUsd: number;
    softGateMultiplier?: number;
}

export function FinOpsBar({ costUsd, budgetUsd, softGateMultiplier = 1.0 }: FinOpsBarProps): JSX.Element {
    const pct = budgetUsd > 0 ? Math.min(costUsd / budgetUsd, 1) : 0;
    const isWarning = costUsd >= budgetUsd * softGateMultiplier;

    return (
        <div className="ai-row" style={{ gap: 5, flex: 1 }} title={`$${costUsd.toFixed(4)} / $${budgetUsd.toFixed(2)}`}>
            <div className="ai-finops-bar-track" style={{ flex: 1 }}>
                <div
                    className="ai-finops-bar-fill"
                    data-warning={isWarning ? 'true' : 'false'}
                    style={{ width: `${pct * 100}%` }}
                />
            </div>
            <span className="ai-muted" style={{ fontSize: 10, whiteSpace: 'nowrap' }}>
                ${costUsd.toFixed(3)}
            </span>
        </div>
    );
}

// ── TPS calculator hook ─────────────────────────────────────────────────────
export function useTpsCalculator(): {
    recordChunk: () => void;
    tps: number;
    history: number[];
} {
    const [tps, setTps] = useState(0);
    const [history, setHistory] = useState<number[]>([]);
    const chunkTimestamps = useRef<number[]>([]);
    const intervalRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

    useEffect(() => {
        intervalRef.current = setInterval(() => {
            const now = Date.now();
            const windowMs = 5000;
            // Prune old timestamps
            chunkTimestamps.current = chunkTimestamps.current.filter(t => now - t <= windowMs);
            const currentTps = chunkTimestamps.current.length / (windowMs / 1000);
            setTps(currentTps);
            setHistory(prev => {
                const next = [...prev, currentTps];
                return next.length > 60 ? next.slice(-60) : next;
            });
        }, 1000);

        return () => {
            if (intervalRef.current !== undefined) { clearInterval(intervalRef.current); }
        };
    }, []);

    const recordChunk = (): void => {
        chunkTimestamps.current.push(Date.now());
    };

    return { recordChunk, tps, history };
}

// ── Composite TelemetryHUD ──────────────────────────────────────────────────
interface TelemetryHUDProps {
    occStatus: OccStatus;
    lockedFiles: number;
    tps: number;
    tpsHistory: number[];
    snapshot: TokenSnapshot | undefined;
    budgetUsd: number;
    telemetry: TelemetryFrame | undefined;
}

export function TelemetryHUD({
    occStatus, lockedFiles, tps, tpsHistory, snapshot, budgetUsd,
}: TelemetryHUDProps): JSX.Element {
    return (
        <div className="ai-section">
            <div className="ai-row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 4 }}>
                <OccRing status={occStatus} lockedFiles={lockedFiles} />
                <div className="ai-col" style={{ gap: 2, flex: 1, minWidth: 80 }}>
                    <Speedometer tps={tps} />
                    <TpsSparkline points={tpsHistory} />
                </div>
                <div style={{ flex: 1, minWidth: 70 }}>
                    <FinOpsBar
                        costUsd={snapshot?.total_cost_usd ?? 0}
                        budgetUsd={budgetUsd}
                    />
                </div>
            </div>
        </div>
    );
}
