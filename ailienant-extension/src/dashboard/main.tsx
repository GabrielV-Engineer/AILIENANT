import './dashboard.css';
// @ts-ignore — esbuild resolves .svg as dataurl string; tsc doesn't know this
import logoUrl from './logo.svg';
import { createRoot } from 'react-dom/client';
import { useState, useEffect, useCallback, lazy, Suspense } from 'react';

// ── Lazy-loaded heavy panels (Monaco code splitting) ─────────────────────────
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore — esbuild resolves .tsx without extension; tsc Node16 requires .js
const StagingArea = lazy(() => import('./panels/StagingArea'));

// ── Eagerly-loaded lightweight panels ────────────────────────────────────────
import { BYOMPanel }        from './panels/BYOMPanel';
import { RulesPanel }       from './panels/RulesPanel';
import { AuditPanel }       from './panels/AuditPanel';
import { HardwarePanel }    from './panels/HardwarePanel';

type PanelId = 'hardware' | 'byom' | 'rules' | 'staging' | 'audit';

const NAV: { id: PanelId; icon: string; label: string }[] = [
    { id: 'hardware', icon: '🖥',  label: 'Hardware Monitor' },
    { id: 'byom',     icon: '🔌',  label: 'BYOM Models' },
    { id: 'rules',    icon: '📋',  label: 'Rules & Governance' },
    { id: 'staging',  icon: '🔍',  label: 'Staging Area' },
    { id: 'audit',    icon: '🔏',  label: 'Audit Ledger' },
];

function StagingAreaSkeleton(): JSX.Element {
    return (
        <div style={{ padding: 20 }}>
            <div className="db-card">
                <div style={{ height: 24, width: '40%', borderRadius: 4, background: '#E8D9CA', marginBottom: 12 }} />
                <div style={{ height: 400, borderRadius: 4, background: '#E8D9CA' }} />
            </div>
        </div>
    );
}

function Dashboard(): JSX.Element {
    const [activePanel, setActivePanel] = useState<PanelId>('hardware');

    return (
        <div className="db-layout">
            {/* Header */}
            <header className="db-header">
                <img src={logoUrl as string} alt="AILIENANT" className="db-header-logo-img" />
                <span className="db-header-sub">Local Command Center</span>
            </header>

            {/* Sidebar nav */}
            <nav className="db-sidebar">
                {NAV.map(n => (
                    <div
                        key={n.id}
                        className="db-nav-item"
                        data-active={activePanel === n.id ? 'true' : 'false'}
                        onClick={() => setActivePanel(n.id)}
                    >
                        <span>{n.icon}</span>
                        <span>{n.label}</span>
                    </div>
                ))}
            </nav>

            {/* Main content */}
            <main className="db-main">
                {activePanel === 'hardware' && <HardwarePanel />}
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
    );
}

document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('root');
    if (!root) { return; }
    createRoot(root).render(<Dashboard />);
});
