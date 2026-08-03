// AILIENANT — Devcontainer provisioner + session-handler factory (host wiring).
//
// Binds the vscode-free DevcontainerProvisioner and DevcontainerSessionHandler
// cores to real host capabilities (child_process, vscode extension probe, fs,
// the AILIENANT logger) and exposes process-wide singletons. Kept separate
// from the cores so they stay free of a `vscode` import and their contract
// tests remain hermetic.

import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import { logger } from '../shared/logger';
import { WSClient } from '../api/ws_client';
import { DevcontainerProvisioner, ProvisionerDeps } from './devcontainerProvisioner';
import { DevcontainerSessionHandler, DevcontainerSessionHandlerDeps } from './devcontainerSessionHandler';

let _instance: DevcontainerProvisioner | null = null;
let _sessionInstance: DevcontainerSessionHandler | null = null;

/**
 * Resolve the bundled `@devcontainers/cli` entry when present (dev/unpackaged).
 * It is a soft/optional dependency and is NOT shipped in the packaged `.vsix`
 * (node_modules is excluded), so this returns null there — the driver then falls
 * back to PATH / the Dev Containers extension and degrades when neither exists.
 */
function resolveBundledCliEntry(): string | null {
    try {
        return require.resolve('@devcontainers/cli');
    } catch {
        return null;
    }
}

function realDeps(): ProvisionerDeps {
    return {
        spawn: (command, args, options) => cp.spawn(command, args, options),
        isExtensionInstalled: (id) => vscode.extensions.getExtension(id) !== undefined,
        fileExists: (p) => fs.existsSync(p),
        log: (message) => logger.log(`[devcontainer] ${message}`),
        bundledCliEntry: resolveBundledCliEntry(),
    };
}

/** Lazily construct (once) the process-wide devcontainer lifecycle owner. */
export function getDevcontainerProvisioner(): DevcontainerProvisioner {
    if (!_instance) {
        _instance = new DevcontainerProvisioner(realDeps());
    }
    return _instance;
}

/** Tear down the singleton (kills any in-flight child). Idempotent. */
export function disposeDevcontainerProvisioner(): void {
    _instance?.dispose();
    _instance = null;
}

function realSessionDeps(): DevcontainerSessionHandlerDeps {
    return {
        provisioner: getDevcontainerProvisioner(),
        spawn: (command, args, options) => cp.spawn(command, args, options),
        workspaceRoot: () => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
        send: (m) => WSClient.getInstance().send(m as never),
        env: process.env,
        log: (message) => logger.log(`[devcontainer-session] ${message}`),
    };
}

/** Lazily construct (once) the process-wide devcontainer session-handler owner. */
export function getDevcontainerSessionHandler(): DevcontainerSessionHandler {
    if (!_sessionInstance) {
        _sessionInstance = new DevcontainerSessionHandler(realSessionDeps());
    }
    return _sessionInstance;
}

/** Kill every tracked session and tear down the singleton. Idempotent. */
export function disposeDevcontainerSessionHandler(): void {
    _sessionInstance?.dispose();
    _sessionInstance = null;
}
