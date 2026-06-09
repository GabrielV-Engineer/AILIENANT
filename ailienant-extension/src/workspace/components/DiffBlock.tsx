import { memo, useCallback, useMemo, useState } from 'react';
import ReactDiffViewer, { type ReactDiffViewerStylesOverride } from 'react-diff-viewer-continued';
import { diffLines } from 'diff';
import type { ASTToken, DiffBlockShape } from '../../shared/config';
import { scopeColor } from '../utils/scopeColor';
import type { HitlRespond } from '../utils/useHitlResponder';
import { DiffHitlActions } from './DiffHitlActions';

interface DiffBlockProps {
    block: DiffBlockShape;
    // ADR-724 — when this turn is awaiting authorization, the inline per-patch
    // HITL action row is shown under the diff and `Ctrl+Enter`/`Esc` on the
    // focused diff Accept/Reject. Absent on already-applied (read-only) diffs.
    hitlActive?: boolean;
    onRespond?: HitlRespond;
    // Decline + re-submit the feedback so the agent re-proposes. Required
    // alongside `onRespond` whenever the inline action row is live.
    onRequestChanges?: (feedback: string) => void;
}

// Beyond this many changed lines we stop handing the full file to the viewer.
// The cap bounds the *mounted DOM*, not just what's visible — see truncate().
const DIFF_RENDER_LINE_CAP = 400;
// When a giant diff is truncated, keep this much unchanged context around the
// changed region so the preview still reads in place.
const TRUNCATION_CONTEXT_LINES = 3;

interface Truncated {
    oldValue: string;
    newValue: string;
    truncated: boolean;
    changedLineCount: number;
}

/**
 * M1 — memory-level truncation. We must NOT hand a 2,000-line file to the diff
 * viewer and hide the overflow with CSS: the viewer would still mount every row
 * and freeze the main thread. Instead, if the changed-line count exceeds the
 * cap, we slice the *strings* down to the changed hunks (plus a little context)
 * in JavaScript before they ever reach the viewer, so the mounted node count is
 * bounded by the cap rather than the file size. The full diff is opt-in.
 */
function truncate(oldValue: string, newValue: string): Truncated {
    const parts = diffLines(oldValue, newValue);
    let changedLineCount = 0;
    for (const p of parts) {
        if (p.added || p.removed) {
            changedLineCount += p.count ?? (p.value ? p.value.split('\n').length : 0);
        }
    }
    if (changedLineCount <= DIFF_RENDER_LINE_CAP) {
        return { oldValue, newValue, truncated: false, changedLineCount };
    }

    // Rebuild a compact pair of strings: every changed hunk in full, unchanged
    // runs collapsed to a few context lines on each side. This keeps the diff
    // semantically aligned while capping how many lines the viewer must render.
    const oldOut: string[] = [];
    const newOut: string[] = [];
    parts.forEach((p, idx) => {
        const lines = p.value.replace(/\n$/, '').split('\n');
        if (p.added) {
            newOut.push(...lines);
        } else if (p.removed) {
            oldOut.push(...lines);
        } else {
            const isFirst = idx === 0;
            const isLast = idx === parts.length - 1;
            let ctx = lines;
            if (lines.length > TRUNCATION_CONTEXT_LINES * 2) {
                const head = isFirst ? [] : lines.slice(0, TRUNCATION_CONTEXT_LINES);
                const tail = isLast ? [] : lines.slice(-TRUNCATION_CONTEXT_LINES);
                ctx = [...head, ...tail];
            }
            oldOut.push(...ctx);
            newOut.push(...ctx);
        }
    });
    return {
        oldValue: oldOut.join('\n'),
        newValue: newOut.join('\n'),
        truncated: true,
        changedLineCount,
    };
}

/** VS Code injects a theme class on <body>; everything but explicit light is dark. */
function isDarkTheme(): boolean {
    if (typeof document === 'undefined') { return true; }
    return !document.body.classList.contains('vscode-light');
}

