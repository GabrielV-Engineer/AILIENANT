import { Icon } from '../../shared/Icon';
import { Tooltip } from '../../shared/Tooltip';
import type { IndexingState } from '../../shared/types';

interface Props {
    state: IndexingState;
}

export function IndexingStatus({ state }: Props): JSX.Element {
    if (state.state === 'idle') {
        return (
            <Tooltip content="Awaiting workspace — open a folder and AILIENANT will begin indexing.">
                <div className="ws-indexing" data-state="idle">
                    <span className="ws-indexing-dot" />
                    <span>Awaiting workspace</span>
                </div>
            </Tooltip>
        );
    }

    if (state.state === 'indexing') {
        const pct = Math.max(0, Math.min(100, Math.round(state.pct)));
        const detail = state.total_files
            ? `${state.files_indexed ?? 0}/${state.total_files} files`
            : `${pct}%`;
        return (
            <Tooltip content={`Building the GraphRAG semantic index for the workspace. ${detail}.`}>
                <div className="ws-indexing" data-state="indexing">
                    <Icon name="loader" size={12} className="ws-indexing-spin" />
                    <span>Indexing · {pct}%</span>
                    <span className="ws-indexing-bar">
                        <span className="ws-indexing-fill" style={{ width: `${pct}%` }} />
                    </span>
                </div>
            </Tooltip>
        );
    }

    if (state.state === 'error') {
        return (
            <Tooltip content={state.reason}>
                <div className="ws-indexing" data-state="error">
                    <Icon name="alert" size={12} />
                    <span>Waiting for AI configuration</span>
                </div>
            </Tooltip>
        );
    }

    return (
        <Tooltip content={`GraphRAG ready. ${state.node_count.toLocaleString()} nodes indexed. Semantic queries available.`}>
            <div className="ws-indexing" data-state="ready">
                <span className="ws-indexing-dot" />
                <span>GraphRAG ready · {state.node_count.toLocaleString()} nodes</span>
            </div>
        </Tooltip>
    );
}
