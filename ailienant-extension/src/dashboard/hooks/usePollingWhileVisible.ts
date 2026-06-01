import { useEffect, useRef } from 'react';

/**
 * Runs `fn` once immediately, then on an interval — but only while the page is
 * visible.
 *
 * A backgrounded dashboard window (its tab hidden or the OS window obscured)
 * stops polling and resumes the instant it becomes visible again, so an idle
 * dashboard never hammers the core over HTTP. `fn` is held in a ref, so callers
 * may pass a fresh closure each render without resetting the timer; all timers
 * and listeners are torn down on unmount.
 */
export function usePollingWhileVisible(fn: () => void, intervalMs: number): void {
    const savedFn = useRef(fn);
    savedFn.current = fn;

    useEffect(() => {
        let timer: ReturnType<typeof setInterval> | undefined;

        const tick = (): void => { savedFn.current(); };

        const start = (): void => {
            if (timer !== undefined) { return; }
            timer = setInterval(tick, intervalMs);
        };
        const stop = (): void => {
            if (timer === undefined) { return; }
            clearInterval(timer);
            timer = undefined;
        };
        const sync = (): void => {
            if (document.visibilityState === 'visible') { start(); } else { stop(); }
        };

        tick();   // fire once on mount so the panel paints without waiting a full interval
        sync();   // begin polling only if currently visible
        document.addEventListener('visibilitychange', sync);

        return () => {
            document.removeEventListener('visibilitychange', sync);
            stop();
        };
    }, [intervalMs]);
}
