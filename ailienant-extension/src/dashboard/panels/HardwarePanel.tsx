import { useState, useEffect, useRef } from 'react';
import { usePollingWhileVisible } from '../hooks/usePollingWhileVisible';
import { useRingBuffer } from '../hooks/useRingBuffer';
import { Badge, RadialGauge, Skeleton, Sparkline } from '../ui';
import { Icon } from '../../shared/Icon';
import { formatAgo } from '../format';

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

interface Band { warn: number; crit: number; }
interface Thresholds { ram: Band; vram: Band; }

const MICRO_SWARM_MIN_GB = 4.0;
const FULL_SWARM_MIN_GB  = 12.0;
const PROFILE_POLL_MS    = 3_000;
const VRAM_HISTORY_LEN   = 20; // ~60 s at the 3 s poll cadence

const SEMAPHORE_COLORS = { green: '#63a583', yellow: '#E3B341', red: '#F85149' } as const;
type SemaphoreLevel = keyof typeof SEMAPHORE_COLORS;

const THRESH_KEY = 'ailienant.dashboard.hwThresholds';
const DEFAULT_THRESH: Thresholds = { ram: { warn: 70, crit: 90 }, vram: { warn: 70, crit: 90 } };

function clampPct(n: number): number {
    if (!Number.isFinite(n)) { return 0; }
    return Math.max(0, Math.min(100, Math.round(n)));
}

