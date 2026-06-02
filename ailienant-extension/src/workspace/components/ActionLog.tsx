/**
 * Ghost Telemetry — live action-log (ADR-723).
 *
 * A muted, while-you-wait stream of the tool invocations in flight, shown
 * beside the Thought Box only while the assistant turn is streaming. It exists
 * to fill the status-less gap between the reasoning collapse and the answer
 * render so the user sees motion rather than a silent spinner.
 *
 * It is a pure DERIVED view of the turn's `toolCalls` — no own state, no new
 * message. Once the stream ends the parent stops rendering it and the permanent
 * ToolChip stack becomes the canonical, inspectable record; this log never tries
 * to be that record (no output, no retry, no expand).
 *
 * Memoised on the stable shape of the calls (id + status) so a composer
 * keystroke that re-renders Workspace never reconciles the log.
 */
import { memo } from 'react';
import type { ToolCallShape } from '../../shared/config';

interface Props {
    toolCalls: ToolCallShape[];
}

const MAX_DETAIL_CHARS = 60;

/**
 * Pick the single most informative argument to show next to the tool name.
 * Different tools key their primary target on different arg names; we probe the
 * common ones in priority order and fall back to nothing rather than dumping a
 * whole args blob into the muted one-liner.
 */
function primaryArg(args: Record<string, unknown>): string {
    const candidate =
        args.file_path ??
        args.path ??
        args.command ??
        args.pattern ??
        args.query ??
        args.url ??
        args.cmd;
    if (typeof candidate !== 'string' || candidate.length === 0) {
        return '';
    }
    return candidate.length > MAX_DETAIL_CHARS
        ? candidate.slice(0, MAX_DETAIL_CHARS - 1) + '…'
        : candidate;
}

function ActionLogImpl({ toolCalls }: Props): JSX.Element {
    return (
        <ul className="ws-action-log" aria-label="Live tool activity">
            {toolCalls.map(tc => {
                const detail = primaryArg(tc.args);
                return (
                    <li key={tc.tool_call_id} className="ws-action-log-row">
                        <span className="ws-status-dot" data-status={tc.status} aria-hidden="true" />
                        <span className="ws-action-log-name">{tc.tool_name}</span>
                        {detail && <span className="ws-action-log-detail">{detail}</span>}
                    </li>
                );
            })}
        </ul>
    );
}

/**
 * Re-render only when the visible shape changes: a call is added, or one of the
 * existing calls flips status. The args/output churn that doesn't alter this
 * surface is ignored, so streaming output into a chip doesn't reconcile the log.
 */
export const ActionLog = memo(ActionLogImpl, (a, b) => {
    if (a.toolCalls.length !== b.toolCalls.length) { return false; }
    for (let i = 0; i < a.toolCalls.length; i++) {
        const x = a.toolCalls[i];
        const y = b.toolCalls[i];
        if (x.tool_call_id !== y.tool_call_id || x.status !== y.status) { return false; }
    }
    return true;
});
