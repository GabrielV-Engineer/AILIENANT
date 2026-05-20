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

type PanelId = 'hardware' | 'memory' | 'byom' | 'rules' | 'staging' | 'audit';

const NAV: { id: PanelId; icon: IconName; label: string; desc: string }[] = [
    { id: 'hardware', icon: 'cpu',       label: 'Hardware Monitor',   desc: 'Live RAM/VRAM gauges and execution mode' },
    { id: 'memory',   icon: 'network',   label: 'Memory Management',  desc: 'Interactive GraphRAG node viewer' },
    { id: 'byom',     icon: 'plug',      label: 'BYOM Models',        desc: 'Manage local & cloud model endpoints' },
    { id: 'rules',    icon: 'clipboard', label: 'Rules & Governance', desc: 'Directory rules and custom instructions' },
    { id: 'staging',  icon: 'eye',       label: 'Staging Area',       desc: 'HITL review of pending patches' },
    { id: 'audit',    icon: 'shield',    label: 'Audit Ledger',       desc: 'Blake2b-verified HITL audit log' },
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

function Dashboard(): JSX.Element {
    const [activePanel, setActivePanel] = useState<PanelId>('hardware');

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
                    {activePanel === 'hardware' && <HardwarePanel />}
                    {activePanel === 'memory'   && <MemoryManagement />}
                    {activePanel === 'byom'     && <BYOMPanel />}
                    {activePanel === 'rules'    && <RulesPanel />}
                    {activePanel === 'staging'  && (
                        <Suspense fallback={<StagingAreaSkeleton />}>
                            <StagingArea />
                        </Suspense>
                    )}
                    {activePanel === 'audit'    && <AuditPanel />}
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
