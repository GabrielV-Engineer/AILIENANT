/**
 * Agent TODO Panel.
 *
 * Renders the agentic cell's live structured TODO list (content / status /
 * active_form), streamed on its own `server_agent_todos` WS event and parked in
 * the workspace store keyed by session id — the same delivery shape as
 * CoderCompanionCard. Replace semantics: each write is the full list, so an
 * explicit empty array clears the panel (the backend's anti-immortal-TODO
 * invariant, mirrored here by simply rendering nothing).
 *
 * Renders nothing when there is no list, or the list is empty — no skeleton, no
 * "waiting" state. Unlike the Companion card this channel is optional cell
 * output, not a guaranteed post-turn broadcast, so there is nothing to time out.
 */
import { Icon } from '../../shared/Icon';
import { useWorkspaceStore } from '../workspaceStore';

interface Props {
    /** The session this TODO list belongs to (server payload's session_id). */
    sessionId: string;
}

export function AgentTodoPanel({ sessionId }: Props): JSX.Element | null {
    const todos = useWorkspaceStore(s => s.agentTodos[sessionId]);

    if (!todos || todos.length === 0) { return null; }

    return (
        <div className="ws-todo-panel">
            <div className="ws-todo-title">
                <Icon name="clipboard" size={13} />
                <span>Agent TODOs</span>
            </div>
            <ul className="ws-todo-list">
                {todos.map((item, i) => (
                    <li key={i} className={`ws-todo-item ws-todo-item-${item.status}`}>
                        <Icon
                            name={item.status === 'completed' ? 'check' : 'circle'}
                            size={12}
                            className="ws-todo-item-icon"
                        />
                        <span className="ws-todo-item-label">
                            {item.status === 'in_progress' ? item.active_form : item.content}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