function loadThresholds(): Thresholds {
    try {
        const raw = localStorage.getItem(THRESH_KEY);
        if (!raw) { return DEFAULT_THRESH; }
        const parsed = JSON.parse(raw) as Partial<Thresholds>;
        return {
            ram:  { ...DEFAULT_THRESH.ram,  ...parsed.ram },
            vram: { ...DEFAULT_THRESH.vram, ...parsed.vram },
        };
    } catch { return DEFAULT_THRESH; }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function LiveTag({ at }: { at: number | null }): JSX.Element | null {
    if (at === null) { return null; }
    return <span className="db-live"><span className="db-live-dot" />updated {formatAgo(at)}</span>;
}

function ThresholdMenu({ thresholds, onChange }: { thresholds: Thresholds; onChange: (t: Thresholds) => void }): JSX.Element {
    const [open, setOpen] = useState(false);
    const wrapRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (!open) { return; }
        const handler = (e: MouseEvent): void => {
            if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) { setOpen(false); }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    const setBand = (metric: keyof Thresholds, key: keyof Band, val: number): void => {
        onChange({ ...thresholds, [metric]: { ...thresholds[metric], [key]: clampPct(val) } });
    };

    return (
        <div className="db-threshold-wrap" ref={wrapRef}>
            <button
                className="db-btn db-btn-secondary"
                style={{ padding: '2px 8px', fontSize: 11 }}
                onClick={() => setOpen(v => !v)}
                aria-expanded={open}
                aria-label="Configure alarm thresholds"
            >
                <Icon name="settings" size={12} /> Thresholds
            </button>
            {open && (
                <div className="db-threshold-menu" role="dialog" aria-label="Alarm thresholds">
                    <div className="db-threshold-head">Alarm thresholds (%)</div>
                    {(['ram', 'vram'] as (keyof Thresholds)[]).map(metric => (
                        <div key={metric} style={{ marginBottom: 8 }}>
                            <div className="db-label" style={{ marginBottom: 4, textTransform: 'uppercase' }}>{metric}</div>
                            <div className="db-threshold-row">
                                <span className="db-muted">Warn</span>
                                <input
                                    className="db-input db-threshold-input" type="number" min={0} max={100}
                                    value={thresholds[metric].warn}
                                    onChange={e => setBand(metric, 'warn', Number(e.target.value))}
                                />
                            </div>
                            <div className="db-threshold-row">
                                <span className="db-muted">Critical</span>
                                <input
                                    className="db-input db-threshold-input" type="number" min={0} max={100}
                                    value={thresholds[metric].crit}
                                    onChange={e => setBand(metric, 'crit', Number(e.target.value))}
                                />
                            </div>
                        </div>
                    ))}
                </div>
            )}
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
    const [updatedAt, setUpdatedAt]       = useState<number | null>(null);
    const [modeChoice, setModeChoice]     = useState<ExecutionModeChoice>('AUTO');
    const [modeSaving, setModeSaving]     = useState(false);
    const [thresholds, setThresholds]     = useState<Thresholds>(() => loadThresholds());
    const vramHistory = useRingBuffer<number>(VRAM_HISTORY_LEN);

    const saveThresholds = (t: Thresholds): void => {
        setThresholds(t);
        try { localStorage.setItem(THRESH_KEY, JSON.stringify(t)); } catch { /* storage unavailable */ }
    };

    // Fetch stored mode preference once on mount
    useEffect(() => {
        fetch('/api/v1/hardware/mode')
            .then(r => r.ok ? r.json() : null)
            .then(d => { if (d?.mode) setModeChoice(d.mode as ExecutionModeChoice); })
            .catch(() => { /* backend offline */ });
    }, []);

    // Poll the hardware profile while the dashboard is visible; a hidden window
    // pauses polling and resumes on return. Each reading feeds the VRAM timeline.
    usePollingWhileVisible(() => {
        void (async (): Promise<void> => {
            try {
                const r = await fetch('/api/v1/hardware/profile');
                if (!r.ok) return;
                const data: HardwareProfile = await r.json();
                const now = Date.now();
                setProfile(data);
                setProfileReady(true);
                setUpdatedAt(now);
                if (!data.is_apple_silicon && data.vram_gb > 0) {
                    vramHistory.push(data.vram_used_gb, now);
                }
            } catch { /* backend offline */ }
        })();
    }, PROFILE_POLL_MS);

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
            <div className="db-row" style={{ gap: 10, alignItems: 'center' }}>
                <div className="db-section-title" style={{ marginBottom: 0 }}>Hardware Monitor</div>
                <Badge status="neutral" icon="cpu" >Machine-global — not project-scoped</Badge>
            </div>

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
                    <div className="db-row" style={{ justifyContent: 'space-between' }}>
                        <div className="db-card-title">Memory</div>
                        <div className="db-row" style={{ gap: 8, alignItems: 'center' }}>
                            <LiveTag at={updatedAt} />
                            <ThresholdMenu thresholds={thresholds} onChange={saveThresholds} />
                        </div>
                    </div>

                    {!profileReady ? (
                        <Skeleton height={120} />
                    ) : (
                        <div className="db-row" style={{ gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                            <div style={{ flex: 1, minWidth: 150 }}>
                                <RadialGauge
                                    value={ramUsed} max={ramTotal} label={isUnified ? 'Unified' : 'RAM'} unit="GB"
                                    warnPct={thresholds.ram.warn / 100} critPct={thresholds.ram.crit / 100}
                                />
                                <div className="db-gauge-caption">{ramUsed.toFixed(1)} / {ramTotal.toFixed(1)} GB</div>
                            </div>
                            {hasGpu && (
                                <div style={{ flex: 1, minWidth: 150 }}>
                                    <RadialGauge
                                        value={vramUsed} max={vramTotal} label="VRAM" unit="GB"
                                        warnPct={thresholds.vram.warn / 100} critPct={thresholds.vram.crit / 100}
                                    />
                                    <div className="db-gauge-caption">{vramUsed.toFixed(1)} / {vramTotal.toFixed(1)} GB</div>
                                </div>
                            )}
                        </div>
                    )}

                    {hasGpu && (
                        <div style={{ marginTop: 12 }}>
                            <div className="db-label">VRAM — last 60s</div>
                            <Sparkline
                                samples={vramHistory.samples}
                                domainMin={0}
                                domainMax={vramTotal > 0 ? vramTotal : undefined}
                                ariaLabel={`VRAM usage timeline, currently ${vramUsed.toFixed(1)} of ${vramTotal.toFixed(1)} gigabytes`}
                                warmingHint="Warming up — VRAM timeline builds over ~1 minute…"
                            />
                        </div>
                    )}

                    {hasGpu && profile?.gpu_name && (
                        <div className="db-muted" style={{ fontSize: 11, marginTop: 6 }}>{profile.gpu_name}</div>
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
