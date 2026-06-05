/**
 * Phase 7.11.5 (ADR-706 §4.5e) — Anti-flicker streaming markdown renderer.
 *
 * Renders the accumulated `content` of an assistant turn as React nodes.
 * The renderer is a PURE function of its props — it never mutates `content`.
 * "Virtual closures" are not added to the source buffer; instead they are
 * implicit in the emitted JSX tree (which is always balanced by definition),
 * so an open code fence at mid-stream is wrapped in `<pre><code>` and the
 * closing `</code></pre>` arrives speculatively before the closing ` ``` `
 * does. This is what makes code-block typography appear the moment the
 * opening fence arrives, instead of waiting for the closer.
 *
 * Block-level handling:
 *   - Fenced code blocks (CommonMark §4.5 symmetry: closer's run length must
 *     be ≥ opener's, with the SAME char) → `<pre><code class="language-…">`.
 *   - Everything else → paragraph runs of inline-formatted text.
 *
 * Inline handling (left-to-right, first-match wins):
 *   - `` `inline code` ``     → `<code>`
 *   - `**bold**` / `__bold__` → `<strong>`
 *   - `*italic*` / `_italic_` → `<em>`
 *   - `~~strike~~`            → `<del>`
 *   - `[text](url)`           → `<a href>` (rendered as URL; LLM output is
 *                                untrusted so we never auto-click).
 *
 * Out of scope for this milestone (renderer documents as known): headings,
 * lists, blockquotes, tables, images, reference links, HTML blocks.
 */
import { memo, Fragment } from 'react';
import type { ReactNode } from 'react';
import type { ASTToken } from '../../shared/config';
import {
    FENCE_OPEN_RE, FENCE_CLOSE_RE, hashCodeBlock,
    type ParserState,
} from '../utils/StreamingMarkdownParser';
import { scopeColor } from '../utils/scopeColor';

interface Props {
    content: string;
    /** Parser state at end-of-stream. Currently used only for `memo`'s
     *  reference-equality optimisation — the renderer does its own scan. */
    parserState: ParserState | undefined;
    /** True while the assistant turn is still streaming. The renderer
     *  emits the same JSX shape either way, but `streaming` is part of the
     *  prop bundle so a stream-end re-render reliably invalidates `memo`. */
    streaming: boolean;
    /** Host-tokenized syntax spans for this turn's fenced code blocks, keyed by
     *  `hashCodeBlock(lang, code)`. Populated on stream-end via the round-trip to
     *  the host grammar engine; absent keys fall back to plain text. The renderer
     *  carries no grammar dependency of its own — it only paints precomputed tokens. */
    codeTokens?: Record<string, ASTToken[][]>;
    /** Streaming partial AST keyed by fence ordinal. The host pushes one entry per
     *  completed code line during streaming; `codeTokens` supersedes this once the
     *  full block is tokenized on stream-end. Ordinals match the fence-counter the
     *  host's `StreamingCodeTokenizer` maintains (same fence rules as this renderer). */
    streamingCodeTokens?: Record<number, ASTToken[][]>;
}

export const MarkdownRenderer = memo(function MarkdownRenderer(
    { content, codeTokens, streamingCodeTokens }: Props,
): JSX.Element {
    return <>{renderBlocks(content, codeTokens, streamingCodeTokens)}</>;
});

// Paint one scope-colored token span.
function TokenSpan({ token, i }: { token: ASTToken; i: number }): JSX.Element {
    return <span key={i} style={{ color: scopeColor(token.type) }}>{token.content}</span>;
}

/**
 * Render a code block whose lines may be partially tokenized.
 *
 * `tokenLines[i]` = the host-tokenized spans for source line i. If a line has
 * no entry (streaming not yet delivered it, or the block was unsupported) the
 * raw plaintext line is rendered instead — so the block always displays something
 * and progressively lights up as lines arrive during streaming.
 */
