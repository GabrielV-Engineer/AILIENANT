const esbuild = require("esbuild");

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
	const ctx = await esbuild.context({
		entryPoints: [
			'src/extension.ts'
		],
		bundle: true,
		format: 'cjs',
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'node',
		outfile: 'dist/extension.js',
		external: ['vscode'],
		logLevel: 'silent',
		plugins: [
			esbuildProblemMatcherPlugin,
		],
	});

	// Sidebar webview — IIFE, ~200KB budget (no Monaco/React Flow here)
	const webviewCtx = await esbuild.context({
		entryPoints: ['src/webview/App.tsx'],
		bundle: true,
		format: 'iife',
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'browser',
		outfile: 'dist/webview.js',
		logLevel: 'silent',
		plugins: [esbuildProblemMatcherPlugin],
	});

	// Dashboard SPA — ESM with code splitting so Monaco loads lazily
	// Monaco chunk (~5MB) is only downloaded when Staging Area is opened
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
		logLevel: 'silent',
		plugins: [esbuildProblemMatcherPlugin],
	});

	if (watch) {
		await ctx.watch();
		await webviewCtx.watch();
		await dashboardCtx.watch();
	} else {
		await ctx.rebuild();
		await ctx.dispose();
		await webviewCtx.rebuild();
		await webviewCtx.dispose();
		await dashboardCtx.rebuild();
		await dashboardCtx.dispose();
	}
}

main().catch(e => {
	console.error(e);
	process.exit(1);
});
