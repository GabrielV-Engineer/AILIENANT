import type { ToastItem } from '../types';

/**
 * Bottom-corner transient notification stack.
 *
 * Each toast is its own `role="alert"` live region; the container intentionally
 * carries NO `aria-live`, because wrapping alert children in a parent live region
 * makes NVDA/VoiceOver/JAWS announce each toast twice.
 */
export function ToastStack({ toasts }: { toasts: ToastItem[] }): JSX.Element {
    return (
        <div className="ws-toast-stack">
            {toasts.map(t => (
                <div key={t.id} className="ws-toast" data-level={t.level} role="alert">
                    {t.message}
                </div>
            ))}
        </div>
    );
}
