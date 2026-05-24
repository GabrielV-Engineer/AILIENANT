import { Icon } from '../../shared/Icon';

interface Props {
    steps: string[];
}

/**
 * Phase 7.9.B.12 — ephemeral LangGraph pipeline ticker.
 *
 * Renders the live sequence of completed nodes while a task runs. This is NOT
 * chat content: it is cleared the moment the real assistant answer streams in
 * (server_token_chunk) or the stream ends. Keeps the internal execution trace
 * out of the conversation transcript.
 */
export function PipelineProgress({ steps }: Props): JSX.Element | null {
    if (steps.length === 0) {
        return null;
    }
    const latest = steps[steps.length - 1];
    return (
        <div className="ws-pipeline" role="status" aria-live="polite">
            <Icon name="loader" size={12} className="ws-pipeline-spin" />
            <span className="ws-pipeline-label">Running pipeline</span>
            <span className="ws-pipeline-node">{latest.replace(/_/g, ' ')}</span>
            <span className="ws-pipeline-count">{steps.length} step{steps.length === 1 ? '' : 's'}</span>
        </div>
    );
}
