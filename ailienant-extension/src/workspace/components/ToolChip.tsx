/**
 * Phase 7.11.6 (ADR-706 §4.5f) — Stateful Rich Tool Chip.
 *
 * Renders one tracked tool invocation as a collapsible card:
 *   - HEADER: tool name + status pill + duration + Retry button + expander.
 *   - BODY (when expanded):
 *       • Output tab — ANSI-decoded mini-terminal (the headline UX).
 *       • Args tab   — pretty-printed JSON of the original invocation args.
 *       • Graph tab  — DepGraphView (only when `tc.dep_graph` is present).
 *
 * Security mandate (the most important part):
 *   - The output stream is treated as UNTRUSTED. We DO NOT pass it to
 *     `dangerouslySetInnerHTML`. Instead we run it through `parseAnsi` to get
 *     structured `{text, classes, style}` runs and render each run as a React
 *     `<span>` whose text is set via the JSX text node (React auto-escapes)
 *     and whose `style.color` (truecolor case) is bounded to `rgb()` literals
 *     produced by the parser. The DOMPurify sanitizer is plumbed in for any
 *     callers that DO want to inject HTML — currently none on this surface,
 *     but the seatbelt is there.
 *
 * Retry semantics (the second-most important part):
 *   - Clicking Retry posts a `{ type: 'RETRY_TOOL', tool_call_id }` host
 *     message — the backend looks up the spec and re-invokes verbatim.
 *   - For tools with `side_effect_free === false` (default for sandbox_bash),
 *     a confirmation step is required before the message fires. We use a
 *     simple two-step click pattern: the first click flips the button into
 *     "Confirm?" mode; a second click within 3 seconds dispatches; otherwise
 *     the button reverts. No modal dialog → no focus management complexity,
 *     no overlay layering pitfalls.
 */
import { memo, useCallback, useEffect, useMemo, useState } from 'react';
import type { ToolCallShape } from '../../shared/config';
import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import {
    INITIAL_STATE as ANSI_INITIAL,
    parseAnsi,
    type AnsiRun,
    type AnsiStyleState,
} from '../utils/ansiParser';
import { DepGraphView } from './DepGraphView';

interface Props {
    tc: ToolCallShape;
    onRetry: (toolCallId: string) => void;
}

type TabId = 'output' | 'args' | 'graph';