// Diagonal hatching for the empty side of an unbalanced hunk — a theme-neutral
// low-alpha gradient over whatever the editor background is, so the reader's
// spatial anchor across the split never collapses.
const HATCH = {
    backgroundImage:
        'repeating-linear-gradient(45deg, transparent, transparent 5px,' +
        ' var(--vscode-editorWhitespace-foreground, rgba(127,127,127,0.18)) 5px,' +
        ' var(--vscode-editorWhitespace-foreground, rgba(127,127,127,0.18)) 6px)',
} as const;

// All diff backgrounds bind to the editor's own diff palette so a light/dark
// theme switch repaints without reload (ADR-722) — never hard-coded reds/greens.
// The same palette is supplied for both `dark` and `light` because the values
// are CSS vars the host resolves per active theme.
const DIFF_PALETTE = {
    diffViewerBackground: 'var(--vscode-editor-background, #0D1117)',
    diffViewerColor: 'var(--vscode-editor-foreground, #C9D1D9)',
    addedBackground: 'var(--vscode-diffEditor-insertedTextBackground, rgba(46,160,67,0.15))',
    removedBackground: 'var(--vscode-diffEditor-removedTextBackground, rgba(248,81,73,0.15))',
    wordAddedBackground: 'var(--vscode-diffEditor-insertedTextBackground, rgba(46,160,67,0.4))',
    wordRemovedBackground: 'var(--vscode-diffEditor-removedTextBackground, rgba(248,81,73,0.4))',
    gutterBackground: 'var(--vscode-editor-background, #0D1117)',
    gutterBackgroundDark: 'var(--vscode-editor-background, #0D1117)',
    codeFoldBackground: 'var(--vscode-editorWidget-background, #161B22)',
    codeFoldGutterBackground: 'var(--vscode-editorWidget-background, #161B22)',
} as const;

const DIFF_STYLES: ReactDiffViewerStylesOverride = {
    variables: { dark: DIFF_PALETTE, light: DIFF_PALETTE },
    emptyLine: HATCH,
    emptyGutter: HATCH,
    contentText: { fontFamily: 'var(--vscode-editor-font-family, monospace)' },
    lineNumber: { color: 'var(--vscode-editorLineNumber-foreground, #6E7681)' },
};

// Map each source line's text to its host-tokenized scope spans. The diff viewer
// hands `renderContent` a raw line string with no index, so we key by content: a
// line tokenizes identically wherever it appears, added lines exist only on the new
// side and removed only on the old, so a single merged map has no harmful collision.
// Keying by content (not row index) also survives truncate()'s string rebuild —
// every rendered line is still a verbatim source line, so its key resolves.
// Exported for the phase checkpoint gate (the diff highlighting contract).
export function buildTokenMap(block: DiffBlockShape): Map<string, ASTToken[]> | undefined {
    const { old_content, new_content, old_ast_lines, new_ast_lines } = block;
    if (!old_ast_lines && !new_ast_lines) { return undefined; }
    const map = new Map<string, ASTToken[]>();
    const add = (content: string, ast: ASTToken[][] | undefined): void => {
        if (!ast) { return; }
        const lines = content.split('\n');
        const n = Math.min(lines.length, ast.length);
        for (let i = 0; i < n; i++) {
            if (!map.has(lines[i])) { map.set(lines[i], ast[i]); }
        }
    };
    add(old_content, old_ast_lines);
    add(new_content, new_ast_lines);
    return map;
}

