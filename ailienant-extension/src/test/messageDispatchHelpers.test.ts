/**
 * messageDispatchHelpers.ts — attachOrUpdateCompanion (13.0.7).
 *
 * `messageDispatchHelpers.ts` imports `vscode_bridge.ts`, which calls the real
 * `acquireVsCodeApi()` at module load — a stub must be injected BEFORE
 * importing it, hence the dynamic import inside each test (mirrors
 * src/test/agentTodoPanel.test.ts / nativeThinking.test.ts).
 */
import * as assert from 'assert';
import { _setVsCodeApiForTesting, VsCodeApi } from '../shared/vscodeApi';
import type { CoderCompanionPayload } from '../api/contracts';
import type { ConversationMessage, Message } from '../workspace/types';

function makeStub(): VsCodeApi {
    let store: unknown = undefined;
    return {
        postMessage(): void { /* no-op */ },
        getState<T = unknown>(): T | undefined { return store as T | undefined; },
        setState<T>(state: T): void { store = state; },
    };
}

function payload(over: Partial<CoderCompanionPayload> & { emission_id?: string; correlation_id: string }): CoderCompanionPayload {
    return {
        session_id: 's1', task_id: 't1', objective: 'Explained something',
        decisions: [], patterns_applied: [], bottlenecks: [], security_notes: [],
        errors_found: [], follow_ups: [], degraded: false, scope: 'coding',
        ...over,
    };
}

suite('13.0.7 — attachOrUpdateCompanion', () => {
    setup(() => { _setVsCodeApiForTesting(makeStub()); });
    teardown(() => { _setVsCodeApiForTesting(undefined); });

    test('attaches the first companion to the last assistant turn', async () => {
        const { attachOrUpdateCompanion } = await import('../workspace/utils/messageDispatchHelpers');
        const prev: Message[] = [{ id: 'm1', role: 'assistant', content: '' }];
        const p = payload({ correlation_id: 't1:0', emission_id: 't1:coding:0' });
        const next = attachOrUpdateCompanion(prev, p, 'AILIENANT');
        assert.strictEqual((next[0] as ConversationMessage).companions?.length, 1);
        assert.strictEqual((next[0] as ConversationMessage).companions?.[0], p);
    });

    test('two distinct emission_ids append — the ideation bug this exists to fix', async () => {
        const { attachOrUpdateCompanion } = await import('../workspace/utils/messageDispatchHelpers');
        let prev: Message[] = [{ id: 'm1', role: 'assistant', content: '' }];
        const round1 = payload({ correlation_id: 't1:0', emission_id: 't1:ideation:0', objective: 'Round 1' });
        const round2 = payload({ correlation_id: 't1:0', emission_id: 't1:ideation:1', objective: 'Round 2' });
        prev = attachOrUpdateCompanion(prev, round1, 'AILIENANT');
        prev = attachOrUpdateCompanion(prev, round2, 'AILIENANT');
        const companions = (prev[0] as ConversationMessage).companions;
        assert.strictEqual(companions?.length, 2);
        assert.strictEqual(companions?.[0].objective, 'Round 1');
        assert.strictEqual(companions?.[1].objective, 'Round 2');
    });

    test('a second payload sharing an emission_id replaces in place, not append (idempotent retry)', async () => {
        const { attachOrUpdateCompanion } = await import('../workspace/utils/messageDispatchHelpers');
        let prev: Message[] = [{ id: 'm1', role: 'assistant', content: '' }];
        const first = payload({ correlation_id: 't1:0', emission_id: 't1:coding:0', objective: 'First pass' });
        const retry = payload({ correlation_id: 't1:0', emission_id: 't1:coding:0', objective: 'Retried pass' });
        prev = attachOrUpdateCompanion(prev, first, 'AILIENANT');
        prev = attachOrUpdateCompanion(prev, retry, 'AILIENANT');
        const companions = (prev[0] as ConversationMessage).companions;
        assert.strictEqual(companions?.length, 1);
        assert.strictEqual(companions?.[0].objective, 'Retried pass');
    });

    test('falls back to correlation_id when emission_id is absent (an older event shape)', async () => {
        const { attachOrUpdateCompanion } = await import('../workspace/utils/messageDispatchHelpers');
        let prev: Message[] = [{ id: 'm1', role: 'assistant', content: '' }];
        const p1 = payload({ correlation_id: 't1:0', objective: 'First' });
        const p2 = payload({ correlation_id: 't1:0', objective: 'Second, same correlation_id' });
        prev = attachOrUpdateCompanion(prev, p1, 'AILIENANT');
        prev = attachOrUpdateCompanion(prev, p2, 'AILIENANT');
        const companions = (prev[0] as ConversationMessage).companions;
        assert.strictEqual(companions?.length, 1, 'no emission_id ⇒ keyed by correlation_id ⇒ replace, not append');
        assert.strictEqual(companions?.[0].objective, 'Second, same correlation_id');
    });

    test('does not attach to an OLDER message — only ever the last assistant turn', async () => {
        const { attachOrUpdateCompanion } = await import('../workspace/utils/messageDispatchHelpers');
        const older: ConversationMessage = { id: 'm0', role: 'assistant', content: 'old turn' };
        const prev: Message[] = [older, { id: 'm1', role: 'assistant', content: '' }];
        const p = payload({ correlation_id: 't1:0', emission_id: 't1:coding:0' });
        const next = attachOrUpdateCompanion(prev, p, 'AILIENANT');
        assert.strictEqual((next[0] as ConversationMessage).companions, undefined, 'older turn untouched');
        assert.strictEqual((next[1] as ConversationMessage).companions?.length, 1);
    });

    test('seeds a placeholder assistant turn when none is active yet', async () => {
        const { attachOrUpdateCompanion } = await import('../workspace/utils/messageDispatchHelpers');
        const p = payload({ correlation_id: 't1:0', emission_id: 't1:coding:0' });
        const next = attachOrUpdateCompanion([], p, 'AILIENANT');
        assert.strictEqual(next.length, 1);
        assert.strictEqual(next[0].role, 'assistant');
        assert.strictEqual((next[0] as ConversationMessage).companions?.length, 1);
    });
});
