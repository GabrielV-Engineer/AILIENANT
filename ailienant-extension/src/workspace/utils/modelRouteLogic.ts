/**
 * Model-route review decision logic — pure, no React and no vscode bridge.
 *
 * Extracted from `ModelRouteCard` for the same reason `briefReviewLogic.ts` was
 * extracted from `BriefReviewCard`: this project's mocha suite tests component
 * logic directly rather than rendering into a DOM, and importing anything that
 * reaches `vscode_bridge.ts` triggers its eager `acquireVsCodeApi()` call, which
 * throws outside a real WebView.
 */

/** Mirrors `core/memory/context_auditor.py::resolve_model_alias_for_routing`'s
 *  own `mapping` dict — the ladder lives once on the backend; this is the
 *  frontend's necessary mirror of the same wire vocabulary (the same pattern
 *  `ActivityKind`/`ExecutionSource` already use in `api/contracts.ts`). */
export const TIER_ORDER = ['small', 'medium', 'big', 'cloud'] as const;
export type ModelTier = (typeof TIER_ORDER)[number];

const TIER_TO_DECISION: Record<ModelTier, string> = {
    small: 'LOCAL_SMALL', medium: 'LOCAL_MEDIUM', big: 'LOCAL_BIG', cloud: 'CLOUD',
};
const DECISION_TO_TIER: Record<string, ModelTier> = Object.fromEntries(
    (Object.entries(TIER_TO_DECISION) as [ModelTier, string][]).map(([tier, decision]) => [decision, tier]),
);

/** The active preset's tier for a `routing_decision` string, or `undefined`
 *  for a value outside the known ladder (rendered as the raw string instead
 *  of guessing). */
export function tierForDecision(routingDecision: string): ModelTier | undefined {
    return DECISION_TO_TIER[routingDecision];
}

export type RouteAction = 'accept' | 'override' | 'cancel';

export interface RouteDecision {
    approved: boolean;
    modified_content?: string;
}

/** Map an operator action (+ a chosen tier, for 'override') onto a resume payload. */
export function buildRouteDecision(action: RouteAction, chosenTier?: ModelTier): RouteDecision {
    if (action === 'accept') { return { approved: true }; }
    if (action === 'override') {
        const decision = chosenTier ? TIER_TO_DECISION[chosenTier] : undefined;
        return decision ? { approved: true, modified_content: decision } : { approved: true };
    }
    return { approved: false };
}

export interface ParsedRoutePayload {
    routing_decision: string;
    tci?: number;
    css?: number;
}

/**
 * `intervention.proposed_content` carries the drafted route as a JSON string
 * (`brain/routing_gate.py::_resolve_route_review`) rather than a new wire
 * field — `request_graph_approval`'s payload only has a single `str` content
 * slot. Defensive: a malformed or missing string degrades to `undefined`,
 * never throws — the card falls back to a minimal accept/cancel render.
 */
export function parseRoutePayload(proposedContent: string | undefined | null): ParsedRoutePayload | undefined {
    if (!proposedContent) { return undefined; }
    try {
        const parsed: unknown = JSON.parse(proposedContent);
        if (
            parsed && typeof parsed === 'object' &&
            typeof (parsed as Record<string, unknown>).routing_decision === 'string'
        ) {
            const p = parsed as Record<string, unknown>;
            return {
                routing_decision: p.routing_decision as string,
                tci: typeof p.tci === 'number' ? p.tci : undefined,
                css: typeof p.css === 'number' ? p.css : undefined,
            };
        }
    } catch {
        // Malformed JSON — fall through to undefined.
    }
    return undefined;
}
