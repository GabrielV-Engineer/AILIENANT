import './workspace.css';
import { createRoot } from 'react-dom/client';
import { Workspace } from './Workspace';
import type { Message, NattMessage } from './Workspace';
import type { AilienantConfig } from '../shared/types';
import type { BudgetLimitMode, OrchestrationMode } from '../shared/config';
import { ErrorBoundary } from './components/ErrorBoundary';

/**
 * Last-resort fallback for a catastrophic render fault that escapes every per-row
 * boundary. A root trip has already unmounted the live transcript, so recovery
 * remounts from the creation-time snapshot; the host re-posts the authoritative
 * transcript the next time the panel regains visibility. Nothing is lost — the
 * debounced PERSIST_TRANSCRIPT keeps the host copy current.
 */
function WorkspaceCrashPanel({ error, reset }: { error: Error; reset: () => void }): JSX.Element {
    return (
        <div className="ws-crash-panel" role="alert">
            <div className="ws-crash-title">AILIENANT hit a rendering error.</div>
            <div className="ws-crash-detail">{error.message}</div>
            <div className="ws-crash-actions">
                <button className="ws-crash-btn" onClick={reset}>Try again</button>
                <button className="ws-crash-btn ws-crash-btn-primary" onClick={() => location.reload()}>
                    Reload panel
                </button>
            </div>
        </div>
    );
}

interface InitialAttrs {
    sessionId:        string;
    sessionTitle:     string;
    config:           AilienantConfig | null;
    logoUri:          string;
    budgetLimitMode:  BudgetLimitMode;
    budgetWeeklyUsd:  number;
    budgetMonthlyUsd: number;
    activeModelId:    string;
    orchestrationMode: OrchestrationMode;
    workspaceFolder:  string;
    initialMessages:     Message[];      // Phase 7.9.B.20 — restored chat transcript
    initialNattMessages: NattMessage[];  // Phase 7.9.B.20 — restored analyst transcript
}

function readInitial(root: HTMLElement): InitialAttrs {
    const raw = root.dataset.initial;
    const defaults: InitialAttrs = {
        sessionId:        'default',
        sessionTitle:     'Untitled session',
        config:           null,
        logoUri:          root.dataset.logo ?? '',
        budgetLimitMode:  'none',
        budgetWeeklyUsd:  20,
        budgetMonthlyUsd: 50,
        activeModelId:    '',
        orchestrationMode: 'auto',
        workspaceFolder:  '',
        initialMessages:     [],
        initialNattMessages: [],
    };
    if (!raw) { return defaults; }
    try {
        return { ...defaults, ...(JSON.parse(raw) as Partial<InitialAttrs>) };
    } catch {
        return defaults;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (!root) { return; }
    const initial = readInitial(root);
    createRoot(root).render(
        <ErrorBoundary
            label="workspace-root"
            fallback={(error, reset) => <WorkspaceCrashPanel error={error} reset={reset} />}
        >
            <Workspace initial={initial} />
        </ErrorBoundary>,
    );
});
