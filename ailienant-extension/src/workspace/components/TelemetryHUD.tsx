import { useEffect, useRef, useState } from 'react';
import { OccStatus, TelemetryFrame, TokenSnapshot } from '../../shared/config';
import { Tooltip } from '../../shared/Tooltip';

const OCC_COLORS: Record<OccStatus, string> = {
    clear:         'var(--accent-primary)',
    soft_conflict: 'var(--accent-warn)',
    hard_conflict: 'var(--accent-alert)',
};

interface OccRingProps { status: OccStatus; lockedFiles: number; }

export function OccRing({ status, lockedFiles }: OccRingProps): JSX.Element {
    const r = 10;
    const circ = 2 * Math.PI * r;
    const fill = status === 'clear' ? circ : status === 'soft_conflict' ? circ * 0.6 : circ * 0.3;
    const label = status === 'clear' ? 'OCC clear' : `OCC ${status} — ${lockedFiles} file(s) locked`;

    return (
        <Tooltip content={label}>
            <div className="ws-instr">
                <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden>
                    <circle cx="14" cy="14" r={r} fill="none" stroke="var(--border-subtle)" strokeWidth="3" />
                    <circle cx="14" cy="14" r={r} fill="none"
                        stroke={OCC_COLORS[status]} strokeWidth="3"
                        strokeDasharray={`${fill} ${circ}`}
                        strokeLinecap="round"
                        transform="rotate(-90 14 14)"
                        style={{ transition: 'stroke-dasharray 0.4s ease, stroke 0.3s ease' }}
                    />
                </svg>
                <span className="ws-instr-label">OCC</span>
            </div>
        </Tooltip>
    );
}

interface SpeedometerProps { tps: number; maxTps?: number; }

export function Speedometer({ tps, maxTps = 150 }: SpeedometerProps): JSX.Element {
    const pct = Math.min(tps / maxTps, 1);
    const r = 11;
    const startX = 14 - r, startY = 14;
    const endX = 14 + r * Math.cos(Math.PI - pct * Math.PI);
    const endY = 14 - r * Math.sin(pct * Math.PI);
    const largeArc = pct > 0.5 ? 1 : 0;

    return (
        <Tooltip content={`Inference throughput — ${tps.toFixed(1)} tokens/sec`}>
            <div className="ws-instr">
                <svg width="28" height="18" viewBox="0 0 28 18" aria-hidden>
                    <path
                        d={`M ${14 - r} 14 A ${r} ${r} 0 0 1 ${14 + r} 14`}
                        fill="none" stroke="var(--border-subtle)" strokeWidth="3" strokeLinecap="round"
                    />
                    {pct > 0 && (
                        <path
                            d={`M ${startX} ${startY} A ${r} ${r} 0 ${largeArc} 1 ${endX} ${endY}`}
                            fill="none" stroke="var(--accent-primary)" strokeWidth="3" strokeLinecap="round"
                        />
                    )}
                </svg>
                <div className="ws-instr-stack">
                    <span className="ws-instr-value">{tps.toFixed(0)}</span>
                    <span className="ws-instr-unit">tok/s</span>
                </div>
            </div>
        </Tooltip>
    );
}

interface SparklineProps { points: number[]; maxVal?: number; }

export function TpsSparkline({ points, maxVal = 150 }: SparklineProps): JSX.Element {
    const W = 80, H = 16;
    if (points.length < 2) { return <div style={{ width: W, height: H }} aria-hidden />; }
    const step = W / (points.length - 1);
    const pts = points.map((v, i) =>
        `${i * step},${H - (Math.min(v, maxVal) / maxVal) * H}`
    ).join(' ');
    return (
        <svg width={W} height={H} style={{ display: 'block' }} aria-hidden>
            <polyline points={pts} fill="none" stroke="var(--accent-primary)" strokeWidth="1.5"
                strokeLinejoin="round" strokeLinecap="round" />
        </svg>
    );
}

interface FinOpsBarProps { costUsd: number; budgetUsd: number; softGateMultiplier?: number; }

export function FinOpsBar({ costUsd, budgetUsd, softGateMultiplier = 1.0 }: FinOpsBarProps): JSX.Element {
    const pct = budgetUsd > 0 ? Math.min(costUsd / budgetUsd, 1) : 0;
    const isWarning = costUsd >= budgetUsd * softGateMultiplier;
    return (
        <Tooltip content={`Spent $${costUsd.toFixed(4)} of $${budgetUsd.toFixed(2)} session budget`}>
            <div className="ws-finops-wrap">
                <div className="ws-finops-track">
                    <div
                        className="ws-finops-fill"
                        data-warning={isWarning ? 'true' : 'false'}
                        style={{ width: `${pct * 100}%` }}
                    />
                </div>
                <span className="ws-instr-unit">${costUsd.toFixed(3)}</span>
            </div>
        </Tooltip>
    );
}

export function useTpsCalculator(): { recordChunk: () => void; tps: number; history: number[] } {
    const [tps, setTps] = useState(0);
    const [history, setHistory] = useState<number[]>([]);
    const chunkTimestamps = useRef<number[]>([]);
    const intervalRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

    useEffect(() => {
        intervalRef.current = setInterval(() => {
            const now = Date.now();
            const windowMs = 5000;
            chunkTimestamps.current = chunkTimestamps.current.filter(t => now - t <= windowMs);
            const currentTps = chunkTimestamps.current.length / (windowMs / 1000);
            setTps(currentTps);
            setHistory(prev => {
                const next = [...prev, currentTps];
                return next.length > 60 ? next.slice(-60) : next;
            });
        }, 1000);
        return () => { if (intervalRef.current !== undefined) { clearInterval(intervalRef.current); } };
    }, []);

    const recordChunk = (): void => { chunkTimestamps.current.push(Date.now()); };
    return { recordChunk, tps, history };
}

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
        <div className="ws-telemetry ai-card">
            <OccRing status={occStatus} lockedFiles={lockedFiles} />
            <div className="ws-instr-stack" style={{ flex: 1, minWidth: 90 }}>
                <Speedometer tps={tps} />
                <TpsSparkline points={tpsHistory} />
            </div>
            <FinOpsBar
                costUsd={snapshot?.total_cost_usd ?? 0}
                budgetUsd={budgetUsd}
            />
        </div>
    );
}
