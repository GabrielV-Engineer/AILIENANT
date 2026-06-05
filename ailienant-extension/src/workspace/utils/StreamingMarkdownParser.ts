/**
 * Phase 7.11.5 (ADR-706 §4.5e) — Stateful Streaming Markdown Parser.
 *
 * O(1) amortized per server token: `pushToken(state, token)` scans ONLY the
 * incoming token (bounded by the network chunk size, not the message length)
 * plus a 1-char `prev_char` lookback. It NEVER re-scans the historical buffer.
 *
 * The parser's job is to track which markdown constructs are currently OPEN at
 * the tail of the stream so the renderer can inject virtual closing tags
 * (`</code>`, `</strong>`, …) into the DOM without mutating the source buffer.
 * On stream completion the renderer drops to a stable one-shot path.
 *
 * Critical invariants (enforced by tests in
 *   `src/test/streamingMarkdownParser.test.ts`):
 *
 *   - **W1 (O(1) per token):** every `pushToken` call mutates ≤ 3 top-level
 *     formatting flags (`flagDelta`) between input and output state.
 *   - **W6 (anti-flicker):** when `in_code_fence === true` mid-stream the
 *     renderer wraps the open fence in `<pre><code>` immediately — typography
 *     does not wait for the closing ` ``` ` token.
 *   - **W7 (bold/italic across token boundary):** the 1-char `prev_char`
 *     window lets us detect `**` even when the two asterisks arrive in
 *     separate tokens.
 *   - **W9 (CommonMark §4.5 fence symmetry):** a fence closes ONLY by a
 *     start-of-line run of the SAME `fence_char` with length ≥ `fence_len`.
 *     Lets the LLM demonstrate nested markdown (a ` ```` `-fenced block
 *     containing a ` ``` `-fenced sub-block) without prematurely closing the
 *     outer fence on inner backticks.
 *   - **Source-buffer immutability:** `pushToken` is pure; the token string
 *     and the previous state object are not mutated. The caller's
 *     `Message.content` is the concatenation of all tokens — byte-identical
 *     to what the model emitted.
 *
 * Known approximations (deliberately out of scope — renderer compensates):
 *   - List markers (`* `, `- `, `+ `) are not specially tracked; a line-start
 *     `*` followed by space leaves `in_italic` set until the line resets.
 *     Visually the renderer treats the unclosed emphasis as literal text,
 *     and the next `\n` propagates correctly.
 *   - Setext headings, reference links, HTML blocks, and tables are not
 *     parsed — they round-trip through as plain text + paragraph wrapping.
 */

export interface ParserState {
    /** True while a fenced code block is open. */
    in_code_fence: boolean;
    /** CommonMark §4.5 — the char used to OPEN the active fence. */
    fence_char: '`' | '~' | '';
    /** CommonMark §4.5 — run length of the OPENING fence (3, 4, 5, …). The
     *  closer must be a run of `fence_char` with length ≥ `fence_len`. */
    fence_len: number;
    /** Info string captured after the opening fence (e.g. `js`, `python`). */
    fence_lang: string;

    /** True while inline (single/double-backtick) code is open. Wins over emphasis. */
    in_inline_code: boolean;
    in_bold: boolean;
    in_italic: boolean;
    in_strike: boolean;

    /** Line-scoped: resets on '\n'. */
    in_blockquote: boolean;
    /** Indent-depth of the active list (0 = no list). Capped at 6. Reserved
     *  for future use — the parser does not currently update this field. */
    list_depth: number;

    /** Inside `[link text` — looking for `]`. */
    in_link_text: boolean;
    /** Inside `(url` — looking for `)`. */
    in_link_href: boolean;

    /** 1-char lookback so a `**` opener split across tokens still parses. */
    prev_char: string;
    /** Cursor on the current line. Resets on '\n'. */
    at_line_start: boolean;
    /** Run length of consecutive fence chars seen at the current line start.
     *  Resets when a non-fence char arrives. Bounded — O(1). */
    fence_run: number;
    /** Char of the current `fence_run` ('`' / '~' / ''). */
    fence_run_char: '`' | '~' | '';
    /** True while we are still consuming the info-string of an opening fence
     *  (between the opening ` ``` ` and the next `\n`). */
    capturing_lang: boolean;
}

export const INITIAL_STATE: Readonly<ParserState> = Object.freeze({
    in_code_fence: false,
    fence_char: '' as const,
    fence_len: 0,
    fence_lang: '',

    in_inline_code: false,
    in_bold: false,
    in_italic: false,
    in_strike: false,

    in_blockquote: false,
    list_depth: 0,

    in_link_text: false,
    in_link_href: false,

    prev_char: '',
    at_line_start: true,
    fence_run: 0,
    fence_run_char: '' as const,
    capturing_lang: false,
});

