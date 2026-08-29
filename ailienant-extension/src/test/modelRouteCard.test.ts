/**
 * Model route review (13.1.10) — pure-logic coverage.
 *
 * Mirrors briefReviewCard.test.ts: this suite tests the decision logic
 * directly (utils/modelRouteLogic.ts, no React, no vscode bridge) rather than
 * rendering into a DOM.
 */
import * as assert from 'assert';
import {
    buildRouteDecision, parseRoutePayload, tierForDecision, TIER_ORDER,
} from '../workspace/utils/modelRouteLogic';

suite('13.1.10 — buildRouteDecision', () => {
    test('accept sends no modified_content', () => {
        const d = buildRouteDecision('accept');
        assert.strictEqual(d.approved, true);
        assert.strictEqual(d.modified_content, undefined);
    });

    test('override sends the chosen tier as the wire routing_decision string', () => {
        const d = buildRouteDecision('override', 'big');
        assert.strictEqual(d.approved, true);
        assert.strictEqual(d.modified_content, 'LOCAL_BIG');
    });

    test('every tier maps to a distinct routing_decision string', () => {
        const seen = new Set<string>();
        for (const tier of TIER_ORDER) {
            const d = buildRouteDecision('override', tier);
            assert.ok(d.modified_content, `${tier} produced no modified_content`);
            seen.add(d.modified_content!);
        }
        assert.strictEqual(seen.size, TIER_ORDER.length, 'tiers collided onto the same decision string');
    });

    test('override with no tier degrades to a plain accept', () => {
        const d = buildRouteDecision('override');
        assert.strictEqual(d.approved, true);
        assert.strictEqual(d.modified_content, undefined);
    });

    test('cancel is never approved and carries no override', () => {
        const d = buildRouteDecision('cancel', 'cloud');
        assert.strictEqual(d.approved, false);
        assert.strictEqual(d.modified_content, undefined);
    });
});

suite('13.1.10 — tierForDecision', () => {
    test('resolves every known routing_decision back to its tier', () => {
        assert.strictEqual(tierForDecision('LOCAL_SMALL'), 'small');
        assert.strictEqual(tierForDecision('LOCAL_MEDIUM'), 'medium');
        assert.strictEqual(tierForDecision('LOCAL_BIG'), 'big');
        assert.strictEqual(tierForDecision('CLOUD'), 'cloud');
    });

    test('an unrecognized decision string resolves to undefined, not a guess', () => {
        assert.strictEqual(tierForDecision('SOMETHING_ELSE'), undefined);
    });
});

suite('13.1.10 — parseRoutePayload', () => {
    test('parses a well-formed drafted-route JSON string', () => {
        const parsed = parseRoutePayload(JSON.stringify({ routing_decision: 'LOCAL_BIG', tci: 62, css: 78 }));
        assert.deepStrictEqual(parsed, { routing_decision: 'LOCAL_BIG', tci: 62, css: 78 });
    });

    test('tolerates a payload with no tci/css — still resolves the decision', () => {
        const parsed = parseRoutePayload(JSON.stringify({ routing_decision: 'CLOUD' }));
        assert.strictEqual(parsed?.routing_decision, 'CLOUD');
        assert.strictEqual(parsed?.tci, undefined);
        assert.strictEqual(parsed?.css, undefined);
    });

    test('malformed JSON degrades to undefined, never throws', () => {
        assert.strictEqual(parseRoutePayload('{not json'), undefined);
    });

    test('a JSON value with no routing_decision degrades to undefined', () => {
        assert.strictEqual(parseRoutePayload(JSON.stringify({ tci: 10 })), undefined);
    });

    test('null/undefined/empty content all degrade to undefined', () => {
        assert.strictEqual(parseRoutePayload(undefined), undefined);
        assert.strictEqual(parseRoutePayload(null), undefined);
        assert.strictEqual(parseRoutePayload(''), undefined);
    });
});
