import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'ailienant.dashboard.sidebarCollapsed';
const NARROW_QUERY = '(max-width: 900px)';

/**
 * Owns the sidebar collapse state. The user's explicit choice is persisted to
 * localStorage and wins on wide viewports; on narrow viewports the sidebar is
 * force-collapsed to an icon rail regardless of the stored preference so the
 * content area is never crowded. Returns the effective collapsed flag plus a
 * toggle that updates the stored preference.
 */
export function useSidebarCollapsed(): { collapsed: boolean; toggle: () => void } {
    const [preferred, setPreferred] = useState<boolean>(() => {
        try { return localStorage.getItem(STORAGE_KEY) === '1'; } catch { return false; }
    });
    const [narrow, setNarrow] = useState<boolean>(() => {
        try { return window.matchMedia(NARROW_QUERY).matches; } catch { return false; }
    });

    useEffect(() => {
        const mql = window.matchMedia(NARROW_QUERY);
        const onChange = (e: MediaQueryListEvent): void => setNarrow(e.matches);
        mql.addEventListener('change', onChange);
        return () => mql.removeEventListener('change', onChange);
    }, []);

    const toggle = useCallback(() => {
        setPreferred(prev => {
            const next = !prev;
            try { localStorage.setItem(STORAGE_KEY, next ? '1' : '0'); } catch { /* storage unavailable */ }
            return next;
        });
    }, []);

    return { collapsed: narrow || preferred, toggle };
}
