import './workspace.css';
import { createRoot } from 'react-dom/client';
import { Workspace } from './Workspace';
import type { Message, NattMessage } from './Workspace';
import type { AilienantConfig } from '../shared/types';
import type { BudgetLimitMode, OrchestrationMode } from '../shared/config';

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
    createRoot(root).render(<Workspace initial={initial} />);
});
