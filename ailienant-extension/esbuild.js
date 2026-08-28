const esbuild = require("esbuild");
const fs = require("fs");
const path = require("path");

// react-diff-viewer-continued imports the full js-yaml parser only for its
// structural YAML diff mode, which the chat surface never uses (it diffs source
// code with the default char/word compare). Alias it to a fail-fast stub so the
// dead ~39 KB parser stays off the non-splittable webview IIFE.
const WEBVIEW_ALIAS = {
	'js-yaml': path.resolve(__dirname, 'src/shims/js-yaml-stub.ts'),
};

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
		// '@devcontainers/cli' is spawned as a child process, not imported — keep it
		// external so esbuild never tries to bundle it (it is a soft/optional dep).
		external: ['vscode', '@devcontainers/cli'],
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
		alias: WEBVIEW_ALIAS,
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

	// Copy dashboard index.html into dist on every build. esbuild.context()
	// only prepares the build, it doesn't create outdir yet (that happens on
	// the later rebuild()/watch() call), so the destination directory isn't
	// guaranteed to exist here.
	fs.mkdirSync('dist/dashboard', { recursive: true });
	fs.copyFileSync('src/dashboard/index.html', 'dist/dashboard/index.html');

	copyUserGuides();

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
		assertStylesheetsParseCleanly();
	}
}

// The end-user guides live at the repository root, one level above the packaged
// extension folder, so `vsce` cannot see them. Copy them into dist/docs/ (which
// .vscodeignore does not exclude) so the Support → Help documents action works
// offline in a published VSIX. Missing sources are skipped, never fatal: a
// partial checkout must not break the build, and docsCatalog.ts probes for
// existence before offering an entry.
const USER_GUIDES = ['README.md', 'HowToUseIt.md', 'HowItWorks.md'];
function copyUserGuides() {
	const outDir = path.join('dist', 'docs');
	fs.mkdirSync(outDir, { recursive: true });
	for (const name of USER_GUIDES) {
		const src = path.join('..', name);
		if (fs.existsSync(src)) {
			fs.copyFileSync(src, path.join(outDir, name));
		} else {
			console.warn(`[docs] skipped missing user guide: ${src}`);
		}
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
// The specific shiki/grammar-engine leak this guard exists to catch has its own
// dedicated signature check (assertGrammarEngineOffWebview, above) — this ceiling
// is the coarser backstop. Raised 550→555 KB in Phase 12.8 (Glass-Box Timeline
// live-chunk streaming + persisted compaction fold added ~200 bytes of organic,
// reviewed feature code, not a dependency regression); raised 555→557 KB across
// 13.0.7 (per-entry reasoning chronometry — several reasoning spans in one turn
// each get their own independent clock instead of sharing the turn's single one
// — plus the Agent Companion generalization: additive scope/emission_id wire
// fields, message-scoped append storage replacing the old single-payload store,
// a rewritten multi-entry CoderCompanionCard, and purely-frontend work-loop
// phase grouping in AgentTimeline — organic, reviewed feature code, not a
// dependency regression); raised 557→559 KB for the Effort Budget selector
// added to ModelsMenu.tsx's orchestration view (light/balanced/deep rows with
// live per-level cost estimates fetched from the backend) — organic, reviewed
// feature code, not a dependency regression; raised 559→560 KB for the
// transparency-persistence policy change (reasoning now survives a reload
// bounded rather than being dropped, the in-flight snapshot sheds its
// recoverable diff/cell/execution bodies and is budget-trimmed, and the shared
// setState envelope no longer loses every slot when one write throws) — three
// pure functions and two named budgets, organic reviewed feature code, not a
// dependency regression. Bump again only with the same
// justification, never to silently absorb an unreviewed size increase.
const WEBVIEW_BUNDLE_CEILING_BYTES = 560 * 1024;
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

// Hard build-time guard: a stylesheet the browser cannot parse fails the build
// instead of shipping silently. Neither tsc nor eslint reads CSS and esbuild
// passes malformed rule text straight through, so nothing else catches it — the
// browser then discards rules with no error, which reads as an inexplicable
// layout regression rather than a build failure. The usual cause is a comment
// closed early by a stray terminator, spilling prose into the stylesheet where
// an apostrophe or paren then unbalances everything below it. Both assertions
// hold for any well-formed stylesheet, so a failure is never a false positive.
function assertStylesheetsParseCleanly() {
	const sheets = ['dist/workspace.css', 'dist/sidebar.css', 'dist/dashboard/index.css'];
	for (const sheet of sheets) {
		if (!fs.existsSync(sheet)) { continue; }
		const src = fs.readFileSync(sheet, 'utf8');
		const stack = [];
		const closers = { '{': '}', '(': ')', '[': ']' };
		let line = 1;

		for (let i = 0; i < src.length; i++) {
			const ch = src[i];
			if (ch === '\n') { line++; continue; }

			// Comments: skip to the NEXT `*/`, exactly as a CSS parser does.
			if (ch === '/' && src[i + 1] === '*') {
				const end = src.indexOf('*/', i + 2);
				if (end === -1) {
					throw new Error(`${sheet}:${line} — unterminated CSS comment.`);
				}
				for (let j = i; j < end; j++) { if (src[j] === '\n') { line++; } }
				i = end + 1;
				continue;
			}

			// Strings: a raw newline before the closing quote is a bad-string token.
			if (ch === '"' || ch === '\'') {
				let j = i + 1;
				while (j < src.length && src[j] !== ch) {
					if (src[j] === '\\') { j++; }
					else if (src[j] === '\n') {
						throw new Error(
							`${sheet}:${line} — string opened with ${ch} is not closed before the ` +
							`end of the line. The browser treats this as a bad-string token and ` +
							`discards rules. Most often this means a comment ended early on a ` +
							`stray "*/" and its prose is being parsed as CSS.`,
						);
					}
					j++;
				}
				i = j;
				continue;
			}

			if (closers[ch]) { stack.push({ ch, line }); continue; }
			if (ch === '}' || ch === ')' || ch === ']') {
				const open = stack.pop();
				if (!open || closers[open.ch] !== ch) {
					throw new Error(
						`${sheet}:${line} — unbalanced "${ch}"` +
						(open ? ` (does not close the "${open.ch}" opened on line ${open.line})` : '') +
						`. The browser would discard rules from here on.`,
					);
				}
			}
		}

		if (stack.length > 0) {
			const { ch, line: openLine } = stack[0];
			throw new Error(
				`${sheet} — "${ch}" opened on line ${openLine} is never closed. The browser ` +
				`consumes every following rule into it and drops them all.`,
			);
		}
	}
}

main().catch(e => {
	console.error(e);
	process.exit(1);
});
