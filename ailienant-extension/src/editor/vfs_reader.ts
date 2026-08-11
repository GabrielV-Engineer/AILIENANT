import * as vscode from 'vscode';

// Data contract aligned with the backend (FastAPI) schema.
export interface DirtyBuffer {
    uri: string;        // Absolute path to the file
    content: string;    // Current unsaved code
    version: number;    // Native LSP version id, used for conflict resolution
    languageId: string; // Lets GraphRAG pick the right AST parser
}

export class VFSReader {
    /**
     * 1 MB safety ceiling per buffer.
     * Prevents Extension Host stalls and network overload.
     */
    private static readonly MAX_BUFFER_SIZE_BYTES = 1024 * 1024;

    /**
     * Capture the IDE's real unsaved state.
     * @returns A filtered, size-safe array of unsaved buffers.
     */
    public static captureEntropy(): DirtyBuffer[] {
        const dirtyBuffers: DirtyBuffer[] = [];
        const documents = vscode.workspace.textDocuments;

        for (const doc of documents) {
            // 1. Filter noise: real on-disk files only
            if (doc.uri.scheme !== 'file') {
                continue;
            }

            // 2. Filter state: only files with unsaved changes
            if (!doc.isDirty) {
                continue;
            }

            const textContent = doc.getText();

            // 3. SecOps & performance: reject oversized payloads.
            // Assumes ~1 byte per character for standard ASCII.
            if (textContent.length > this.MAX_BUFFER_SIZE_BYTES) {
                vscode.window.showWarningMessage(`AILIENANT: ${doc.fileName} is too large — its unsaved changes will be ignored by the AI.`);
                continue;
            }

            dirtyBuffers.push({
                uri: doc.uri.fsPath,
                content: textContent,
                version: doc.version,
                languageId: doc.languageId
            });
        }

        return dirtyBuffers;
    }
}