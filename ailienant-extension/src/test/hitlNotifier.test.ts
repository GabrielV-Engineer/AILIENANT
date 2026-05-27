/**
 * Phase 7.11.7 (ADR-706 §4.5f) — Native HITL push-notification bridge tests.
 *
 * Verifies the host-side decision logic without spinning up a real VS Code
 * panel: a stub `WindowApi` records the (level, message, items) of the last
 * call and returns a pre-configured choice; the test asserts the notifier
 * fires the correct severity, threads the dedupe Set, and routes the button
 * outcomes to the right callback (`send` for Approve/Reject, `revealPanel`
 * for Open Chat, no-op for dismiss).
 *
 * The full toast UX (renders on every OS, dismiss timeout, button order)
 * lives behind the real `vscode.window` API and is covered by the manual
 * smoke per the plan; this file pins the *logic* of the bridge.
 */
import * as assert from 'assert';
import {
    HitlNotifier,
    type HitlMode,
    type HITLApprovalRequestPayload,
    type WindowApi,
} from '../providers/hitlNotifier';

interface StubCall {
    level: 'info' | 'warning';
    message: string;
    items: string[];
}

interface Stub extends WindowApi {
    calls: StubCall[];
    /** Next return value for both info + warning. Reset to `undefined` after each
     *  fire so a second call must explicitly opt in. Setter for ergonomics. */
    nextChoice: string | undefined;
}

function makeStub(): Stub {
    const stub: Stub = {
        calls: [],
        nextChoice: undefined,
        showInformationMessage(
            message: string,
            _options: { modal?: boolean },
            ...items: string[]
        ): Thenable<string | undefined> {
            stub.calls.push({ level: 'info', message, items });
            const c = stub.nextChoice;
            return Promise.resolve(c);
        },
        showWarningMessage(
            message: string,
            _options: { modal?: boolean },
            ...items: string[]
        ): Thenable<string | undefined> {
            stub.calls.push({ level: 'warning', message, items });
            const c = stub.nextChoice;
            return Promise.resolve(c);
        },
    };
    return stub;
}

interface SendRecord { approvalId: string; approved: boolean; }

function buildNotifier(
    stub: Stub,
    mode: HitlMode,
    sendSink: SendRecord[],
    revealCounter: { n: number },
): HitlNotifier {
    return new HitlNotifier({
        windowApi: stub,
        getMode: () => mode,
        send: (approvalId, approved) => { sendSink.push({ approvalId, approved }); },
        revealPanel: () => { revealCounter.n += 1; },
    });
}

const PAYLOAD_FILE_WRITE: HITLApprovalRequestPayload = {
    session_id: 'sess-1',
    approval_id: 'aid-1',
    action_description: 'Apply 2 file change(s): foo.py, bar.py',
    request_kind: 'FILE_WRITE',
};

const PAYLOAD_BUDGET: HITLApprovalRequestPayload = {
    session_id: 'sess-1',
    approval_id: 'aid-2',
    action_description: 'BUDGET_OVERFLOW — cost $1.20 exceeded ceiling $1.00',
    request_kind: 'BUDGET_OVERFLOW',
};

/** Walk the microtask queue twice so the `Promise.resolve(choice).then(...)`
 *  inside the notifier has definitely flushed. */
async function flush(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

suite('Phase 7.11.7 — hitlNotifier', () => {

    test('auto + visible → no toast fires', async () => {
        const stub = makeStub();
        const sends: SendRecord[] = [];
        const reveal = { n: 0 };
        const notifier = buildNotifier(stub, 'auto', sends, reveal);

        notifier.setVisibility(true);
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();

        assert.strictEqual(stub.calls.length, 0, 'no toast expected when chat is visible');
        assert.strictEqual(sends.length, 0);
    });

    test('auto + hidden → info toast for low-risk kind, buttons in order', async () => {
        const stub = makeStub();
        const sends: SendRecord[] = [];
        const reveal = { n: 0 };
        const notifier = buildNotifier(stub, 'auto', sends, reveal);

        notifier.setVisibility(false);
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();

        assert.strictEqual(stub.calls.length, 1);
        assert.strictEqual(stub.calls[0].level, 'info', 'FILE_WRITE → info');
        assert.deepStrictEqual(
            stub.calls[0].items,
            ['Approve', 'Reject', 'Open Chat'],
            'three buttons in fixed order',
        );
        assert.ok(
            stub.calls[0].message.includes('file write'),
            `message should contain the lowercased kind; got: ${stub.calls[0].message}`,
        );
    });

    test('auto + hidden + high-risk kind → warning toast', async () => {
        const stub = makeStub();
        const sends: SendRecord[] = [];
        const reveal = { n: 0 };
        const notifier = buildNotifier(stub, 'auto', sends, reveal);

        notifier.setVisibility(false);
        notifier.onApprovalRequest(PAYLOAD_BUDGET);
        await flush();

        assert.strictEqual(stub.calls.length, 1);
        assert.strictEqual(stub.calls[0].level, 'warning', 'BUDGET_OVERFLOW → warning');
    });

    test('Approve click → send(true) + idempotent second request is a no-op', async () => {
        const stub = makeStub();
        stub.nextChoice = 'Approve';
        const sends: SendRecord[] = [];
        const reveal = { n: 0 };
        const notifier = buildNotifier(stub, 'auto', sends, reveal);

        notifier.setVisibility(false);
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();

        assert.deepStrictEqual(sends, [{ approvalId: 'aid-1', approved: true }]);

        // A second request for the SAME approval_id is rejected by the dedupe Set —
        // no second toast, no second send.
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();
        assert.strictEqual(stub.calls.length, 1, 'no second toast for resolved approval_id');
        assert.strictEqual(sends.length, 1, 'no second send for resolved approval_id');
    });

    test('Reject click → send(false)', async () => {
        const stub = makeStub();
        stub.nextChoice = 'Reject';
        const sends: SendRecord[] = [];
        const reveal = { n: 0 };
        const notifier = buildNotifier(stub, 'auto', sends, reveal);

        notifier.setVisibility(false);
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();

        assert.deepStrictEqual(sends, [{ approvalId: 'aid-1', approved: false }]);
        assert.strictEqual(reveal.n, 0);
    });

    test('Open Chat → revealPanel; no WS send; approval stays unresolved', async () => {
        const stub = makeStub();
        stub.nextChoice = 'Open Chat';
        const sends: SendRecord[] = [];
        const reveal = { n: 0 };
        const notifier = buildNotifier(stub, 'auto', sends, reveal);

        notifier.setVisibility(false);
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();

        assert.strictEqual(reveal.n, 1, 'revealPanel fired once');
        assert.strictEqual(sends.length, 0, 'no WS send — user must still decide');

        // A subsequent toast for the SAME approval_id is allowed (not resolved yet).
        // We simulate the user dismissing the toast (no choice) by leaving nextChoice
        // at undefined, but the toast should still surface.
        stub.nextChoice = undefined;
        notifier.onApprovalRequest(PAYLOAD_FILE_WRITE);
        await flush();
        assert.strictEqual(stub.calls.length, 2, 'second toast surfaces — approval still open');
    });
});