export interface VirtualClosure {
    tag: 'code_fence' | 'inline_code' | 'bold' | 'italic' | 'strike' | 'link';
    /** Language captured from the opener (code fences only). */
    lang?: string;
}

/**
 * Compute the virtual closing tags the renderer must inject for the current
 * open constructs. Pure, O(1).
 *
 * Order is innermost-first (closest to the cursor) so the renderer can pop
 * them in the order it would close a real DOM tree.
 */
export function closuresFor(state: ParserState): VirtualClosure[] {
    const out: VirtualClosure[] = [];
    if (state.in_link_text || state.in_link_href) { out.push({ tag: 'link' }); }
    if (state.in_strike)       { out.push({ tag: 'strike' }); }
    if (state.in_italic)       { out.push({ tag: 'italic' }); }
    if (state.in_bold)         { out.push({ tag: 'bold' }); }
    if (state.in_inline_code)  { out.push({ tag: 'inline_code' }); }
    if (state.in_code_fence)   { out.push({ tag: 'code_fence', lang: state.fence_lang }); }
    return out;
}

/**
 * Resolve a pending fence run accumulated at the start of the current line.
 *
 * Returns the next slice of formatting state. Called from two sites:
 *   (a) when the FIRST non-fence char of the line arrives — for an OPEN we
 *       leave `capturing_lang=true` so the rest of the line is the info string.
 *   (b) when '\n' arrives with no other content on the line — for an OPEN
 *       with empty info string, OR for a CLOSE.
 *
 * Asymmetric runs (inner 5-backtick line while `fence_len === 3`) DO close —
 * that's correct per CommonMark; spec compliance over user surprise.
 */
function resolveFenceRun(
    in_code_fence: boolean,
    fence_char: '`' | '~' | '',
    fence_len: number,
    in_inline_code: boolean,
    in_strike: boolean,
    capturing_lang: boolean,
    fence_run: number,
    fence_run_char: '`' | '~' | '',
): {
    in_code_fence: boolean;
    fence_char: '`' | '~' | '';
    fence_len: number;
    fence_lang_reset: boolean;
    in_inline_code: boolean;
    in_strike: boolean;
    capturing_lang: boolean;
} {
    let fence_lang_reset = false;

    if (fence_run >= 3 && fence_run_char !== '') {
        if (!in_code_fence) {
            // OPEN
            in_code_fence = true;
            fence_char = fence_run_char;
            fence_len = fence_run;
            fence_lang_reset = true;
            capturing_lang = true;
        } else if (fence_run_char === fence_char && fence_run >= fence_len) {
            // CLOSE (CommonMark §4.5 symmetry)
            in_code_fence = false;
            fence_char = '';
            fence_len = 0;
            fence_lang_reset = true;
            capturing_lang = false;
        }
        // else: inner run inside an active fence that doesn't satisfy the
        // close contract → treat as literal content (no flag change).
    } else if (fence_run > 0 && fence_run_char === '`') {
        // Short backtick run (1 or 2) → inline code toggle. CommonMark §6.1
        // length-matching is approximated by a single-toggle rule.
        in_inline_code = !in_inline_code;
    } else if (fence_run === 2 && fence_run_char === '~') {
        // ~~strike~~
        in_strike = !in_strike;
    }
    // fence_run === 1 && fence_run_char === '~' → literal '~' (no flip).

    return {
        in_code_fence,
        fence_char,
        fence_len,
        fence_lang_reset,
        in_inline_code,
        in_strike,
        capturing_lang,
    };
}

/**
 * Advance the parser state by one token. Pure: never mutates inputs.
 *
 * Complexity: O(token.length). Since the token is the network chunk size
 * (bounded, doesn't grow with the message), the amortized cost per ARRIVING
 * TOKEN is O(1) — the contract ADR-706 §4.5e asks for.
 */
