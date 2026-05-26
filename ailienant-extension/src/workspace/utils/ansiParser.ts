/**
 * Phase 7.11.6 (ADR-706 §4.5f) — Zero-dep ANSI SGR parser.
 *
 * A tiny incremental parser for the ANSI Select-Graphic-Rendition escape
 * sequences emitted by ordinary terminal tools (`grep --color`, `pytest`,
 * `npm test`, ...). Translates `\x1b[…m` sequences into a list of styled
 * text "runs" the React renderer maps to `<span class="ansi-red">` etc.
 *
 * Why not `xterm.js` or `ansi-to-html`?
 *   - xterm.js is ~250 KB and emulates a full terminal (cursors, scrollback,
 *     bell). Massive overkill for "render a few colored lines in a chip".
 *   - ansi-to-html bundles ~10 KB but emits raw HTML strings, forcing every
 *     caller to pipe through DOMPurify. The custom parser emits structured
 *     run objects that React renders as text nodes — no HTML-string
 *     intermediate, no sanitizer round-trip required for the common case.
 *
 * Streaming contract (the W3 carry-over from the plan):
 *   - `parseAnsi(input, prevState)` returns `{ runs, state }`.
 *   - The returned `state` includes `partial_escape: string` — if a chunk
 *     ends mid-escape (`'\x1b[31'`), the unfinished sequence is held over
 *     and prepended to the next chunk so colors don't bleed.
 *   - Resetting via `\x1b[0m` clears every flag back to INITIAL_STATE.
 *
 * Supported SGR codes:
 *   - 0          reset (clears every flag)
 *   - 1          bold        → class `ansi-bold`
 *   - 2          dim         → class `ansi-dim`
 *   - 3          italic      → class `ansi-italic`
 *   - 4          underline   → class `ansi-underline`
 *   - 22, 23, 24 turn off bold/italic/underline respectively
 *   - 30-37      8 standard FG colors    → `ansi-{black,red,…,white}`
 *   - 90-97      8 bright FG colors      → `ansi-bright-{…}`
 *   - 40-47      8 standard BG colors    → `ansi-bg-{…}`
 *   - 100-107    8 bright BG colors      → `ansi-bg-bright-{…}`
 *   - 38;2;r;g;b 24-bit truecolor FG     → inline `style.color = "rgb(r,g,b)"`
 *   - 48;2;r;g;b 24-bit truecolor BG     → inline `style.backgroundColor`
 *   - 39 / 49    reset FG / BG only
 *   - Unknown codes are silently ignored (forward-compat).
 *
 * Out of scope (the parser drops without parsing — tools rarely use these in
 * chat-style output):
 *   - 38;5;n     8-bit indexed palette
 *   - Cursor movement (`\x1b[2J`, `\x1b[H`, etc.)
 *   - Title sequences (`\x1b]0;…\x07`)
 *   - Hyperlink (`\x1b]8;;url\x07text\x1b]8;;\x07`)
 */

/** Style class names — match the CSS rules in `workspace.css`. */
export type AnsiColorName =
    | 'black' | 'red' | 'green' | 'yellow'
    | 'blue' | 'magenta' | 'cyan' | 'white';

const FG_BASE_TO_NAME: Record<number, AnsiColorName> = {
    30: 'black', 31: 'red', 32: 'green', 33: 'yellow',
    34: 'blue', 35: 'magenta', 36: 'cyan', 37: 'white',
};
const BG_BASE_TO_NAME: Record<number, AnsiColorName> = {
    40: 'black', 41: 'red', 42: 'green', 43: 'yellow',
    44: 'blue', 45: 'magenta', 46: 'cyan', 47: 'white',
};

export interface AnsiStyleState {
    fg?: AnsiColorName;
    fg_bright?: boolean;
    bg?: AnsiColorName;
    bg_bright?: boolean;
    bold: boolean;
    dim: boolean;
    italic: boolean;
    underline: boolean;
    /** 24-bit truecolor — emitted as inline style, not as a class. */
    rgb_fg?: [number, number, number];
    rgb_bg?: [number, number, number];
    /** Unfinished escape sequence carried from the previous chunk. */
    partial_escape: string;
}

export const INITIAL_STATE: Readonly<AnsiStyleState> = Object.freeze({
    bold: false,
    dim: false,
    italic: false,
    underline: false,
    partial_escape: '',
});

