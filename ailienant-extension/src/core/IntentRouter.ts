import * as vscode from 'vscode';

interface IntentPattern {
    regex: RegExp;
    label: string;
    handler: (document?: vscode.TextDocument) => Promise<boolean>;
}

export class IntentRouter {
    private static readonly PATTERNS: IntentPattern[] = [
        {
            regex: /\b(format|embellecer|formatear)\b/i,
            label: 'Format document',
            handler: async () => {
                await vscode.commands.executeCommand('editor.action.formatDocument');
                return true;
            },
        },
        {
            regex: /\b(constify|let\s+to\s+const|convert\s+let\s+to\s+const)\b/i,
            label: 'Convert let to const',
            handler: async (document?) => {
                if (!document) { return false; }
                return IntentRouter._applyConstify(document);
            },
        },
    ];

    /**
     * Intercepts the prompt before it reaches the backend.
     * Returns true if handled locally (caller must NOT call startAITask).
     */
    static async intercept(prompt: string, document?: vscode.TextDocument): Promise<boolean> {
        console.time('intent_router');
        try {
            for (const pattern of IntentRouter.PATTERNS) {
                if (pattern.regex.test(prompt)) {
                    const handled = await pattern.handler(document);
                    if (handled) {
                        vscode.window.setStatusBarMessage('AILIENANT: Local optimization applied', 3000);
                        return true;
                    }
                }
            }
            return false;
        } finally {
            console.timeEnd('intent_router');
        }
    }

    private static async _applyConstify(document: vscode.TextDocument): Promise<boolean> {
        const text = document.getText();
        const edit = new vscode.WorkspaceEdit();
        let changed = false;

        const letDecl = /\blet\s+([a-zA-Z_$][a-zA-Z0-9_$]*)\s*=/g;
        let match: RegExpExecArray | null;
        while ((match = letDecl.exec(text)) !== null) {
            const name = match[1];
            // Covers =, +=, -=, *=, /=, %=, **=, &=, |=, ^=, <<=, >>=, >>>=, ++, --
            const reassign = new RegExp(
                `\\b${name}\\s*(?:=(?!=)|[+\\-*/%&|^]=|\\*\\*=|<<=|>>=|>>>=|\\+\\+|--)`,
                'g'
            );
            const hits = Array.from(text.matchAll(reassign));
            // Only one hit = the declaration itself → safe to promote to const
            if (hits.length === 1) {
                const start = document.positionAt(match.index);
                const end = document.positionAt(match.index + 3); // 'let'.length === 3
                edit.replace(document.uri, new vscode.Range(start, end), 'const');
                changed = true;
            }
        }

        if (changed) {
            await vscode.workspace.applyEdit(edit);
        }
        return changed;
    }
}
