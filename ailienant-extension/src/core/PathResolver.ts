import * as vscode from 'vscode';
import * as path from 'path';
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
     * Normalize a filesystem path so it hashes identically across casing, separator
     * style and a trailing slash — the byte-for-byte equivalent of the backend's
     * `os.path.normcase(os.path.normpath(...))`. Node's `path.normalize` is NOT a
     * drop-in match: it keeps a non-root trailing separator (`C:\Proj\` stays
     * `C:\Proj\`) where Python strips it, so the trailing-sep removal below is
     * essential to keep the two `sha256` inputs in sync. The disk/UNC/POSIX root is
     * preserved (stripping `C:\` to `C:` would re-key and orphan the index).
     */
    private static normalizeForId(rootPath: string): string {
        const isWin = process.platform === 'win32';
        let n = isWin ? path.win32.normalize(rootPath) : path.posix.normalize(rootPath);
        // Mirror normpath: drop exactly one trailing separator unless `n` is a root.
        const isRoot = isWin
            ? /^[A-Za-z]:\\$/.test(n) || n === '\\'
            : /^\/+$/.test(n);
        if (!isRoot && (n.endsWith('\\') || n.endsWith('/'))) {
            n = n.slice(0, -1);
        }
        // Mirror normcase: lowercase on Windows, identity on POSIX.
        return isWin ? n.toLowerCase() : n;
    }

    /**
     * Deterministic SHA-256 hex digest of the given path, normalized so a workspace
     * always yields the same id regardless of casing, separators or a trailing slash.
     * Identical input always produces identical output, even across VS Code restarts.
     */
    static computeProjectId(rootPath: string): string {
        return createHash('sha256').update(PathResolver.normalizeForId(rootPath)).digest('hex');
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
