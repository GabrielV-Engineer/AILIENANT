import './dashboard.css';
// @ts-ignore — esbuild resolves .svg as dataurl string; tsc doesn't know this
import logoUrl from './logo.svg';
import { createRoot } from 'react-dom/client';
import { useMemo, useState, lazy, Suspense } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import { Icon, type IconName } from '../shared/Icon';
import { Tooltip as AiTooltip } from '../shared/Tooltip';
import { useSidebarCollapsed } from './hooks/useSidebarCollapsed';
import { useKeyboardShortcuts } from './hooks/useKeyboardShortcuts';
import { ShortcutsOverlay, type ShortcutHint } from './ui/ShortcutsOverlay';
import { ProjectSelector } from './ui/ProjectSelector';
import { ActiveProjectProvider } from './hooks/useActiveProject';

// ── Lazy-loaded heavy panels (Monaco code splitting) ─────────
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore — esbuild resolves .tsx without extension; tsc Node16 requires .js
const StagingArea = lazy(() => import('./panels/StagingArea'));

// ── Eagerly-loaded panels ────────────────────────────────────
import { MemoryManagement } from './panels/MemoryManagement';
import { BYOMPanel }    from './panels/BYOMPanel';
import { RulesPanel }   from './panels/RulesPanel';
import { AuditPanel }   from './panels/AuditPanel';
import { HardwarePanel } from './panels/HardwarePanel';
import { OverviewPanel } from './panels/OverviewPanel';
import { ExtensionsPanel } from './panels/ExtensionsPanel';
import { TelemetryPanel } from './panels/TelemetryPanel';
import { RuntimePanel } from './panels/RuntimePanel';
import { RecoveryPanel } from './panels/RecoveryPanel';

type PanelId = 'overview' | 'hardware' | 'memory' | 'byom' | 'rules' | 'staging' | 'audit' | 'extensions' | 'telemetry' | 'runtime' | 'recovery';

interface NavItem { id: PanelId; icon: IconName; label: string; desc: string; }
interface NavGroup { label: string; items: NavItem[]; }

// Panels grouped by operational intent. The flat order (top-to-bottom across
// groups) also defines the 1–9 keyboard-jump index.
const NAV_GROUPS: NavGroup[] = [
    {
        label: 'Monitoring',
        items: [
            { id: 'overview',  icon: 'gauge',     label: 'Overview',          desc: 'At-a-glance summary of cost, MCP, HITL and activity' },
            { id: 'hardware',  icon: 'cpu',       label: 'Hardware Monitor',  desc: 'Live RAM/VRAM gauges and execution mode' },
            { id: 'runtime',   icon: 'zap',       label: 'Runtime',           desc: 'Sandbox tier, Docker status and lifecycle controls' },
            { id: 'memory',    icon: 'network',   label: 'Memory Management', desc: 'Interactive GraphRAG node viewer' },
            { id: 'telemetry', icon: 'telescope', label: 'Telemetry',         desc: 'Token cost snapshot and routing decision log' },
        ],
    },
    {
        label: 'Configuration',
        items: [
            { id: 'byom',       icon: 'plug',      label: 'BYOM Models',        desc: 'Manage local & cloud model endpoints' },
            { id: 'extensions', icon: 'wand',      label: 'Extensions',         desc: 'MCP servers and reusable skill templates' },
            { id: 'rules',      icon: 'clipboard', label: 'Rules & Governance', desc: 'Directory rules and custom instructions' },
        ],
    },
    {
        label: 'Operations',
        items: [
            { id: 'staging',  icon: 'eye',    label: 'Staging Area', desc: 'HITL review of pending patches' },
            { id: 'audit',    icon: 'shield', label: 'Audit Ledger', desc: 'Blake2b-verified HITL audit log' },
            { id: 'recovery', icon: 'alert',  label: 'Task Recovery', desc: 'Resume crashed tasks from the dead-letter queue' },
        ],
    },
];

const FLAT_NAV: NavItem[] = NAV_GROUPS.flatMap(g => g.items);

const SHORTCUTS: ShortcutHint[] = [
    { keys: ['Ctrl', 'B'], label: 'Toggle the sidebar' },
    { keys: ['1', '–', '9'], label: 'Jump to a panel' },
    { keys: ['?'], label: 'Show this help' },
    { keys: ['Esc'], label: 'Close this dialog' },
];

