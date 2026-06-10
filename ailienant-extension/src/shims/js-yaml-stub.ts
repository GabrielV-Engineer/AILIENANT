/**
 * Webview-only stub for js-yaml.
 *
 * react-diff-viewer-continued pulls in the full js-yaml parser (~39 KB minified)
 * solely for its structural YAML diff mode (compareMethod === DiffMethod.YAML).
 * The chat surface only ever diffs source code with the default character/word
 * compare method, so that code path is never reached at runtime. Aliasing js-yaml
 * to this stub in the workspace build keeps the dead parser off the IIFE bundle.
 *
 * The functions throw rather than degrade silently: if a future change ever routes
 * a real YAML diff through the webview, it fails loudly here instead of rendering a
 * wrong diff — the signal to bundle the real parser (or code-split it) at that time.
 */
const STUB_MESSAGE =
    'js-yaml is stubbed out of the webview bundle; structural YAML diffs are not supported here.';

export function load(): never {
    throw new Error(STUB_MESSAGE);
}

export function dump(): never {
    throw new Error(STUB_MESSAGE);
}

export default { load, dump };
