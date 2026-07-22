import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

/** One selectable project as returned by GET /api/v1/projects. */
export interface ProjectInfo {
    id:        string;
    name:      string;
    path:      string;
    last_seen: number;
}

interface ActiveProjectValue {
    /** The active project id, or '' before any project is known. */
    projectId: string;
    setProjectId: (id: string) => void;
    projects: ProjectInfo[];
    loading: boolean;
    /** Re-fetch the project list (e.g. after a new workspace connects). */
    refresh: () => void;
}

const STORAGE_KEY = 'ailienant.dashboard.activeProject';
const URL_PARAM = 'project_id';

const ActiveProjectContext = createContext<ActiveProjectValue | null>(null);

function readInitial(): string {
    try {
        const fromUrl = new URLSearchParams(window.location.search).get(URL_PARAM);
        if (fromUrl) { return fromUrl; }
        return localStorage.getItem(STORAGE_KEY) ?? '';
    } catch { return ''; }
}

function persist(id: string): void {
    try {
        if (id) { localStorage.setItem(STORAGE_KEY, id); } else { localStorage.removeItem(STORAGE_KEY); }
        const url = new URL(window.location.href);
        if (id) { url.searchParams.set(URL_PARAM, id); } else { url.searchParams.delete(URL_PARAM); }
        window.history.replaceState(null, '', url.toString());
    } catch { /* storage / history unavailable */ }
}

/**
 * Owns the dashboard-wide "active project" selection. Fetches the selectable
 * projects from the backend registry, persists the choice to localStorage + a
 * `?project_id=` URL param, and — crucially — reconciles a stale/ghost selection
 * on load: if the persisted id is no longer in the list (folder deleted, never
 * connected on this machine), it falls back to the first available project so no
 * panel ever boots pointed at a dead id.
 */
export function ActiveProjectProvider({ children }: { children: ReactNode }): JSX.Element {
    const [projects, setProjects] = useState<ProjectInfo[]>([]);
    const [projectId, setProjectIdState] = useState<string>(readInitial);
    const [loading, setLoading] = useState(true);

    const setProjectId = useCallback((id: string) => {
        setProjectIdState(id);
        persist(id);
    }, []);

    const refresh = useCallback(() => {
        let cancelled = false;
        setLoading(true);
        void (async () => {
            try {
                const r = await fetch('/api/v1/projects');
                if (!r.ok || cancelled) { return; }
                const list = await r.json() as ProjectInfo[];
                if (cancelled) { return; }
                setProjects(list);
                // Reconcile: keep the current selection only if it still exists,
                // else adopt the first available project (or clear if none).
                setProjectIdState(prev => {
                    if (prev && list.some(p => p.id === prev)) { return prev; }
                    const next = list.length > 0 ? list[0].id : '';
                    persist(next);
                    return next;
                });
            } catch { /* leave prior state; panels degrade to their empty view */ }
            finally { if (!cancelled) { setLoading(false); } }
        })();
        return () => { cancelled = true; };
    }, []);

    useEffect(() => { const cancel = refresh(); return cancel; }, [refresh]);

    const value = useMemo<ActiveProjectValue>(
        () => ({ projectId, setProjectId, projects, loading, refresh }),
        [projectId, setProjectId, projects, loading, refresh],
    );

    return createElement(ActiveProjectContext.Provider, { value }, children);
}

/** Read the active-project selection from anywhere under the provider. */
export function useActiveProject(): ActiveProjectValue {
    const ctx = useContext(ActiveProjectContext);
    if (ctx === null) {
        throw new Error('useActiveProject must be used within an ActiveProjectProvider');
    }
    return ctx;
}

/** Append `?project_id=` (or `&project_id=`) to a URL when a project is active. */
export function withProject(url: string, projectId: string): string {
    if (!projectId) { return url; }
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}project_id=${encodeURIComponent(projectId)}`;
}
