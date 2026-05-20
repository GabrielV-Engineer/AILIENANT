import { useState, useEffect } from 'react';

interface HardwareMetrics {
    ram_used_gb:   number;
    ram_total_gb:  number;
    vram_used_gb:  number;
    vram_total_gb: number;
}

type SemaphoreLevel = 'green' | 'yellow' | 'red';
type ExecutionMode  = 'SEQUENTIAL' | 'MICRO_SWARM' | 'FULL_SWARM';

const SEMAPHORE_COLORS = { green: '#63a583', yellow: '#E3B341', red: '#F85149' };
const SEMAPHORE_LABELS = {
    green:  'Sufficient VRAM',
    yellow: 'Paging risk — degraded latency',
    red:    'OOM risk — blocking local, suggesting Cloud',
};

function getSemaphoreLevel(vramUsedGb: number, vramTotalGb: number): SemaphoreLevel {
    if (vramTotalGb === 0) { return 'red'; }
    const pct = vramUsedGb / vramTotalGb;
    if (pct < 0.7)  { return 'green'; }
    if (pct < 0.9)  { return 'yellow'; }
    return 'red';
}

function GaugeBar({ used, total, label }: { used: number; total: number; label: string }): JSX.Element {
    const pct  = total > 0 ? Math.min(used / total, 1) : 0;
    const warn = pct > 0.7;
    const crit = pct > 0.9;
    return (
        <div style={{ marginBottom: 10 }}>
            <div className="db-row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
                <span className="db-label" style={{ marginBottom: 0 }}>{label}</span>
                <span className="db-muted">{used.toFixed(1)} / {total.toFixed(1)} GB</span>
            </div>
            <div className="db-gauge-track">
                <div
                    className="db-gauge-fill"
                    data-warn={warn && !crit ? 'true' : 'false'}
                    data-crit={crit ? 'true' : 'false'}
                    style={{ width: `${pct * 100}%` }}
                />
            </div>
        </div>
    );
}

export function HardwarePanel(): JSX.Element {
    const [metrics, setMetrics] = useState<HardwareMetrics>({
        ram_used_gb: 0, ram_total_gb: 0, vram_used_gb: 0, vram_total_gb: 0,
    });
    const [mode, setMode] = useState<ExecutionMode>('SEQUENTIAL');

    // Poll backend every 3s
    useEffect(() => {
        const poll = async (): Promise<void> => {
            try {
                const r = await fetch('/api/v1/telemetry/tokens');
                if (!r.ok) { return; }
                // Token snapshot doesn't have hardware — in production wire hardware_profiler endpoint
                // For now show placeholder
            } catch { /* backend not connected in dev */ }
        };
        poll();
        const t = setInterval(poll, 3000);
        return () => clearInterval(t);
    }, []);

    const semaphore = getSemaphoreLevel(metrics.vram_used_gb, metrics.vram_total_gb);

    const changeMode = async (m: ExecutionMode): Promise<void> => {
        setMode(m);
        try {
            await fetch('/api/v1/system/execution_mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: m }),
            });
        } catch { /* no-op in dev */ }
    };

    return (
        <div>
            <div className="db-section-title">Hardware Monitor</div>

            <div className="db-grid-2">
                {/* Hardware gauges */}
                <div className="db-card">
                    <div className="db-card-title">Memory Usage</div>
                    <GaugeBar used={metrics.ram_used_gb}  total={metrics.ram_total_gb}  label="RAM" />
                    <GaugeBar used={metrics.vram_used_gb} total={metrics.vram_total_gb} label="VRAM" />
                </div>

                {/* Semaphore */}
                <div className="db-card">
                    <div className="db-card-title">Hardware Semaphore</div>
                    <div className="db-traffic-light" style={{ marginBottom: 12 }}>
                        <div
                            className="db-tl-dot"
                            style={{ background: SEMAPHORE_COLORS[semaphore] }}
                        />
                        <span>{SEMAPHORE_LABELS[semaphore]}</span>
                    </div>
                    <div className="db-label">Execution Mode</div>
                    <div className="db-row" style={{ gap: 6 }}>
                        {(['SEQUENTIAL', 'MICRO_SWARM', 'FULL_SWARM'] as ExecutionMode[]).map(m => (
                            <button
                                key={m}
                                className={`db-btn ${mode === m ? 'db-btn-primary' : 'db-btn-secondary'}`}
                                onClick={() => changeMode(m)}
                                style={{ flex: 1, fontSize: 11 }}
                            >
                                {m.replace('_', ' ')}
                            </button>
                        ))}
                    </div>
                </div>
            </div>
        </div>
    );
}
