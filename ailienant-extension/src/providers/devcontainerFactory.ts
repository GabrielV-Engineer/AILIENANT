// AILIENANT — Devcontainer provisioner factory (host wiring).
//
// Binds the vscode-free DevcontainerProvisioner core to real host capabilities
// (child_process, vscode extension probe, fs, the AILIENANT logger) and exposes a
// process-wide singleton. Kept separate from the core so the core stays free of a
// `vscode` import and its contract test remains hermetic.

import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as fs from 'fs';
import { logger } from '../shared/logger';
import { DevcontainerProvisioner, ProvisionerDeps } from './devcontainerProvisioner';

let _instance: DevcontainerProvisioner | null = null;

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
