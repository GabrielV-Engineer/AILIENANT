/**
 * Lightweight PTY output sanitizer for the static cell-audit log.
 *
 * Raw pseudo-terminal output is not plain text: tools such as npm, pytest and git
 * inject ANSI escape sequences (SGR colors, cursor moves) and use carriage returns
 * to repaint a line in place (progress bars, spinners). Rendered verbatim into the
 * DOM this becomes unreadable control-code noise plus a flood of phantom rows.
 *
 * This is a read-only forensic log, not an interactive terminal, so a regex-grade
 * cleaner is sufficient -- a full terminal emulator (xterm.js) is a separate
 * surface. The cleaner (1) strips ANSI escape sequences and (2) collapses
 * carriage-return overwrites to their final frame, so a 200-tick progress bar
 * reduces to a single line.
 */

// ANSI/VT control sequences: a leading ESC (U+001B) or single-char CSI (U+009B),
// then the standard CSI parameter/intermediate bytes and a final byte. Covers the
// SGR color codes and cursor movements that dominate CLI tool output. Built from a
// string so the control-byte class is expressed purely with \u escapes.
const ANSI_ESCAPE = new RegExp(
    '[\\u001B\\u009B][[\\]()#;?]*(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-PR-TZcf-nqry=><~]',
    'g',
);

/**
 * Clean a raw PTY chunk for display. Strips ANSI sequences, normalizes CRLF to LF
 * so genuine line breaks survive, then keeps only the text after the last lone
 * carriage return on each line (the final repaint frame).
 */
export function sanitizePtyChunk(text: string): string {
    const stripped = text.replace(ANSI_ESCAPE, '');
    const normalized = stripped.replace(/\r\n/g, '\n');
    return normalized
        .split('\n')
        .map((line) => {
            const cr = line.lastIndexOf('\r');
            return cr === -1 ? line : line.slice(cr + 1);
        })
        .join('\n');
}
