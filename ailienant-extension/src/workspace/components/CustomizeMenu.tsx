import { useEffect, useState } from 'react';
import { Icon } from '../../shared/Icon';
import { vscode } from '../vscode_bridge';
import type {
    AgentRoleInfo, Hook, HookEvent, McpServer, McpTestResult,
    OutputStyle, PermissionMode, SystemSettings,
} from '../../shared/types';

export type CustomizeView = 'permissions' | 'output-styles' | 'agents' | 'hooks' | 'mcp';

interface Props {
    view: CustomizeView;
    onClose: () => void;
}

const PERMISSION_MODES: Array<{ id: PermissionMode; title: string; desc: string }> = [
    { id: 'default', title: 'Default', desc: 'HITL gates every write / execute that is not pre-approved' },
    { id: 'plan',    title: 'Plan',    desc: 'Read-only — blocks all mutating actions (Planner mode)' },
    { id: 'auto',    title: 'Auto',    desc: 'Uninterrupted execution; dangerous commands still require approval' },
];

const OUTPUT_STYLES: Array<{ id: OutputStyle; title: string; desc: string }> = [
    { id: 'default',     title: 'Default',     desc: 'Balanced responses' },
    { id: 'concise',     title: 'Concise',     desc: 'Short, to-the-point answers' },
    { id: 'explanatory', title: 'Explanatory', desc: 'Step-by-step reasoning and commentary' },
    { id: 'code_only',   title: 'Code only',   desc: 'Just the code, minimal prose' },
];

const HOOK_EVENTS: HookEvent[] = ['pre_patch', 'post_patch'];

