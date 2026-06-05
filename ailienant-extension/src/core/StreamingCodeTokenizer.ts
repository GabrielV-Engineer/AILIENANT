/**
 * Per-turn streaming code tokenizer.
 *
 * Consumes a stream of arbitrary-length token strings (WS chunks) and, for
 * each fenced code block it detects, emits one `LineEmit` per completed code
 * line as soon as the line is finished. The grammar engine runs on the host;
 * the webview receives ready-to-paint `ASTToken[]` lines with no parsing dep.
 *
 * Complexity:
 *   - O(chunk.length) per `push` call (char-by-char scan of the arriving text).
 *   - Each completed code line is tokenized exactly once (no buffer re-lex).
 *
 * Hardening:
 *   1. Async-init race: lines that complete before the block's LineTokenizer
 *      resolves are queued (FIFO) in `drainBuf` — a reference captured at
 *      `.then()` registration time, so the close handler's reassignment of
 *      `this.pendingLinesBuffer` does not discard them.
 *   2. Chunk-boundary safety: fence detection operates on the complete line
 *      buffer accumulated across multiple push calls, so a fence split across
 *      chunks (e.g. `` ` `` then ` `` `) never triggers a false match.
 *   3. Memory-leak / zombie guard: `reset()` increments a `generation` counter.
 *      Every `.then()` closure captures its `myGen` at registration time and
 *      exits early when `this.generation !== myGen`, so a late-resolving promise
 *      from a prior turn (or reset) can never emit into a new turn.
 */
import { FENCE_OPEN_RE, FENCE_CLOSE_RE } from '../workspace/utils/StreamingMarkdownParser';
import type { LineTokenizer } from './GrammarLexer';
import type { ASTToken } from '../shared/config';

/** A single tokenized code line emitted by the streaming tokenizer. */
export interface LineEmit {
    /** Which fenced block this line belongs to (0-indexed, increments per fence). */
    block_seq: number;
    /** 0-indexed line position within this block. */
    line_index: number;
    /** Host-tokenized scope spans for this line. */
    ast: ASTToken[];
}

/** Factory injected at construction; returns a per-block incremental tokenizer. */
export type CreateLineTokenizer = (langHint: string) => Promise<LineTokenizer | undefined>;

export class StreamingCodeTokenizer {
    private readonly createTokenizer: CreateLineTokenizer;
    private readonly onLine: (emit: LineEmit) => void;

    // Incremented by reset() to invalidate all in-flight .then() closures.
    private generation = 0;

    // Per-turn fence parse state — all cleared by reset().
    private currentLineBuffer = '';
    private inFence = false;
    private fenceChar = '';
    private fenceLen = 0;
    private blockSeq = -1;
    private lineIndex = 0;
    private abandoned = true;

    // Per-block async state.
    // null  = promise pending (initializing)
    // undefined = init failed or unsupported lang (no tokenizer)
    // LineTokenizer = ready for real-time use
    private resolvedTokenizer: LineTokenizer | undefined | null = null;
    // Lines that arrived before the factory resolved, queued FIFO.
    private pendingLinesBuffer: string[] = [];

    constructor(
        createTokenizer: CreateLineTokenizer,
        onLine: (emit: LineEmit) => void,
    ) {
        this.createTokenizer = createTokenizer;
        this.onLine = onLine;
    }

    push(token: string): void {
        for (let i = 0; i < token.length; i++) {
            const ch = token[i];
            if (ch === '\r') { continue; }   // normalize \r\n: strip CR, keep LF
            if (ch !== '\n') {
                this.currentLineBuffer += ch;
                continue;
            }
            // '\n' — commit the completed line.
            this.handleCompletedLine(this.currentLineBuffer);
            this.currentLineBuffer = '';
        }
    }

    /**
     * Clear all per-turn and per-block state. Increments the generation counter
     * so any in-flight .then() closures from the previous turn become no-ops.
     */
    reset(): void {
        this.generation++;         // invalidates all previous .then() closures
        this.abandoned = true;
        this.currentLineBuffer = '';
        this.inFence = false;
        this.fenceChar = '';
        this.fenceLen = 0;
        this.blockSeq = -1;
        this.lineIndex = 0;
        this.resolvedTokenizer = null;
        this.pendingLinesBuffer = [];
    }

