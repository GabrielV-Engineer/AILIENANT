import { useEffect, useRef, useState } from 'react';
import { BudgetLimitMode, OccStatus, TelemetryFrame, TokenSnapshot } from '../../shared/config';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';

const OCC_COLORS: Record<OccStatus, string> = {
    clear:         'var(--accent-primary)',
    soft_conflict: 'var(--accent-warn)',
    hard_conflict: 'var(--accent-alert)',
};

interface OccRingProps { status: OccStatus; lockedFiles: number; }

export function OccRing({ status, lockedFiles }: OccRingProps): JSX.Element {
    const r = 12;
    const circ = 2 * Math.PI * r;
    const fill = status === 'clear' ? circ : status === 'soft_conflict' ? circ * 0.6 : circ * 0.3;
    const label = status === 'clear'
        ? 'Optimistic Concurrency Control (OCC) — no concurrent file locks'
        : `Optimistic Concurrency Control (OCC) — ${status.replace('_', ' ')} · ${lockedFiles} file(s) locked`;

    return (
        <Tooltip content={label}>
            <div className="ws-telemetry-cell">
                <svg width="30" height="30" viewBox="0 0 40 40" aria-hidden>
                    <circle cx="20" cy="20" r={r} fill="none" stroke="var(--border-subtle)" strokeWidth="3" />
                    <circle
                        cx="20" cy="20" r={r} fill="none"
                        stroke={OCC_COLORS[status]} strokeWidth="3"
                        strokeDasharray={`${fill} ${circ}`}
                        strokeLinecap="round"
                        transform="rotate(-90 20 20)"
                        style={{ transition: 'stroke-dasharray 0.4s ease, stroke 0.3s ease' }}
                    />
                </svg>
                <span className="ws-telemetry-label">OCC</span>
            </div>
        </Tooltip>
    );
}

interface SpeedometerProps { tps: number; maxTps?: number; }

/**
 * Automotive-style speedometer: semicircular arc with tick marks and a needle.
 * Needle rotates from -90° (0 tps) to +90° (maxTps). Arc color tracks TPS:
 * green up to 60%, amber 60-85%, red above 85%.
 */
export function Speedometer({ tps, maxTps = 150 }: SpeedometerProps): JSX.Element {
    const cx = 20, cy = 22, r = 14;
    const pct = Math.max(0, Math.min(tps / maxTps, 1));

    // Arc geometry: 180° sweep from (cx - r, cy) to (cx + r, cy)
    const tickPositions = [0, 0.25, 0.5, 0.75, 1];
    const ticks = tickPositions.map(t => {
        const angle = Math.PI - t * Math.PI; // π (left) → 0 (right)
        const x1 = cx + r * Math.cos(angle);
        const y1 = cy - r * Math.sin(angle);
        const x2 = cx + (r + 3) * Math.cos(angle);
        const y2 = cy - (r + 3) * Math.sin(angle);
        return <line key={t} x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--border-subtle)" strokeWidth="1" />;
    });

    // Needle
    const needleAngle = Math.PI - pct * Math.PI;
    const nx = cx + (r - 1) * Math.cos(needleAngle);
    const ny = cy - (r - 1) * Math.sin(needleAngle);

    // Arc color
    const arcColor =
        pct < 0.6 ? 'var(--accent-primary)' :
        pct < 0.85 ? 'var(--accent-warn)' :
        'var(--accent-alert)';

    return (
        <Tooltip content={`Inference throughput · ${tps.toFixed(1)} tokens/sec`}>
            <div className="ws-telemetry-cell">
                <svg width="30" height="22" viewBox="0 0 40 30" aria-hidden>
                    {/* Background arc */}
                    <path
                        d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
                        fill="none" stroke="var(--border-subtle)" strokeWidth="2.5" strokeLinecap="round"
                    />
                    {/* Filled arc proportional to pct */}
                    {pct > 0 && (
                        <path
                            d={`M ${cx - r} ${cy} A ${r} ${r} 0 ${pct > 0.5 ? 1 : 0} 1 ${nx} ${ny}`}
                            fill="none" stroke={arcColor} strokeWidth="2.5" strokeLinecap="round"
                            style={{ transition: 'd 0.3s ease, stroke 0.3s ease' }}
                        />
                    )}
                    {ticks}
                    {/* Needle */}
                    <line
                        x1={cx} y1={cy} x2={nx} y2={ny}
                        stroke="var(--text-primary)" strokeWidth="1.5" strokeLinecap="round"
                        style={{ transition: 'x2 0.3s ease, y2 0.3s ease' }}
                    />
                    <circle cx={cx} cy={cy} r="1.5" fill="var(--text-primary)" />
                </svg>
                <span className="ws-telemetry-label">{tps.toFixed(0)} tok/s</span>
            </div>
        </Tooltip>
    );
}

