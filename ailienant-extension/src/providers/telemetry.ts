// ailienant-extension/src/providers/telemetry.ts
// Phase 3.4.7 — Silent Bounding Box telemetry.
//
// Tracks blocks of AI-merged code. If the user edits >=70% of the original
// characters within 3 minutes of the merge, fire `AI_PAYLOAD_REJECTED` to the
// backend and untrack the box. Decay heuristic is O(1) per change event.

import * as vscode from 'vscode';
import { APIClient } from '../api/api_client';

const REJECTION_RATIO = 0.70;
const REJECTION_WINDOW_MS = 3 * 60 * 1000; // 3 minutes

export interface BoundingBox {
    uri: string;            // absolute fsPath
    workspaceRoot: string;  // absolute workspace fsPath (used by backend to locate .ailienant.json)
    originalText: string;
    originalLength: number;
    timestamp: number;      // Date.now() at registration
    cumulativeChangedChars: number;
}

export class BoundingBoxRegistry {
    private boxes = new Map<string, BoundingBox>();

    public register(box: Omit<BoundingBox, 'cumulativeChangedChars'>): void {
        this.boxes.set(box.uri, { ...box, cumulativeChangedChars: 0 });
    }

    public get(uri: string): BoundingBox | undefined {
        return this.boxes.get(uri);
    }

    public untrack(uri: string): void {
        this.boxes.delete(uri);
    }

    public size(): number {
        return this.boxes.size;
    }

    /**
     * Process a TextDocumentChangeEvent. Returns the box if it just exceeded
     * the rejection threshold, else null. Pure / testable — no I/O, no async.
     */
    public processChange(event: vscode.TextDocumentChangeEvent): BoundingBox | null {
        const uri = event.document.uri.fsPath;
        const box = this.boxes.get(uri);
        if (!box) {
            return null;
        }
        const now = Date.now();
        if (now - box.timestamp > REJECTION_WINDOW_MS) {
            this.boxes.delete(uri);
            return null;
        }
        for (const change of event.contentChanges) {
            box.cumulativeChangedChars += change.rangeLength;
        }
        if (box.cumulativeChangedChars >= REJECTION_RATIO * box.originalLength) {
            return box;
        }
        return null;
    }
}

export function installDecayListener(
    context: vscode.ExtensionContext,
    registry: BoundingBoxRegistry,
): void {
    const subscription = vscode.workspace.onDidChangeTextDocument(async (event) => {
        const fired = registry.processChange(event);
        if (!fired) {
            return;
        }
        // Detach immediately so a single noisy document doesn't spam the backend.
        registry.untrack(fired.uri);
        const currentUserCode = event.document.getText();
        await APIClient.getInstance().reportRejection({
            uri: fired.uri,
            original_ai_code: fired.originalText,
            current_user_code: currentUserCode,
            timestamp: fired.timestamp,
            workspace_root: fired.workspaceRoot,
        });
    });
    context.subscriptions.push(subscription);
}

// Module-level singleton consumed by mirror.ts on merge success.
export const boundingBoxRegistry = new BoundingBoxRegistry();
