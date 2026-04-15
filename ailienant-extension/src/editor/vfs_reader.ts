"ailienant-extension/src/editor/vfs_reader.ts"

import * as vscode from 'vscode';

// Contrato de datos alineado con el esquema del backend (FastAPI)
export interface DirtyBuffer {
    uri: string;        // Ruta absoluta del archivo
    content: string;    // Entropía actual (código sin guardar)
    version: number;    // ID de versión nativo del LSP para resolución de conflictos
    languageId: string; // Útil para que el GraphRAG sepa qué parser AST usar
}

export class VFSReader {
    /**
     * Límite de seguridad de 1MB por buffer. 
     * Previene el bloqueo del Extension Host y sobrecarga de red.
     */
    private static readonly MAX_BUFFER_SIZE_BYTES = 1024 * 1024;

    /**
     * Extrae el estado real del IDE (Entropía).
     * @returns Un array de buffers no guardados, filtrados y seguros.
     */
    public static captureEntropy(): DirtyBuffer[] {
        const dirtyBuffers: DirtyBuffer[] = [];
        const documents = vscode.workspace.textDocuments;

        for (const doc of documents) {
            // 1. Filtrar ruido: Solo archivos físicos reales del disco
            if (doc.uri.scheme !== 'file') {
                continue;
            }

            // 2. Filtrar estado: Solo archivos con cambios sin guardar
            if (!doc.isDirty) {
                continue;
            }

            const textContent = doc.getText();

            // 3. SecOps & Performance: Bloquear payloads masivos
            // Asumimos ~1 byte por caracter en ASCII estándar
            if (textContent.length > this.MAX_BUFFER_SIZE_BYTES) {
                vscode.window.showWarningMessage(`AILIENANT: El archivo ${doc.fileName} es demasiado grande y sus cambios no guardados serán ignorados por la IA.`);
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