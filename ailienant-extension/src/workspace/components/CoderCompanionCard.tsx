/**
 * Agent Companion Explanation Stack.
 *
 * Renders the structured explanation(s) (objective / decisions / patterns /
 * bottlenecks / errors / follow-ups) a best-effort background pass produces at a
 * decision point — a grill round closing, a plan committing, a patch set landing,
 * or a self-heal resolving (13.0.7). It is a droppable side-channel: it never
 * gates the turn, and everything beside it stays fully interactive whether or
 * not any of this ever arrives.
 *
 * Several decision points can fire in ONE turn (e.g. two grill rounds, each with
 * their own explanation), so this renders an APPEND-ordered stack — the calling
 * message's own `companions` array (attached message-scoped via
 * `attachOrUpdateCompanion`, keyed by emission_id), not a single payload and not
 * a session-wide store: a companion for THIS turn never bleeds onto another
 * turn's row. The pending affordance belongs to the TURN, not to any one entry:
 * it shows while the turn is still active and nothing has arrived yet, and
 * disappears the instant the turn settles (`isTurnActive`, 13.0.6) rather than
 * running its own independent timeout — a companion emission is fire-and-forget
 * and can legitimately still land after the turn visibly ends (SCHEMA_EVOLUTION
 * §37 limitation 6); it simply appears with no preceding placeholder in that
 * rare case, instead of a stale "unavailable" card sitting against a turn that
 * already moved on.
 */
import { useState } from 'react';
import { Icon } from '../../shared/Icon';
import type { CoderCompanionPayload, CompanionScope } from '../../api/contracts';

interface Props {
    /** This turn's own companion entries, in arrival order (Message.companions). */
    entries: CoderCompanionPayload[] | undefined;
    /** Whether this turn is still active — gates the single pending affordance. */
    turnActive: boolean;
}

const SCOPE_LABEL: Record<CompanionScope, string> = {
    coding: 'Coding Explanation',
    ideation: 'Clarification Explanation',
    planning: 'Planning Explanation',
    healing: 'Self-Heal Explanation',
};

export function CoderCompanionCard({ entries, turnActive }: Props): JSX.Element | null {
    const list = entries ?? [];
    const real = list.filter(p => !p.degraded);
    const skippedCount = list.length - real.length;

    if (real.length === 0 && skippedCount === 0 && !turnActive) { return null; }

    return (
        <div className="ws-companion-stack">
            {real.map(payload => (
                <CompanionEntryCard key={payload.emission_id ?? payload.correlation_id} payload={payload} />
            ))}
            {skippedCount > 0 && (
                <div className="ws-companion-note">
                    {skippedCount === 1
                        ? 'One explanation was unavailable.'
                        : `${skippedCount} explanations were unavailable.`}
                </div>
            )}
            {turnActive && real.length === 0 && skippedCount === 0 && (
                <div className="ws-companion-card ws-companion-skeleton" aria-busy="true">
                    <div className="ws-companion-title">
                        <div className="ws-companion-skeleton-bar" style={{ width: 160 }} />
                    </div>
                    <div className="ws-companion-skeleton-bar" style={{ width: '100%' }} />
                    <div className="ws-companion-skeleton-bar" style={{ width: '92%' }} />
                    <div className="ws-companion-skeleton-bar" style={{ width: '84%' }} />
                </div>
            )}
        </div>
    );
}

function CompanionEntryCard({ payload }: { payload: CoderCompanionPayload }): JSX.Element {
    const [expanded, setExpanded] = useState(false);
    const scopeLabel = SCOPE_LABEL[payload.scope ?? 'coding'];

    const hasDetails =
        payload.patterns_applied.length > 0 ||
        payload.bottlenecks.length > 0 ||
        payload.errors_found.length > 0 ||
        payload.follow_ups.length > 0;

    return (
        <div className="ws-companion-card">
            <div className="ws-companion-title">
                <Icon name="sparkles" size={13} />
                <span>{scopeLabel}</span>
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