function renderZippedLines(codeLines: string[], tokenLines: ASTToken[][]): ReactNode {
    return codeLines.map((line, li) => {
        const toks = tokenLines[li];
        return (
            <Fragment key={li}>
                {li > 0 && '\n'}
                {toks && toks.length > 0
                    ? toks.map((t, ti) => <TokenSpan key={ti} token={t} i={ti} />)
                    : line}
            </Fragment>
        );
    });
}

// Paint a fully-tokenized code block (every line present). Used for the
// final stream-end token arrays where no plain-text fallback is needed.
function renderTokenLines(lines: ASTToken[][]): ReactNode {
    return lines.map((line, li) => (
        <Fragment key={li}>
            {li > 0 && '\n'}
            {line.map((t, ti) => (
                <span key={ti} style={{ color: scopeColor(t.type) }}>{t.content}</span>
            ))}
        </Fragment>
    ));
}

// ── Block-level scan: fenced code vs prose ──────────────────────────────────

function renderBlocks(
    text: string,
    codeTokens?: Record<string, ASTToken[][]>,
    streamingCodeTokens?: Record<number, ASTToken[][]>,
): ReactNode[] {
    const out: ReactNode[] = [];
    const lines = text.split('\n');
    let i = 0;
    let key = 0;
    // Fence ordinal: incremented for each fenced block, mirrors the host's
    // block_seq counter so streamingCodeTokens keys resolve correctly.
    let fenceOrdinal = -1;

    while (i < lines.length) {
        const openMatch = FENCE_OPEN_RE.exec(lines[i]);
        if (openMatch) {
            const opener = openMatch[1];
            const lang = openMatch[2] ?? '';
            fenceOrdinal++;
            i += 1;
            const codeLines: string[] = [];
            while (i < lines.length) {
                const closeMatch = FENCE_CLOSE_RE.exec(lines[i]);
                if (
                    closeMatch &&
                    closeMatch[1][0] === opener[0] &&
                    closeMatch[1].length >= opener.length
                ) {
                    i += 1;
                    break;
                }
                codeLines.push(lines[i]);
                i += 1;
            }
            // If the loop exited because we reached EOF without a closer
            // (streaming mid-fence), we still render the partial block —
            // the JSX </code></pre> is the virtual closure.
            const codeText = codeLines.join('\n');

            // Token precedence:
            //   1. Final hash-keyed tokens (stream-end CODE_TOKENS round-trip) — full block
            //   2. Streaming ordinal tokens (STREAM_CODE_TOKENS per-line pushes) — partial
            //   3. Plain text fallback
            const finalTokens = codeTokens?.[hashCodeBlock(lang, codeText)];
            const streamTokens = streamingCodeTokens?.[fenceOrdinal];

            let content: ReactNode;
            if (finalTokens) {
                content = renderTokenLines(finalTokens);
            } else if (streamTokens) {
                // Progressive: lines with tokens are lit; the in-progress tail is plain.
                content = renderZippedLines(codeLines, streamTokens);
            } else {
                content = codeText;
            }

            out.push(
                <pre key={`f-${key++}`} className="ws-md-pre">
                    <code className={lang ? `language-${lang}` : undefined}>
                        {content}
                    </code>
                </pre>,
            );
            continue;
        }

        // Prose paragraph: collect contiguous non-fence non-empty lines, then
        // a blank line breaks the paragraph.
        const para: string[] = [];
        while (
            i < lines.length &&
            lines[i].trim().length > 0 &&
            !FENCE_OPEN_RE.test(lines[i])
        ) {
            para.push(lines[i]);
            i += 1;
        }
        if (para.length > 0) {
            out.push(
                <p key={`p-${key++}`} className="ws-md-p">
                    {renderInlineRuns(para.join('\n'))}
                </p>,
            );
        }
        // Skip blank lines (paragraph separators).
        while (i < lines.length && lines[i].trim().length === 0) {
            i += 1;
        }
    }

    return out;
}

// ── Inline-level scan: greedy left-to-right marker search ───────────────────

interface MatchHit {
    start: number;
    end: number;
    kind: 'code' | 'bold' | 'italic' | 'strike' | 'link';
    text: string;
    href?: string;
}

