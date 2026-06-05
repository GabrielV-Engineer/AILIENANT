const esbuild = require("esbuild");
const fs = require("fs");

const production = process.argv.includes('--production');
const watch = process.argv.includes('--watch');

/**
 * @type {import('esbuild').Plugin}
 */
const esbuildProblemMatcherPlugin = {
	name: 'esbuild-problem-matcher',

	setup(build) {
		build.onStart(() => {
			console.log('[watch] build started');
		});
		build.onEnd((result) => {
			result.errors.forEach(({ text, location }) => {
				console.error(`✘ [ERROR] ${text}`);
				console.error(`    ${location.file}:${location.line}:${location.column}:`);
			});
			console.log('[watch] build finished');
		});
	},
};

async function main() {
	// Extension host (Node, CJS)
	const ctx = await esbuild.context({
		entryPoints: ['src/extension.ts'],
		bundle: true,
		format: 'cjs',
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'node',
		outfile: 'dist/extension.js',
		external: ['vscode'],
		logLevel: 'silent',
		plugins: [esbuildProblemMatcherPlugin],
	});

	// Sidebar — session browser, minimal IIFE (~30KB target)
	const sidebarCtx = await esbuild.context({
		entryPoints: ['src/sidebar/main.tsx'],
		bundle: true,
		format: 'iife',
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'browser',
		outfile: 'dist/sidebar.js',
		logLevel: 'silent',
		plugins: [esbuildProblemMatcherPlugin],
	});

	// Workspace — full editor-tab chat UI (replaces old webview.js).
	// Phase 7.11.6 — `jsdom` is a test-only fallback used by sanitizer.ts when
	// `window` is absent (extension-host Node rig). The WebView always has
	// `window`, so we externalise jsdom here to keep the production bundle
	// lean. esbuild emits a runtime `require()` call that never fires.
	const workspaceCtx = await esbuild.context({
		entryPoints: ['src/workspace/main.tsx'],
		bundle: true,
		format: 'iife',
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'browser',
		outfile: 'dist/workspace.js',
		external: ['jsdom'],
		logLevel: 'silent',
		plugins: [esbuildProblemMatcherPlugin],
	});

	// Dashboard SPA — ESM with code splitting so Monaco loads lazily
	const dashboardCtx = await esbuild.context({
		entryPoints: ['src/dashboard/main.tsx'],
		bundle: true,
		format: 'esm',
		splitting: true,
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'browser',
		outdir: 'dist/dashboard',
		chunkNames: 'chunks/[name]-[hash]',
		loader: { '.svg': 'dataurl' },
		logLevel: 'silent',
		plugins: [esbuildProblemMatcherPlugin],
	});

	// Copy dashboard index.html into dist on every build
	fs.copyFileSync('src/dashboard/index.html', 'dist/dashboard/index.html');

	if (watch) {
		await ctx.watch();
		await sidebarCtx.watch();
		await workspaceCtx.watch();
		await dashboardCtx.watch();
	} else {
		await ctx.rebuild();        await ctx.dispose();
		await sidebarCtx.rebuild();  await sidebarCtx.dispose();
		await workspaceCtx.rebuild(); await workspaceCtx.dispose();
		assertGrammarEngineOffWebview();
		assertWebviewBundleUnderCeiling();
		await dashboardCtx.rebuild(); await dashboardCtx.dispose();
	}
}

// Hard build-time guard: shiki must stay in the host bundle only.
// The webview iife has a 550 KB ceiling and cannot code-split — any grammar
// engine import there would blow the ceiling and break DEBT-006 resolution.
function assertGrammarEngineOffWebview() {
	const bundle = 'dist/workspace.js';
	if (!fs.existsSync(bundle)) { return; }
	const src = fs.readFileSync(bundle, 'utf8');
	const leaks = ['@shikijs', 'createHighlighterCore', 'engine-javascript'].filter(s => src.includes(s));
	if (leaks.length > 0) {
		throw new Error(`Grammar engine leaked into ${bundle} (${leaks.join(', ')}); it must stay host-only.`);
	}
}

// Hard build-time guard: the workspace webview is a single non-splittable IIFE,
// so its byte size is the load-time cost of the chat surface. Moving the grammar
// engine host-side is what keeps this bundle small; assert the ceiling on
// production builds so a regression that pulls a heavy dep back into the webview
// breaks the build instead of silently regressing Time-to-Interactive. Dev builds
// are unminified and expected to be larger, so the check is production-only.
const WEBVIEW_BUNDLE_CEILING_BYTES = 550 * 1024;
function assertWebviewBundleUnderCeiling() {
	if (!production) { return; }
	const bundle = 'dist/workspace.js';
	if (!fs.existsSync(bundle)) { return; }
	const bytes = fs.statSync(bundle).size;
	if (bytes > WEBVIEW_BUNDLE_CEILING_BYTES) {
		throw new Error(
			`Webview bundle ${bundle} is ${(bytes / 1024).toFixed(1)} KB, over the ` +
			`${(WEBVIEW_BUNDLE_CEILING_BYTES / 1024).toFixed(0)} KB ceiling.`,
		);
	}
}

main().catch(e => {
	console.error(e);
	process.exit(1);
});