export const ToolChip = memo(function ToolChip({ tc, onRetry }: Props): JSX.Element {
    // Auto-expand on failure so the user sees the error without an extra click.
    const [expanded, setExpanded] = useState<boolean>(tc.status === 'error');
    const [activeTab, setActiveTab] = useState<TabId>('output');
    const [confirmingRetry, setConfirmingRetry] = useState<boolean>(false);

    // Auto-revert the Retry-confirm state after 3 seconds of inactivity so a
    // stray click doesn't leave the button stuck in the "Confirm?" phase.
    useEffect(() => {
        if (!confirmingRetry) { return; }
        const t = setTimeout(() => setConfirmingRetry(false), 3000);
        return () => clearTimeout(t);
    }, [confirmingRetry]);

    const handleRetry = useCallback(() => {
        if (tc.side_effect_free === false && !confirmingRetry) {
            setConfirmingRetry(true);
            return;
        }
        setConfirmingRetry(false);
        onRetry(tc.tool_call_id);
    }, [tc.side_effect_free, tc.tool_call_id, confirmingRetry, onRetry]);

    return (
        <div
            className="ws-tool-chip"
            data-status={tc.status}
            data-expanded={expanded ? 'true' : 'false'}
        >
            <header className="ws-tool-chip-head">
                <Icon name="terminal" size={12} />
                <span className="ws-tool-chip-name">{tc.tool_name}</span>
                <span className="ws-tool-chip-status" data-status={tc.status}>
                    {tc.status}
                </span>
                {typeof tc.exit_code === 'number' && (
                    <span className="ws-tool-chip-exit" title={`exit code ${tc.exit_code}`}>
                        exit {tc.exit_code}
                    </span>
                )}
                {typeof tc.duration_ms === 'number' && (
                    <span className="ws-tool-chip-dur" aria-label="duration">
                        {tc.duration_ms}ms
                    </span>
                )}
                <span className="ws-tool-chip-spacer" />
                {tc.status !== 'pending' && (
                    <Tooltip
                        content={
                            confirmingRetry
                                ? 'Click again to confirm (side-effects possible)'
                                : 'Re-run this tool with the same arguments'
                        }
                        side="top"
                    >
                        <button
                            className="ws-tool-chip-retry"
                            data-confirming={confirmingRetry ? 'true' : 'false'}
                            onClick={handleRetry}
                            aria-label={confirmingRetry ? 'Confirm retry' : 'Retry tool'}
                            type="button"
                        >
                            {confirmingRetry ? '⟳ confirm?' : '⟳'}
                        </button>
                    </Tooltip>
                )}
                <button
                    className="ws-tool-chip-toggle"
                    onClick={() => setExpanded(v => !v)}
                    aria-label={expanded ? 'Collapse tool output' : 'Expand tool output'}
                    aria-expanded={expanded}
                    type="button"
                >
                    {expanded ? '▾' : '▸'}
                </button>
            </header>

            {expanded && (
                <div className="ws-tool-chip-body">
                    <nav className="ws-tool-chip-tabs" role="tablist">
                        <ToolChipTab id="output" active={activeTab} setActive={setActiveTab}>
                            Output
                        </ToolChipTab>
                        <ToolChipTab id="args" active={activeTab} setActive={setActiveTab}>
                            Args
                        </ToolChipTab>
                        {tc.dep_graph && (
                            <ToolChipTab id="graph" active={activeTab} setActive={setActiveTab}>
                                Graph
                            </ToolChipTab>
                        )}
                    </nav>
                    {activeTab === 'output' && (
                        <AnsiTerminal lines={tc.output_lines} />
                    )}
                    {activeTab === 'args' && (
                        <pre className="ws-tool-chip-args">
                            {JSON.stringify(tc.args, null, 2)}
                        </pre>
                    )}
                    {activeTab === 'graph' && tc.dep_graph && (
                        <DepGraphView graph={tc.dep_graph} />
                    )}
                </div>
            )}
        </div>
    );
});

interface TabProps {
    id: TabId;
    active: TabId;
    setActive: (id: TabId) => void;
    children: React.ReactNode;
}

function ToolChipTab({ id, active, setActive, children }: TabProps): JSX.Element {
    return (
        <button
            type="button"
            role="tab"
            aria-selected={active === id}
            data-active={active === id ? 'true' : 'false'}
            className="ws-tool-chip-tab"
            onClick={() => setActive(id)}
        >
            {children}
        </button>
    );
}

// ── Inline mini-terminal: ANSI runs → safe JSX spans ───────────────────────

interface AnsiTerminalProps {
    lines: string[];
}

const AnsiTerminal = memo(function AnsiTerminal({ lines }: AnsiTerminalProps): JSX.Element {
    // Parse all incoming chunks once per props update. The parser is O(n)
    // over the input and the chip output is bounded by the backend's
    // 2 KB truncation rule, so cost is negligible.
    const allRuns = useMemo(() => {
        let state: AnsiStyleState = ANSI_INITIAL;
        const runs: AnsiRun[] = [];
        for (const chunk of lines) {
            const result = parseAnsi(chunk, state);
            runs.push(...result.runs);
            state = result.state;
        }
        // If the stream ended with an unclosed ANSI sequence, the
        // `partial_escape` is preserved on `state` but the renderer doesn't
        // need it — there's no "next chunk" to feed.
        return runs;
    }, [lines]);

    if (allRuns.length === 0) {
        return (
            <pre className="ws-mini-terminal ws-mini-terminal-empty">
                <span className="ws-mini-terminal-placeholder">
                    (no output)
                </span>
            </pre>
        );
    }

    return (
        <pre className="ws-mini-terminal">
            {allRuns.map((run, i) => {
                const className = run.classes.length > 0 ? run.classes.join(' ') : undefined;
                if (run.style) {
                    return (
                        <span key={i} className={className} style={run.style}>
                            {run.text}
                        </span>
                    );
                }
                if (className) {
                    return <span key={i} className={className}>{run.text}</span>;
                }
                // No style → emit a plain text node directly.
                return <span key={i}>{run.text}</span>;
            })}
        </pre>
    );
});
