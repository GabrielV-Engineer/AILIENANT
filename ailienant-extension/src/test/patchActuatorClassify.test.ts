/**
 * classifyPatchEdit (13.0.9) — the pure per-file decision PatchActuator.apply
 * makes before touching any vscode.* API. Extracted so the decision itself is
 * unit-testable, mirroring buildHitlResponseData's extraction pattern.
 *
 * Regression guard: a WS disconnect right after a prior apply already wrote
 * and saved a file, before the ack reached the backend, made the backend
 * re-send the same edit on retry. The retry's currentHash no longer matches
 * base_hash (the file moved on to its own already-correct destination), so
 * the whole atomic set used to abort as "stale" even though nothing was
 * actually wrong — this file's case must classify as 'already-applied'
 * instead, not 'stale'.
 */
import * as assert from 'assert';
import * as crypto from 'crypto';
import { classifyPatchEdit, type WorkspaceEditItem } from '../core/PatchActuator';

function hash(text: string): string {
    return crypto.createHash('sha256').update(text, 'utf8').digest('hex');
}

suite('13.0.9 — classifyPatchEdit', () => {
    test('no base_hash at all: writes unconditionally (legacy/no-guard shape)', () => {
        const item: WorkspaceEditItem = { file_path: 'a.py', new_content: 'x = 1\n' };
        const result = classifyPatchEdit(item, hash('anything'), 'anything');
        assert.deepStrictEqual(result, { kind: 'write', newContent: 'x = 1\n' });
    });

    test('base_hash matches current content: a normal, non-conflicting write', () => {
        const oldContent = 'x = 1\n';
        const item: WorkspaceEditItem = {
            file_path: 'a.py', new_content: 'x = 2\n', base_hash: hash(oldContent),
        };
        const result = classifyPatchEdit(item, hash(oldContent), oldContent);
        assert.deepStrictEqual(result, { kind: 'write', newContent: 'x = 2\n' });
    });

    test('base_hash mismatch, current content is genuinely different: stale', () => {
        const item: WorkspaceEditItem = {
            file_path: 'a.py', new_content: 'x = 2\n', base_hash: hash('x = 1\n'),
        };
        const result = classifyPatchEdit(item, hash('x = 999  # someone else edited this\n'), 'x = 999  # someone else edited this\n');
        assert.deepStrictEqual(result, { kind: 'stale' });
    });

    test('base_hash mismatch, but current content already equals new_content: already-applied', () => {
        // The lost-ack retry scenario: the file was already written with the
        // proposed content by a PRIOR apply, so it no longer matches the
        // ORIGINAL base_hash — but it's not a conflict, it's the intended outcome.
        const newContent = 'x = 2\n';
        const item: WorkspaceEditItem = {
            file_path: 'a.py', new_content: newContent, base_hash: hash('x = 1\n'),
        };
        const result = classifyPatchEdit(item, hash(newContent), newContent);
        assert.deepStrictEqual(result, { kind: 'already-applied', newContent });
    });

    test('unified_diff path: a reconstructible diff against the live old side still writes', () => {
        const oldContent = 'line1\nline2\nline3\n';
        const diff = [
            '@@ -1,3 +1,3 @@',
            ' line1',
            '-line2',
            '+line2-changed',
            ' line3',
        ].join('\n');
        const item: WorkspaceEditItem = { file_path: 'a.py', unified_diff: diff };
        const result = classifyPatchEdit(item, hash(oldContent), oldContent);
        assert.strictEqual(result.kind, 'write');
        if (result.kind === 'write') {
            assert.ok(result.newContent.includes('line2-changed'));
        }
    });

    test('unified_diff path: drift against the live old side (cannot reconstruct) is stale', () => {
        const item: WorkspaceEditItem = {
            file_path: 'a.py',
            unified_diff: '@@ -1,3 +1,3 @@\n line1\n-line2\n+line2-changed\n line3\n',
        };
        // The "old side" here bears no resemblance to what the diff's context expects.
        const result = classifyPatchEdit(item, hash('totally unrelated content'), 'totally unrelated content');
        assert.deepStrictEqual(result, { kind: 'stale' });
    });

    test('neither new_content nor unified_diff present: stale (nothing to write)', () => {
        const item: WorkspaceEditItem = { file_path: 'a.py' };
        const result = classifyPatchEdit(item, hash('x'), 'x');
        assert.deepStrictEqual(result, { kind: 'stale' });
    });
});
