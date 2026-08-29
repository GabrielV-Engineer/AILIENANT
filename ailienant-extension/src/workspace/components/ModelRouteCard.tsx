/**
 * Model Route Review — confirm or override the router's model pick, once per
 * turn, before the planner drafts.
 *
 * The router (`agents/researcher.py`'s CSS/TCI cascade) already selects a
 * model tier before this card can appear, and the Glass-Box Timeline's lane
 * badge (13.1.9) already shows what it picked — this card is what lets the
 * operator actually act on that pick instead of only observing it after the
 * fact. TCI/CSS render alongside the decision so it is a real justification,
 * not a rubber stamp.
 *
 * Rides the same approval channel and single-resolve guard as every other
 * HITL surface (`useHitlResponder`); the drafted route travels as a JSON
 * string inside the existing `proposed_content` field rather than a new wire
 * field (`utils/modelRouteLogic.ts::parseRoutePayload`).
 */
import { useCallback, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import { useHitlResponder } from '../utils/useHitlResponder';
import { useChatStore } from '../chatStore';
import { formatModelBadge } from '../utils/agentLanes';
import {
    buildRouteDecision, parseRoutePayload, tierForDecision, TIER_ORDER, type ModelTier,
} from '../utils/modelRouteLogic';
import type { HITLIntervention } from './HITLInterventionCard';

export const MODEL_ROUTE_REVIEW_KIND = 'MODEL_ROUTE_REVIEW';

interface Props {
    intervention: HITLIntervention;
    onResolved: (approvalId: string) => void;
}

export function ModelRouteCard({ intervention, onResolved }: Props): JSX.Element {
    const config = useChatStore((s) => s.config);
    const [menuOpen, setMenuOpen] = useState(false);
    const { respond, resolvedRef } = useHitlResponder(intervention.approval_id, onResolved);

    const decide = useCallback((action: 'accept' | 'override' | 'cancel', tier?: ModelTier) => {
        const d = buildRouteDecision(action, tier);
        respond(d.approved, { modified_content: d.modified_content });
    }, [respond]);

    const accept = useCallback(() => decide('accept'), [decide]);
    const cancel = useCallback(() => decide('cancel'), [decide]);
    const pick = useCallback((tier: ModelTier) => {
        setMenuOpen(false);
        decide('override', tier);
    }, [decide]);

    const parsed = parseRoutePayload(intervention.proposed_content);
    const disabled = resolvedRef.current;

    if (!parsed) {
        // A malformed or missing payload must never crash the card — degrade
        // to a minimal accept/cancel so the turn can still proceed.
        return (
            <div className="ws-route-review ai-card" role="group" aria-label="Confirm the model for this turn">
                <p className="ws-route-review-sub">Routing selected a model for this turn.</p>
                <div className="ws-route-review-actions">
                    <button className="ai-btn" data-variant="primary" type="button" onClick={accept} disabled={disabled}>
                        <Icon name="check" size={13} /><span>Accept</span>
                    </button>
                    <button className="ai-btn" data-variant="danger" type="button" onClick={cancel} disabled={disabled}>
                        <Icon name="x" size={13} /><span>Cancel</span>
                    </button>
                </div>
            </div>
        );
    }

    const currentTier = tierForDecision(parsed.routing_decision);
    const badge = currentTier ? formatModelBadge(currentTier, config) : parsed.routing_decision;
    const hasJustification = typeof parsed.tci === 'number' && typeof parsed.css === 'number';

    return (
        <div className="ws-route-review ai-card" role="group" aria-label="Confirm the model for this turn">
            <div className="ws-route-review-head">
                <Icon name="cpu" size={14} />
                <span className="ws-route-review-title">Routing chose <code>{badge}</code></span>
            </div>
            <p className="ws-route-review-sub">
                {hasJustification
                    ? `TCI ${parsed.tci!.toFixed(0)} · CSS ${parsed.css!.toFixed(0)} → ${parsed.routing_decision}`
                    : parsed.routing_decision}
            </p>

            <div className="ws-route-review-actions">
                <Tooltip content="Use this model for the rest of the turn">
                    <button className="ai-btn" data-variant="primary" type="button" onClick={accept} disabled={disabled}>
                        <Icon name="check" size={13} /><span>Accept</span>
                    </button>
                </Tooltip>
                <div className="ws-route-review-menu-wrap">
                    <button
                        className="ai-btn" type="button" onClick={() => setMenuOpen(o => !o)}
                        aria-expanded={menuOpen} disabled={disabled}
                    >
                        <Icon name="chevron-down" size={13} /><span>Use a different model</span>
                    </button>
                    {menuOpen && (
                        <div className="ws-route-review-menu" role="menu">
                            {TIER_ORDER.filter(t => t !== currentTier).map(tier => (
                                <button
                                    key={tier} type="button" role="menuitem"
                                    className="ws-route-review-menu-item" onClick={() => pick(tier)}
                                >
                                    {formatModelBadge(tier, config)}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
                <Tooltip content="End this turn">
                    <button className="ai-btn" data-variant="danger" type="button" onClick={cancel} disabled={disabled}>
                        <Icon name="x" size={13} /><span>Cancel</span>
                    </button>
                </Tooltip>
            </div>
        </div>
    );
}