export function pushToken(state: ParserState, token: string): ParserState {
    if (token.length === 0) {
        // No-op chunk — happens on flush boundaries. Return the same shape so
        // tests can rely on referential stability when nothing changes.
        return state;
    }

    // Local mutable copy — we return a fresh object at the end so the caller's
    // previous state object is never mutated.
    let in_code_fence  = state.in_code_fence;
    let fence_char     = state.fence_char;
    let fence_len      = state.fence_len;
    let fence_lang     = state.fence_lang;
    let in_inline_code = state.in_inline_code;
    let in_bold        = state.in_bold;
    let in_italic      = state.in_italic;
    let in_strike      = state.in_strike;
    let in_blockquote  = state.in_blockquote;
    const list_depth   = state.list_depth;     // reserved; never written
    let in_link_text   = state.in_link_text;
    let in_link_href   = state.in_link_href;
    let prev_char      = state.prev_char;
    let at_line_start  = state.at_line_start;
    let fence_run      = state.fence_run;
    let fence_run_char = state.fence_run_char;
    let capturing_lang = state.capturing_lang;

    // In-flight info-string buffer. Re-seeded on every fence open.
    let lang_buf = capturing_lang ? fence_lang : '';

    for (let i = 0; i < token.length; i++) {
        const c = token[i];

        // ── 1. Fence run accumulation (only while at line start) ──
        if (at_line_start && (c === '`' || c === '~')) {
            if (fence_run === 0) {
                fence_run_char = c;
                fence_run = 1;
            } else if (c === fence_run_char) {
                fence_run += 1;
            } else {
                // Mixed run on the same line → abort fence detection and
                // process this char as content.
                fence_run = 0;
                fence_run_char = '';
                at_line_start = false;
                // Fall through to inline-content handling for `c`. Don't
                // `continue` — let step 5+ handle it.
            }
            if (fence_run > 0) {
                prev_char = c;
                continue;
            }
        }

        // ── 2. Resolve any pending fence run before processing `c` ──
        if (fence_run > 0) {
            const r = resolveFenceRun(
                in_code_fence, fence_char, fence_len,
                in_inline_code, in_strike, capturing_lang,
                fence_run, fence_run_char,
            );
            in_code_fence  = r.in_code_fence;
            fence_char     = r.fence_char;
            fence_len      = r.fence_len;
            if (r.fence_lang_reset) { fence_lang = ''; lang_buf = ''; }
            in_inline_code = r.in_inline_code;
            in_strike      = r.in_strike;
            capturing_lang = r.capturing_lang;
            fence_run = 0;
            fence_run_char = '';
            at_line_start = false;
        }

        // ── 3. Newline: line-scoped resets + commit info string ──
        if (c === '\n') {
            if (capturing_lang) {
                fence_lang = lang_buf.trim();
                capturing_lang = false;
                lang_buf = '';
            }
            in_blockquote = false;
            at_line_start = true;
            prev_char = c;
            continue;
        }

        // ── 4. Inside a fenced code block: only newlines / fence runs matter ──
        if (in_code_fence) {
            if (capturing_lang) { lang_buf += c; }
            at_line_start = false;
            prev_char = c;
            continue;
        }

        // ── 5. Inline code (any backtick mid-line) — wins over emphasis ──
        // Line-start backticks were captured by the fence_run branch in step 1.
        // A mid-line backtick toggles inline_code directly (single-toggle
        // approximation of CommonMark §6.1 length matching).
        if (c === '`') {
            in_inline_code = !in_inline_code;
            at_line_start = false;
            prev_char = c;
            continue;
        }
        if (in_inline_code) {
            // Asterisks / underscores / brackets are inert text inside `…`.
            at_line_start = false;
            prev_char = c;
            continue;
        }

        // ── 6. Emphasis: bold (** / __) vs italic (* / _) ──
        if (c === '*' || c === '_') {
            if (prev_char === c) {
                // Two-char digraph. The previous-char ALREADY flipped italic
                // when it arrived (we tentatively treated it as a single
                // marker). Revert that and flip bold instead.
                in_italic = !in_italic;
                in_bold = !in_bold;
                prev_char = '';   // consume — prevents '***' edge-case fanout
                at_line_start = false;
                continue;
            }
            // Single-char marker (so far). Flip italic provisionally; if the
            // next char is the same marker we'll undo it above.
            in_italic = !in_italic;
            at_line_start = false;
            prev_char = c;
            continue;
        }

        // ── 7. Strikethrough (GFM): mid-line ~~ ──
        // (Line-start ~~ was already handled by fence_run.)
        if (c === '~') {
            if (prev_char === '~') {
                in_strike = !in_strike;
                prev_char = '';
                at_line_start = false;
                continue;
            }
            prev_char = c;
            at_line_start = false;
            continue;
        }

        // ── 8. Links: [text](url) ──
        if (c === '[' && !in_link_text && !in_link_href) {
            in_link_text = true;
            at_line_start = false;
            prev_char = c;
            continue;
        }
        if (c === ']' && in_link_text && !in_link_href) {
            in_link_text = false;
            prev_char = c;
            at_line_start = false;
            continue;
        }
        if (c === '(' && prev_char === ']' && !in_link_href) {
            in_link_href = true;
            prev_char = c;
            at_line_start = false;
            continue;
        }
        if (c === ')' && in_link_href) {
            in_link_href = false;
            prev_char = c;
            at_line_start = false;
            continue;
        }

        // ── 9. Blockquote (line-scoped) ──
        if (at_line_start && c === '>') {
            in_blockquote = true;
            at_line_start = false;
            prev_char = c;
            continue;
        }

        // ── 10. Default content char. ──
        if (c !== ' ' && c !== '\t') {
            at_line_start = false;
        }
        prev_char = c;
    }

    // End-of-token: if we are still capturing the info string (no newline
    // arrived in this token), reflect what we've accumulated so far.
    if (capturing_lang) {
        fence_lang = lang_buf.trim();
    }

    return {
        in_code_fence,
        fence_char,
        fence_len,
        fence_lang,
        in_inline_code,
        in_bold,
        in_italic,
        in_strike,
        in_blockquote,
        list_depth,
        in_link_text,
        in_link_href,
        prev_char,
        at_line_start,
        fence_run,
        fence_run_char,
        capturing_lang,
    };
}

