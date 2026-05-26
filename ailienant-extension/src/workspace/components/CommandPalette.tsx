import { useState, useEffect, useCallback, useMemo } from 'react';
import { Icon, type IconName } from '../../shared/Icon';
import { vscode } from '../vscode_bridge';
import { ModelsMenu, type ModelsView } from './ModelsMenu';
import { CustomizeMenu, type CustomizeView } from './CustomizeMenu';
import { SkillsMenu, type SkillsView } from './SkillsMenu';
import type { AilienantConfig } from '../../shared/types';
import type { OrchestrationMode } from '../../shared/config';

type SubView = ModelsView | CustomizeView | SkillsView;
const MODELS_VIEWS: ModelsView[] = ['switch', 'orchestration', 'usage', 'preset'];
const SKILLS_VIEWS: SkillsView[] = ['skills-insert', 'skills-create'];

interface MenuItem {
    key: string;
    cmd: string;       // slash hint, used for filtering + display
    label: string;
    desc: string;
    icon: IconName;
    run: () => void;
    opensView?: boolean; // keep menu open (nested view)
    soon?: boolean;      // disabled "Coming soon" placeholder
}

interface MenuSection {
    id: string;
    title: string;     // e.g. "/context — Context"
    items: MenuItem[];
}

interface Props {
    query: string;
    activeTaskId?: string;
    config: AilienantConfig | null;
    activeModelId: string;
    orchestrationMode: OrchestrationMode;
    onPrefChange: (activeModelId: string, orchestrationMode: OrchestrationMode) => void;
    onOpenContext: () => void;
    onClose: () => void;
}

const VIEW_TITLES: Record<SubView, string> = {
    switch:          'Switch model',
    orchestration:   'Orchestration mode',
    usage:           'Account & Usage',
    preset:          'Switch model preset',
    permissions:     'Permissions',
    'output-styles': 'Output styles',
    agents:          'Agents',
    hooks:           'Hooks',
    mcp:             'MCP Servers',
    'skills-insert': 'Insert skill',
    'skills-create': 'Create skill',
};

