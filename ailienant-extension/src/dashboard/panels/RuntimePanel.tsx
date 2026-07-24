import { useState, useEffect, useCallback, useRef } from 'react';
import { usePollingWhileVisible } from '../hooks/usePollingWhileVisible';
import { Badge, EmptyState } from '../ui';
import { Icon } from '../../shared/Icon';
import { formatAgo } from '../format';

interface RuntimeStatus {
    tier:              'DOCKER' | 'WASM' | 'NATIVE_HITL' | null;
    docker_reachable:  boolean;
    image_exists:      boolean;
    container_running: boolean;
    mode_label:        string;
}

interface LifecycleEvent {
    id:           number;
    timestamp:    string;
    event:        string;
    container_id: string;
    image:        string;
    tier:         string;
}

interface Span { id: string; startMs: number; endMs: number | null; tier: string; }

interface ExecEntry {
    seq:         number;
    ts:          number;
    session_id:  string;
    source:      string;
    command:     string;
    exit_code:   number;
    output:      string;
    duration_ms: number;
}

const EXEC_LOG_KEEP = 100;

function fmtDuration(ms: number): string {
    const s = Math.round(ms / 1000);
    if (s < 60) { return `${s}s`; }
    return `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** Command durations are usually sub-second, so keep ms resolution below 1 s. */
function fmtExecMs(ms: number): string {
    return ms < 1000 ? `${Math.round(ms)}ms` : fmtDuration(ms);
}

/** Pair started→stopped/removed events (by container id) into timeline spans. */
function buildSpans(events: LifecycleEvent[]): Span[] {
    const map = new Map<string, Span>();
    // events arrive newest-first; walk oldest-first so a start is seen before its stop
    for (const e of [...events].reverse()) {
        const ms = Date.parse(e.timestamp.replace(' ', 'T') + 'Z');
        if (Number.isNaN(ms)) { continue; }
        const key = e.container_id || String(e.id);
        let span = map.get(key);
        if (!span) { span = { id: key, startMs: ms, endMs: null, tier: e.tier }; map.set(key, span); }
        if (e.event === 'started') { span.startMs = ms; } else { span.endMs = ms; }
    }
    return [...map.values()].sort((a, b) => b.startMs - a.startMs).slice(0, 12);
}

function LifecycleTimeline({ events }: { events: LifecycleEvent[] }): JSX.Element {
    const spans = buildSpans(events);
    if (spans.length === 0) {
        return <EmptyState icon="clock" title="No container activity yet" hint="Sandbox start/stop events appear here." />;
    }
    const now = Date.now();
    const minStart = Math.min(...spans.map(s => s.startMs));
    const maxEnd = Math.max(now, ...spans.map(s => s.endMs ?? now));
    const total = Math.max(1, maxEnd - minStart);
    return (
        <div className="db-lifecycle">
            {spans.map(s => {
                const end = s.endMs ?? now;
                const open = s.endMs === null;
                const left = ((s.startMs - minStart) / total) * 100;
                const width = Math.max(2, ((end - s.startMs) / total) * 100);
                const durLabel = open ? 'running' : fmtDuration(end - s.startMs);
                return (
                    <div key={s.id} className="db-lifecycle-row">
                        <div className="db-lifecycle-meta">
                            <span className="db-lifecycle-cid">{s.id.slice(0, 12)}</span>
                            <span className="db-muted">{new Date(s.startMs).toLocaleTimeString()} · {durLabel}</span>
                        </div>
                        <div className="db-lifecycle-track">
                            <div
                                className={`db-lifecycle-bar${open ? ' db-lifecycle-bar--active' : ''}`}
                                style={{ left: `${left}%`, width: `${width}%` }}
                                title={`${s.tier} · ${durLabel}`}
                            />
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

/** Recent sandbox command executions — newest-first, scrollable, output collapsed per row. */
function ExecLogCard({ entries }: { entries: ExecEntry[] }): JSX.Element {
    if (entries.length === 0) {
        return <EmptyState icon="terminal" title="No commands run yet" hint="Sandbox command executions appear here as the agent works." />;
    }
    return (
        <div className="db-execlog">
            {entries.map(e => {
                const ok = e.exit_code === 0;
                return (
                    <div key={e.seq} className="db-execlog-row">
                        <div className="db-execlog-head">
                            <Badge status={ok ? 'good' : 'critical'} icon={ok ? 'check' : 'x'}>exit {e.exit_code}</Badge>
                            <span className="db-execlog-source" title="Which subsystem ran this command">{e.source}</span>
                            <code className="db-execlog-cmd" title={e.command}>{e.command}</code>
                            <span className="db-muted db-execlog-when">{formatAgo(e.ts)} · {fmtExecMs(e.duration_ms)}</span>
                        </div>
                        {e.output !== '' && <pre className="db-execlog-out">{e.output}</pre>}
                    </div>
                );
            })}
        </div>
    );
}

type TierId = 'DOCKER' | 'WASM' | 'NATIVE_HITL';
type TierState = 'active' | 'fallback' | 'blocked';

const TIERS: { id: TierId; name: string; blurb: string }[] = [
    { id: 'DOCKER',      name: 'Docker sandbox',      blurb: 'Isolated container — read-only rootfs, no network' },
    { id: 'WASM',        name: 'WebAssembly sandbox', blurb: 'In-process Wasm isolation' },
    { id: 'NATIVE_HITL', name: 'Native + HITL',       blurb: 'Host execution gated by per-command approval' },
];

/**
 * Resolve the adapter fallback ladder from the current status flags. The tier
 * the backend selected is `active`; higher-priority tiers it skipped are
 * `blocked` (with the reason we fell through), lower-priority tiers are the
 * remaining `fallback` options. Needs no new backend — it reads the flags the
 * status endpoint already returns.
 */
function tierLadder(status: RuntimeStatus | null): { id: TierId; name: string; reason: string; state: TierState }[] {
    const active: TierId | null = status?.docker_reachable ? 'DOCKER' : status?.tier ?? null;
    const activeIdx = TIERS.findIndex(t => t.id === active);

    const dockerReason = (): string => {
        if (!status) { return 'Awaiting status…'; }
        if (!status.docker_reachable) { return 'Daemon unreachable — falling through'; }
        if (!status.image_exists) { return 'Reachable, but sandbox image not installed'; }
        if (!status.container_running) { return 'Reachable, image present, container idle'; }
        return 'Daemon reachable, image present, container running';
    };

    return TIERS.map((t, i) => {
        const state: TierState = i === activeIdx ? 'active' : activeIdx === -1 || i < activeIdx ? 'blocked' : 'fallback';
        const reason = t.id === 'DOCKER' ? dockerReason() : t.blurb;
        return { id: t.id, name: t.name, reason, state };
    });
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
    const [events, setEvents] = useState<LifecycleEvent[] | null>(null);
    const [execEntries, setExecEntries] = useState<ExecEntry[] | null>(null);
    const launchDeadlineRef = useRef<number>(0);
    const latestSeqRef = useRef<number>(0);

    const fetchStatus = useCallback(async (force = false): Promise<void> => {
        try {
            const r = await fetch(force ? '/api/v1/runtime/status?force=true' : '/api/v1/runtime/status');
            if (r.ok) { setStatus(await r.json() as RuntimeStatus); }
        } catch { /* backend offline */ }
    }, []);

    const fetchLifecycle = useCallback(async (): Promise<void> => {
        try {
            const r = await fetch('/api/v1/runtime/lifecycle?limit=100');
            if (r.ok) { setEvents((await r.json() as { events: LifecycleEvent[] }).events ?? []); }
        } catch { /* backend offline */ }
    }, []);

    // Cursor-paged exec log: send our last-seen seq as `since` so an idle poll
    // returns nothing. New entries arrive chronological; prepend them (reversed)
    // to the newest-first list and trim to a bounded window.
    const fetchExecLog = useCallback(async (): Promise<void> => {
        try {
            const since = latestSeqRef.current;
            const url = since > 0
                ? `/api/v1/runtime/exec-log?tail=50&since=${since}`
                : '/api/v1/runtime/exec-log?tail=50';
            const r = await fetch(url);
            if (!r.ok) { return; }
            const data = await r.json() as { entries: ExecEntry[]; latest_seq: number };
            latestSeqRef.current = data.latest_seq ?? since;
            if (data.entries.length === 0) { setExecEntries(prev => prev ?? []); return; }
            const incoming = [...data.entries].reverse(); // chronological → newest-first
            setExecEntries(prev => [...incoming, ...(prev ?? [])].slice(0, EXEC_LOG_KEEP));
        } catch { /* backend offline */ }
    }, []);

    // Poll runtime status + lifecycle + exec log while the dashboard is visible;
    // a hidden window pauses polling and resumes on return.
    usePollingWhileVisible(() => {
        void fetchStatus();
        void fetchLifecycle();
        void fetchExecLog();
    }, POLL_INTERVAL_MS);

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
    const ladder = tierLadder(status);

    return (
        <div>
            <div className="db-row" style={{ gap: 10, alignItems: 'center' }}>
                <div className="db-section-title" style={{ marginBottom: 0 }}>Runtime / Environment</div>
                <Badge status="neutral" icon="cpu">Machine-global — not project-scoped</Badge>
            </div>

            {/* ── Sandbox Status card ── */}
            <div className="db-card">
                <div className="db-card-title">Sandbox Status</div>

                {/* Tier badge */}
                <div className="db-traffic-light" style={{ marginBottom: 16 }}>
                    <div className="db-tl-dot" style={{ background: tierColor, width: 16, height: 16 }} />
                    <span style={{ fontWeight: 700, fontSize: 14 }}>{modeLabel}</span>
                </div>

                {/* Adapter fallback ladder — which execution tier is active and why */}
                <div className="db-label" style={{ marginBottom: 6 }}>Execution tier resolution</div>
                <div className="db-tier-ladder" style={{ marginBottom: 16 }}>
                    {ladder.map((s, i) => (
                        <div key={s.id} className="db-tier-step" data-state={s.state}>
                            <div className="db-tier-rail">
                                <div className="db-tier-node" />
                                {i < ladder.length - 1 && <div className="db-tier-connector" />}
                            </div>
                            <div className="db-tier-body">
                                <div className="db-tier-name">
                                    {s.name}
                                    {s.state === 'active' && <span className="db-tier-pill">Active</span>}
                                </div>
                                <div className="db-tier-reason">{s.reason}</div>
                            </div>
                        </div>
                    ))}
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

            {/* ── Container lifecycle timeline ── */}
            <div className="db-card">
                <div className="db-card-title">Container lifecycle</div>
                {events === null
                    ? <div className="db-muted">Polling Core…</div>
                    : <LifecycleTimeline events={events} />}
            </div>

            {/* ── Sandbox command log ── */}
            <div className="db-card">
                <div className="db-row" style={{ gap: 8, alignItems: 'center', marginBottom: 4 }}>
                    <div className="db-card-title" style={{ marginBottom: 0 }}>Sandbox command log</div>
                    <Icon name="terminal" size={12} />
                </div>
                <div className="db-muted" style={{ fontSize: 11, marginBottom: 8 }}>
                    Recent commands the agent ran in the sandbox (secrets masked). Ephemeral — cleared on restart.
                </div>
                {execEntries === null
                    ? <div className="db-muted">Polling Core…</div>
                    : <ExecLogCard entries={execEntries} />}
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