function DiffBlockInner({ block, hitlActive, onRespond, onRequestChanges }: DiffBlockProps): JSX.Element {
    const { file_path, old_content, new_content, status } = block;
    const [showFull, setShowFull] = useState(false);
    const dark = isDarkTheme();

    // Scoped keyboard: only fires when this diff has focus AND an approval is
    // pending. Unlike the Natt card's global document listener, this stays on the
    // element so it never hijacks the composer or double-fires with the card.
    const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
        if (!hitlActive || !onRespond) { return; }
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            e.stopPropagation();
            onRespond(true);
        } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            onRespond(false);
        }
    }, [hitlActive, onRespond]);

    const view = useMemo(
        () => (showFull
            ? { oldValue: old_content, newValue: new_content, truncated: false, changedLineCount: 0 }
            : truncate(old_content, new_content)),
        [old_content, new_content, showFull],
    );

    const badge = status === 'create' ? 'Create' : 'Edit';

    // Host-tokenized syntax spans (filled by the grammar engine before the diff is
    // posted). The webview only paints them — it carries no grammar dependency.
    const tokenMap = useMemo(
        () => buildTokenMap(block),
        [block.old_content, block.new_content, block.old_ast_lines, block.new_ast_lines],
    );
    // Per-line renderer for the split grid: look up the line's scope spans and paint
    // them with VS Code CSS vars (theme-reactive); fall back to the raw line when a
    // file wasn't tokenized. Undefined when there are no tokens, so an untokenized
    // diff renders exactly as before.
    const renderContent = useMemo(() => {
        if (!tokenMap) { return undefined; }
        return (source: string): JSX.Element => {
            const tokens = tokenMap.get(source);
            if (!tokens || tokens.length === 0) { return <>{source}</>; }
            return (
                <>
                    {tokens.map((t, i) => (
                        <span key={i} style={{ color: scopeColor(t.type) }}>{t.content}</span>
                    ))}
                </>
            );
        };
    }, [tokenMap]);

    return (
        <div
            className="ws-diff"
            data-status={status}
            data-hitl={hitlActive ? 'true' : 'false'}
            tabIndex={hitlActive ? 0 : undefined}
            onKeyDown={onKeyDown}
        >
            <div className="ws-diff-header">
                <span className="ws-diff-badge" data-status={status}>{badge}</span>
                <span className="ws-diff-path">{file_path}</span>
            </div>
            <div className="ws-diff-body">
                <ReactDiffViewer
                    oldValue={view.oldValue}
                    newValue={view.newValue}
                    splitView={true}
                    useDarkTheme={dark}
                    showDiffOnly={true}
                    extraLinesSurroundingDiff={TRUNCATION_CONTEXT_LINES}
                    hideLineNumbers={false}
                    styles={DIFF_STYLES}
                    // Paint host-tokenized syntax spans per line. Word-level diffing
                    // is disabled because it would split a line into fragments and
                    // call renderContent per fragment, breaking the per-line token
                    // mapping; we trade intra-line word shading for full syntax color.
                    // Line-level add/remove backgrounds are unaffected.
                    renderContent={renderContent}
                    disableWordDiff={true}
                    // The library defaults to a Web Worker for diff math, whose
                    // blob: worker URL is blocked by this webview's CSP. Force the
                    // synchronous fallback so the diff actually computes.
                    disableWorker={true}
                />
            </div>
            {view.truncated && !showFull && (
                <button
                    type="button"
                    className="ws-diff-loadfull"
                    onClick={() => setShowFull(true)}
                >
                    Load full diff ({view.changedLineCount} changed lines)
                </button>
            )}
            {hitlActive && onRespond && onRequestChanges && (
                <DiffHitlActions onRespond={onRespond} onRequestChanges={onRequestChanges} />
            )}
        </div>
    );
}

// M3 — DiffBlock does heavy diff math, and Workspace re-renders on every composer
// keystroke. Memoize on the stable identity of the block so typing in the chat
// never reconciles a rendered diff. The block object is replaced by reference
// only when its own content changes (the reducer builds a new array immutably).
export const DiffBlock = memo(DiffBlockInner, (a, b) =>
    a.block.patch_id === b.block.patch_id &&
    a.block.file_path === b.block.file_path &&
    a.block.old_content === b.block.old_content &&
    a.block.new_content === b.block.new_content &&
    a.block.status === b.block.status &&
    // Token arrays are populated host-side before the block is posted; compare by
    // reference so a (re)enriched block repaints its highlighting.
    a.block.old_ast_lines === b.block.old_ast_lines &&
    a.block.new_ast_lines === b.block.new_ast_lines &&
    // Compare only the stable HITL primitive; `onRespond` is a stable useCallback
    // from the parent, so a composer keystroke that re-renders Workspace never
    // reconciles a read-only diff (M3 — preserved from 7.14.2).
    !!a.hitlActive === !!b.hitlActive,
);