function findNextMarker(src: string, from: number): MatchHit | null {
    // Inline code: `…` (single backtick). Earlier than other markers because
    // it suppresses emphasis inside.
    let best: MatchHit | null = null;
    const consider = (hit: MatchHit | null): void => {
        if (!hit) { return; }
        if (best === null || hit.start < best.start) { best = hit; }
    };

    consider(findDelim(src, from, '`',  '`',  'code'));
    consider(findDelim(src, from, '**', '**', 'bold'));
    consider(findDelim(src, from, '__', '__', 'bold'));
    consider(findDelim(src, from, '~~', '~~', 'strike'));
    consider(findDelim(src, from, '*',  '*',  'italic'));
    consider(findDelim(src, from, '_',  '_',  'italic'));
    consider(findLink(src, from));

    return best;
}

function findDelim(
    src: string, from: number, open: string, close: string,
    kind: 'code' | 'bold' | 'italic' | 'strike',
): MatchHit | null {
    const start = src.indexOf(open, from);
    if (start < 0) { return null; }
    // Don't match `*` if it's part of `**` (let bold handle it).
    if (open === '*' && (src[start + 1] === '*' || src[start - 1] === '*')) {
        return null;
    }
    if (open === '_' && (src[start + 1] === '_' || src[start - 1] === '_')) {
        return null;
    }
    if (open === '`' && src[start + 1] === '`') {
        return null;       // multi-backtick — leave for inline-code fallback
    }
    const innerStart = start + open.length;
    const closeIdx = src.indexOf(close, innerStart);
    if (closeIdx < 0) { return null; }
    return {
        start,
        end: closeIdx + close.length,
        kind,
        text: src.slice(innerStart, closeIdx),
    };
}

function findLink(src: string, from: number): MatchHit | null {
    const lbr = src.indexOf('[', from);
    if (lbr < 0) { return null; }
    const rbr = src.indexOf(']', lbr + 1);
    if (rbr < 0 || src[rbr + 1] !== '(') { return null; }
    const rpar = src.indexOf(')', rbr + 2);
    if (rpar < 0) { return null; }
    return {
        start: lbr,
        end: rpar + 1,
        kind: 'link',
        text: src.slice(lbr + 1, rbr),
        href: src.slice(rbr + 2, rpar),
    };
}

function renderInlineRuns(src: string): ReactNode[] {
    const out: ReactNode[] = [];
    let cursor = 0;
    let key = 0;
    while (cursor < src.length) {
        const hit = findNextMarker(src, cursor);
        if (!hit) {
            out.push(renderPlainWithBreaks(src.slice(cursor), key++));
            break;
        }
        if (hit.start > cursor) {
            out.push(renderPlainWithBreaks(src.slice(cursor, hit.start), key++));
        }
        switch (hit.kind) {
            case 'code':
                out.push(<code key={key++} className="ws-md-code">{hit.text}</code>);
                break;
            case 'bold':
                out.push(<strong key={key++}>{renderInlineRuns(hit.text)}</strong>);
                break;
            case 'italic':
                out.push(<em key={key++}>{renderInlineRuns(hit.text)}</em>);
                break;
            case 'strike':
                out.push(<del key={key++}>{renderInlineRuns(hit.text)}</del>);
                break;
            case 'link':
                // LLM-emitted URLs are untrusted — render as a non-clickable
                // span (visually still styled as a link) to avoid drive-by
                // navigation. Users can copy-paste if needed.
                out.push(
                    <span key={key++} className="ws-md-link" title={hit.href ?? ''}>
                        {hit.text}
                    </span>,
                );
                break;
        }
        cursor = hit.end;
    }
    return out;
}

function renderPlainWithBreaks(text: string, baseKey: number): ReactNode {
    // Preserve hard newlines inside paragraphs as <br/>.
    const parts = text.split('\n');
    if (parts.length === 1) { return parts[0]; }
    return (
        <>
            {parts.map((p, i) => (
                <span key={`${baseKey}-${i}`}>
                    {p}
                    {i < parts.length - 1 && <br />}
                </span>
            ))}
        </>
    );
}