function StagingAreaSkeleton(): JSX.Element {
    return (
        <div style={{ padding: 20 }}>
            <div className="db-card">
                <div style={{ height: 24, width: '40%', borderRadius: 4, background: 'var(--bg-hover)', marginBottom: 12 }} />
                <div style={{ height: 400, borderRadius: 4, background: 'var(--bg-hover)' }} />
            </div>
        </div>
    );
}

function getInitialPanel(): PanelId {
    const requested = new URLSearchParams(window.location.search).get('tab');
    const ids = FLAT_NAV.map(n => n.id) as string[];
    return requested && ids.includes(requested) ? (requested as PanelId) : 'overview';
}

function Dashboard(): JSX.Element {
    const [activePanel, setActivePanel] = useState<PanelId>(getInitialPanel);
    const [helpOpen, setHelpOpen] = useState(false);
    const { collapsed, toggle } = useSidebarCollapsed();

    useKeyboardShortcuts({
        onToggleSidebar: toggle,
        onSelectIndex: (i) => { const item = FLAT_NAV[i]; if (item) { setActivePanel(item.id); } },
        onToggleHelp: () => setHelpOpen(o => !o),
    });

    const collapseTip = useMemo(() => (collapsed ? 'Expand sidebar (Ctrl+B)' : 'Collapse sidebar (Ctrl+B)'), [collapsed]);

    return (
        <Tooltip.Provider delayDuration={400} skipDelayDuration={150}>
            <div className="db-layout" data-collapsed={collapsed ? 'true' : 'false'}>
                <header className="db-header">
                    <AiTooltip content={collapseTip} side="bottom">
                        <button
                            className="db-collapse-btn"
                            onClick={toggle}
                            aria-label={collapseTip}
                            aria-pressed={collapsed}
                        >
                            <Icon name={collapsed ? 'panel-right-open' : 'panel-right-close'} size={16} />
                        </button>
                    </AiTooltip>
                    <img src={logoUrl as string} alt="AILIENANT" className="db-header-logo-img" />
                    <span className="db-header-sub">Local Command Center</span>
                    <ProjectSelector />
                    <div className="db-spacer" />
                    <AiTooltip content="Keyboard shortcuts (?)" side="bottom">
                        <button className="db-collapse-btn" onClick={() => setHelpOpen(true)} aria-label="Keyboard shortcuts">
                            <Icon name="clipboard" size={16} />
                        </button>
                    </AiTooltip>
                </header>

                <nav className="db-sidebar" aria-label="Dashboard navigation">
                    {NAV_GROUPS.map(group => (
                        <div key={group.label} className="db-nav-group">
                            <div className="db-nav-group-label">{group.label}</div>
                            {group.items.map(n => (
                                <AiTooltip key={n.id} content={collapsed ? n.label : n.desc} side="right">
                                    <button
                                        className="db-nav-item"
                                        data-active={activePanel === n.id ? 'true' : 'false'}
                                        onClick={() => setActivePanel(n.id)}
                                        aria-current={activePanel === n.id ? 'page' : undefined}
                                    >
                                        <Icon name={n.icon} size={16} />
                                        <span className="db-nav-label">{n.label}</span>
                                    </button>
                                </AiTooltip>
                            ))}
                        </div>
                    ))}
                </nav>

                <main className="db-main">
                    {activePanel === 'overview'   && <OverviewPanel onNavigate={(id) => setActivePanel(id as PanelId)} />}
                    {activePanel === 'hardware'   && <HardwarePanel />}
                    {activePanel === 'runtime'    && <RuntimePanel />}
                    {activePanel === 'memory'     && <MemoryManagement />}
                    {activePanel === 'byom'       && <BYOMPanel />}
                    {activePanel === 'extensions' && <ExtensionsPanel />}
                    {activePanel === 'rules'      && <RulesPanel />}
                    {activePanel === 'telemetry'  && <TelemetryPanel />}
                    {activePanel === 'staging'    && (
                        <Suspense fallback={<StagingAreaSkeleton />}>
                            <StagingArea />
                        </Suspense>
                    )}
                    {activePanel === 'audit'      && <AuditPanel />}
                    {activePanel === 'recovery'   && <RecoveryPanel />}
                </main>
            </div>

            <ShortcutsOverlay open={helpOpen} onClose={() => setHelpOpen(false)} shortcuts={SHORTCUTS} />
        </Tooltip.Provider>
    );
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (!root) { return; }
    createRoot(root).render(
        <ActiveProjectProvider>
            <Dashboard />
        </ActiveProjectProvider>
    );
});
