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

	// Workspace — full editor-tab chat UI (replaces old webview.js)
	const workspaceCtx = await esbuild.context({
		entryPoints: ['src/workspace/main.tsx'],
		bundle: true,
		format: 'iife',
		minify: production,
		sourcemap: !production,
		sourcesContent: false,
		platform: 'browser',
		outfile: 'dist/workspace.js',
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
		await dashboardCtx.rebuild(); await dashboardCtx.dispose();
	}
}

main().catch(e => {
	console.error(e);
	process.exit(1);
});
