import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Icon, type IconName } from '../../shared/Icon';
import { vscode } from '../vscode_bridge';
import { ModelsMenu, type ModelsView } from './ModelsMenu';
import { CustomizeMenu, type CustomizeView } from './CustomizeMenu';
import { SkillsMenu, type SkillsView } from './SkillsMenu';
import type { ReasoningPreset } from '../../shared/config';

type SubView = ModelsView | CustomizeView | SkillsView;
const MODELS_VIEWS: ModelsView[] = ['llm-config', 'usage', 'preset', 'thinking'];
const SKILLS_VIEWS: SkillsView[] = ['skills-insert', 'skills-create'];

interface MenuItem {
    key: string;
    cmd: string;       // slash hint, used for filtering + display
    label: string;
    desc: string;
    icon: IconName;
    run: () => void;
    opensView?: boolean; // keep menu open (nested view)
}

interface MenuSection {
    id: string;
    title: string;     // e.g. "/context — Context"
    items: MenuItem[];
}

interface Props {
    query: string;
    activeTaskId?: string;
    preset: ReasoningPreset;
    onPresetChange: (p: ReasoningPreset) => void;
    /** Host-side `ailienant.developerMode`. Gates the Developer section, whose
     *  command runs arbitrary shell in the workspace root. */
    developerMode: boolean;
    onOpenContext: () => void;
    onClose: () => void;
    /** Dismissal that is not a command decision (a press outside the menu).
     *  Separate from `onClose` because `onClose` also clears a half-typed slash
     *  command — correct for Esc, destructive for an incidental click away. */
    onDismiss: () => void;
}

const VIEW_TITLES: Record<SubView, string> = {
    'llm-config':    'LLM configuration',
    usage:           'Account & Usage',
    preset:          'Switch model preset',
    thinking:        'Native Thinking',
    permissions:     'Permissions',
    'output-styles': 'Output styles',
    agents:          'Agents',
    hooks:           'Hooks',
    mcp:             'MCP Servers',
    'skills-insert': 'Insert skill',
    'skills-create': 'Create skill',
};