export function CommandPalette({
    query, activeTaskId, config, activeModelId, orchestrationMode, onPrefChange, onOpenContext, onClose,
}: Props): JSX.Element | null {
    const [view, setView] = useState<'root' | SubView>('root');
    const [focused, setFocused] = useState(0);

    const post = useCallback((message: Record<string, unknown>) => {
        vscode.postMessage(message);
    }, []);

    const sections = useMemo<MenuSection[]>(() => [
        {
            id: 'context',
            title: '/context — Context',
            items: [
                { key: 'ctx-attach',  cmd: '/context attach',  label: 'Attach file',        desc: 'Add files, folders, or terminal output to context', icon: 'plus',     run: onOpenContext },
                { key: 'ctx-mention', cmd: '/context mention', label: 'Mention file',       desc: 'Reference a project file inline (@path)',           icon: 'search',   run: () => post({ type: 'MENTION_FILE' }) },
                { key: 'ctx-clear',   cmd: '/context clear',   label: 'Clear conversation', desc: 'Clear the chat window and short-term memory',       icon: 'trash',    run: () => post({ type: 'CLEAR_CONVERSATION' }) },
                { key: 'ctx-rewind',  cmd: '/context rewind',  label: 'Rewind',             desc: 'Roll back the agent graph to its last checkpoint',  icon: 'clock',    run: () => post({ type: 'SUBMIT_TASK', value: `/context rewind ${activeTaskId ?? ''}` }) },
            ],
        },
        {
            id: 'models',
            title: '/models — Brain',
            items: [
                { key: 'mdl-switch',  cmd: '/models switch',        label: 'Switch model',        desc: 'Pick the active model from discovered list',   icon: 'cpu',     opensView: true, run: () => setView('switch') },
                { key: 'mdl-orch',   cmd: '/models orchestration', label: 'Orchestration mode',  desc: 'Manual single-model vs. auto tier routing',    icon: 'network', opensView: true, run: () => setView('orchestration') },
                { key: 'mdl-usage',  cmd: '/models usage',         label: 'Account & Usage',     desc: 'Token counts and estimated cost this session',  icon: 'wallet',  opensView: true, run: () => setView('usage') },
                { key: 'mdl-preset', cmd: '/models preset',        label: 'Switch model preset', desc: 'Apply a saved model configuration preset',     icon: 'sparkles',  opensView: true, run: () => setView('preset') },
                { key: 'mdl-cfg',    cmd: '/models configure',     label: 'Configure models…',   desc: 'Open the dashboard BYOM panel',                icon: 'plug',    run: () => post({ type: 'OPEN_DASHBOARD', tab: 'byom' }) },
            ],
        },
        {
            id: 'customize',
            title: '/customize — Customize',
            items: [
                { key: 'cz-styles', cmd: '/customize output-styles', label: 'Output styles', desc: 'Concise, explanatory, or code-only responses', icon: 'pencil', opensView: true, run: () => setView('output-styles') },
                { key: 'cz-agents', cmd: '/customize agents',        label: 'Agents',        desc: 'Edit orchestrator and sub-agent prompts',     icon: 'bot',    opensView: true, run: () => setView('agents') },
                { key: 'cz-hooks',  cmd: '/customize hooks',         label: 'Hooks',         desc: 'Pre/post-execution scripts',                  icon: 'zap',    opensView: true, run: () => setView('hooks') },
                { key: 'cz-memory', cmd: '/customize memory',        label: 'Memory',        desc: 'Open the Vector/RAG management panel',         icon: 'brain',  run: () => post({ type: 'OPEN_DASHBOARD', tab: 'memory' }) },
                { key: 'cz-perms',  cmd: '/customize permissions',   label: 'Permissions',   desc: 'Grant or revoke HITL permissions',            icon: 'shield', opensView: true, run: () => setView('permissions') },
                { key: 'cz-mcp',    cmd: '/customize mcp',           label: 'MCP Servers',   desc: 'Model Context Protocol server config',         icon: 'plug',   opensView: true, run: () => setView('mcp') },
                { key: 'cz-panel',  cmd: '/customize control-panel', label: 'AILIENANT Control Panel', desc: 'Open the full web dashboard',         icon: 'gauge',  run: () => post({ type: 'OPEN_DASHBOARD' }) },
            ],
        },
        {
            id: 'skills',
            title: '/skills — Skills',
            items: [
                { key: 'sk-insert', cmd: '/skills insert', label: 'Insert skill', desc: 'Insert a saved prompt template into the prompt', icon: 'plus',   opensView: true, run: () => setView('skills-insert') },
                { key: 'sk-create', cmd: '/skills create', label: 'Create skill', desc: 'Author and save a reusable prompt template',     icon: 'pencil', opensView: true, run: () => setView('skills-create') },
            ],
        },
        {
            id: 'settings',
            title: '/settings — Settings',
            items: [
                { key: 'set-general', cmd: '/settings general', label: 'General configurations', desc: 'Open AILIENANT settings in VS Code', icon: 'settings', run: () => post({ type: 'OPEN_SETTINGS' }) },
            ],
        },
        {
            id: 'support',
            title: '/support — Support',
            items: [
                { key: 'sup-docs', cmd: '/support help', label: 'Help documents', desc: 'Open the technical documentation', icon: 'external-link', run: () => post({ type: 'OPEN_DOCS' }) },
            ],
        },
        {
            // Phase 7.11.6 — developer-only smoke command to exercise the
            // Rich Tool Chips pipeline end-to-end without an agent rewrite.
            // Future tools follow the same `INVOKE_TRACKED_BASH` shape.
            id: 'dev',
            title: '/dev — Developer',
            items: [
                {
                    key: 'dev-run-bash',
                    cmd: '/dev run-bash',
                    label: 'Run tracked bash (smoke)',
                    desc: 'Run a one-shot sandbox_bash and render it as a Rich Tool Chip',
                    icon: 'terminal',
                    run: () => post({ type: 'PROMPT_FOR_BASH' }),
                },
            ],
        },
    ], [activeTaskId, onOpenContext, post]);

    const q = query.toLowerCase();
    const visibleSections = useMemo<MenuSection[]>(() => {
        if (!q) { return sections; }
        return sections
            .map(s => ({ ...s, items: s.items.filter(i =>
                i.cmd.toLowerCase().includes(q) || i.label.toLowerCase().includes(q) || i.desc.toLowerCase().includes(q)) }))
            .filter(s => s.items.length > 0);
    }, [sections, q]);

    const flat = useMemo<MenuItem[]>(() => visibleSections.flatMap(s => s.items), [visibleSections]);

    useEffect(() => { setFocused(0); }, [query, view]);

    const execute = useCallback((item: MenuItem) => {
        if (item.soon) { return; }
        item.run();
        if (!item.opensView) { onClose(); }
    }, [onClose]);

    useEffect(() => {
        const onKey = (e: KeyboardEvent): void => {
            if (e.key === 'Escape') {
                e.preventDefault();
                if (view !== 'root') { setView('root'); } else { onClose(); }
                return;
            }
            if (view !== 'root') { return; }
            if (flat.length === 0) { return; }
            if (e.key === 'ArrowDown') { e.preventDefault(); setFocused(f => (f + 1) % flat.length); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setFocused(f => (f - 1 + flat.length) % flat.length); }
            else if (e.key === 'Enter') { e.preventDefault(); execute(flat[focused]); }
        };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [flat, focused, execute, onClose, view]);

    // ── Nested sub-views (Models / Customize / Skills) ───────────
    if (view !== 'root') {
        const isModels = (MODELS_VIEWS as string[]).includes(view);
        const isSkills = (SKILLS_VIEWS as string[]).includes(view);
        return (
            <div className="ws-palette ws-menu" role="dialog" aria-label={VIEW_TITLES[view]}>
                <button className="ws-menu-back" onClick={() => setView('root')} aria-label="Back">
                    <Icon name="chevron-right" size={13} className="ws-menu-back-icon" />
                    <span>{VIEW_TITLES[view]}</span>
                </button>
                {isModels ? (
                    <ModelsMenu
                        view={view as ModelsView}
                        config={config}
                        activeModelId={activeModelId}
                        orchestrationMode={orchestrationMode}
                        onPrefChange={onPrefChange}
                        onClose={onClose}
                    />
                ) : isSkills ? (
                    <SkillsMenu view={view as SkillsView} onClose={onClose} onSwitchView={(v) => setView(v)} />
                ) : (
                    <CustomizeMenu view={view as CustomizeView} onClose={onClose} />
                )}
            </div>
        );
    }

    if (flat.length === 0) { return null; }

    // ── Root sectioned list ──────────────────────────────────────
    let runningIndex = -1;
    return (
        <div className="ws-palette ws-menu" role="listbox" aria-label="Command menu">
            <div className="ws-palette-hint">Command menu · ↑↓ navigate · Enter to run · Esc to close</div>
            {visibleSections.map(section => (
                <div key={section.id} className="ws-menu-section">
                    <div className="ws-mode-label ws-menu-section-title">{section.title}</div>
                    {section.items.map(item => {
                        runningIndex += 1;
                        const idx = runningIndex;
                        return (
                            <button
                                key={item.key}
                                className="ws-palette-item ws-menu-item"
                                data-focused={idx === focused ? 'true' : 'false'}
                                data-soon={item.soon ? 'true' : 'false'}
                                role="option"
                                aria-selected={idx === focused}
                                aria-disabled={item.soon ? 'true' : undefined}
                                onMouseEnter={() => setFocused(idx)}
                                onClick={() => execute(item)}
                            >
                                <Icon name={item.icon} size={14} className="ws-menu-item-icon" />
                                <span className="ws-menu-item-text">
                                    <span className="ws-menu-item-label">{item.label}</span>
                                    <span className="ws-palette-desc">{item.desc}</span>
                                </span>
                                {item.soon && <span className="ws-menu-soon">Soon</span>}
                                {item.opensView && !item.soon && <Icon name="chevron-right" size={13} />}
                            </button>
                        );
                    })}
                </div>
            ))}
        </div>
    );
}

export function useSlashDetect(value: string): { slashActive: boolean; slashQuery: string } {
    const match = /^\/(.*)/.exec(value);
    return { slashActive: match !== null, slashQuery: match ? match[1] : '' };
}
