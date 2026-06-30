import { useState, useEffect, useCallback, useRef } from 'react';
import { usePollingWhileVisible } from '../hooks/usePollingWhileVisible';

interface RuntimeStatus {
    tier:              'DOCKER' | 'WASM' | 'NATIVE_HITL' | null;
    docker_reachable:  boolean;
    image_exists:      boolean;
    container_running: boolean;
    mode_label:        string;
}

interface LaunchResult { launched: boolean; platform: string; message: string; }
interface PullResult { pulled: boolean; image?: string; error?: string; message: string; }

const POLL_INTERVAL_MS = 5_000;
const LAUNCH_TIMEOUT_MS = 30_000;
const REMOTE_IMAGE = 'ghcr.io/gabrielv-engineer/ailienant-sandbox:latest';
const SEMAPHORE = { green: '#63a583', yellow: '#E3B341', red: '#F85149', gray: '#888888' } as const;
type Tone = 'ok' | 'warn' | 'bad';

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusRow({ tone, label, pulse }: { tone: Tone; label: string; pulse?: boolean }): JSX.Element {
    const color = tone === 'ok' ? SEMAPHORE.green : tone === 'warn' ? SEMAPHORE.yellow : SEMAPHORE.red;
    return (
        <div className="db-traffic-light" style={{ marginBottom: 8 }}>
            <div
                className={`db-tl-dot${pulse && tone === 'ok' ? ' byom-status-dot--pulse' : ''}`}
                style={{ background: color }}
            />
            <span className="db-muted" style={{ fontSize: 12 }}>{label}</span>
        </div>
    );
}

