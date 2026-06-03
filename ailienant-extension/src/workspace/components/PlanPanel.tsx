/**
 * Rich Plan side-panel (ADR-732).
 *
 * Renders a finalized MissionSpecification as a structured document — outcome
 * prose, the scope / constraints / decisions / checks sections, and the Work
 * Breakdown Structure as a table of steps — instead of a markdown-flattened chat
 * bubble. File paths (each WBS step's target_file, and scope entries that look
 * like paths) render as clickable blue links; clicking one asks the host to open
 * that file in the editor via the `onOpenFile` callback (the parent owns the
 * `vscode.postMessage` so this component stays transport-decoupled, matching
 * ToolChip's `onRetry` shape).
 *
 * The component is a pure function of its props and tolerates empty sections —
 * an ideation-stub plan can carry an empty WBS, so every list renders gracefully
 * when absent.
 */
import { memo, useState } from 'react';
import type { PlanDocumentShape } from '../../shared/config';
import { Icon } from '../../shared/Icon';
import { MarkdownRenderer } from './MarkdownRenderer';

interface Props {
    plan: PlanDocumentShape;
    onOpenFile: (path: string) => void;
    onClose: () => void;
}

// A scope entry is treated as a file-link when it looks like a path (has a slash
// or a dotted extension) rather than free prose like "Out: the auth module".
function looksLikePath(s: string): boolean {
    return /[\\/]/.test(s) || /\.[A-Za-z0-9]{1,8}$/.test(s.trim());
}

function FileLink({ path, onOpenFile }: { path: string; onOpenFile: (p: string) => void }): JSX.Element {
    return (
        <button
            type="button"
            className="ws-plan-filelink"
            title={`Open ${path}`}
            onClick={() => onOpenFile(path)}
        >
            {path}
        </button>
    );
}

function Section({ title, items }: { title: string; items: string[] }): JSX.Element | null {
    if (!items || items.length === 0) { return null; }
    return (
        <section className="ws-plan-section">
            <h4 className="ws-plan-section-title">{title}</h4>
            <ul className="ws-plan-list">
                {items.map((it, i) => (
                    <li key={i}><MarkdownRenderer content={it} parserState={undefined} streaming={false} /></li>
                ))}
            </ul>
        </section>
    );
}

export const PlanPanel = memo(function PlanPanel({ plan, onOpenFile, onClose }: Props): JSX.Element {
    const [collapsed, setCollapsed] = useState<boolean>(false);

    // Scope entries that name files become links; the rest stay as prose.
    const scopePaths = plan.scope.filter(looksLikePath);
    const scopeProse = plan.scope.filter((s) => !looksLikePath(s));

    return (
        <aside className={`ws-plan-panel${collapsed ? ' ws-plan-collapsed' : ''}`} aria-label="Plan">
            <header className="ws-plan-header">
                <button
                    type="button"
                    className="ws-plan-toggle"
                    aria-label={collapsed ? 'Expand plan' : 'Collapse plan'}
                    onClick={() => setCollapsed((c) => !c)}
                >
                    <Icon name={collapsed ? 'chevron-right' : 'chevron-down'} />
                </button>
                <span className="ws-plan-title">Plan</span>
                <button type="button" className="ws-plan-close" aria-label="Hide plan" onClick={onClose}>
                    <Icon name="x" />
                </button>
            </header>

            {!collapsed && (
                <div className="ws-plan-body">
                    {plan.outcome && (
                        <section className="ws-plan-section">
                            <h4 className="ws-plan-section-title">Outcome</h4>
                            <div className="ws-plan-outcome">
                                <MarkdownRenderer content={plan.outcome} parserState={undefined} streaming={false} />
                            </div>
                        </section>
                    )}

                    {(scopePaths.length > 0 || scopeProse.length > 0) && (
                        <section className="ws-plan-section">
                            <h4 className="ws-plan-section-title">Scope</h4>
                            {scopePaths.length > 0 && (
                                <ul className="ws-plan-list">
                                    {scopePaths.map((p, i) => (
                                        <li key={`p${i}`}><FileLink path={p} onOpenFile={onOpenFile} /></li>
                                    ))}
                                </ul>
                            )}
                            {scopeProse.length > 0 && (
                                <ul className="ws-plan-list">
                                    {scopeProse.map((s, i) => (
                                        <li key={`s${i}`}>{s}</li>
                                    ))}
                                </ul>
                            )}
                        </section>
                    )}

                    <Section title="Constraints" items={plan.constraints} />
                    <Section title="Decisions" items={plan.decisions} />

                    {plan.tasks.length > 0 && (
                        <section className="ws-plan-section">
                            <h4 className="ws-plan-section-title">Work breakdown</h4>
                            <ol className="ws-plan-wbs">
                                {plan.tasks.map((t) => (
                                    <li key={t.step_number} className="ws-plan-step">
                                        <div className="ws-plan-step-head">
                                            <span className="ws-plan-step-num">{t.step_number}</span>
                                            <span className="ws-plan-chip ws-plan-chip-role">{t.target_role}</span>
                                            <span className="ws-plan-chip ws-plan-chip-action">{t.action}</span>
                                            {t.target_file && <FileLink path={t.target_file} onOpenFile={onOpenFile} />}
                                        </div>
                                        {t.description && (
                                            <div className="ws-plan-step-desc">
                                                <MarkdownRenderer content={t.description} parserState={undefined} streaming={false} />
                                            </div>
                                        )}
                                    </li>
                                ))}
                            </ol>
                        </section>
                    )}

                    <Section title="Acceptance checks" items={plan.checks} />
                </div>
            )}
        </aside>
    );
});
