import * as vscode from 'vscode';
import { createHash } from 'crypto'; // Node.js built-in — safe on Node16 extension host

export class PathResolver {
    /**
     * Returns the absolute filesystem path of the first workspace folder, or undefined
     * when no workspace is open (e.g. a single loose file).
     */
    static getWorkspaceRoot(): string | undefined {
        return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    }

    /**
     * Deterministic SHA-256 hex digest of the given path.
     * Identical input always produces identical output, even across VS Code restarts.
     */
    static computeProjectId(rootPath: string): string {
        return createHash('sha256').update(rootPath).digest('hex');
    }

    /**
     * Convenience wrapper: resolves the workspace root and hashes it.
     * Returns undefined when there is no open workspace folder.
     */
    static resolveProjectId(): string | undefined {
        const root = PathResolver.getWorkspaceRoot();
        return root ? PathResolver.computeProjectId(root) : undefined;
    }
}
