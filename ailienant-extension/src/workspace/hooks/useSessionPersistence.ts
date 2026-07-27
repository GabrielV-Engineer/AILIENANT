/**
 * Session-survival effects for the workspace transcript.
 *
 *  1. Debounced PERSIST_TRANSCRIPT — mirror the completed transcript to the host
 *     so closing VS Code doesn't empty the session. Transient stream flags and the
 *     large `parserState` object are stripped; system chips are display-only and
 *     never persisted; `timeline`'s 'reasoning' entries are dropped (display-only,
 *     matching `thinking`'s own exclusion) while every other kind survives as the
 *     durable audit trail AgentTimeline renders on rehydrate.
 *  2. In-flight Thought-Box resilience — throttled snapshot of the active streaming
 *     turn into the panel-survivable store, so a partial reasoning trace survives a
 *     teardown/reconnect (cleared on server_stream_end).
 *  3. Mount rehydrate — restore a persisted in-flight turn once, merged by id so it
 *     never duplicates a turn already present in the restored transcript.
 */
import { useEffect } from 'react';
import { vscode } from '../vscode_bridge';
import { useChatStore } from '../chatStore';
import { useWorkspaceStore } from '../workspaceStore';
import type { ConversationMessage, Message } from '../types';
import { stripReasoningForPersist } from '../utils/timelineBuilder';

export function useSessionPersistence(): void {
    const messages = useChatStore((s) => s.messages);
    const nattMessages = useChatStore((s) => s.nattMessages);
    const setMessages = useChatStore((s) => s.setMessages);
    const setInflightTurn = useWorkspaceStore((s) => s.setInflightTurn);

    // Persist the per-session transcript (debounced); transient stream flags are
    // stripped and `parserState` (large per-message object) never reaches the host.
    useEffect(() => {
        const handle = setTimeout(() => {
            vscode.postMessage({
                type: 'PERSIST_TRANSCRIPT',
                // Carry checkpoint_id + is_abort_savepoint so the rehydrated transcript
                // still shows the ↪ Branch button. The type predicate narrows to
                // ConversationMessage[] so the destructure of rich fields (steps,
                // toolCalls, …) that don't exist on SystemMessage is type-safe. System
                // chips are transient display markers — not persisted.
                messages: messages
                    .filter((m): m is ConversationMessage => m.role !== 'system')
                    .map(({
                        id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                        checkpoint_id, is_abort_savepoint, authorLabel, liveTokens, checklist,
                        timeline,
                    }) => ({
                        id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                        checkpoint_id, is_abort_savepoint, authorLabel, liveTokens, checklist,
                        // 'reasoning' entries are display-only (matching thinking*'s
                        // pre-existing exclusion above) — everything else (plan/diff/
                        // read/edit/command/…) is durable audit evidence, same as
                        // checklist/diffBlocks were before AgentTimeline took over
                        // rendering them, so a rehydrated turn still shows its trace.
                        timeline: timeline ? stripReasoningForPersist(timeline) : timeline,
                    })),
                nattMessages: nattMessages.map(({ id, role, content }) => ({ id, role, content })),
            });
        }, 400);
        return () => clearTimeout(handle);
    }, [messages, nattMessages]);

    // Snapshot the active streaming turn (id + content + thinking slice, NO
    // parserState/toolCalls) into the panel-survivable store, throttled. Reasoning
    // is display-only and out of the host transcript, so this webview-local copy is
    // the only way a partial trace survives a teardown/reconnect.
    useEffect(() => {
        const inflight = messages.find((m): m is ConversationMessage => m.role === 'assistant' && !!(m as ConversationMessage).streaming);
        const handle = setTimeout(() => {
            setInflightTurn(inflight
                ? {
                    id: inflight.id,
                    role: inflight.role,
                    content: inflight.content,
                    streaming: true,
                    thinking: inflight.thinking,
                    thinkingTokens: inflight.thinkingTokens,
                    thinkingStartedAt: inflight.thinkingStartedAt,
                    thinkingElapsedMs: inflight.thinkingElapsedMs,
                    thinkingOpen: inflight.thinkingOpen,
                    steps: inflight.steps,
                    stepsDone: inflight.stepsDone,
                }
                : null);
        }, 200);
        return () => clearTimeout(handle);
    }, [messages, setInflightTurn]);

    // On mount, rehydrate a persisted in-flight turn (survives a panel
    // teardown/reload). Merge by id so it never duplicates a turn already present in
    // the restored transcript. Runs once.
    useEffect(() => {
        const saved = useWorkspaceStore.getState().inflightTurn;
        if (saved?.id && saved.streaming) {
            setMessages(prev =>
                prev.some(m => m.id === saved.id) ? prev : [...prev, saved as Message]);
        }
    }, [setMessages]);
}