interface FinOpsBarProps { costUsd: number; budgetUsd: number; }

export function FinOpsBar({ costUsd, budgetUsd }: FinOpsBarProps): JSX.Element {
    const hasBudget = budgetUsd > 0;
    const remaining = hasBudget ? Math.max(0, 1 - costUsd / budgetUsd) : 1;
    const remainingPct = remaining * 100;

    const color =
        !hasBudget ? 'var(--accent-primary)' :
        remaining > 0.25 ? 'var(--accent-primary)' :
        remaining > 0.1 ? 'var(--accent-warn)' :
        'var(--accent-alert)';

    const tip = hasBudget
        ? `$${costUsd.toFixed(3)} spent · $${budgetUsd.toFixed(2)} budget · ${(remaining * 100).toFixed(0)}% remaining`
        : `$${costUsd.toFixed(3)} spent · no budget cap set`;

    return (
        <Tooltip content={tip}>
            <div className="ws-finops-vertical" aria-label="Session budget remaining">
                <div
                    className="ws-finops-vertical-fill"
                    style={{ height: `${remainingPct}%`, background: color }}
                />
            </div>
        </Tooltip>
    );
}

export function useTpsCalculator(): { recordChunk: () => void; tps: number } {
    const [tps, setTps] = useState(0);
    const chunkTimestamps = useRef<number[]>([]);
    const intervalRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

    useEffect(() => {
        intervalRef.current = setInterval(() => {
            const now = Date.now();
            const windowMs = 5000;
            chunkTimestamps.current = chunkTimestamps.current.filter(t => now - t <= windowMs);
            const currentTps = chunkTimestamps.current.length / (windowMs / 1000);
            setTps(currentTps);
        }, 1000);
        return () => { if (intervalRef.current !== undefined) { clearInterval(intervalRef.current); } };
    }, []);

    const recordChunk = (): void => { chunkTimestamps.current.push(Date.now()); };
    return { recordChunk, tps };
}

interface BudgetMenuProps {
    mode:       BudgetLimitMode;
    weeklyUsd:  number;
    monthlyUsd: number;
    onChange:   (mode: BudgetLimitMode, weeklyUsd: number, monthlyUsd: number) => void;
}

