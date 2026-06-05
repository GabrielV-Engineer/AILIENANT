import * as path from 'path';
import { createHighlighterCore, type HighlighterCore, type ThemedToken } from 'shiki/core';
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript';
import langTypescript from 'shiki/langs/typescript.mjs';
import langTsx from 'shiki/langs/tsx.mjs';
import langJavascript from 'shiki/langs/javascript.mjs';
import langJsx from 'shiki/langs/jsx.mjs';
import langPython from 'shiki/langs/python.mjs';
import langJson from 'shiki/langs/json.mjs';
import langBash from 'shiki/langs/bash.mjs';
import langCss from 'shiki/langs/css.mjs';
import langHtml from 'shiki/langs/html.mjs';
import langMarkdown from 'shiki/langs/markdown.mjs';
import themeGithubDark from 'shiki/themes/github-dark.mjs';
import type { ASTToken } from '../shared/config';
import type { PatchedFileDiff } from './PatchActuator';

/**
 * Host-side TextMate lexer for inline diffs.
 *
 * The grammar engine runs in the extension host (Node) rather than the webview:
 * the webview bundle is a single non-splittable IIFE with a hard size ceiling, and
 * a real grammar engine plus its grammars would blow it. Running here — where there
 * is no bundle ceiling — lets the host emit a token AST that the webview paints with
 * zero parsing dependency of its own.
 *
 * Tokens are emitted as raw TextMate scope strings (the renderer resolves the most
 * specific scope to a theme colour), so highlighting stays theme-reactive without the
 * host having to re-tokenize when the user switches themes.
 */

// A theme is required by the shiki API to tokenize, but the emitted `type` is the
// grammar scope — not the resolved colour — so the specific theme is immaterial here.
const THEME = 'github-dark';

// Tokenizing the full pre/post text of a multi-thousand-line file would peg the host
// main thread for no visual gain (the webview collapses oversized diffs anyway). Above
// these bounds we skip lexing and let the webview fall back to themed monospace.
const MAX_LEX_CHARS = 200_000;
const MAX_LEX_LINES = 5_000;

// File extension → the grammar id the engine registers. Anything absent resolves to a
// monospace fallback rather than an error.
const EXT_TO_LANG: Readonly<Record<string, string>> = {
    '.ts': 'typescript',
    '.mts': 'typescript',
    '.cts': 'typescript',
    '.tsx': 'tsx',
    '.js': 'javascript',
    '.mjs': 'javascript',
    '.cjs': 'javascript',
    '.jsx': 'jsx',
    '.py': 'python',
    '.pyi': 'python',
    '.json': 'json',
    '.sh': 'bash',
    '.bash': 'bash',
    '.zsh': 'bash',
    '.css': 'css',
    '.html': 'html',
    '.htm': 'html',
    '.md': 'markdown',
    '.markdown': 'markdown',
};

let highlighterPromise: Promise<HighlighterCore> | null = null;

function getHighlighter(): Promise<HighlighterCore> {
    if (!highlighterPromise) {
        highlighterPromise = createHighlighterCore({
            themes: [themeGithubDark],
            langs: [
                langTypescript,
                langTsx,
                langJavascript,
                langJsx,
                langPython,
                langJson,
                langBash,
                langCss,
                langHtml,
                langMarkdown,
            ],
            engine: createJavaScriptRegexEngine(),
        }).catch((err: unknown) => {
            // Drop the cached rejection so a later edit can retry the (one-time) init.
            highlighterPromise = null;
            throw err;
        });
    }
    return highlighterPromise;
}

function langForPath(filePath: string): string | undefined {
    return EXT_TO_LANG[path.extname(filePath).toLowerCase()];
}

// One shiki token can span several grammar scopes (e.g. "(x):" splits into the paren,
// the parameter and the colon). Flattening per explanation segment preserves the same
// scope granularity VS Code emits, instead of collapsing the run to a single scope.
function lineToAst(line: ThemedToken[]): ASTToken[] {
    const out: ASTToken[] = [];
    for (const token of line) {
        const segments = token.explanation;
        if (segments && segments.length > 0) {
            for (const seg of segments) {
                if (seg.content.length === 0) { continue; }
                out.push({ type: seg.scopes.map(s => s.scopeName).join(' '), content: seg.content });
            }
        } else if (token.content.length > 0) {
            out.push({ type: '', content: token.content });
        }
    }
    return out;
}

/**
 * Tokenize `content` into one ASTToken array per source line, row-aligned to the input.
 * Returns undefined when the language is unsupported, the input is empty or over the
 * size bounds, or the engine faults — every such case degrades to monospace rendering.
 */
async function tokenizeToAstLines(content: string, filePath: string): Promise<ASTToken[][] | undefined> {
    const lang = langForPath(filePath);
    if (!lang || content.length === 0 || content.length > MAX_LEX_CHARS) {
        return undefined;
    }
    try {
        const highlighter = await getHighlighter();
        const lines = highlighter.codeToTokensBase(content, {
            lang,
            theme: THEME,
            includeExplanation: 'scopeName',
        });
        if (lines.length > MAX_LEX_LINES) {
            return undefined;
        }
        return lines.map(lineToAst);
    } catch {
        return undefined;
    }
}

/**
 * Populate `old_ast_lines` / `new_ast_lines` on each diff in place. Best-effort: any
 * file that cannot be tokenized simply keeps its arrays undefined. Never throws, so it
 * is safe to await on the hot patch-applied path without risking the diff render.
 */
async function enrich(diffs: PatchedFileDiff[]): Promise<void> {
    await Promise.all(
        diffs.map(async (diff) => {
            const [oldLines, newLines] = await Promise.all([
                tokenizeToAstLines(diff.old_content, diff.file_path),
                tokenizeToAstLines(diff.new_content, diff.file_path),
            ]);
            if (oldLines) { diff.old_ast_lines = oldLines; }
            if (newLines) { diff.new_ast_lines = newLines; }
        }),
    );
}

export const GrammarLexer = { enrich, tokenizeToAstLines };
