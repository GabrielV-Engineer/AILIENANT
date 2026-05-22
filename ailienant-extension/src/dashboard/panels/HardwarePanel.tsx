import { useState, useEffect } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface HardwareProfile {
    ram_gb:           number;
    ram_available_gb: number;
    vram_gb:          number;
    vram_used_gb:     number;
    gpu_name:         string | null;
    cpu_name:         string;
    cpu_cores:        number;
    cpu_freq_mhz:     number;
    is_apple_silicon: boolean;
    suggested_mode:   ExecutionMode;
}

type ExecutionMode       = 'SEQUENTIAL' | 'MICRO_SWARM' | 'FULL_SWARM';
type ExecutionModeChoice = 'AUTO' | ExecutionMode;

const MICRO_SWARM_MIN_GB = 4.0;
const FULL_SWARM_MIN_GB  = 12.0;

const SEMAPHORE_COLORS = { green: '#63a583', yellow: '#E3B341', red: '#F85149' } as const;
type SemaphoreLevel = keyof typeof SEMAPHORE_COLORS;

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

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

function ModeSkeleton(): JSX.Element {
    return (
        <div className="db-row" style={{ gap: 6 }}>
            {[0, 1, 2, 3].map(i => (
                <div key={i} style={{
                    flex: 1, height: 30, borderRadius: 4,
                    background: 'var(--bg-surface)', opacity: 0.3,
                    border: '1px solid var(--border-subtle)',
                }} />
            ))}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function HardwarePanel(): JSX.Element {
    const [profile, setProfile]           = useState<HardwareProfile | null>(null);
    const [profileReady, setProfileReady] = useState(false);
    const [modeChoice, setModeChoice]     = useState<ExecutionModeChoice>('AUTO');
    const [modeSaving, setModeSaving]     = useState(false);

    // Fetch stored mode preference once on mount
    useEffect(() => {
        fetch('/api/v1/hardware/mode')
            .then(r => r.ok ? r.json() : null)
            .then(d => { if (d?.mode) setModeChoice(d.mode as ExecutionModeChoice); })
            .catch(() => { /* backend offline */ });
    }, []);

    // Poll hardware profile every 3 s; clean up on unmount / tab-switch
    useEffect(() => {
        let cancelled = false;
        const poll = async (): Promise<void> => {
            try {
                const r = await fetch('/api/v1/hardware/profile');
                if (!r.ok || cancelled) return;
                const data: HardwareProfile = await r.json();
                setProfile(data);
                setProfileReady(true);
            } catch { /* backend offline */ }
        };
        poll();
        const id = setInterval(poll, 3000);
        return () => { cancelled = true; clearInterval(id); };
    }, []);

    const changeMode = async (m: ExecutionModeChoice): Promise<void> => {
        setModeChoice(m);
        setModeSaving(true);
        try {
            await fetch('/api/v1/hardware/mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: m }),
            });
        } catch { /* no-op */ } finally {
            setModeSaving(false);
        }
    };

    // Derived values
    const isUnified = profile?.is_apple_silicon ?? false;
    const ramUsed   = (profile?.ram_gb ?? 0) - (profile?.ram_available_gb ?? 0);
    const ramTotal  = profile?.ram_gb ?? 0;
    const vramUsed  = profile?.vram_used_gb ?? 0;
    const vramTotal = profile?.vram_gb ?? 0;
    const hasGpu    = !isUnified && vramTotal > 0;

    const effectiveAvailable = profileReady
        ? (isUnified ? (profile?.ram_available_gb ?? 0) : Math.max(0, vramTotal - vramUsed))
        : 0;

    const modeAvailable = (m: ExecutionMode): boolean => {
        if (!profileReady) return false;
        if (m === 'SEQUENTIAL') return true;
        if (m === 'MICRO_SWARM') return effectiveAvailable >= MICRO_SWARM_MIN_GB;
        return effectiveAvailable >= FULL_SWARM_MIN_GB;
    };

    const lockReason = (m: ExecutionMode): string | null => {
        if (!profileReady || modeAvailable(m)) return null;
        const needed = m === 'MICRO_SWARM' ? MICRO_SWARM_MIN_GB : FULL_SWARM_MIN_GB;
        const label  = isUnified ? 'unified RAM' : 'VRAM';
        return `Requires ${needed} GB ${label} available (current: ${effectiveAvailable.toFixed(1)} GB)`;
    };

    // Semaphore maps to suggested_mode
    const semLevel: SemaphoreLevel =
        profile?.suggested_mode === 'FULL_SWARM'  ? 'green'  :
        profile?.suggested_mode === 'MICRO_SWARM' ? 'yellow' : 'red';
    const semLabel =
        semLevel === 'green'  ? 'Sufficient — Full Swarm available' :
        semLevel === 'yellow' ? 'Limited — Micro Swarm available' :
                                'Constrained — Sequential only';

    // Detect if manually chosen mode is now unsupported (hardware degraded)
    const chosenModeUnsupported =
        modeChoice !== 'AUTO' &&
        profileReady &&
        !modeAvailable(modeChoice as ExecutionMode);

    // Resolved auto label
    const autoLabel = profileReady && profile
        ? `Auto (${profile.suggested_mode.replace('_', ' ')})`
        : 'Auto';

    // CPU frequency label
    const freqLabel = profile && profile.cpu_freq_mhz > 0
        ? `${(profile.cpu_freq_mhz / 1000).toFixed(2)} GHz`
        : null;

    return (
        <div>
            <div className="db-section-title">Hardware Monitor</div>

            <div className="db-grid-2" style={{ marginBottom: 0 }}>
                {/* ── CPU Card ── */}
                <div className="db-card">
                    <div className="db-card-title">Processor</div>
                    {profileReady && profile ? (
                        <>
                            <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6, lineHeight: 1.4 }}>
                                {profile.cpu_name || 'Unknown CPU'}
                            </div>
                            <div className="db-muted" style={{ fontSize: 12 }}>
                                {profile.cpu_cores > 0 ? `${profile.cpu_cores} physical cores` : ''}
                                {profile.cpu_cores > 0 && freqLabel ? ' · ' : ''}
                                {freqLabel ?? ''}
                            </div>
                            {isUnified && (
                                <div className="db-muted" style={{ fontSize: 11, marginTop: 6 }}>
                                    Unified Memory — RAM is shared with GPU
                                </div>
                            )}
                        </>
                    ) : (
                        <div className="db-muted">Detecting…</div>
                    )}
                </div>

                {/* ── Memory Card ── */}
                <div className="db-card">
                    <div className="db-card-title">Memory</div>
                    <GaugeBar
                        used={ramUsed}
                        total={ramTotal}
                        label={isUnified ? 'Unified Memory' : 'RAM'}
                    />
                    {hasGpu && (
                        <>
                            <GaugeBar used={vramUsed} total={vramTotal} label="VRAM" />
                            {profile?.gpu_name && (
                                <div className="db-muted" style={{ fontSize: 11, marginTop: 2 }}>
                                    {profile.gpu_name}
                                </div>
                            )}
                        </>
                    )}
                    {!hasGpu && !isUnified && profileReady && (
                        <div className="db-muted" style={{ fontSize: 11, marginTop: 4 }}>
                            No dedicated GPU — local inference via CPU
                        </div>
                    )}
                </div>
            </div>

            {/* ── Execution Mode Card (full-width) ── */}
            <div className="db-card">
                <div className="db-card-title">Execution Mode</div>

                {/* Semaphore */}
                <div className="db-traffic-light" style={{ marginBottom: 14 }}>
                    <div
                        className="db-tl-dot"
                        style={{ background: SEMAPHORE_COLORS[semLevel] }}
                    />
                    <span>{semLabel}</span>
                </div>

                {/* Hardware degradation warning */}
                {chosenModeUnsupported && (
                    <div style={{
                        fontSize: 11, color: '#F85149', marginBottom: 10,
                        padding: '6px 10px', background: 'rgba(248,81,73,0.08)',
                        borderRadius: 4, border: '1px solid rgba(248,81,73,0.25)',
                    }}>
                        Hardware no longer meets {modeChoice.replace('_', ' ')} requirements.
                        Falling back to AUTO.
                    </div>
                )}

                {/* Mode buttons */}
                {profileReady ? (
                    <div className="db-row" style={{ gap: 6 }}>
                        {/* Auto */}
                        <button
                            className={`db-btn ${modeChoice === 'AUTO' ? 'db-btn-primary' : 'db-btn-secondary'}`}
                            style={{ flex: 1, fontSize: 11 }}
                            disabled={modeSaving}
                            onClick={() => changeMode('AUTO')}
                        >
                            {autoLabel}
                        </button>

                        {/* Manual modes */}
                        {(['SEQUENTIAL', 'MICRO_SWARM', 'FULL_SWARM'] as ExecutionMode[]).map(m => {
                            const locked  = !modeAvailable(m);
                            const reason  = lockReason(m);
                            const isActive = modeChoice === m && !chosenModeUnsupported;
                            return (
                                <button
                                    key={m}
                                    className={`db-btn ${isActive ? 'db-btn-primary' : 'db-btn-secondary'}`}
                                    style={{
                                        flex: 1, fontSize: 11,
                                        ...(locked ? { opacity: 0.45, cursor: 'not-allowed' } : {}),
                                    }}
                                    disabled={locked || modeSaving}
                                    title={reason ?? undefined}
                                    onClick={() => !locked && changeMode(m)}
                                >
                                    {m.replace('_', ' ')}
                                </button>
                            );
                        })}
                    </div>
                ) : (
                    <ModeSkeleton />
                )}
            </div>
        </div>
    );
}
