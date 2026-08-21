/**
 * Regression guard for a real dropped-field bug: the `HITL_RESPONSE` host
 * bridge (`providers/workspace_panel.ts::buildHitlResponseData`) forwarded
 * only `approval_id`/`approved`/`comment`/`modified_content` for a full
 * release cycle after the multi-question clarification contract (DEBT-172)
 * added `answer`/`selected_option`/`answers` — every option a
 * ClarificationGrillCard answer picked was silently discarded here, even
 * though the WS contract, the resume path, and every backend test all
 * round-tripped correctly. A schema check alone cannot catch a field getting
 * dropped mid-bridge; only a fixture on the bridge function itself can.
 *
 * Also guards a second, more severe bug (DEBT-188): a `client_hitl_response`
 * carried no session identity at all, so `main.py` resumed by the WS
 * connection's own `client_id` — stable across every session multiplexed on
 * that connection (register_alias) — instead of the specific session the
 * reply belonged to. With more than one session open at once (e.g. the same
 * prompt retried in several sessions), the reply could resume the wrong
 * session or none at all, leaving a paused grill silently un-resumed.
 */
import * as assert from 'assert';
import { buildHitlResponseData } from '../providers/workspace_panel';

suite('HITL_RESPONSE host bridge — field forwarding', () => {
    test('forwards the original approve/reject fields unchanged', () => {
        const out = buildHitlResponseData({
            approval_id: 'req-1', approved: true, comment: 'ok', modified_content: 'x = 2',
        }, 'session-1');
        assert.strictEqual(out.approval_id, 'req-1');
        assert.strictEqual(out.approved, true);
        assert.strictEqual(out.comment, 'ok');
        assert.strictEqual(out.modified_content, 'x = 2');
    });

    test('forwards a multi-question batch answers array intact', () => {
        const answers = [
            { id: 'q0', selected_labels: ['Single container'], free_text: null },
            { id: 'q1', selected_labels: [], free_text: 'Later this week' },
        ];
        const out = buildHitlResponseData({ approval_id: 'req-2', approved: true, answers }, 'session-1');
        assert.deepStrictEqual(out.answers, answers);
    });

    test('forwards a single-question answer/selected_option', () => {
        const out = buildHitlResponseData({
            approval_id: 'req-3', approved: true, answer: 'Use approach A', selected_option: 'A',
        }, 'session-1');
        assert.strictEqual(out.answer, 'Use approach A');
        assert.strictEqual(out.selected_option, 'A');
    });

    test('omits answer/selected_option/answers entirely when absent (no explicit undefined)', () => {
        const out = buildHitlResponseData({ approval_id: 'req-4', approved: false, comment: 'no' }, 'session-1');
        assert.strictEqual('answer' in out, false);
        assert.strictEqual('selected_option' in out, false);
        assert.strictEqual('answers' in out, false);
    });

    test('ignores a malformed answers field (not an array) rather than forwarding garbage', () => {
        const out = buildHitlResponseData({
            approval_id: 'req-5', approved: true, answers: 'not-an-array',
        }, 'session-1');
        assert.strictEqual('answers' in out, false);
    });
});

suite('HITL_RESPONSE host bridge — session routing (DEBT-188)', () => {
    test('always stamps the panel\'s own session id, regardless of webview-supplied data', () => {
        const out = buildHitlResponseData(
            { approval_id: 'req-6', approved: true },
            'c7cde11e-1c78-4cf8-90d1-d7ab3ecd0024',
        );
        assert.strictEqual(out.session_id, 'c7cde11e-1c78-4cf8-90d1-d7ab3ecd0024');
    });

    test('two panels answering concurrently produce distinct session ids', () => {
        const outA = buildHitlResponseData({ approval_id: 'req-a', approved: true }, 'session-a');
        const outB = buildHitlResponseData({ approval_id: 'req-b', approved: true }, 'session-b');
        assert.notStrictEqual(outA.session_id, outB.session_id);
    });
});
