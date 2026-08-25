/**
 * `.gitignore` managed-block logic (DEBT-173) — pure-logic coverage.
 *
 * `computeNextGitignore` is the pure string transform `ensureGitignoreBlock`
 * wraps around real filesystem I/O; testing it directly avoids mocking
 * `vscode.workspace.fs`. Covers the three branches that matter for the fix:
 * no managed block yet (first provision), an already-current block (the
 * common no-op case, now re-checked on every activation instead of only
 * once), and a STALE block from a workspace provisioned before a pattern was
 * added to the managed body — which must heal in place, not duplicate.
 */
import * as assert from 'assert';
import {
    computeNextGitignore,
    GITIGNORE_MARKER,
    GITIGNORE_END,
} from '../workspace_provisioning';

suite('DEBT-173 — .gitignore managed-block healing', () => {
    test('no existing .gitignore → appends the managed block, including the generated-file pattern', () => {
        const next = computeNextGitignore('');
        assert.ok(next !== null);
        assert.ok(next!.includes(GITIGNORE_MARKER));
        assert.ok(next!.includes(GITIGNORE_END));
        assert.ok(
            next!.includes('*.generated.md'),
            'the /init-generated-file fork must be ignored from the very first provision',
        );
    });

    test('existing .gitignore with unrelated content → appends the managed block after it', () => {
        const existing = 'node_modules/\ndist/\n';
        const next = computeNextGitignore(existing);
        assert.ok(next !== null);
        assert.ok(next!.startsWith(existing));
        assert.ok(next!.includes('*.generated.md'));
    });

    test('already-current managed block → returns null (no write)', () => {
        const first = computeNextGitignore('')!;
        const second = computeNextGitignore(first);
        assert.strictEqual(second, null);
    });

    test('stale managed block missing the generated-file pattern is healed in place, not duplicated', () => {
        // Simulates a workspace provisioned before `*.generated.md` was added
        // to GITIGNORE_BLOCK_BODY — the exact DEBT-173 reproduction shape.
        const staleBlock = [
            GITIGNORE_MARKER,
            '# Runtime and cache artifacts — never commit these.',
            '.ailienant_telemetry.log*',
            '.ailienant/AGENTS.md',
            '.ailienant/.ailienant.json',
            '.ailienant/dreams/',
            '.ailienant/plans/',
            '# Keep .ailienant/AILIENANT.md tracked — it is your shareable project guidance.',
            GITIGNORE_END,
            '',
        ].join('\n');
        const existing = 'node_modules/\n\n' + staleBlock;

        const next = computeNextGitignore(existing);
        assert.ok(next !== null, 'a stale block must trigger a rewrite, not a silent no-op');
        assert.ok(next!.includes('*.generated.md'), 'the healed block must gain the missing pattern');

        // Healed in place: exactly one marked region, not a second one appended.
        const markerCount = next!.split(GITIGNORE_MARKER).length - 1;
        assert.strictEqual(markerCount, 1, 'the stale block must be replaced, never duplicated');
        assert.ok(next!.startsWith('node_modules/\n\n'), 'unrelated pre-existing content must survive untouched');
    });

    test('healing is convergent: re-running against its own output is a no-op', () => {
        const staleBlock = [
            GITIGNORE_MARKER,
            '# Runtime and cache artifacts — never commit these.',
            '.ailienant_telemetry.log*',
            GITIGNORE_END,
            '',
        ].join('\n');
        const healed = computeNextGitignore(staleBlock);
        assert.ok(healed !== null);
        assert.strictEqual(computeNextGitignore(healed!), null);
    });
});
