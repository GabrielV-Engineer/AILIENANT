import './workspace.css';
import { createRoot } from 'react-dom/client';
import { Workspace } from './Workspace';
import type { AilienantConfig } from '../shared/types';

interface InitialAttrs {
    sessionId: string;
    sessionTitle: string;
    config: AilienantConfig | null;
    logoUri: string;
}

function readInitial(root: HTMLElement): InitialAttrs {
    const raw = root.dataset.initial;
    const defaults: InitialAttrs = {
        sessionId: 'default',
        sessionTitle: 'Untitled session',
        config: null,
        logoUri: root.dataset.logo ?? '',
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
