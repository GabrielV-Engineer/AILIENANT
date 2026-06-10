/**
 * Glass-box audit log for the autonomous agentic cell.
 *
 * Renders each iteration of the cell loop as a collapsible accordion section:
 * the tool calls it issued, the (sanitized) terminal output they produced, and the
 * AST edits it applied — capped by a budget-governor footer. It is a pure derived
 * view of the turn's `cellRun` artifact; it owns no stream state and never mutates.
 *
 * The newest iteration auto-expands while the turn streams and collapses as the
 * next one begins, while staying re-expandable so any past iteration can be
 * inspected. The terminal panel is row-virtualized (see useWindowedRows) so a
 * multi-thousand-line build log stays at 60 FPS without flooding the DOM.
 */
import { memo, useEffect, useState } from 'react';
import { Icon, type IconName } from '../../shared/Icon';
import type { CellIterationShape, CellRunShape } from '../../shared/config';
import { useWindowedRows } from '../utils/useWindowedRows';

// Must match the line-height of .ws-cell-pty-line in workspace.css.
const PTY_ROW_HEIGHT = 16;
// Render every line below this count; virtualize above it.
const PTY_WINDOW_THRESHOLD = 1000;

interface Props {
    run: CellRunShape;
    streaming: boolean;
}

function toolIcon(toolName: string): IconName {
    if (toolName === 'run_terminal') { return 'terminal'; }
    if (toolName === 'apply_granular_edit') { return 'pencil'; }
    if (toolName === 'read_file_ast') { return 'file'; }
    return 'zap';
}

/** Row-virtualized terminal panel, scrolled within its own container. */
function PtyPanel({ lines }: { lines: string[] }): JSX.Element {
    const { scrollRef, onScroll, startIndex, endIndex, topPad, bottomPad } =
        useWindowedRows(lines.length, PTY_ROW_HEIGHT, PTY_WINDOW_THRESHOLD);
    const slice = lines.slice(startIndex, endIndex);
    return (
        <div className="ws-cell-pty-scroll" ref={scrollRef} onScroll={onScroll}>
            {topPad > 0 && <div style={{ height: topPad }} aria-hidden="true" />}
            {slice.map((line, i) => (
                <div key={startIndex + i} className="ws-cell-pty-line">
                    {line.length > 0 ? line : ' '}
                </div>
            ))}
            {bottomPad > 0 && <div style={{ height: bottomPad }} aria-hidden="true" />}
        </div>
    );
}

function IterationSection({ it, autoOpen }: { it: CellIterationShape; autoOpen: boolean }): JSX.Element {
    const [expanded, setExpanded] = useState(autoOpen);
    // Follow the auto-open signal (newest-while-streaming) but let a manual toggle
    // override until the signal next changes.
    useEffect(() => { setExpanded(autoOpen); }, [autoOpen]);

    const toolNames = it.tools.map((t) => t.tool_name).join(', ') || 'thinking';
    const gov = it.governor;

    return (
        <div className="ws-cell-iter">
            <button
                type="button"
                className="ws-cell-iter-header"
                aria-expanded={expanded}
                onClick={() => setExpanded((e) => !e)}
            >
                <Icon name={expanded ? 'chevron-down' : 'chevron-right'} size={12} className="ws-cell-iter-chevron" />
                <span className="ws-cell-iter-num">#{it.iteration + 1}</span>
                <span className="ws-cell-iter-tools">{toolNames}</span>
                {gov && (
                    <span className="ws-cell-iter-meta">
                        ${gov.cost_usd.toFixed(4)} · {gov.elapsed_s.toFixed(1)}s
                    </span>
                )}
                {gov?.axis && (
                    <span className="ws-cell-iter-axis" title={`Budget axis exhausted: ${gov.axis}`}>
                        <Icon name="alert" size={11} /> {gov.axis}
                    </span>
                )}
            </button>
            {expanded && (
                <div className="ws-cell-iter-body">
                    {it.tools.length > 0 && (
                        <ul className="ws-cell-tools">
                            {it.tools.map((t, i) => {
                                const arg =
                                    t.args_scrubbed.command ??
                                    t.args_scrubbed.path ??
                                    Object.values(t.args_scrubbed)[0] ??
                                    '';
                                return (
                                    <li key={i} className="ws-cell-tool">
                                        <Icon name={toolIcon(t.tool_name)} size={12} />
                                        <span className="ws-cell-tool-name">{t.tool_name}</span>
                                        {arg && <code className="ws-cell-tool-arg">{arg}</code>}
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                    {it.pty.length > 0 && <PtyPanel lines={it.pty} />}
                    {it.diffs.length > 0 && (
                        <ul className="ws-cell-diffs">
                            {it.diffs.map((d, i) => (
                                <li key={i} className="ws-cell-diff">
                                    <div className="ws-cell-diff-path">
                                        <Icon name="pencil" size={11} /> {d.path}
                                    </div>
                                    {d.search && <pre className="ws-cell-diff-search">{d.search}</pre>}
                                    <pre className="ws-cell-diff-replace">{d.replace}</pre>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            )}
        </div>
    );
}

function CellAuditWidgetImpl({ run, streaming }: Props): JSX.Element | null {
    if (run.iterations.length === 0) { return null; }
    const lastIdx = run.iterations.length - 1;
    return (
        <div className="ws-cell-audit" aria-label="Agentic cell audit log">
            {run.iterations.map((it, i) => (
                <IterationSection key={it.iteration} it={it} autoOpen={streaming && i === lastIdx} />
            ))}
        </div>
    );
}

/**
 * Re-render only when the visible shape changes: a new iteration, a streaming flag
 * flip, or growth in any iteration's tools / pty / diffs / governor. Output churn
 * that does not alter these counts (e.g. a composer keystroke re-rendering the
 * parent) is ignored.
 */
export const CellAuditWidget = memo(CellAuditWidgetImpl, (a, b) => {
    if (a.streaming !== b.streaming) { return false; }
    const ai = a.run.iterations;
    const bi = b.run.iterations;
    if (ai.length !== bi.length) { return false; }
    for (let i = 0; i < ai.length; i++) {
        const x = ai[i];
        const y = bi[i];
        if (x.pty.length !== y.pty.length) { return false; }
        if (x.tools.length !== y.tools.length) { return false; }
        if (x.diffs.length !== y.diffs.length) { return false; }
        if (!!x.governor !== !!y.governor) { return false; }
        if (x.governor?.axis !== y.governor?.axis) { return false; }
    }
    return true;
});
