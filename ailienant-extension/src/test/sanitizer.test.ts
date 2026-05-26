/**
 * Phase 7.11.6 (ADR-706 §4.5f) — DOMPurify sanitizer contract tests.
 *
 * The mandate: NO scripts, NO event handlers, NO `<a>`/`<img>` survive.
 * Allowed inline structure (`<span class="ansi-*">`, `<strong>`, `<em>`,
 * `<code>`, `<pre>`, `<br>`) DOES survive — that's the surface our ANSI mini-
 * terminal renders into when the chip's output is a structured HTML string.
 *
 * vscode-test runs in a real VS Code Electron host, so `window` exists and
 * DOMPurify can initialise itself. No JSDOM seam needed.
 */
import * as assert from 'assert';
import { sanitizeHtml, sanitizeText } from '../workspace/utils/sanitizer';

suite('Phase 7.11.6 — sanitizer (DOMPurify chokepoint)', function () {
    // jsdom's first initialization (window + DOMParser warmup) can take >2s
    // on cold CI. Warm the sanitizer once before any test runs so the
    // per-test default mocha timeout of 2000ms is plenty for actual sanitize
    // calls (each cached call is sub-millisecond).
    this.timeout(20_000);
    suiteSetup(() => {
        // Drive the lazy-init path so subsequent tests don't pay the cost.
        sanitizeHtml('warmup');
    });

    test('strips <script> tags entirely', () => {
        const dirty = 'Hello <script>alert(1)</script> World';
        const clean = sanitizeHtml(dirty);
        assert.ok(!clean.toLowerCase().includes('<script'), `<script> survived: ${clean}`);
        // Text content is preserved per KEEP_CONTENT — but the actual alert()
        // call literal stays as text, which is harmless and visible to user.
        assert.ok(clean.includes('Hello'));
        assert.ok(clean.includes('World'));
    });

    test('strips <img> with onerror handler', () => {
        const dirty = 'before <img src=x onerror="alert(1)"> after';
        const clean = sanitizeHtml(dirty);
        assert.ok(!clean.toLowerCase().includes('<img'),     `<img> survived: ${clean}`);
        assert.ok(!clean.toLowerCase().includes('onerror'),  `onerror survived: ${clean}`);
        assert.ok(!clean.toLowerCase().includes('alert('),   `alert() call survived: ${clean}`);
    });

    test('strips <a href="javascript:..."> entirely', () => {
        const dirty = '<a href="javascript:alert(1)">click</a>';
        const clean = sanitizeHtml(dirty);
        assert.ok(!clean.toLowerCase().includes('<a'),         `<a> survived: ${clean}`);
        assert.ok(!clean.toLowerCase().includes('javascript:'), `javascript: URL survived: ${clean}`);
        // Anchor text content is preserved (KEEP_CONTENT) — that's expected.
        assert.ok(clean.includes('click'));
    });

    test('allowed tags + class attribute survive untouched', () => {
        const dirty = '<span class="ansi-red"><strong>err</strong></span>';
        const clean = sanitizeHtml(dirty);
        assert.ok(clean.includes('ansi-red'),  'ansi class must survive');
        assert.ok(clean.includes('<strong'),   '<strong> must survive');
        assert.ok(clean.includes('err'),       'inner text must survive');
    });

    test('the style attribute is forbidden entirely (CSS sanitization is unsafe)', () => {
        // DOMPurify v3 does NOT sanitize CSS values inside style attributes,
        // so we forbid the attribute outright. Truecolor (24-bit) reaches the
        // DOM via React JSX `style={{...}}` from the ANSI parser — never
        // through this sanitizer. See the ALLOWED_ATTR comment in sanitizer.ts.
        const safeLooking = '<span style="color: rgb(255,0,0)">red</span>';
        const cleanSafe = sanitizeHtml(safeLooking);
        assert.ok(!cleanSafe.toLowerCase().includes('style='),
            `style attribute should be stripped, got: ${cleanSafe}`);
        // The span + content still survive.
        assert.ok(cleanSafe.includes('red'));

        const dangerous = '<span style="background: url(javascript:alert(1))">x</span>';
        const cleanDangerous = sanitizeHtml(dangerous);
        assert.ok(!cleanDangerous.toLowerCase().includes('javascript:'),
            `javascript: URL survived: ${cleanDangerous}`);
        assert.ok(!cleanDangerous.toLowerCase().includes('style='),
            'style attribute should be stripped');
    });

    test('sanitizeText drops EVERY tag (defense in depth for text nodes)', () => {
        // Used when a tool emits free-form text that we want to render via
        // JSX text nodes (React already escapes, but we belt-and-suspenders).
        const dirty = '<strong>bold</strong> and <i>italic</i> and <script>x</script>';
        const clean = sanitizeText(dirty);
        assert.ok(!clean.includes('<strong'), '<strong> must be stripped in text mode');
        assert.ok(!clean.includes('<i'),      '<i> must be stripped in text mode');
        assert.ok(!clean.includes('<script'), '<script> must be stripped in text mode');
        // Inner text survives.
        assert.ok(clean.includes('bold'));
        assert.ok(clean.includes('italic'));
    });

    test('sanitizers are no-ops on empty strings', () => {
        assert.strictEqual(sanitizeHtml(''), '');
        assert.strictEqual(sanitizeText(''), '');
    });
});