function BudgetMenu({ mode, weeklyUsd, monthlyUsd, onChange }: BudgetMenuProps): JSX.Element {
    const [open, setOpen] = useState(false);
    const [localWeekly,  setLocalWeekly]  = useState(weeklyUsd);
    const [localMonthly, setLocalMonthly] = useState(monthlyUsd);
    const wrapRef = useRef<HTMLDivElement>(null);

    const weeklyOn  = mode === 'weekly';
    const monthlyOn = mode === 'monthly';

    useEffect(() => {
        if (!open) { return; }
        const handler = (e: MouseEvent): void => {
            if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    const handleWeeklyToggle = (checked: boolean): void => {
        const next: BudgetLimitMode = checked ? 'weekly' : 'none';
        onChange(next, localWeekly, localMonthly);
    };
    const handleMonthlyToggle = (checked: boolean): void => {
        const next: BudgetLimitMode = checked ? 'monthly' : 'none';
        onChange(next, localWeekly, localMonthly);
    };

    return (
        <div ref={wrapRef} className="ws-finops-budget-wrap">
            <Tooltip content="Configure spend limit">
                <button
                    className="ws-finops-cfg-btn"
                    aria-label="Configure budget"
                    aria-expanded={open}
                    onClick={() => setOpen(v => !v)}
                >
                    <Icon name="wallet" size={11} />
                </button>
            </Tooltip>
            {open && (
                <div className="ws-budget-menu" role="dialog" aria-label="Spend limit">
                    <div className="ws-budget-head">Spend limit</div>

                    <div className="ws-budget-row">
                        <div className="ws-budget-row-info">
                            <span className="ws-budget-label">Weekly</span>
                        </div>
                        <input
                            type="number"
                            className="ws-budget-input"
                            value={localWeekly}
                            min={0}
                            step={1}
                            disabled={monthlyOn}
                            onChange={(e) => setLocalWeekly(Number(e.target.value))}
                            onBlur={() => onChange(mode, localWeekly, localMonthly)}
                        />
                        <label className="ws-switch">
                            <input
                                type="checkbox"
                                checked={weeklyOn}
                                disabled={monthlyOn}
                                onChange={(e) => handleWeeklyToggle(e.target.checked)}
                            />
                            <span className="ws-switch-track" />
                            <span className="ws-switch-thumb" />
                        </label>
                    </div>

                    <div className="ws-budget-row">
                        <div className="ws-budget-row-info">
                            <span className="ws-budget-label">Monthly</span>
                        </div>
                        <input
                            type="number"
                            className="ws-budget-input"
                            value={localMonthly}
                            min={0}
                            step={1}
                            disabled={weeklyOn}
                            onChange={(e) => setLocalMonthly(Number(e.target.value))}
                            onBlur={() => onChange(mode, localWeekly, localMonthly)}
                        />
                        <label className="ws-switch">
                            <input
                                type="checkbox"
                                checked={monthlyOn}
                                disabled={weeklyOn}
                                onChange={(e) => handleMonthlyToggle(e.target.checked)}
                            />
                            <span className="ws-switch-track" />
                            <span className="ws-switch-thumb" />
                        </label>
                    </div>
                </div>
            )}
        </div>
    );
}

interface TelemetryHUDProps {
    occStatus:       OccStatus;
    lockedFiles:     number;
    tps:             number;
    snapshot:        TokenSnapshot | undefined;
    budgetUsd:       number;
    telemetry:       TelemetryFrame | undefined;
    budgetLimitMode:  BudgetLimitMode;
    budgetWeeklyUsd:  number;
    budgetMonthlyUsd: number;
    onBudgetChange:   (mode: BudgetLimitMode, weeklyUsd: number, monthlyUsd: number) => void;
}

/**
 * Right-side sibling card to the PromptBar. 2-column grid:
 *   left column (stacked) — top: OccRing, bottom: Speedometer
 *   right column (full height) — vertical FinOpsBar
 */
export function TelemetryHUD({
    occStatus, lockedFiles, tps, snapshot, budgetUsd,
    budgetLimitMode, budgetWeeklyUsd, budgetMonthlyUsd, onBudgetChange,
}: TelemetryHUDProps): JSX.Element {
    return (
        <div className="ws-telemetry-card ai-card">
            <div className="ws-telemetry-left">
                <OccRing status={occStatus} lockedFiles={lockedFiles} />
                <Speedometer tps={tps} />
            </div>
            <div className="ws-telemetry-right">
                <Tooltip content="Spend cap — account cost ceiling">
                    <span className="ws-finops-icon-static">
                        <Icon name="gauge" size={11} color="var(--text-secondary)" />
                    </span>
                </Tooltip>
                <div className="ws-finops-bar-wrapper">
                    <FinOpsBar
                        costUsd={snapshot?.total_cost_usd ?? 0}
                        budgetUsd={budgetUsd}
                    />
                </div>
                <BudgetMenu
                    mode={budgetLimitMode}
                    weeklyUsd={budgetWeeklyUsd}
                    monthlyUsd={budgetMonthlyUsd}
                    onChange={onBudgetChange}
                />
            </div>
        </div>
    );
}
