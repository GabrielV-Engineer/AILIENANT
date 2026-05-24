import { useState, useEffect } from 'react';
import { Icon } from '../../shared/Icon';

interface Props {
    steps: string[];
    done?: boolean;
}

/**
 * Phase 7.9.B.14 — collapsible "Thinking" execution trace.
 *
 * Collapsed by default: a muted single line showing a spinner + the currently
 * executing node. Click to expand the full vertical node stepper. On completion
 * the spinner becomes a checkmark, the label switches to the step count, and the
 * block auto-collapses — while remaining re-expandable so the user can inspect
 * any past turn's execution trace. Rendered per assistant turn (not chat content).
 */
export function PipelineProgress({ steps, done = false }: Props): JSX.Element | null {
    const [expanded, setExpanded] = useState(false);
    useEffect(() => { if (done) { setExpanded(false); } }, [done]);   // auto-collapse on completion
    if (steps.length === 0) {
        return null;
    }

    const latest = steps[steps.length - 1].replace(/_/g, ' ');
    const label = done
        ? `Task completed in ${steps.length} step${steps.length === 1 ? '' : 's'}`
        : `Executing: ${latest}`;

    return (
        <div className={`ws-thinking${done ? ' is-done' : ''}`}>
            <button
                type="button"
                className="ws-thinking-header"
                aria-expanded={expanded}
                onClick={() => setExpanded(e => !e)}
            >
                {done
                    ? <Icon name="check" size={12} className="ws-thinking-check" />
                    : <Icon name="loader" size={12} className="ws-thinking-spin" />}
                <span className="ws-thinking-label">{label}</span>
                <Icon
                    name={expanded ? 'chevron-down' : 'chevron-right'}
                    size={12}
                    className="ws-thinking-chevron"
                />
            </button>
            {expanded && (
                <ol className="ws-thinking-steps">
                    {steps.map((s, i) => (
                        <li
                            key={i}
                            className="ws-thinking-step"
                            data-current={!done && i === steps.length - 1 ? 'true' : 'false'}
                        >
                            <span className="ws-thinking-step-dot" />
                            <span className="ws-thinking-step-name">{s.replace(/_/g, ' ')}</span>
                        </li>
                    ))}
                </ol>
            )}
        </div>
    );
}
