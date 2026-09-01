/**
 * Brief review (the grill's closing step) — pure-logic coverage.
 *
 * Mirrors clarificationGrillCard.test.ts: this suite tests the component's
 * decision logic directly rather than rendering into a DOM, which is why that
 * logic lives in briefReviewLogic.ts (no React, no vscode bridge) in the first
 * place.
 *
 * What matters here is that the brief becomes the planner's literal input, so
 * "accepted unchanged" and "accepted with an edit" must never be confusable, and
 * an action the operator did not intend must never be synthesized from an empty
 * field.
 */
import * as assert from 'assert';
import {
    buildBriefDecision,
    canAcceptBrief,
    canSendBriefBack,
} from '../workspace/utils/briefReviewLogic';

const ORIGINAL = 'Build a JWT auth service.\n\nConstraints:\n- No new external deps.';

suite('Brief review — decision logic', () => {
    test('accepting an untouched brief sends no modified_content', () => {
        // The planner must receive the backend's own text, not a round-tripped
        // copy that merely happens to be identical today.
        const d = buildBriefDecision('accept', ORIGINAL, ORIGINAL, '');
        assert.strictEqual(d.approved, true);
        assert.strictEqual(d.modified_content, undefined);
        assert.strictEqual(d.comment, undefined);
    });

    test('accepting an edited brief sends the edit verbatim', () => {
        const edited = `${ORIGINAL}\n- p99 under 50ms.`;
        const d = buildBriefDecision('accept', ORIGINAL, edited, '');
        assert.strictEqual(d.approved, true);
        assert.strictEqual(d.modified_content, edited);
    });

    test('a whitespace-only change still counts as an edit', () => {
        // Exact comparison, deliberately: whatever the operator typed is theirs.
        const d = buildBriefDecision('accept', ORIGINAL, `${ORIGINAL}\n`, '');
        assert.strictEqual(d.modified_content, `${ORIGINAL}\n`);
    });

    test('a rewrite carries the trimmed note as the steer', () => {
        const d = buildBriefDecision('rewrite', ORIGINAL, ORIGINAL, '  you dropped latency  ');
        assert.strictEqual(d.approved, false);
        assert.strictEqual(d.comment, 'you dropped latency');
    });

    test('a rewrite with an empty note degrades to a plain cancel', () => {
        // Nothing to steer by — re-distilling would burn a MODEL_BIG call to
        // produce the same brief again. Unreachable from the UI (see the gate
        // below); retained as the fail-safe for any other caller.
        const d = buildBriefDecision('rewrite', ORIGINAL, ORIGINAL, '   ');
        assert.strictEqual(d.approved, false);
        assert.strictEqual(d.comment, undefined);
    });

    test('send back is refused until the note carries a steer', () => {
        // Without this gate a blank note produced a payload identical to a
        // cancel, so the backend ended the turn and the SAME brief came back —
        // reading as "I asked for changes and nothing changed".
        assert.strictEqual(canSendBriefBack(''), false);
        assert.strictEqual(canSendBriefBack('   \n  '), false);
        assert.strictEqual(canSendBriefBack('you dropped latency'), true);
    });

    test('a gated send back can only ever produce a steered rewrite', () => {
        // The gate and the payload builder must agree: anything canSendBriefBack
        // admits must carry a comment, or the cancel-collision returns.
        const note = 'add the latency budget back';
        assert.strictEqual(canSendBriefBack(note), true);
        const d = buildBriefDecision('rewrite', ORIGINAL, ORIGINAL, note);
        assert.strictEqual(d.approved, false);
        assert.ok(d.comment && d.comment.length > 0);
    });

    test('cancel never carries an edit or a note, even with both staged', () => {
        const d = buildBriefDecision('cancel', ORIGINAL, 'half-typed replacement', 'half-typed note');
        assert.strictEqual(d.approved, false);
        assert.strictEqual(d.comment, undefined);
        assert.strictEqual(d.modified_content, undefined);
    });

    test('an emptied brief cannot be accepted', () => {
        // The backend reads a blank modified_content as absent and would hand off
        // the original — so the operator would have cleared it and got it back.
        assert.strictEqual(canAcceptBrief(''), false);
        assert.strictEqual(canAcceptBrief('   \n  '), false);
        assert.strictEqual(canAcceptBrief(ORIGINAL), true);
    });
});