    private handleCompletedLine(line: string): void {
        if (!this.inFence) {
            const open = FENCE_OPEN_RE.exec(line);
            if (open) {
                this.inFence = true;
                this.fenceChar = open[1][0];
                this.fenceLen = open[1].length;
                const lang = open[2] ?? '';
                this.blockSeq++;
                this.lineIndex = 0;
                this.abandoned = false;
                this.resolvedTokenizer = null;

                // Allocate the pending buffer for this block and capture the
                // reference BEFORE registering .then() — the close handler will
                // assign a new array to this.pendingLinesBuffer, but the closure
                // retains the reference to the lines that had already queued.
                const drainBuf: string[] = [];
                this.pendingLinesBuffer = drainBuf;

                const myGen = this.generation;
                const seq = this.blockSeq;

                void this.createTokenizer(lang).then((tok) => {
                    // Only the generation check is needed here: blockSeq alone is
                    // insufficient because two consecutive blocks in the same turn
                    // would both have valid generations but the second open changes
                    // blockSeq, which would falsely suppress the first block's drain.
                    if (this.generation !== myGen) { return undefined; }
                    const resolved = tok ?? undefined;
                    if (resolved) {
                        // Drain FIFO with the captured block identity — never use
                        // this.blockSeq or this.lineIndex here, since a second block
                        // may have already opened and changed those fields.
                        let drainIdx = 0;
                        for (const pending of drainBuf) {
                            if (this.generation !== myGen) { break; } // reset() mid-drain
                            const ast = resolved.tokenizeLine(pending);
                            if (ast && ast.length > 0) {
                                this.onLine({ block_seq: seq, line_index: drainIdx, ast });
                            }
                            drainIdx++;
                        }
                        // If the block is still open (not yet closed), wire up the
                        // resolved tokenizer and continue lineIndex from where drain left off.
                        if (this.generation === myGen && this.inFence && this.blockSeq === seq) {
                            this.resolvedTokenizer = resolved;
                            this.lineIndex = drainIdx;
                        }
                    } else if (this.generation === myGen && this.inFence && this.blockSeq === seq) {
                        // Unsupported lang: signal real-time path to skip tokenization.
                        this.resolvedTokenizer = undefined;
                    }
                    return tok;
                }).catch(() => {
                    if (this.generation === myGen && this.inFence && this.blockSeq === seq) {
                        this.resolvedTokenizer = undefined;
                    }
                });
            }
            return;
        }

        // Inside a fence: check for a closer first (same symmetry as extractCodeBlocks).
        const close = FENCE_CLOSE_RE.exec(line);
        if (close && close[1][0] === this.fenceChar && close[1].length >= this.fenceLen) {
            this.inFence = false;
            this.fenceChar = '';
            this.fenceLen = 0;
            // Do NOT clear pendingLinesBuffer here: the in-flight .then() needs
            // to drain whatever was queued before the close arrived. The generation
            // + blockSeq checks prevent the drain from running for wrong blocks.
            return;
        }

        // Code line inside the block.
        if (this.resolvedTokenizer === null) {
            // Tokenizer still initializing — add to pending buffer (same ref as drainBuf).
            this.pendingLinesBuffer.push(line);
        } else if (this.resolvedTokenizer) {
            // Ready — emit immediately.
            this.emitLine(this.resolvedTokenizer, line);
        }
        // resolvedTokenizer === undefined: unsupported lang — no emit (plain fallback).
    }

    private emitLine(tokenizer: LineTokenizer, line: string): void {
        if (this.abandoned) { return; }
        const ast = tokenizer.tokenizeLine(line);
        if (ast && ast.length > 0) {
            this.onLine({ block_seq: this.blockSeq, line_index: this.lineIndex, ast });
        }
        this.lineIndex++;
    }
}
