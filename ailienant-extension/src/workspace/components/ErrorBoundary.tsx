import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

/**
 * Reusable React error boundary. Boundaries are the only construct that can catch
 * an error thrown while rendering a descendant component — there is no hook
 * equivalent — so this must be a class.
 *
 * It catches render/lifecycle faults only; errors thrown inside event handlers or
 * async callbacks (e.g. a WebSocket reducer) are outside React's render and are not
 * caught here. A malformed value that lands in state is still caught, because it
 * surfaces when the affected subtree next renders.
 *
 * `fallback` may be a static node or a render-prop that receives the captured error
 * plus a `reset` callback to clear the boundary and re-attempt rendering. Pass
 * `resetKeys` to auto-clear a tripped boundary when its inputs change — a row whose
 * content was malformed mid-stream recovers once the next update arrives.
 */
interface ErrorBoundaryProps {
    children: ReactNode;
    fallback: ReactNode | ((error: Error, reset: () => void) => ReactNode);
    resetKeys?: ReadonlyArray<unknown>;
    onError?: (error: Error, info: ErrorInfo) => void;
    /** Identifies the boundary in the console diagnostic. */
    label?: string;
}

interface ErrorBoundaryState {
    error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
    public state: ErrorBoundaryState = { error: null };

    public static getDerivedStateFromError(error: Error): ErrorBoundaryState {
        return { error };
    }

    public componentDidCatch(error: Error, info: ErrorInfo): void {
        // Webview-side diagnostic. The host output-channel logger is bound to
        // `vscode` and is intentionally not reachable from the webview bundle.
        console.error(`[ErrorBoundary] ${this.props.label ?? 'unlabeled'}`, error, info.componentStack);
        this.props.onError?.(error, info);
    }

    public componentDidUpdate(prev: ErrorBoundaryProps): void {
        // Auto-clear a tripped boundary when any reset key changes, so a transient
        // fault (e.g. a partial streamed message) recovers on the next update.
        if (this.state.error && this.didResetKeysChange(prev.resetKeys, this.props.resetKeys)) {
            this.reset();
        }
    }

    private didResetKeysChange(
        a: ReadonlyArray<unknown> | undefined,
        b: ReadonlyArray<unknown> | undefined,
    ): boolean {
        if (a === b) { return false; }
        if (!a || !b || a.length !== b.length) { return true; }
        return a.some((v, i) => !Object.is(v, b[i]));
    }

    private reset = (): void => {
        this.setState({ error: null });
    };

    public render(): ReactNode {
        const { error } = this.state;
        if (error) {
            const { fallback } = this.props;
            return typeof fallback === 'function' ? fallback(error, this.reset) : fallback;
        }
        return this.props.children;
    }
}
