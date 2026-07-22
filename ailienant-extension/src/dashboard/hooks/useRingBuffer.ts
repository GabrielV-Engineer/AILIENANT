import { useCallback, useRef, useState } from 'react';

export interface Sample<T> {
    /** Wall-clock capture time (ms epoch). */
    t: number;
    /** The captured reading. */
    v: T;
}

export interface RingBuffer<T> {
    /** Append a reading, evicting the oldest once `capacity` is exceeded. */
    push: (v: T, t?: number) => void;
    /** The retained samples, oldest first. */
    samples: Sample<T>[];
    /** Drop every retained sample (e.g. on a scope change). */
    clear: () => void;
}

/**
 * Bounded, timestamped rolling buffer for live "oscilloscope"-style charts.
 *
 * Retains at most `capacity` most-recent readings; older ones are dropped, so
 * memory stays bounded regardless of how long a panel is left open. Each sample
 * carries a wall-clock timestamp so charts plot against real elapsed time and
 * render honest gaps when polling pauses (a hidden tab), instead of faking
 * continuity by plotting against a sample index.
 */
export function useRingBuffer<T>(capacity: number): RingBuffer<T> {
    const [samples, setSamples] = useState<Sample<T>[]>([]);
    const capRef = useRef(capacity);
    capRef.current = Math.max(1, capacity);

    const push = useCallback((v: T, t: number = Date.now()): void => {
        setSamples(prev => {
            const next = [...prev, { t, v }];
            return next.length > capRef.current
                ? next.slice(next.length - capRef.current)
                : next;
        });
    }, []);

    const clear = useCallback((): void => { setSamples([]); }, []);

    return { push, samples, clear };
}
