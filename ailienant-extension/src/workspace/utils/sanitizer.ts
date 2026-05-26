/**
 * Phase 7.11.6 (ADR-706 §4.5f) — DOMPurify chokepoint for tool-derived output.
 *
 * VS Code WebViews run with elevated privileges (they can post messages back
 * to the host that may invoke commands), so an XSS vector here can become an
 * RCE on the user's machine. ALL strings that originate outside the IDE — tool
 * stdout/stderr, MCP responses, third-party sandbox text — MUST pass through
 * one of the two functions in this module before reaching `innerHTML` /
 * `dangerouslySetInnerHTML`.
 *
 * The current `ToolChip` design prefers React JSX (which auto-escapes text
 * nodes); the sanitizer is the seatbelt for the edge cases where structured
 * HTML is actually desired (e.g., the future "diff render" feature). For
 * plain text — the common case — call `sanitizeText`, not `sanitizeHtml`.
 *
 * Runtime: in the WebView (production) `globalThis.window` is the DOM and
 * DOMPurify auto-detects it; in the extension-host test rig (Node) we lazily
 * fall back to a `jsdom` window. The jsdom dependency is `devDependencies`
 * only and esbuild externalizes it for the production bundle so it never
 * ships to users.
 */

// DOMPurify ships as CommonJS with `export =` typings, so we use TS's
// `import = require()` form to avoid needing `esModuleInterop` (which the
// project's tsconfig deliberately leaves off).
import DOMPurifyFactory = require('dompurify');

/**
 * Strict profile for tool-output HTML: only inline formatting + ANSI color
 * spans (`<span class="ansi-red">`) survive. NO links, NO images, NO scripts,
 * NO event handlers. `<style>` (the *tag*) is also forbidden; the `style`
 * *attribute* is allowed so 24-bit truecolor (`38;2;r;g;b`) can survive as
 * `<span style="color: rgb(255,0,0)">…</span>`. DOMPurify parses the style
 * value and strips dangerous URLs (e.g. `background-image: url(javascript:…)`).
 *
 * Typed as `Record<string, unknown>` because `@types/dompurify`'s `Config`
 * type lives inside a namespace that doesn't merge cleanly with the
 * `import = require()` form under Node16 module resolution; DOMPurify
 * accepts the object structurally regardless.
 */
const HTML_PROFILE: Record<string, unknown> = {
    // Only inline formatting + ANSI color spans (by `class`) survive.
    ALLOWED_TAGS: ['span', 'code', 'pre', 'br', 'strong', 'em', 'i', 'b'],
    // CRITICAL: `style` is INTENTIONALLY NOT allowed. DOMPurify v3 does not
    // sanitize CSS *values* inside style attributes — a `style="background:
    // url(javascript:alert(1))"` round-trips intact. Tool-emitted HTML must
    // therefore use safe color classes (`.ansi-red`) instead. 24-bit truecolor
    // (`38;2;r;g;b` from ansiParser) flows through React JSX `style={{...}}`
    // directly, NOT through this sanitizer — JSX style objects can only
    // contain CSS property names + values, never executable URLs.
    ALLOWED_ATTR: ['class'],
    FORBID_TAGS: ['a', 'img', 'iframe', 'script', 'object', 'embed', 'svg', 'style'],
    FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur'],
    KEEP_CONTENT: true,
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
};

const TEXT_PROFILE: Record<string, unknown> = {
    ALLOWED_TAGS: [],
    ALLOWED_ATTR: [],
    KEEP_CONTENT: true,
};

/** Minimal shape we use from the DOMPurify instance — `sanitize` is all we
 *  call, with the config object passed through structurally. */
interface DOMPurifyInstance {
    sanitize(source: string, config: Record<string, unknown>): string;
}

// Lazy singleton — initialised on first use so the jsdom fallback only loads
// when we're running in a Node context (tests, never production).
let _instance: DOMPurifyInstance | null = null;

function getPurify(): DOMPurifyInstance {
    if (_instance) { return _instance; }
    const win = (globalThis as unknown as { window?: unknown }).window;
    if (win) {
        // Browser / WebView context — DOMPurify auto-detects `window`.
        _instance = (DOMPurifyFactory as unknown as (w: unknown) => DOMPurifyInstance)(win);
    } else {
        // Node-side fallback for the vscode-test extension-host rig. `jsdom`
        // is a devDependency and is externalised in production esbuild
        // bundling (see esbuild.js), so this branch never ships to users.
        const { JSDOM } = require('jsdom');
        _instance = (DOMPurifyFactory as unknown as (w: unknown) => DOMPurifyInstance)(
            new JSDOM('').window,
        );
    }
    return _instance!;
}

/**
 * Sanitize a string that is *intended* to be DOM-injected as HTML. Returns a
 * scrubbed string safe to pass to `dangerouslySetInnerHTML`. Prefer
 * `sanitizeText` for plain-text outputs — JSX text nodes are auto-escaped by
 * React and need no sanitizer pass to be safe.
 */
export function sanitizeHtml(raw: string): string {
    if (!raw) { return ''; }
    return getPurify().sanitize(raw, HTML_PROFILE);
}

/**
 * Defense-in-depth: strip every HTML tag from a string before using it as a
 * React text node. React already auto-escapes text nodes, so this is a
 * belt-and-suspenders helper — useful when the same string flows through both
 * a JSX text path and an HTML-injection path (e.g. copy-to-clipboard buttons).
 */
export function sanitizeText(raw: string): string {
    if (!raw) { return ''; }
    return getPurify().sanitize(raw, TEXT_PROFILE);
}