export interface AnsiRun {
    /** Plain text — never contains escape sequences. */
    text: string;
    /** CSS class names matching the style flags. */
    classes: string[];
    /** Inline style for 24-bit truecolor; absent for standard 16 colors. */
    style?: { color?: string; backgroundColor?: string };
}

export interface AnsiParseResult {
    runs: AnsiRun[];
    state: AnsiStyleState;
}

/** Apply a single SGR numeric parameter to the running style state. Mutates
 *  the state object — caller copies first. */
function applySgr(state: AnsiStyleState, code: number): void {
    if (code === 0) {
        // Reset every flag. The partial_escape is preserved (it's parser
        // bookkeeping, not a style).
        state.fg = undefined;
        state.fg_bright = undefined;
        state.bg = undefined;
        state.bg_bright = undefined;
        state.bold = false;
        state.dim = false;
        state.italic = false;
        state.underline = false;
        state.rgb_fg = undefined;
        state.rgb_bg = undefined;
        return;
    }
    if (code === 1) { state.bold = true; return; }
    if (code === 2) { state.dim = true; return; }
    if (code === 3) { state.italic = true; return; }
    if (code === 4) { state.underline = true; return; }
    if (code === 22) { state.bold = false; state.dim = false; return; }
    if (code === 23) { state.italic = false; return; }
    if (code === 24) { state.underline = false; return; }
    if (code === 39) {
        state.fg = undefined; state.fg_bright = undefined; state.rgb_fg = undefined;
        return;
    }
    if (code === 49) {
        state.bg = undefined; state.bg_bright = undefined; state.rgb_bg = undefined;
        return;
    }
    if (code >= 30 && code <= 37) {
        state.fg = FG_BASE_TO_NAME[code];
        state.fg_bright = false;
        state.rgb_fg = undefined;
        return;
    }
    if (code >= 90 && code <= 97) {
        state.fg = FG_BASE_TO_NAME[code - 60];
        state.fg_bright = true;
        state.rgb_fg = undefined;
        return;
    }
    if (code >= 40 && code <= 47) {
        state.bg = BG_BASE_TO_NAME[code];
        state.bg_bright = false;
        state.rgb_bg = undefined;
        return;
    }
    if (code >= 100 && code <= 107) {
        state.bg = BG_BASE_TO_NAME[code - 60];
        state.bg_bright = true;
        state.rgb_bg = undefined;
        return;
    }
    // Unknown codes silently dropped.
}

/** Apply a full SGR parameter list (the `;`-separated digits between `\x1b[`
 *  and `m`). Returns nothing; mutates `state`. Handles the multi-parameter
 *  truecolor encodings (`38;2;r;g;b` and `48;2;r;g;b`) by consuming the next
 *  four params after a 38/48. */
function applySgrParams(state: AnsiStyleState, params: number[]): void {
    let i = 0;
    while (i < params.length) {
        const code = params[i];
        if ((code === 38 || code === 48) && params[i + 1] === 2) {
            // 24-bit truecolor: 38;2;R;G;B (FG) or 48;2;R;G;B (BG).
            const r = params[i + 2] ?? 0;
            const g = params[i + 3] ?? 0;
            const b = params[i + 4] ?? 0;
            if (code === 38) {
                state.rgb_fg = [r, g, b];
                state.fg = undefined;
                state.fg_bright = undefined;
            } else {
                state.rgb_bg = [r, g, b];
                state.bg = undefined;
                state.bg_bright = undefined;
            }
            i += 5;
            continue;
        }
        if ((code === 38 || code === 48) && params[i + 1] === 5) {
            // 8-bit indexed palette — out of scope; skip the 3-param form
            // entirely so we don't misread the index as a separate SGR code.
            i += 3;
            continue;
        }
        applySgr(state, code);
        i += 1;
    }
}

/** Build the runtime view of the current style (class list + optional inline
 *  style). Pure; never mutates `state`. */