/**
 * Resolve any state we deliberately left pending at end-of-token because we
 * couldn't tell if more content was coming. Called by `server_stream_end`
 * in `Workspace.tsx`. Safe to call multiple times.
 *
 * Today this only commits a trailing fence_run as a CLOSE if it satisfies
 * the symmetry contract — required for the "user finished a code block on
 * the very last token without a trailing newline" case.
 */
export function finalize(state: ParserState): ParserState {
    if (state.fence_run >= 3 && state.fence_run_char !== '') {
        const r = resolveFenceRun(
            state.in_code_fence, state.fence_char, state.fence_len,
            state.in_inline_code, state.in_strike, state.capturing_lang,
            state.fence_run, state.fence_run_char,
        );
        return {
            ...state,
            in_code_fence: r.in_code_fence,
            fence_char: r.fence_char,
            fence_len: r.fence_len,
            fence_lang: r.fence_lang_reset ? '' : state.fence_lang,
            in_inline_code: r.in_inline_code,
            in_strike: r.in_strike,
            capturing_lang: r.capturing_lang,
            fence_run: 0,
            fence_run_char: '',
        };
    }
    return state;
}

// ── Fenced code-block extraction ────────────────────────────────────────────
// Shared by the renderer and the host-tokenize request path so a block's identity
// (its `hash`) is computed identically on both sides — the IPC reply can then be
// matched back to the exact block the renderer is painting.

/** Opening fence: a run of ≥3 backticks or tildes plus an optional info string. */
export const FENCE_OPEN_RE = /^(`{3,}|~{3,})\s*([^\s`~]*)\s*$/;
/** Closing fence: a bare run of the same char (length checked against the opener). */
export const FENCE_CLOSE_RE = /^(`{3,}|~{3,})\s*$/;

export interface CodeBlock {
    /** Stable identity of (lang, code) — the IPC/render correlation key. */
    hash: string;
    /** Info-string language hint (may be empty). */
    lang: string;
    /** Block body, newline-joined exactly as the renderer emits it. */
    code: string;
}

/**
 * FNV-1a (32-bit) hex digest of `lang\ncode`. Deterministic and dependency-free so
 * the renderer and the stream-end requester derive the same key for a block.
 */
export function hashCodeBlock(lang: string, code: string): string {
    const s = `${lang}\n${code}`;
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = Math.imul(h, 0x01000193);
    }
    return (h >>> 0).toString(16);
}

/**
 * Extract every fenced code block from a finalized message. Mirrors the renderer's
 * fence scan exactly (same regexes, same symmetry rule, same `\n` join) so the
 * resulting `hash` matches what the renderer looks up.
 */
export function extractCodeBlocks(text: string): CodeBlock[] {
    const out: CodeBlock[] = [];
    const lines = text.split('\n');
    let i = 0;
    while (i < lines.length) {
        const open = FENCE_OPEN_RE.exec(lines[i]);
        if (open) {
            const opener = open[1];
            const lang = open[2] ?? '';
            i += 1;
            const codeLines: string[] = [];
            while (i < lines.length) {
                const close = FENCE_CLOSE_RE.exec(lines[i]);
                if (close && close[1][0] === opener[0] && close[1].length >= opener.length) {
                    i += 1;
                    break;
                }
                codeLines.push(lines[i]);
                i += 1;
            }
            const code = codeLines.join('\n');
            out.push({ hash: hashCodeBlock(lang, code), lang, code });
            continue;
        }
        i += 1;
    }
    return out;
}

/** Count how many top-level formatting flags differ between two states.
 *  Used by the O(1) audit test (W1) — every `pushToken` must flip ≤ 3. */
export function flagDelta(a: ParserState, b: ParserState): number {
    let n = 0;
    if (a.in_code_fence  !== b.in_code_fence)  { n++; }
    if (a.in_inline_code !== b.in_inline_code) { n++; }
    if (a.in_bold        !== b.in_bold)        { n++; }
    if (a.in_italic      !== b.in_italic)      { n++; }
    if (a.in_strike      !== b.in_strike)      { n++; }
    if (a.in_blockquote  !== b.in_blockquote)  { n++; }
    if (a.in_link_text   !== b.in_link_text)   { n++; }
    if (a.in_link_href   !== b.in_link_href)   { n++; }
    return n;
}
