import { useState } from 'react';
import { AgentRole } from '../shared/config';
import { vscode } from './vscode_bridge';

interface AgentCell {
    role: AgentRole;
    icon: string;
    label: string;
    color: string;
}

const AGENTS: AgentCell[] = [
    { role: 'core_dev',           icon: '⚙️',  label: 'Core Dev',      color: '#63a583' },
    { role: 'architect_refactor', icon: '🏛',  label: 'Architect',     color: '#7B9ED9' },
    { role: 'devops_infra',       icon: '🔧',  label: 'DevOps',        color: '#E8C43A' },
    { role: 'secops',             icon: '🛡',  label: 'SecOps',        color: '#E85A4F' },
    { role: 'qa_tester',          icon: '🧪',  label: 'QA Tester',     color: '#C47ED1' },
    { role: 'doc_manager',        icon: '📄',  label: 'Doc Mgr',       color: '#9EA8B8' },
    { role: 'vcs_manager',        icon: '🌿',  label: 'VCS Mgr',       color: '#5DB8A0' },
    { role: 'data_ml_engineer',   icon: '📊',  label: 'Data/ML',       color: '#F0884A' },
    { role: 'orchestrator',       icon: '🧠',  label: 'Orchestrator',  color: '#B5A9FF' },
];

interface Props {
    disabled?: boolean;
}

export function BentoMenu({ disabled }: Props): JSX.Element {
    const [lastInvoked, setLastInvoked] = useState<AgentRole | undefined>();

    const forceAgent = (role: AgentRole): void => {
        if (disabled) { return; }
        setLastInvoked(role);
        vscode.postMessage({ type: 'FORCE_AGENT', role });
        // Reset bypass badge after 3s
        setTimeout(() => setLastInvoked(prev => prev === role ? undefined : prev), 3000);
    };

    return (
        <div className="ai-section">
            <div className="ai-popover-label" style={{ marginBottom: 4 }}>
                Agent Launcher — Smart Router Bypass
            </div>
            <div className="ai-bento-grid">
                {AGENTS.map(a => (
                    <button
                        key={a.role}
                        className="ai-bento-cell"
                        onClick={() => forceAgent(a.role)}
                        disabled={disabled}
                        title={`Force ${a.label} agent (bypass Smart Router)`}
                        style={{
                            borderColor: lastInvoked === a.role ? a.color : undefined,
                        }}
                    >
                        {lastInvoked === a.role && (
                            <span className="ai-bento-bypass-badge">⚡</span>
                        )}
                        <span className="ai-bento-cell-icon">{a.icon}</span>
                        <span className="ai-bento-cell-label">{a.label}</span>
                    </button>
                ))}
            </div>
        </div>
    );
}
