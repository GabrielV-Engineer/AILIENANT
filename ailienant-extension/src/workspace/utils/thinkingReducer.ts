/**
 * Phase 9 (ADR-707) — pure, immutable reducers for the Native Thinking stream.
 *
 * Extracted from Workspace.tsx so the accumulation / chronometric-freeze logic
 * can be unit-tested without rendering the panel, and so the message-update
 * path has a single source of truth. Every function returns NEW objects — it
 * never mutates the turn it is given (no ghost mutations in the Virtual DOM).
 *
 * Reasoning text is display-only: these helpers only ever touch the
 * `thinking*` slice, never `content` or history.
 */

/** The Native-Thinking fields carried on an assistant `Message`. */
export interface ThinkingSlice {
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
}

/**
 * Append a reasoning delta to an existing streaming assistant turn.
 * Stamps `thinkingStartedAt` on the first delta and auto-expands the box.
 */
export function accumulateThinking<T extends ThinkingSlice>(
    turn: T,
    delta: string,
    tokenCount: number | undefined,
    now: number,
): T {
    return {
        ...turn,
        thinking: (turn.thinking ?? '') + delta,
        thinkingTokens: tokenCount ?? turn.thinkingTokens ?? 0,
        thinkingStartedAt: turn.thinkingStartedAt ?? now,
        thinkingOpen: turn.thinkingOpen ?? true,
    };
}

/** Seed a brand-new streaming assistant turn from the first reasoning delta. */
export function newThinkingTurn(
    delta: string,
    tokenCount: number | undefined,
    now: number,
): ThinkingSlice & { role: 'assistant'; content: string; streaming: boolean } {
    return {
        role: 'assistant',
        content: '',
        streaming: true,
        thinking: delta,
        thinkingTokens: tokenCount ?? 0,
        thinkingStartedAt: now,
        thinkingOpen: true,
    };
}

/**
 * Compute the freeze update applied when the FIRST answer (text) token arrives
 * after a reasoning phase: freeze the elapsed clock and collapse the box.
 * Returns the partial update to merge, or `null` when no freeze is needed
 * (no prior thinking, or already frozen — idempotent).
 */
export function freezeThinkingOnText<T extends ThinkingSlice>(
    turn: T,
    now: number,
): Pick<ThinkingSlice, 'thinkingElapsedMs' | 'thinkingOpen'> | null {
    if (turn.thinking === undefined || turn.thinkingElapsedMs !== undefined) {
        return null;
    }
    return {
        thinkingElapsedMs:
            turn.thinkingStartedAt !== undefined ? Math.max(0, now - turn.thinkingStartedAt) : 0,
        thinkingOpen: false,
    };
}
