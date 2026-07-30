/**
 * Resolver for the command menu's Support → Help documents action.
 *
 * Kept free of the `vscode` module so it can be contract-tested directly: the
 * caller injects the extension root, the configured URL, and an existence probe.
 *
 * The user guides live at the repository root, outside the packaged extension
 * folder, so the build copies them into `dist/docs/` (see esbuild.js). A build
 * that did not run the copy step must degrade to an honest empty list rather
 * than offering entries that cannot open.
 */

/** Basenames copied into `dist/docs/`, in the order users should meet them. */
export const BUNDLED_DOCS: ReadonlyArray<{ file: string; label: string; detail: string }> = [
    { file: 'HowToUseIt.md', label: 'How to use AILIENANT', detail: 'Step-by-step user manual' },
    { file: 'HowItWorks.md', label: 'How it works', detail: 'Architecture explainer with diagrams' },
    { file: 'README.md', label: 'Overview', detail: 'What AILIENANT is and what it does' },
];

/** Where the build copies the guides, relative to the extension root. Kept
 *  forward-slashed: callers split it back into `Uri.joinPath` segments. */
export const DOCS_DIST_DIR = 'dist/docs';

export interface DocEntry {
    label: string;
    detail: string;
    /** A bundled markdown file to open in a preview. */
    relativePath?: string;
    /** An external URL to hand to the OS browser. */
    url?: string;
}

export interface ResolveDocsOptions {
    /** Absolute filesystem path of the extension root. */
    extensionPath: string;
    /** Value of `ailienant.docsUrl`; blank when unset. */
    docsUrl: string;
    /** Existence probe for an absolute path. Injected for testability. */
    exists: (absolutePath: string) => boolean;
    /** Path join. Injected so the resolver stays platform-agnostic in tests. */
    join: (...parts: string[]) => string;
}

/**
 * Build the pick list. Bundled guides come first (they always work offline);
 * a configured URL is appended as an extra choice, never a precondition.
 */
export function resolveDocEntries(opts: ResolveDocsOptions): DocEntry[] {
    const entries: DocEntry[] = [];
    for (const doc of BUNDLED_DOCS) {
        const relativePath = opts.join(DOCS_DIST_DIR, doc.file);
        if (opts.exists(opts.join(opts.extensionPath, relativePath))) {
            entries.push({ label: doc.label, detail: doc.detail, relativePath });
        }
    }
    const url = opts.docsUrl.trim();
    if (url) {
        entries.push({ label: 'Online documentation', detail: url, url });
    }
    return entries;
}
