import { useEffect, useRef } from 'react';

export interface ShortcutHint {
    /** Key tokens rendered as individual <kbd> chips, e.g. ['Ctrl', 'B']. */
    keys: string[];
    label: string;
}

interface ShortcutsOverlayProps {
    open: boolean;
    onClose: () => void;
    shortcuts: ShortcutHint[];
}

/**
 * Lightweight, dependency-free keyboard-shortcuts help dialog. Closes on Esc or
 * backdrop click and returns focus to the element that opened it.
 */
export function ShortcutsOverlay({ open, onClose, shortcuts }: ShortcutsOverlayProps): JSX.Element | null {
    const closeRef = useRef<HTMLButtonElement>(null);
    const openerRef = useRef<Element | null>(null);

    useEffect(() => {
        if (!open) { return; }
        openerRef.current = document.activeElement;
        closeRef.current?.focus();
        const onKey = (e: KeyboardEvent): void => {
            if (e.key === 'Escape') { e.preventDefault(); onClose(); }
        };
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('keydown', onKey);
            // Restore focus to the opener when the dialog closes.
            (openerRef.current as HTMLElement | null)?.focus?.();
        };
    }, [open, onClose]);

    if (!open) { return null; }

    return (
        <div className="ui-overlay" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" onClick={onClose}>
            <div className="ui-overlay-panel" onClick={e => e.stopPropagation()}>
                <div className="ui-overlay-head">
                    <span className="db-card-title" style={{ marginBottom: 0 }}>Keyboard shortcuts</span>
                    <button ref={closeRef} className="db-btn db-btn-ghost" onClick={onClose} aria-label="Close">Esc</button>
                </div>
                <ul className="ui-shortcut-list">
                    {shortcuts.map((s, i) => (
                        <li key={i} className="ui-shortcut-row">
                            <span className="ui-shortcut-keys">
                                {s.keys.map((k, j) => <kbd key={j} className="ui-kbd">{k}</kbd>)}
                            </span>
                            <span className="ui-shortcut-label">{s.label}</span>
                        </li>
                    ))}
                </ul>
            </div>
        </div>
    );
}
