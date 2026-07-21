import { useEffect } from 'react';

interface ShortcutHandlers {
    /** Toggle the sidebar collapse (Ctrl/Cmd+B). */
    onToggleSidebar: () => void;
    /** Jump to the 0-based Nth nav item (digit keys 1–9). */
    onSelectIndex: (index: number) => void;
    /** Toggle the shortcuts help overlay (?). */
    onToggleHelp: () => void;
}

/** True when focus is in a field where typing must not trigger shortcuts. */
function isEditableTarget(target: EventTarget | null): boolean {
    const el = target as HTMLElement | null;
    if (!el) { return false; }
    const tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable === true;
}

/**
 * Global keyboard shortcuts for the dashboard shell. Ignored while the user is
 * typing in a form field, and fully torn down on unmount.
 */
export function useKeyboardShortcuts({ onToggleSidebar, onSelectIndex, onToggleHelp }: ShortcutHandlers): void {
    useEffect(() => {
        const onKey = (e: KeyboardEvent): void => {
            if (isEditableTarget(e.target)) { return; }

            // Ctrl/Cmd+B — toggle the sidebar.
            if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'b' || e.key === 'B')) {
                e.preventDefault();
                onToggleSidebar();
                return;
            }

            // Bare modifier-free keys below.
            if (e.ctrlKey || e.metaKey || e.altKey) { return; }

            // "?" — help overlay (Shift+/ on most layouts).
            if (e.key === '?') {
                e.preventDefault();
                onToggleHelp();
                return;
            }

            // 1–9 — jump to the Nth nav item.
            if (e.key >= '1' && e.key <= '9') {
                e.preventDefault();
                onSelectIndex(Number(e.key) - 1);
            }
        };

        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [onToggleSidebar, onSelectIndex, onToggleHelp]);
}
