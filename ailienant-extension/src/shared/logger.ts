// ============================================================
// AILIENANT — Extension-host logger
//
// A thin wrapper around a single named VS Code output channel. It replaces the
// bare `console.*` calls scattered across the host modules so runtime diagnostics
// land in a user-visible "AILIENANT" output channel instead of the (hidden)
// extension-host devtools console.
//
// HOST-ONLY: this module imports `vscode`, which is only available (and esbuild-
// `external`) in the extension-host bundle. It must NOT be imported from any
// webview-reachable module (sidebar / workspace / dashboard bundles build with
// `platform:'browser'` and no `vscode` external). The host build is the guard:
// a leak into a webview bundle fails `npm run compile`.
// ============================================================

import * as vscode from 'vscode';

type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

// Lazily created so importing this module never touches `vscode.window` at load
// time (keeps it inert under test rigs that stub the host API).
let channel: vscode.OutputChannel | undefined;

function getChannel(): vscode.OutputChannel {
    if (!channel) {
        channel = vscode.window.createOutputChannel('AILIENANT');
    }
    return channel;
}

/** Render one extra arg: stacks for Errors, JSON for objects, String otherwise. */
function formatArg(arg: unknown): string {
    if (arg instanceof Error) {
        return arg.stack ?? `${arg.name}: ${arg.message}`;
    }
    if (arg !== null && typeof arg === 'object') {
        try {
            return JSON.stringify(arg);
        } catch {
            // Circular or otherwise non-serializable — fall back to coercion.
            return String(arg);
        }
    }
    return String(arg);
}

function emit(level: LogLevel, message: string, args: unknown[]): void {
    const tail = args.length ? ' ' + args.map(formatArg).join(' ') : '';
    getChannel().appendLine(`[${level}] ${message}${tail}`);
}

/**
 * Host-side logger. Each method appends a level-prefixed line to the shared
 * "AILIENANT" output channel; extra args are formatted and appended inline.
 * No focus stealing — the channel never force-shows itself, so logging stays
 * silent in the UI event loop (consistent with the host's no-toast policy).
 */
export const logger = {
    debug(message: string, ...args: unknown[]): void {
        emit('DEBUG', message, args);
    },
    log(message: string, ...args: unknown[]): void {
        emit('INFO', message, args);
    },
    warn(message: string, ...args: unknown[]): void {
        emit('WARN', message, args);
    },
    error(message: string, ...args: unknown[]): void {
        emit('ERROR', message, args);
    },
};
