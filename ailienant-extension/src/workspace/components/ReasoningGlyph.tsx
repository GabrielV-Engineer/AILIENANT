/**
 * Animated reasoning indicator — a horizontal lemniscate (∞) that draws itself.
 *
 * The stroke self-traces three times, then the whole glyph spins through a few
 * full turns, then the trace restarts — looping while the model reasons. Pure
 * SVG + CSS (see `.ws-reason-glyph*` in workspace.css); the choreography lives in
 * two synced keyframes and collapses to a gentle static pulse under
 * `prefers-reduced-motion`. Uses `currentColor` so it inherits the label color.
 */
interface Props {
    /** Width in px; height follows the 100×60 viewBox ratio. */
    size?: number;
    /** Freeze the animation (drawn, still) — used once the answer begins. */
    still?: boolean;
}

export function ReasoningGlyph({ size = 16, still = false }: Props): JSX.Element {
    return (
        <svg
            className="ws-reason-glyph"
            data-still={still ? 'true' : 'false'}
            width={size}
            height={Math.round((size * 60) / 100)}
            viewBox="0 0 100 60"
            fill="none"
            aria-hidden="true"
            focusable="false"
        >
            <path
                className="ws-reason-glyph-path"
                pathLength={1}
                d="M50,30 C35,8 10,8 10,30 C10,52 35,52 50,30 C65,8 90,8 90,30 C90,52 65,52 50,30 Z"
                stroke="currentColor"
                strokeWidth={8}
                strokeLinecap="round"
                strokeLinejoin="round"
            />
        </svg>
    );
}
