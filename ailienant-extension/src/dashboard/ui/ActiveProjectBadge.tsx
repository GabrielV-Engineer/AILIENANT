import { Badge } from './Badge';
import { useActiveProject } from '../hooks/useActiveProject';

/**
 * Informational chip naming the active project. Used by the global-config panels
 * (BYOM / Extensions / Rules) whose settings are not themselves project-scoped —
 * the badge makes the current project context explicit without implying the
 * panel's data re-scopes on a switch.
 */
export function ActiveProjectBadge(): JSX.Element | null {
    const { projectId, projects } = useActiveProject();
    const active = projects.find(p => p.id === projectId);
    if (!active) { return null; }
    return <Badge status="info" icon="network"><span title={active.path}>Project: {active.name}</span></Badge>;
}
