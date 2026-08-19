/**
 * `isTurnActive` store contract (chatStore.ts).
 *
 * Guards the actual bug: the Stop button and the Active Task Header were
 * gated on `isStreaming`, which is set only by token/thinking deltas — so a
 * turn spent "thinking" (node execution, tool calls, an interrupt()/resume
 * pause with no tokens yet, e.g. a multi-round Socratic grill replay) looked
 * completely idle and had no way to cancel. `isTurnActive` is the strictly
 * wider "a turn is in flight" signal that now gates those affordances
 * instead. This file pins the store's own set/clear contract; the
 * submit-time and HITL-reply set points live in Workspace.tsx/
 * useHitlResponder.ts/useClarificationResponder.ts and are exercised by the
 * manual smoke test (no React-rendering harness exists in this suite — see
 * agentTodoPanel.test.ts).
 */
import * as assert from 'assert';
import { useChatStore } from '../workspace/chatStore';

suite('isTurnActive — turn-scoped busy state', () => {
    test('defaults to false', () => {
        assert.strictEqual(useChatStore.getState().isTurnActive, false);
    });

    test('is independent of isStreaming — the whole point of the flag', () => {
        const { setIsTurnActive, setIsStreaming } = useChatStore.getState();
        setIsTurnActive(true);
        setIsStreaming(false);
        // A turn can be active with zero tokens on the wire — exactly the
        // "thinking" / multi-round-replay gap the old isStreaming-only gate missed.
        assert.strictEqual(useChatStore.getState().isTurnActive, true);
        assert.strictEqual(useChatStore.getState().isStreaming, false);
        setIsTurnActive(false);
    });

    test('setIsTurnActive(true) then (false) round-trips', () => {
        const { setIsTurnActive } = useChatStore.getState();
        setIsTurnActive(true);
        assert.strictEqual(useChatStore.getState().isTurnActive, true);
        setIsTurnActive(false);
        assert.strictEqual(useChatStore.getState().isTurnActive, false);
    });

    test('accepts an updater function, mirroring the other setters', () => {
        const { setIsTurnActive } = useChatStore.getState();
        setIsTurnActive(false);
        setIsTurnActive((prev) => !prev);
        assert.strictEqual(useChatStore.getState().isTurnActive, true);
        setIsTurnActive(false);
    });
});
