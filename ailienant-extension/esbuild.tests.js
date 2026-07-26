const esbuild = require("esbuild");
const fs = require("fs");

// Bundles each src/test/*.test.ts file independently (esbuild resolves every
// import, including extensionless relative paths and shiki's ESM exports maps,
// unlike a plain `tsc` emit). The mocha loader then requires a single
// self-contained CommonJS file per test — no runtime module resolution left
// to trip over. Kept separate from esbuild.js (the production bundle) so the
// default `node esbuild.js` invocation used by compile/package/watch:esbuild
// is structurally unaffected by this file's existence.
const OUT_DIR = "out/test";

async function main() {
    // Clear stale output so removed/renamed test files don't linger and get
    // picked up by .vscode-test.mjs's out/test/**/*.test.js glob.
    fs.rmSync(OUT_DIR, { recursive: true, force: true });

    await esbuild.build({
        entryPoints: ["src/test/*.test.ts"],
        bundle: true,
        format: "cjs",
        platform: "node",
        outdir: OUT_DIR,
        outbase: "src/test",
        sourcemap: true,
        sourcesContent: false,
        // jsdom is external: it loads its own internal assets (e.g. the default
        // CSS stylesheet, the sync XHR worker) via __dirname-relative paths at
        // runtime. Bundling it in rewrites __dirname to out/test/, breaking that
        // resolution. Left external, Node's normal require('jsdom') resolves it
        // from node_modules with jsdom's real __dirname intact.
        external: ["vscode", "jsdom"],
        logLevel: "info",
    });
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
