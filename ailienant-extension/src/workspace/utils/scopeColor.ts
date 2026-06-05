/**
 * Resolve a TextMate scope string to a VS Code theme CSS variable.
 *
 * VS Code does not expose per-scope syntax colors as CSS variables inside a
 * webview — only workbench theme color keys are injected, never the TextMate
 * token rules. So syntax highlighting here maps scope families onto the closest
 * curated, theme-reactive variables (the symbol-icon and debug-token palettes),
 * defaulting to the editor foreground. Because each value is a live
 * `var(--vscode-…)`, switching themes repaints with no re-tokenization.
 *
 * `type` is the space-separated scope stack the host lexer emits, ordered
 * outermost→innermost; the most specific (last) scope that we recognize wins.
 */

// [scope prefix, CSS variable, hex fallback]. The fallback keeps non-webview
// (unit-test) renders sane and covers themes that omit the variable.
type Rule = readonly [prefix: string, cssVar: string, fallback: string];

// Most-specific prefix first within a family so e.g. `keyword.operator`
// resolves before the broader `keyword`.
const RULES: readonly Rule[] = [
    ['comment', '--vscode-descriptionForeground', '#6A9955'],
    ['string', '--vscode-debugTokenExpression-string', '#CE9178'],
    ['constant.numeric', '--vscode-debugTokenExpression-number', '#B5CEA8'],
    ['constant.language', '--vscode-debugTokenExpression-boolean', '#569CD6'],
    ['constant.character', '--vscode-debugTokenExpression-string', '#CE9178'],
    ['keyword.operator', '--vscode-symbolIcon-operatorForeground', '#D4D4D4'],
    ['keyword', '--vscode-symbolIcon-keywordForeground', '#569CD6'],
    ['storage', '--vscode-symbolIcon-keywordForeground', '#569CD6'],
    ['support.function', '--vscode-symbolIcon-functionForeground', '#DCDCAA'],
    ['entity.name.function', '--vscode-symbolIcon-functionForeground', '#DCDCAA'],
    ['meta.function-call', '--vscode-symbolIcon-functionForeground', '#DCDCAA'],
    ['support.type', '--vscode-symbolIcon-classForeground', '#4EC9B0'],
    ['support.class', '--vscode-symbolIcon-classForeground', '#4EC9B0'],
    ['entity.name.type', '--vscode-symbolIcon-classForeground', '#4EC9B0'],
    ['entity.name.class', '--vscode-symbolIcon-classForeground', '#4EC9B0'],
    ['entity.name.namespace', '--vscode-symbolIcon-classForeground', '#4EC9B0'],
    ['entity.name.tag', '--vscode-symbolIcon-keywordForeground', '#569CD6'],
    ['entity.other.attribute-name', '--vscode-symbolIcon-variableForeground', '#9CDCFE'],
    ['variable.parameter', '--vscode-symbolIcon-variableForeground', '#9CDCFE'],
    ['support.variable', '--vscode-symbolIcon-variableForeground', '#9CDCFE'],
    ['variable', '--vscode-symbolIcon-variableForeground', '#9CDCFE'],
    ['entity.name', '--vscode-symbolIcon-functionForeground', '#DCDCAA'],
];

const DEFAULT_VAR = '--vscode-editor-foreground';
const DEFAULT_FALLBACK = '#D4D4D4';

function matches(scope: string, prefix: string): boolean {
    return scope === prefix || scope.startsWith(`${prefix}.`);
}

function ruleFor(scope: string): Rule | undefined {
    for (const rule of RULES) {
        if (matches(scope, rule[0])) { return rule; }
    }
    return undefined;
}

/** Map a raw scope string to a `var(--vscode-…, #fallback)` color expression. */
export function scopeColor(type: string): string {
    if (type) {
        const scopes = type.split(/\s+/);
        for (let i = scopes.length - 1; i >= 0; i--) {
            const rule = ruleFor(scopes[i]);
            if (rule) { return `var(${rule[1]}, ${rule[2]})`; }
        }
    }
    return `var(${DEFAULT_VAR}, ${DEFAULT_FALLBACK})`;
}
