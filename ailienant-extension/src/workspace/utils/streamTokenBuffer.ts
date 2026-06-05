/**
 * Streaming-AST hydration buffer reducer.
 *
 * During streaming, the host pushes one tokenized code line at a time. Those
 * pushes are coalesced into a single state update per animation frame; this
 * module owns the pure fold that merges a batch of pushes into a turn's
 * `streamingCodeTokens` record. It is deliberately free of React and host deps
 * so it can be unit-tested in isolation.
 */
import type { ASTToken } from '../../shared/config';

/** One per-line token push, buffered between animation frames. */
export interface StreamLineEmit {
    /** Which fenced block this line belongs to (0-indexed, matches the host's block_seq). */
    block_seq: number;
    /** 0-indexed line position within the block. */
    line_index: number;
    /** Host-tokenized scope spans for this line. */
    ast: ASTToken[];
}

/**
 * Fold a batch of per-line token pushes into a turn's `streamingCodeTokens`
 * record under a strict Copy-on-Write discipline: clone only the spine that
 * changed — the block dictionary, then each touched block array — and inject the
 * new line by index.
 *
 * Untouched line arrays MUST keep their exact reference. The renderer's `CodeLine`
 * memo compares `tokens` by reference, so regenerating an already-painted line's
 * array (even with identical content) would re-render every row and bring back the
 * very flicker this buffer exists to remove. A per-batch set guards against
 * re-cloning a block that receives more than one line in the same frame.
 */
export function mergeStreamEmits(
    existing: Record<number, ASTToken[][]> | undefined,
    emits: StreamLineEmit[],
): Record<number, ASTToken[][]> {
    if (emits.length === 0) { return existing ?? {}; }
    const next: Record<number, ASTToken[][]> = { ...existing };
    const clonedThisBatch = new Set<number>();
    for (const { block_seq, line_index, ast } of emits) {
        if (!clonedThisBatch.has(block_seq)) {
            next[block_seq] = next[block_seq] ? [...next[block_seq]] : [];
            clonedThisBatch.add(block_seq);
        }
        next[block_seq][line_index] = ast;
    }
    return next;
}
