import { useLayoutEffect } from 'react';
import type { RefObject } from 'react';

/**
 * Grow a textarea to fit its content, capped by the element's CSS max-height.
 *
 * `useLayoutEffect` (not `useEffect`) runs synchronously after DOM mutation but
 * BEFORE the browser paints, so the height reset + `scrollHeight` read + reapply
 * collapse into a single reflow and a single paint — no visible flicker between
 * the natural height and the measured one. The min/max bounds and the scroll-past
 * behavior live in CSS (`min-height`, `max-height`, `overflow-y: auto`), keeping
 * this hook free of presentation policy.
 *
 * Re-runs on every `value` change, including the reset to an empty string on
 * submit, which collapses the field back to its CSS `min-height`.
 */
export function useAutoResizeTextarea(
    ref: RefObject<HTMLTextAreaElement>,
    value: string,
): void {
    useLayoutEffect(() => {
        const el = ref.current;
        if (!el) { return; }
        // Reset first so `scrollHeight` reflects the content height, not the last
        // (possibly taller) applied height.
        el.style.height = 'auto';
        el.style.height = `${el.scrollHeight}px`;
    }, [ref, value]);
}
