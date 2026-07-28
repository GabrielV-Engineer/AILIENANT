/**
 * Coder Companion Explanation Card.
 *
 * Renders the structured post-turn explanation (objective / decisions / patterns /
 * bottlenecks / errors / follow-ups) a best-effort background pass produces after the
 * Coder emits a patch. It is a droppable side-channel: it never gates the diff, and the
 * diff-approval row beside it stays fully interactive whether or not this ever arrives.
 *
 * The explanation is delivered on its own WebSocket event and parked in the workspace
 * store keyed by task_id; this card reads it via selector. Layout is reserved with a
 * fixed-height skeleton from mount so a late (or never-) arriving explanation causes no
 * layout shift. A fresh `correlation_id` (a Coder retry) resets the card to its skeleton
 * so a stale explanation never lingers against a superseded diff. If nothing arrives
 * within the timeout the skeleton resolves to a muted "unavailable" state — deliberately
 * identical for a budget-skip, a VRAM-skip, a call timeout, or plain delivery loss.
 */
import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { useWorkspaceStore } from '../workspaceStore';

interface Props {
    /** The session/task the explanation is correlated with (task_id == session_id). */
    taskId: string;
}

// Safety-net only: the backend now GUARANTEES a `server_coder_companion` broadcast on
// every exit path (success, degraded, budget/VRAM-skip, or an unexpected fault — see
// brain/coder_companion.py::_run_coder_companion, which converges every branch on one
// broadcast), so under normal operation this timer never fires before the payload
// arrives. It exists only to bound the wait if that broadcast itself is lost in
// transit (a dropped WS frame). Set comfortably above the backend's worst realistic
// deadline — a local-tier judge call (45s, _COMPANION_LLM_TIMEOUT_LOCAL_S) plus
// semaphore queueing + network margin — so it is a true last resort, not a race.
// See SCHEMA_EVOLUTION §37 (limitation 6).
const COMPANION_TIMEOUT_MS = 60000;

export function CoderCompanionCard({ taskId }: Props): JSX.Element {
    const payload = useWorkspaceStore(s => s.coderCompanions[taskId]);
    const [expanded, setExpanded] = useState(false);
    const [timedOut, setTimedOut] = useState(false);

    // Reset the timeout whenever a new attempt (correlation_id) supersedes the old one,
    // or when the card first mounts against a task with no explanation yet.
    const correlationId = payload?.correlation_id;
    const lastCorrelation = useRef<string | undefined>(undefined);
    useEffect(() => {
        if (correlationId !== lastCorrelation.current) {
            lastCorrelation.current = correlationId;
            setExpanded(false);
        }
    }, [correlationId]);

    useEffect(() => {
        setTimedOut(false);
        const id = window.setTimeout(() => setTimedOut(true), COMPANION_TIMEOUT_MS);
        return () => window.clearTimeout(id);
        // Re-arm the timer on every fresh attempt so a retry gets its own grace window.
    }, [taskId, correlationId]);

    // ── Loaded ────────────────────────────────────────────────────────────────
    if (payload) {
        const hasDetails =
            payload.patterns_applied.length > 0 ||
            payload.bottlenecks.length > 0 ||
            payload.errors_found.length > 0 ||
            payload.follow_ups.length > 0;

        return (
            <div className="ws-companion-card">
                <div className="ws-companion-title">
                    <Icon name="sparkles" size={13} />
                    <span>Companion Explanation</span>
                    {payload.degraded && <span className="ws-companion-degraded">Partial</span>}
                </div>

                <div className="ws-companion-objective">{payload.objective}</div>

                {payload.security_notes.length > 0 && (
                    <div className="ws-companion-security">
                        <div className="ws-companion-security-title">
                            <Icon name="shield" size={12} />
                            <span>Security Notes</span>
                        </div>
                        <ul className="ws-companion-list">
                            {payload.security_notes.map((note, i) => <li key={i}>{note}</li>)}
                        </ul>
                    </div>
                )}

                {payload.decisions.length > 0 && (
                    <div className="ws-companion-decisions">
                        <div className="ws-companion-subheading">Key Decisions</div>
                        {payload.decisions.map((d, i) => (
                            <div key={i} className="ws-companion-decision">
                                <div className="ws-companion-decision-name">{d.name}</div>
                                <div className="ws-companion-decision-rationale">{d.rationale}</div>
                                {d.risk && (
                                    <div className="ws-companion-decision-meta">
                                        <strong>Risk:</strong> {d.risk}
                                    </div>
                                )}
                                {d.tradeoff && (
                                    <div className="ws-companion-decision-meta">
                                        <strong>Tradeoff:</strong> {d.tradeoff}
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}

                {hasDetails && (
                    <div className="ws-companion-details">
                        <button
                            type="button"
                            className="ws-companion-toggle"
                            onClick={() => setExpanded(e => !e)}
                            aria-expanded={expanded}
                        >
                            <Icon name={expanded ? 'chevron-down' : 'chevron-right'} size={12} />
                            <span>Patterns, bottlenecks & follow-ups</span>
                        </button>
                        {expanded && (
                            <div className="ws-companion-details-body">
                                <CompanionList label="Patterns applied" items={payload.patterns_applied} />
                                <CompanionList label="Bottlenecks" items={payload.bottlenecks} />
                                <CompanionList label="Errors found" items={payload.errors_found} />
                                <CompanionList label="Follow-ups" items={payload.follow_ups} />
                            </div>
                        )}
                    </div>
                )}
            </div>
        );
    }

    // ── Unavailable (timed out, nothing delivered) ──────────────────────────────
    if (timedOut) {
        return (
            <div className="ws-companion-card ws-companion-unavailable">
                <div className="ws-companion-title">
                    <Icon name="alert" size={13} />
                    <span>Explanation unavailable</span>
                </div>
                <div className="ws-companion-muted">
                    No companion explanation for this change. The diff above is unaffected.
                </div>
            </div>
        );
    }

    // ── Skeleton (fixed footprint, no layout shift) ─────────────────────────────
    return (
        <div className="ws-companion-card ws-companion-skeleton" aria-busy="true">
            <div className="ws-companion-title">
                <div className="ws-companion-skeleton-bar" style={{ width: 160 }} />
            </div>
            <div className="ws-companion-skeleton-bar" style={{ width: '100%' }} />
            <div className="ws-companion-skeleton-bar" style={{ width: '92%' }} />
            <div className="ws-companion-skeleton-bar" style={{ width: '84%' }} />
        </div>
    );
}

function CompanionList({ label, items }: { label: string; items: string[] }): JSX.Element | null {
    if (items.length === 0) { return null; }
    return (
        <div className="ws-companion-detail-section">
            <div className="ws-companion-detail-heading">{label}</div>
            <ul className="ws-companion-list">
                {items.map((it, i) => <li key={i}>{it}</li>)}
            </ul>
        </div>
    );
}