export function CustomizeMenu({ view, onClose }: Props): JSX.Element {
    const [settings, setSettings] = useState<SystemSettings | null>(null);
    const [roles, setRoles] = useState<AgentRoleInfo[] | null>(null);
    const [hooks, setHooks] = useState<Hook[] | null>(null);
    const [servers, setServers] = useState<McpServer[] | null>(null);
    const [mcpTests, setMcpTests] = useState<Record<string, McpTestResult | 'loading'>>({});

    // Local form state
    const [activeRole, setActiveRole] = useState<string | null>(null);
    const [roleDraft, setRoleDraft] = useState('');
    const [hookEvent, setHookEvent] = useState<HookEvent>('post_patch');
    const [hookCmd, setHookCmd] = useState('');
    const [mcpName, setMcpName] = useState('');
    const [mcpUri, setMcpUri] = useState('');

    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as {
                type: string;
                data?: SystemSettings | { roles?: AgentRoleInfo[] } | null;
                hooks?: Hook[]; servers?: McpServer[];
                id?: string; result?: McpTestResult | null;
            };
            if (msg.type === 'SYSTEM_SETTINGS') { setSettings((msg.data as SystemSettings) ?? null); }
            else if (msg.type === 'AGENT_ROLES') { setRoles(((msg.data as { roles?: AgentRoleInfo[] })?.roles) ?? []); }
            else if (msg.type === 'HOOKS_DATA') { setHooks(msg.hooks ?? []); }
            else if (msg.type === 'MCP_SERVERS') { setServers(msg.servers ?? []); }
            else if (msg.type === 'MCP_TEST_RESULT' && msg.id) {
                setMcpTests(prev => ({ ...prev, [msg.id!]: msg.result ?? { reachable: false, tool_count: 0, error: 'no response' } }));
            }
        };
        window.addEventListener('message', handler);
        if (view === 'permissions' || view === 'output-styles') { vscode.postMessage({ type: 'GET_SYSTEM_SETTINGS' }); }
        if (view === 'agents') { vscode.postMessage({ type: 'GET_AGENT_ROLES' }); }
        if (view === 'hooks') { vscode.postMessage({ type: 'GET_HOOKS' }); }
        if (view === 'mcp') { vscode.postMessage({ type: 'GET_MCP_SERVERS' }); }
        return () => window.removeEventListener('message', handler);
    }, [view]);

    // ── Permissions ──────────────────────────────────────────────
    if (view === 'permissions') {
        const active = settings?.permission_mode ?? 'default';
        return (
            <div className="ws-models-body">
                {PERMISSION_MODES.map(m => (
                    <button
                        key={m.id}
                        className="ws-mode-row"
                        data-active={active === m.id ? 'true' : 'false'}
                        onClick={() => vscode.postMessage({ type: 'SET_PERMISSION_MODE', mode: m.id })}
                    >
                        <div className="ws-mode-row-text">
                            <span className="ws-mode-row-title">{m.title}</span>
                            <span className="ws-mode-row-desc">{m.desc}</span>
                        </div>
                        {active === m.id && <Icon name="check" size={13} />}
                    </button>
                ))}
                <p className="ws-models-note">Applied to new tasks. The permission engine enforces this in-session.</p>
            </div>
        );
    }

    // ── Output styles ────────────────────────────────────────────
    if (view === 'output-styles') {
        const active = settings?.output_style ?? 'default';
        return (
            <div className="ws-models-body">
                {OUTPUT_STYLES.map(s => (
                    <button
                        key={s.id}
                        className="ws-mode-row"
                        data-active={active === s.id ? 'true' : 'false'}
                        onClick={() => vscode.postMessage({ type: 'SET_OUTPUT_STYLE', style: s.id })}
                    >
                        <div className="ws-mode-row-text">
                            <span className="ws-mode-row-title">{s.title}</span>
                            <span className="ws-mode-row-desc">{s.desc}</span>
                        </div>
                        {active === s.id && <Icon name="check" size={13} />}
                    </button>
                ))}
                <p className="ws-models-note">Saved as a preference. Prompt-injection enforcement is a follow-up.</p>
            </div>
        );
    }

    // ── Agents ───────────────────────────────────────────────────
    if (view === 'agents') {
        if (roles === null) { return <div className="ws-models-body"><div className="ws-models-empty">Loading agents…</div></div>; }
        return (
            <div className="ws-models-body">
                <div className="ws-models-list">
                    {roles.map(r => {
                        const isOpen = activeRole === r.role;
                        return (
                            <div key={r.role} className="ws-models-row" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 6 }}>
                                <button
                                    className="ws-mode-row"
                                    style={{ background: 'none', border: 'none', padding: 0, textAlign: 'left' }}
                                    onClick={() => {
                                        setActiveRole(isOpen ? null : r.role);
                                        setRoleDraft(r.override ?? r.base_prompt);
                                    }}
                                >
                                    <div className="ws-mode-row-text">
                                        <span className="ws-mode-row-title">{r.role}{r.override ? ' ·' : ''}</span>
                                        <span className="ws-mode-row-desc">{r.override ? 'customized' : r.base_prompt}</span>
                                    </div>
                                    <Icon name="chevron-right" size={13} />
                                </button>
                                {isOpen && (
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                        <textarea
                                            className="ws-input"
                                            rows={4}
                                            value={roleDraft}
                                            onChange={e => setRoleDraft(e.target.value)}
                                            placeholder="System-prompt override (empty reverts to base)"
                                        />
                                        <div style={{ display: 'flex', gap: 6 }}>
                                            <button
                                                className="ws-core-menu-btn"
                                                onClick={() => vscode.postMessage({ type: 'SAVE_AGENT_ROLE', role: r.role, system_prompt: roleDraft })}
                                            >Save override</button>
                                            <button
                                                className="ws-core-menu-btn"
                                                onClick={() => { setRoleDraft(''); vscode.postMessage({ type: 'SAVE_AGENT_ROLE', role: r.role, system_prompt: '' }); }}
                                            >Reset to base</button>
                                        </div>
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
                <p className="ws-models-note">
                    Edit the orchestrator persona &amp; Analyst name in the{' '}
                    <button
                        className="ws-core-menu-btn"
                        style={{ display: 'inline', padding: 0, background: 'none', border: 'none', color: 'var(--accent-primary, #63a583)', cursor: 'pointer', fontSize: 'inherit' }}
                        onClick={() => { vscode.postMessage({ type: 'OPEN_DASHBOARD', tab: 'rules' }); onClose(); }}
                    >Rules panel</button>. Overrides are saved; runtime application is a follow-up.
                </p>
            </div>
        );
    }

    // ── Hooks ────────────────────────────────────────────────────
    if (view === 'hooks') {
        if (hooks === null) { return <div className="ws-models-body"><div className="ws-models-empty">Loading hooks…</div></div>; }
        const addHook = (): void => {
            if (!hookCmd.trim()) { return; }
            vscode.postMessage({ type: 'SAVE_HOOK', hook: { event: hookEvent, command: hookCmd.trim(), enabled: true } });
            setHookCmd('');
        };
        return (
            <div className="ws-models-body">
                {hooks.length === 0
                    ? <div className="ws-models-empty">No hooks defined yet.</div>
                    : <div className="ws-models-list">
                        {hooks.map(h => (
                            <div key={h.id} className="ws-models-row" style={{ alignItems: 'center', gap: 8 }}>
                                <div className="ws-models-row-text" style={{ flex: 1 }}>
                                    <span className="ws-models-row-name">{h.command}</span>
                                    <span className="ws-models-row-meta"><span className="ws-tag">{h.event}</span></span>
                                </div>
                                <button
                                    className="ws-core-menu-btn"
                                    onClick={() => vscode.postMessage({ type: 'SAVE_HOOK', hook: { ...h, enabled: !h.enabled } })}
                                >{h.enabled ? 'On' : 'Off'}</button>
                                <button className="ws-core-menu-btn" onClick={() => vscode.postMessage({ type: 'DELETE_HOOK', id: h.id })}>
                                    <Icon name="trash" size={13} />
                                </button>
                            </div>
                        ))}
                    </div>}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
                    <select className="ws-input" value={hookEvent} onChange={e => setHookEvent(e.target.value as HookEvent)}>
                        {HOOK_EVENTS.map(ev => <option key={ev} value={ev}>{ev}</option>)}
                    </select>
                    <input className="ws-input" placeholder="command, e.g. npm run lint" value={hookCmd} onChange={e => setHookCmd(e.target.value)} />
                    <button className="ws-core-menu-btn" onClick={addHook}><Icon name="plus" size={13} /> Add hook</button>
                </div>
                <p className="ws-models-note">Hooks are saved but not yet executed (execution wiring is a follow-up).</p>
            </div>
        );
    }

    // ── MCP Servers ──────────────────────────────────────────────
    if (view === 'mcp') {
        if (servers === null) { return <div className="ws-models-body"><div className="ws-models-empty">Loading servers…</div></div>; }
        const addServer = (): void => {
            if (!mcpName.trim() || !mcpUri.trim()) { return; }
            vscode.postMessage({ type: 'SAVE_MCP_SERVER', server: { name: mcpName.trim(), uri: mcpUri.trim(), enabled: true } });
            setMcpName(''); setMcpUri('');
        };
        return (
            <div className="ws-models-body">
                {servers.length === 0
                    ? <div className="ws-models-empty">No MCP servers configured.</div>
                    : <div className="ws-models-list">
                        {servers.map(s => {
                            const test = mcpTests[s.id];
                            return (
                                <div key={s.id} className="ws-models-row" style={{ alignItems: 'center', gap: 8 }}>
                                    <div className="ws-models-row-text" style={{ flex: 1 }}>
                                        <span className="ws-models-row-name">{s.name}</span>
                                        <span className="ws-models-row-meta">
                                            <span className="ws-tag">{s.uri}</span>
                                            {test && test !== 'loading' && (
                                                <span className="ws-tag">{test.reachable ? `✓ ${test.tool_count} tools` : `✕ ${test.error ?? 'unreachable'}`}</span>
                                            )}
                                        </span>
                                    </div>
                                    <button
                                        className="ws-core-menu-btn"
                                        disabled={test === 'loading'}
                                        onClick={() => { setMcpTests(p => ({ ...p, [s.id]: 'loading' })); vscode.postMessage({ type: 'TEST_MCP_SERVER', id: s.id, uri: s.uri }); }}
                                    >{test === 'loading' ? 'Testing…' : 'Test'}</button>
                                    <button className="ws-core-menu-btn" onClick={() => vscode.postMessage({ type: 'DELETE_MCP_SERVER', id: s.id })}>
                                        <Icon name="trash" size={13} />
                                    </button>
                                </div>
                            );
                        })}
                    </div>}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
                    <input className="ws-input" placeholder="name" value={mcpName} onChange={e => setMcpName(e.target.value)} />
                    <input className="ws-input" placeholder="stdio:///abs/path/to/server?arg=x" value={mcpUri} onChange={e => setMcpUri(e.target.value)} />
                    <button className="ws-core-menu-btn" onClick={addServer}><Icon name="plus" size={13} /> Add server</button>
                </div>
                <p className="ws-models-note">Servers are saved &amp; testable. Auto-connect at task time is a follow-up.</p>
            </div>
        );
    }

    return <div className="ws-models-body" />;
}
