import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

interface ConfirmModalProps {
    open: boolean;
    title: string;
    body: ReactNode;
    /** Optional emphasised caution line shown above the actions. */
    warning?: string;
    confirmLabel?: string;
    cancelLabel?: string;
    /** Style the confirm button as destructive (red). */
    danger?: boolean;
    onConfirm: () => void;
    onCancel: () => void;
}

/**
 * Dependency-free confirmation dialog. Closes on Esc or backdrop click, traps the
 * initial focus on the cancel button (the safe default), and restores focus to the
 * opener on close. Generalised from the BYOM panel's inline confirm modal so any
 * panel can gate a destructive action behind one consistent surface.
 */
export function ConfirmModal({
    open,
    title,
    body,
    warning,
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    danger = false,
    onConfirm,
    onCancel,
}: ConfirmModalProps): JSX.Element | null {
    const cancelRef = useRef<HTMLButtonElement>(null);
    const openerRef = useRef<Element | null>(null);

    useEffect(() => {
        if (!open) { return; }
        openerRef.current = document.activeElement;
        cancelRef.current?.focus();
        const onKey = (e: KeyboardEvent): void => {
            if (e.key === 'Escape') { e.preventDefault(); onCancel(); }
        };
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('keydown', onKey);
            (openerRef.current as HTMLElement | null)?.focus?.();
        };
    }, [open, onCancel]);

    if (!open) { return null; }

    return (
        <div className="ui-overlay" role="dialog" aria-modal="true" aria-label={title} onClick={onCancel}>
            <div className="ui-overlay-panel ui-confirm" onClick={e => e.stopPropagation()}>
                <div className="ui-overlay-head">
                    <span className="db-card-title" style={{ marginBottom: 0 }}>{title}</span>
                </div>
                <div className="ui-confirm-body">{body}</div>
                {warning && <div className="ui-confirm-warning">{warning}</div>}
                <div className="ui-confirm-actions">
                    <button ref={cancelRef} className="db-btn db-btn-ghost" onClick={onCancel}>{cancelLabel}</button>
                    <button
                        className={danger ? 'db-btn db-btn-danger' : 'db-btn db-btn-primary'}
                        onClick={onConfirm}
                    >
                        {confirmLabel}
                    </button>
                </div>
            </div>
        </div>
    );
}
