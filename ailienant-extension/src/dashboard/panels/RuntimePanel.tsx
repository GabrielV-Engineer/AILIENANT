import { useState, useEffect, useCallback } from 'react';

interface RuntimeStatus {
    tier:              'DOCKER' | 'WASM' | 'NATIVE_HITL' | null;
    docker_reachable:  boolean;
    image_exists:      boolean;
    container_running: boolean;
    mode_label:        string;
}

interface LaunchResult {
    launched: boolean;
    platform: 'windows' | 'macos' | 'linux';
    message:  string;
}

const POLL_INTERVAL_MS = 5_000;
const SEMAPHORE = { green: '#63a583', yellow: '#E3B341', red: '#F85149', gray: '#888888' } as const;

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusRow({ ok, label, pulse }: { ok: boolean; label: string; pulse?: boolean }): JSX.Element {
    const color = ok ? SEMAPHORE.green : SEMAPHORE.red;
    return (
        <div className="db-traffic-light" style={{ marginBottom: 8 }}>
            <div
                className={`db-tl-dot${pulse && ok ? ' byom-status-dot--pulse' : ''}`}
                style={{ background: color }}
            />
            <span className="db-muted" style={{ fontSize: 12 }}>{label}</span>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function RuntimePanel(): JSX.Element {
    const [status, setStatus]     = useState<RuntimeStatus | null>(null);
    const [launching, setLaunching] = useState(false);
    const [launchMsg, setLaunchMsg] = useState<string>('');

    const fetchStatus = useCallback(async (): Promise<void> => {
        try {
            const r = await fetch('/api/v1/runtime/status');
            if (r.ok) { setStatus(await r.json() as RuntimeStatus); }
        } catch { /* backend offline */ }
    }, []);

    // Poll every 5 s; clear on unmount.
    useEffect(() => {
        fetchStatus();
        const id = setInterval(fetchStatus, POLL_INTERVAL_MS);
        return () => clearInterval(id);
    }, [fetchStatus]);

    const handleStartDocker = async (): Promise<void> => {
        setLaunching(true);
        setLaunchMsg('');
        try {
            const r = await fetch('/api/v1/runtime/start-docker', { method: 'POST' });
            if (r.ok) {
                const data = await r.json() as LaunchResult;
                setLaunchMsg(data.message);
            } else {
                setLaunchMsg('Request failed. Please start Docker manually.');
            }
        } catch {
            setLaunchMsg('Could not reach the backend. Is Core running?');
        } finally {
            setLaunching(false);
        }
    };

    // ── derived display values ─────────────────────────────────────────────
    const tierColor: string =
        status?.docker_reachable                  ? SEMAPHORE.green  :
        status?.tier === 'WASM'                   ? SEMAPHORE.yellow :
        status?.tier === 'NATIVE_HITL'            ? SEMAPHORE.red    :
                                                    SEMAPHORE.gray;

    const modeLabel  = status?.mode_label ?? 'Loading…';
    const reachable  = status?.docker_reachable ?? false;
    const hasImage   = status?.image_exists ?? false;
    const isRunning  = status?.container_running ?? false;

    return (
        <div>
            <div className="db-section-title">Runtime / Environment</div>

            {/* ── Sandbox Status card ── */}
            <div className="db-card">
                <div className="db-card-title">Sandbox Status</div>

                {/* Tier badge */}
                <div className="db-traffic-light" style={{ marginBottom: 16 }}>
                    <div className="db-tl-dot" style={{ background: tierColor, width: 16, height: 16 }} />
                    <span style={{ fontWeight: 700, fontSize: 14 }}>{modeLabel}</span>
                </div>

                {/* Three status rows */}
                <StatusRow
                    ok={reachable}
                    label={reachable ? 'Docker Daemon — reachable' : 'Docker Daemon — unreachable'}
                    pulse
                />
                <StatusRow
                    ok={hasImage}
                    label={hasImage ? `Sandbox image present (${status?.tier === 'DOCKER' ? 'ailienant-sandbox:latest' : 'N/A'})` : 'Sandbox image not built'}
                />
                <StatusRow
                    ok={isRunning}
                    label={isRunning ? 'Container running' : 'Container stopped / not started'}
                />

                {status === null && (
                    <div className="db-muted" style={{ marginTop: 8, fontSize: 11 }}>Polling Core…</div>
                )}
            </div>

            {/* ── Lifecycle Controls card ── */}
            <div className="db-card">
                <div className="db-card-title">Lifecycle Controls</div>

                {reachable ? (
                    <div className="db-traffic-light">
                        <div className="db-tl-dot byom-status-dot--pulse" style={{ background: SEMAPHORE.green }} />
                        <span style={{ fontSize: 13 }}>Docker is active — sandbox is available.</span>
                    </div>
                ) : (
                    <>
                        <div className="db-muted" style={{ marginBottom: 12, fontSize: 12 }}>
                            Docker daemon is not reachable. Start Docker Desktop to enable the isolated sandbox.
                        </div>
                        <button
                            className="db-btn db-btn-primary"
                            style={{ marginBottom: 8 }}
                            disabled={launching}
                            onClick={handleStartDocker}
                        >
                            {launching ? 'Launching…' : 'Start Docker'}
                        </button>
                    </>
                )}

                {/* Launch result message — plain text node, never dangerouslySetInnerHTML */}
                {launchMsg !== '' && (
                    <div className="db-muted" style={{ marginTop: 8, fontSize: 12 }}>
                        {launchMsg}
                    </div>
                )}

                {/* Static sandbox spec — plain text node */}
                <div className="db-muted" style={{
                    marginTop: 16, fontSize: 11,
                    borderTop: '1px solid var(--border-subtle)', paddingTop: 10,
                }}>
                    {'Sandbox image: ailienant-sandbox:latest · Lifespan-bound per task · Read-only rootfs, no network'}
                </div>
            </div>
        </div>
    );
}
