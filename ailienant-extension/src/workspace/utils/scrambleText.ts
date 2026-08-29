/**
 * Glass-Box Timeline loader — the lead-character "decode" swap (13.1.9).
 *
 * Deliberately NOT a full scramble-every-character effect: only the first
 * `SCRAMBLE_LEAD_CHARS` of the incoming phrase cycle through a small fixed
 * glyph set for a few ticks before the whole phrase settles. Keeps the
 * "decoding in" feel without the maintenance cost of a per-character random
 * state machine — and deterministic (keyed by `tick`, never `Math.random`),
 * so it is trivially unit-testable and reduced-motion-safe (skip straight to
 * the settled frame).
 */

const SCRAMBLE_GLYPHS = ['#', '@', '%', '*', '?'] as const;
const SCRAMBLE_LEAD_CHARS = 2;
export const SCRAMBLE_TICKS = 4;
export const SCRAMBLE_TICK_MS = 40;

/**
 * One animation frame: `tick` 0..SCRAMBLE_TICKS-1 return `text` with its first
 * `SCRAMBLE_LEAD_CHARS` replaced by a deterministic glyph pick; `tick >=
 * SCRAMBLE_TICKS` (or an empty `text`) returns `text` itself, settled.
 */
export function scrambleFrame(text: string, tick: number): string {
    if (tick >= SCRAMBLE_TICKS || text.length === 0) { return text; }
    const leadLen = Math.min(SCRAMBLE_LEAD_CHARS, text.length);
    let lead = '';
    for (let i = 0; i < leadLen; i++) {
        lead += SCRAMBLE_GLYPHS[(tick + i) % SCRAMBLE_GLYPHS.length];
    }
    return lead + text.slice(leadLen);
}
