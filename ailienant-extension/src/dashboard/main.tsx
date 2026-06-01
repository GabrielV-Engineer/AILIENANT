import './dashboard.css';
// @ts-ignore — esbuild resolves .svg as dataurl string; tsc doesn't know this
import logoUrl from './logo.svg';
import { createRoot } from 'react-dom/client';
import { useState, lazy, Suspense } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import { Icon, type IconName } from '../shared/Icon';
import { Tooltip as AiTooltip } from '../shared/Tooltip';

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

const NAV: { id: PanelId; icon: IconName; label: string; desc: string }[] = [
    { id: 'overview',   icon: 'gauge',     label: 'Overview',           desc: 'At-a-glance summary of cost, MCP, HITL and activity' },
    { id: 'hardware',   icon: 'cpu',       label: 'Hardware Monitor',   desc: 'Live RAM/VRAM gauges and execution mode' },
    { id: 'runtime',    icon: 'zap',       label: 'Runtime',            desc: 'Sandbox tier, Docker status and lifecycle controls' },
    { id: 'memory',     icon: 'network',   label: 'Memory Management',  desc: 'Interactive GraphRAG node viewer' },
    { id: 'byom',       icon: 'plug',      label: 'BYOM Models',        desc: 'Manage local & cloud model endpoints' },
    { id: 'extensions', icon: 'wand',      label: 'Extensions',         desc: 'MCP servers and reusable skill templates' },
    { id: 'rules',      icon: 'clipboard', label: 'Rules & Governance', desc: 'Directory rules and custom instructions' },
    { id: 'telemetry',  icon: 'telescope', label: 'Telemetry',          desc: 'Token cost snapshot and routing decision log' },
    { id: 'staging',    icon: 'eye',       label: 'Staging Area',       desc: 'HITL review of pending patches' },
    { id: 'audit',      icon: 'shield',    label: 'Audit Ledger',       desc: 'Blake2b-verified HITL audit log' },
    { id: 'recovery',   icon: 'alert',     label: 'Task Recovery',      desc: 'Resume crashed tasks from the dead-letter queue' },
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
    const ids = NAV.map(n => n.id) as string[];
    return requested && ids.includes(requested) ? (requested as PanelId) : 'overview';
}

function Dashboard(): JSX.Element {
    const [activePanel, setActivePanel] = useState<PanelId>(getInitialPanel);

    return (
        <Tooltip.Provider delayDuration={400} skipDelayDuration={150}>
            <div className="db-layout">
                <header className="db-header">
                    <img src={logoUrl as string} alt="AILIENANT" className="db-header-logo-img" />
                    <span className="db-header-sub">Local Command Center</span>
                </header>

                <nav className="db-sidebar" aria-label="Dashboard navigation">
                    {NAV.map(n => (
                        <AiTooltip key={n.id} content={n.desc} side="right">
                            <button
                                className="db-nav-item"
                                data-active={activePanel === n.id ? 'true' : 'false'}
                                onClick={() => setActivePanel(n.id)}
                                aria-current={activePanel === n.id ? 'page' : undefined}
                            >
                                <Icon name={n.icon} size={16} />
                                <span>{n.label}</span>
                            </button>
                        </AiTooltip>
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
        </Tooltip.Provider>
    );
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (!root) { return; }
    createRoot(root).render(<Dashboard />);
});
