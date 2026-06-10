import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Fixed-height row virtualization for a locally-scrolled container.
 *
 * The audit-log PTY panel can hold thousands of monospace lines. Mounting them all
 * tanks frame rate and bloats the DOM, so only the rows inside the viewport (plus a
 * small overscan) are rendered; two spacer divs of `topPad`/`bottomPad` pixels hold
 * the total scroll height so the scrollbar geometry stays correct.
 *
 * Critically, the scroll math is relative to the OWNING container, never the window.
 * The panel lives inside a collapsible accordion with its own `overflow-y: auto`, so
 * `scrollTop`/`clientHeight` are read from the element `scrollRef` is bound to. Bind
 * `scrollRef` to that scroll container and wire its `onScroll` to the returned
 * handler. Below `threshold` rows the whole list renders directly (windowing off).
 */
export interface WindowedRows {
    scrollRef: React.RefObject<HTMLDivElement>;
    onScroll: () => void;
    startIndex: number;
    endIndex: number;   // exclusive
    topPad: number;
    bottomPad: number;
}

const OVERSCAN = 8;

export function useWindowedRows(
    total: number,
    rowHeight: number,
    threshold = 1000,
): WindowedRows {
    const scrollRef = useRef<HTMLDivElement>(null);
    const rafRef = useRef<number | null>(null);
    const [scrollTop, setScrollTop] = useState(0);
    const [viewportH, setViewportH] = useState(0);

    // rAF-throttled scroll read from the local container — coalesces a burst of
    // scroll events into one state update per frame.
    const onScroll = useCallback(() => {
        if (rafRef.current !== null) { return; }
        rafRef.current = requestAnimationFrame(() => {
            rafRef.current = null;
            const el = scrollRef.current;
            if (el) { setScrollTop(el.scrollTop); }
        });
    }, []);

    // Track the container's own height; re-measure on resize. Cancels any pending
    // scroll frame on teardown so no callback outlives the panel.
    useEffect(() => {
        const el = scrollRef.current;
        if (!el) { return; }
        const measure = (): void => setViewportH(el.clientHeight);
        measure();
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => {
            ro.disconnect();
            if (rafRef.current !== null) {
                cancelAnimationFrame(rafRef.current);
                rafRef.current = null;
            }
        };
    }, []);

    // Windowing disabled below the threshold or before the container is measured.
    if (total <= threshold || viewportH === 0 || rowHeight <= 0) {
        return { scrollRef, onScroll, startIndex: 0, endIndex: total, topPad: 0, bottomPad: 0 };
    }

    const startIndex = Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN);
    const endIndex = Math.min(total, Math.ceil((scrollTop + viewportH) / rowHeight) + OVERSCAN);
    return {
        scrollRef,
        onScroll,
        startIndex,
        endIndex,
        topPad: startIndex * rowHeight,
        bottomPad: (total - endIndex) * rowHeight,
    };
}
