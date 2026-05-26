/**
 * Phase 7.11.2 (ADR-706 §4.5c) — Sidebar WebView store.
 *
 * Tiny rehydratable slice for the session-browser sidebar:
 *   - `query`     — the filter input
 *   - `activeId`  — last-opened session id (visual highlight)
 *
 * The session LIST itself is host-fed (`SESSIONS_UPDATED` message broadcast)
 * and stays in local `useState` — it must reflect the host's source of truth
 * on every render, not the cached persisted value.
 */
import { createPersistedStore } from '../shared/persistedStore';

export interface SidebarState {
    query: string;
    activeId: string | null;

    setQuery: (v: string) => void;
    setActiveId: (v: string | null) => void;
}

export const useSidebarStore = createPersistedStore<SidebarState>(
    (set) => ({
        query: '',
        activeId: null,

        setQuery:    (v) => set({ query: v }),
        setActiveId: (v) => set({ activeId: v }),
    }),
    {
        key: 'sidebar.v1',
        version: 1,
        pick: (s) => ({ query: s.query, activeId: s.activeId }),
    },
);