function styleOf(state: AnsiStyleState): Pick<AnsiRun, 'classes' | 'style'> {
    const classes: string[] = [];
    if (state.fg) {
        classes.push(state.fg_bright ? `ansi-bright-${state.fg}` : `ansi-${state.fg}`);
    }
    if (state.bg) {
        classes.push(state.bg_bright ? `ansi-bg-bright-${state.bg}` : `ansi-bg-${state.bg}`);
    }
    if (state.bold)      { classes.push('ansi-bold'); }
    if (state.dim)       { classes.push('ansi-dim'); }
    if (state.italic)    { classes.push('ansi-italic'); }
    if (state.underline) { classes.push('ansi-underline'); }

    if (state.rgb_fg || state.rgb_bg) {
        const style: { color?: string; backgroundColor?: string } = {};
        if (state.rgb_fg) {
            const [r, g, b] = state.rgb_fg;
            style.color = `rgb(${r}, ${g}, ${b})`;
        }
        if (state.rgb_bg) {
            const [r, g, b] = state.rgb_bg;
            style.backgroundColor = `rgb(${r}, ${g}, ${b})`;
        }
        return { classes, style };
    }
    return { classes };
}

/**
 * Translate a (possibly chunk-truncated) string with ANSI SGR sequences into
 * a list of text runs plus the carry-over state for the next chunk.
 *
 * Linear-time over the input. Never throws on malformed input — unknown
 * sequences are passed through as literal text after one diagnostic scan.
 */
export function parseAnsi(
    input: string,
    prevState: Readonly<AnsiStyleState> = INITIAL_STATE,
): AnsiParseResult {
    // Combine any held-over partial escape with the new chunk so a sequence
    // split across chunk boundaries parses correctly (W3 in the plan).
    const buf = (prevState.partial_escape ?? '') + input;
    const state: AnsiStyleState = {
        fg: prevState.fg,
        fg_bright: prevState.fg_bright,
        bg: prevState.bg,
        bg_bright: prevState.bg_bright,
        bold: prevState.bold,
        dim: prevState.dim,
        italic: prevState.italic,
        underline: prevState.underline,
        rgb_fg: prevState.rgb_fg ? [...prevState.rgb_fg] : undefined,
        rgb_bg: prevState.rgb_bg ? [...prevState.rgb_bg] : undefined,
        partial_escape: '',
    };

    const runs: AnsiRun[] = [];
    let text = '';
    let i = 0;
    const flushRun = (): void => {
        if (text.length === 0) { return; }
        const { classes, style } = styleOf(state);
        const run: AnsiRun = { text, classes };
        if (style) { run.style = style; }
        runs.push(run);
        text = '';
    };

    while (i < buf.length) {
        const c = buf[i];
        if (c !== '\x1b') {
            text += c;
            i += 1;
            continue;
        }
        // Possible escape sequence — flush whatever text we have so its style
        // is preserved BEFORE we apply the new SGR.
        // Need `\x1b[` (CSI). If the next char is missing, hold the partial.
        if (i + 1 >= buf.length) {
            state.partial_escape = buf.slice(i);
            i = buf.length;
            break;
        }
        if (buf[i + 1] !== '[') {
            // Some other escape (OSC, etc.) — drop the ESC + next char and
            // continue. This matches a tolerant terminal's behavior for
            // unsupported sequences.
            i += 2;
            continue;
        }
        // Find the terminator. For SGR it's 'm'; if we don't see one, the
        // sequence is incomplete — hold it for the next chunk.
        const term = findCsiTerminator(buf, i + 2);
        if (term === -1) {
            state.partial_escape = buf.slice(i);
            i = buf.length;
            break;
        }
        flushRun();
        // For non-SGR CSI sequences (cursor moves, screen clears, etc.) we
        // simply drop them — they have no visual representation in a chip.
        if (buf[term] === 'm') {
            const params = parseSgrParams(buf.slice(i + 2, term));
            applySgrParams(state, params);
        }
        i = term + 1;
    }
    flushRun();

    return { runs, state };
}

/** Locate the terminator byte of a CSI sequence starting at `start`. Returns
 *  -1 if not found (caller holds the partial). Terminators are any byte in
 *  0x40..0x7e per ECMA-48. */
function findCsiTerminator(buf: string, start: number): number {
    for (let j = start; j < buf.length; j++) {
        const ch = buf.charCodeAt(j);
        if (ch >= 0x40 && ch <= 0x7e) { return j; }
    }
    return -1;
}

/** Parse a `;`-separated SGR parameter list. Empty params (e.g. the empty
 *  string between `\x1b[` and `m` in `\x1b[m`) act as a single `0` (reset). */
function parseSgrParams(raw: string): number[] {
    if (raw.length === 0) { return [0]; }
    const out: number[] = [];
    for (const part of raw.split(';')) {
        const n = part.length === 0 ? 0 : parseInt(part, 10);
        out.push(Number.isFinite(n) ? n : 0);
    }
    return out;
}