function CliSnippet({ cmd }: { cmd: string }): JSX.Element {
    return (
        <div style={{
            fontFamily: 'var(--font-mono, monospace)', fontSize: 11, background: 'var(--bg-hover)',
            borderRadius: 4, padding: '6px 8px', marginTop: 6, userSelect: 'all', wordBreak: 'break-all',
        }}>{cmd}</div>
    );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function RuntimePanel(): JSX.Element {
    const [status, setStatus]         = useState<RuntimeStatus | null>(null);
    const [launching, setLaunching]   = useState(false);   // waiting for daemon after a launch
    const [launchMsg, setLaunchMsg]   = useState('');
    const [downloading, setDownloading] = useState(false);
    const [downloadMsg, setDownloadMsg] = useState('');
    const [showFallback, setShowFallback] = useState(false);
    const launchDeadlineRef = useRef<number>(0);

    const fetchStatus = useCallback(async (force = false): Promise<void> => {
        try {
            const r = await fetch(force ? '/api/v1/runtime/status?force=true' : '/api/v1/runtime/status');
            if (r.ok) { setStatus(await r.json() as RuntimeStatus); }
        } catch { /* backend offline */ }
    }, []);

    // Poll runtime status while the dashboard is visible; a hidden window
    // pauses polling and resumes on return.
    usePollingWhileVisible(() => { void fetchStatus(); }, POLL_INTERVAL_MS);

    // Escape hatch: clear "launching" once daemon is up OR the 30 s deadline passes.
    useEffect(() => {
        if (!launching) { return; }
        if (status?.docker_reachable) {
            setLaunching(false);
            setLaunchMsg('Docker is now reachable.');
            return;
        }
        const id = setInterval(() => {
            if (Date.now() > launchDeadlineRef.current) {
                setLaunching(false);
                setLaunchMsg('Docker did not become reachable in time. Use Force Retry or start it manually.');
            }
        }, 1_000);
        return () => clearInterval(id);
    }, [launching, status?.docker_reachable]);

    const handleStartDocker = async (): Promise<void> => {
        setLaunchMsg('');
        try {
            const r = await fetch('/api/v1/runtime/start-docker', { method: 'POST' });
            const data = r.ok ? (await r.json() as LaunchResult) : null;
            if (data?.launched) {
                setLaunching(true);
                launchDeadlineRef.current = Date.now() + LAUNCH_TIMEOUT_MS;
                setLaunchMsg(data.message);
            } else {
                setLaunchMsg(data?.message ?? 'Request failed. Please start Docker manually.');
            }
        } catch {
            setLaunchMsg('Could not reach the backend. Is Core running?');
        }
    };

    const handleForceRetry = async (): Promise<void> => {
        setLaunching(false);
        setLaunchMsg('');
        launchDeadlineRef.current = 0;
        await fetchStatus(true);
    };

    const handleDownloadImage = async (): Promise<void> => {
        setDownloading(true);
        setDownloadMsg('Downloading… this may take a few minutes depending on your connection.');
        try {
            const r = await fetch('/api/v1/runtime/pull-image', { method: 'POST' });
            const data = r.ok ? (await r.json() as PullResult) : null;
            setDownloadMsg(data?.message ?? 'Download failed. See the manual fallback below.');
            await fetchStatus(true);
        } catch {
            setDownloadMsg('Could not reach the backend. Is Core running?');
        } finally {
            setDownloading(false);
        }
    };

    // ── derived display values ─────────────────────────────────────────────
    const tierColor: string =
        status?.docker_reachable       ? SEMAPHORE.green  :
        status?.tier === 'WASM'        ? SEMAPHORE.yellow :
        status?.tier === 'NATIVE_HITL' ? SEMAPHORE.red    :
                                         SEMAPHORE.gray;

    const modeLabel = status?.mode_label ?? 'Loading…';
    const reachable = status?.docker_reachable ?? false;
    const hasImage  = status?.image_exists ?? false;
    const isRunning = status?.container_running ?? false;
    const imageTone: Tone = hasImage ? 'ok' : reachable ? 'warn' : 'bad';

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
                    tone={reachable ? 'ok' : 'bad'}
                    pulse
                    label={reachable ? 'Docker Daemon — reachable' : 'Docker Daemon — unreachable / degraded'}
                />
                <StatusRow
                    tone={imageTone}
                    label={hasImage
                        ? 'Sandbox image present (ailienant-sandbox:latest)'
                        : reachable ? 'Sandbox image not downloaded' : 'Sandbox image — unknown (daemon down)'}
                />
                <StatusRow
                    tone={isRunning ? 'ok' : 'bad'}
                    label={isRunning ? 'Container running' : 'Container stopped / not started'}
                />

                {status === null && (
                    <div className="db-muted" style={{ marginTop: 8, fontSize: 11 }}>Polling Core…</div>
                )}
            </div>

            {/* ── Project Devcontainer card (trusted tier) ── */}
            <div className="db-card">
                <div className="db-card-title">Project Devcontainer (trusted tier)</div>
                <div className="db-traffic-light" style={{ marginBottom: 12 }}>
                    <div className="db-tl-dot" style={{ background: SEMAPHORE.gray, width: 16, height: 16 }} />
                    <span style={{ fontWeight: 700, fontSize: 14 }}>Not connected</span>
                </div>
                <div className="db-muted" style={{ fontSize: 12 }}>
                    {"Trusted project commands (your own tests, run_command) run in your repository's "}
                    {"devcontainer.json environment — separate from the locked benchmark sandbox above. "}
                    {"Live provisioning status appears here once the host execution bridge is connected."}
                </div>
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
                            {launching ? 'Launching… polling for status' : 'Start Docker'}
                        </button>
                    </>
                )}

                {/* Escape hatch — always available so the user is never trapped. */}
                <button
                    className="db-btn db-btn-secondary"
                    style={{ marginLeft: reachable ? 0 : 8, marginTop: reachable ? 12 : 0 }}
                    onClick={handleForceRetry}
                >
                    Force Retry / Re-check
                </button>

                {/* Launch result message — plain text node, never dangerouslySetInnerHTML */}
                {launchMsg !== '' && (
                    <div className="db-muted" style={{ marginTop: 8, fontSize: 12 }}>{launchMsg}</div>
                )}

                {/* Image download block — only when daemon is up but image missing. */}
                {reachable && !hasImage && (
                    <div style={{ marginTop: 16, borderTop: '1px solid var(--border-subtle)', paddingTop: 12 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Sandbox image not installed</div>
                        <div className="db-muted" style={{ fontSize: 12, marginBottom: 10 }}>
                            Download the pre-built isolated sandbox environment to enable Docker-tier execution.
                        </div>
                        <button className="db-btn db-btn-primary" disabled={downloading} onClick={handleDownloadImage}>
                            {downloading ? 'Downloading…' : 'Download Sandbox Environment'}
                        </button>
                        {downloadMsg !== '' && (
                            <div className="db-muted" style={{ marginTop: 8, fontSize: 12 }}>{downloadMsg}</div>
                        )}
                        <button
                            className="db-btn db-btn-secondary"
                            style={{ marginTop: 10 }}
                            onClick={() => setShowFallback(v => !v)}
                        >
                            {showFallback ? 'Hide manual install' : 'Manual install (advanced / offline)'}
                        </button>
                        {showFallback && (
                            <div style={{ marginTop: 8 }}>
                                <div className="db-muted" style={{ fontSize: 11 }}>Run from a terminal with Docker access:</div>
                                <CliSnippet cmd={`docker pull ${REMOTE_IMAGE}`} />
                            </div>
                        )}
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