export function CommandPalette({
    query, activeTaskId, preset, onPresetChange, developerMode,
    onOpenContext, onClose, onDismiss,
}: Props): JSX.Element | null {
    const [view, setView] = useState<'root' | SubView>('root');
    const [focused, setFocused] = useState(0);
    const rootRef = useRef<HTMLDivElement | null>(null);

    const post = useCallback((message: Record<string, unknown>) => {
        vscode.postMessage(message);
    }, []);

    // Dismiss on a pointer press outside the menu. `pointerdown` (not `click`) so
    // the menu is gone before the press lands on whatever is underneath, and
    // capture-phase so a child that stops propagation cannot trap the menu open.
    // Two regions are exempt: the trigger button (whose own click toggles, and
    // would otherwise re-open what this just closed) and the prompt input row
    // (the menu doubles as slash autocomplete for the text being typed there).
    useEffect(() => {
        const onPointerDown = (e: PointerEvent): void => {
            const root = rootRef.current;
            if (!root || !(e.target instanceof Element) || root.contains(e.target)) { return; }
            if (e.target.closest('[data-palette-trigger], .ws-prompt-input-row')) { return; }
            onDismiss();
        };
        document.addEventListener('pointerdown', onPointerDown, true);
        return () => document.removeEventListener('pointerdown', onPointerDown, true);
    }, [onDismiss]);

    const sections = useMemo<MenuSection[]>(() => [
        {
            id: 'context',
            title: '/context — Context',
            items: [
                { key: 'ctx-attach',  cmd: '/context attach',  label: 'Attach file',        desc: 'Add files, folders, or terminal output to context', icon: 'plus',     run: onOpenContext },
                { key: 'ctx-mention', cmd: '/context mention', label: 'Mention file',       desc: 'Reference a project file inline (@path)',           icon: 'search',   run: () => post({ type: 'MENTION_FILE' }) },
                { key: 'ctx-clear',   cmd: '/context clear',   label: 'Clear conversation', desc: 'Clear the chat window and short-term memory',       icon: 'trash',    run: () => post({ type: 'CLEAR_CONVERSATION' }) },
                { key: 'ctx-rewind',  cmd: '/context rewind',  label: 'Time-travel',        desc: 'Branch this session from any historical checkpoint (Phase 7.11.8)', icon: 'clock',
                  run: () => post({ type: 'LIST_CHECKPOINTS', session_id: activeTaskId ?? '' }) },
            ],
        },
        {
            id: 'models',
            title: '/models — Brain',
            items: [
                { key: 'mdl-config', cmd: '/models llm-config',    label: 'LLM configuration',   desc: 'Tier routing, effort budget, and reasoning preset', icon: 'network', opensView: true, run: () => setView('llm-config') },
                { key: 'mdl-usage',  cmd: '/models usage',         label: 'Account & Usage',     desc: 'Token counts and estimated cost this session',  icon: 'wallet',  opensView: true, run: () => setView('usage') },
                { key: 'mdl-preset', cmd: '/models preset',        label: 'Switch model preset', desc: 'Apply a saved model configuration preset',     icon: 'sparkles',  opensView: true, run: () => setView('preset') },
                { key: 'mdl-think',  cmd: '/models thinking',      label: 'Native Thinking',     desc: 'Toggle real-time reasoning stream (on by default)', icon: 'brain', opensView: true, run: () => setView('thinking') },
                { key: 'mdl-cfg',    cmd: '/models configure',     label: 'Configure models…',   desc: 'Open the dashboard BYOM panel',                icon: 'plug',    run: () => post({ type: 'OPEN_DASHBOARD', tab: 'byom' }) },
            ],
        },
        {
            id: 'customize',
            title: '/customize — Customize',
            items: [
                { key: 'cz-styles', cmd: '/customize output-styles', label: 'Output styles', desc: 'Concise, explanatory, or code-only responses', icon: 'pencil', opensView: true, run: () => setView('output-styles') },
                { key: 'cz-agents', cmd: '/customize agents',        label: 'Agents',        desc: 'Edit orchestrator and sub-agent prompts',     icon: 'bot',    opensView: true, run: () => setView('agents') },
                { key: 'cz-hooks',  cmd: '/customize hooks',         label: 'Hooks',         desc: 'Scripts run around file writes',              icon: 'zap',    opensView: true, run: () => setView('hooks') },
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
        // Developer smoke command: runs an arbitrary shell command in the
        // workspace root, so it is opt-in behind `ailienant.developerMode`
        // rather than offered to every user. Future tools follow the same
        // `INVOKE_TRACKED_BASH` shape.
        ...(developerMode ? [{
            id: 'dev',
            title: '/dev — Developer',
            items: [
                {
                    key: 'dev-run-bash',
                    cmd: '/dev run-bash',
                    label: 'Run tracked bash (smoke)',
                    desc: 'Run a one-shot sandbox_bash and render it as a Rich Tool Chip',
                    icon: 'terminal' as IconName,
                    run: () => post({ type: 'PROMPT_FOR_BASH' }),
                },
            ],
        }] : []),
    ], [activeTaskId, developerMode, onOpenContext, post]);

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
            <div ref={rootRef} className="ws-palette ws-menu" role="dialog" aria-label={VIEW_TITLES[view]}>
                <button
                    className="ws-menu-back"
                    onClick={() => setView('root')}
                    aria-label={`Back to command menu from ${VIEW_TITLES[view]}`}
                >
                    <Icon name="chevron-right" size={13} className="ws-menu-back-icon" />
                    <span>{VIEW_TITLES[view]}</span>
                </button>
                {isModels ? (
                    <ModelsMenu
                        view={view as ModelsView}
                        preset={preset}
                        onPresetChange={onPresetChange}
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

    // ── Root sectioned list ──────────────────────────────────────
    // An unmatched query previously returned null, blanking the menu with no
    // explanation; it now renders an empty state instead.
    if (flat.length === 0) {
        return (
            <div ref={rootRef} className="ws-palette ws-menu" role="dialog" aria-label="Command menu">
                <div className="ws-palette-hint">Command menu · Esc to close</div>
                <div className="ws-menu-empty">No commands match “{query}”.</div>
            </div>
        );
    }

    const focusedId = `ws-cmd-${flat[focused]?.key ?? flat[0].key}`;
    let runningIndex = -1;
    return (
        <div ref={rootRef} className="ws-palette ws-menu">
            <div className="ws-palette-hint">Command menu · ↑↓ navigate · Enter to run · Esc to close</div>
            {/* The listbox wraps only the options: arrow-key focus lives in React
                state, so `aria-activedescendant` is what makes that focus audible
                to a screen reader. The section headers stay outside the role. */}
            <div role="listbox" aria-label="Command menu" aria-activedescendant={focusedId}>
                {visibleSections.map(section => (
                    <div key={section.id} className="ws-menu-section" role="group" aria-label={section.title}>
                        <div className="ws-mode-label ws-menu-section-title">{section.title}</div>
                        {section.items.map(item => {
                            runningIndex += 1;
                            const idx = runningIndex;
                            return (
                                <button
                                    key={item.key}
                                    id={`ws-cmd-${item.key}`}
                                    className="ws-palette-item ws-menu-item"
                                    data-focused={idx === focused ? 'true' : 'false'}
                                    role="option"
                                    aria-selected={idx === focused}
                                    onMouseEnter={() => setFocused(idx)}
                                    onClick={() => execute(item)}
                                >
                                    <Icon name={item.icon} size={14} className="ws-menu-item-icon" />
                                    <span className="ws-menu-item-text">
                                        <span className="ws-menu-item-label">{item.label}</span>
                                        <span className="ws-palette-desc">{item.desc}</span>
                                    </span>
                                    {item.opensView && <Icon name="chevron-right" size={13} />}
                                </button>
                            );
                        })}
                    </div>
                ))}
            </div>
        </div>
    );
}

export function useSlashDetect(value: string): { slashActive: boolean; slashQuery: string } {
    const match = /^\/(.*)/.exec(value);
    return { slashActive: match !== null, slashQuery: match ? match[1] : '' };
}
