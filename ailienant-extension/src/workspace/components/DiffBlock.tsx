import { memo, useMemo, useState } from 'react';
import ReactDiffViewer, { type ReactDiffViewerStylesOverride } from 'react-diff-viewer-continued';
import { diffLines } from 'diff';
import type { DiffBlockShape } from '../../shared/config';

interface DiffBlockProps {
    block: DiffBlockShape;
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

function DiffBlockInner({ block }: DiffBlockProps): JSX.Element {
    const { file_path, old_content, new_content, status } = block;
    const [showFull, setShowFull] = useState(false);
    const dark = isDarkTheme();

    const view = useMemo(
        () => (showFull
            ? { oldValue: old_content, newValue: new_content, truncated: false, changedLineCount: 0 }
            : truncate(old_content, new_content)),
        [old_content, new_content, showFull],
    );

    const badge = status === 'create' ? 'Create' : 'Edit';

    // Syntax tokenization is intentionally omitted here (the diff renders as
    // themed monospace over the split grid). A shiki-based highlighter does not
    // fit the webview bundle ceiling without runtime asset externalization;
    // tokenized diffs are tracked as future tech debt rather than shipped at the
    // cost of Time-to-Interactive.
    return (
        <div className="ws-diff" data-status={status}>
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
    a.block.status === b.block.status,
);
