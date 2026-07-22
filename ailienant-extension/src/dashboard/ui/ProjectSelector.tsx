import { Icon } from '../../shared/Icon';
import { useActiveProject } from '../hooks/useActiveProject';

/**
 * Top-bar active-project picker. A styled native <select> (keyboard- and
 * screen-reader-accessible with zero dependencies) driven by the shared
 * ActiveProject context. Each option shows the workspace name; the full path is
 * exposed via the option/title tooltip to disambiguate same-named folders.
 */
export function ProjectSelector(): JSX.Element | null {
    const { projectId, setProjectId, projects, loading } = useActiveProject();

    // Nothing to scope by yet — hide the control rather than show an empty box.
    if (!loading && projects.length === 0) { return null; }

    const active = projects.find(p => p.id === projectId);

    return (
        <label className="db-project-selector" title={active?.path ?? 'Active project'}>
            <Icon name="network" size={13} />
            <select
                className="db-project-select"
                aria-label="Active project"
                value={projectId}
                disabled={loading || projects.length === 0}
                onChange={e => setProjectId(e.target.value)}
            >
                {loading && projects.length === 0 && <option value="">Loading projects…</option>}
                {projects.map(p => (
                    <option key={p.id} value={p.id} title={p.path}>{p.name}</option>
                ))}
            </select>
        </label>
    );
}
