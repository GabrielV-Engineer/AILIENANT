/**
 * Session-survival effects for the workspace transcript.
 *
 *  1. Debounced PERSIST_TRANSCRIPT — mirror the completed transcript to the host
 *     so closing VS Code doesn't empty the session. Transient stream flags and the
 *     large `parserState` object are stripped; system chips are display-only and
 *     never persisted. The whole `timeline` survives as the durable audit trail
 *     AgentTimeline renders on rehydrate — reasoning included, bounded per entry
 *     (see `prepareReasoningForPersist`).
 *  2. In-flight resilience — throttled snapshot of the active streaming turn into
 *     the panel-survivable store, so a partial reasoning trace AND its plan
 *     checklist / companion explanation / activity trace survive a teardown/
 *     reconnect that lands inside the first effect's debounce window (cleared
 *     on server_stream_end).
 *  3. Mount rehydrate — restore a persisted in-flight turn once, merged by id so it
 *     never duplicates a turn already present in the restored transcript.
 *
 * The two routes bound themselves differently because their storage does. The
 * host transcript is an on-disk memento with room for the full record; the
 * in-flight snapshot shares one `vscode.setState()` blob with the user's draft,
 * so it keeps only what cannot be rebuilt — the reasoning and the marker spine —
 * and drops the diff/cell/execution bodies that other channels re-deliver.
 */
import { useEffect } from 'react';
import { vscode } from '../vscode_bridge';
import { useChatStore } from '../chatStore';
import { useWorkspaceStore } from '../workspaceStore';
import type { ConversationMessage, Message } from '../types';
import { MAX_INFLIGHT_SNAPSHOT_CHARS } from '../../shared/config';
import {
    prepareReasoningForPersist, dropHeavyBodiesForSnapshot, fitSnapshotBudget,
} from '../utils/timelineBuilder';

export function useSessionPersistence(): void {
    const messages = useChatStore((s) => s.messages);
    const nattMessages = useChatStore((s) => s.nattMessages);
    const setMessages = useChatStore((s) => s.setMessages);
    const setInflightTurn = useWorkspaceStore((s) => s.setInflightTurn);

    // Persist the per-session transcript; transient stream flags are stripped and
    // `parserState` (large per-message object) never reaches the host. A hide→show
    // cycle on the panel (retainContextWhenHidden:false) destroys the whole JS
    // context on hide, including any pending setTimeout — a fixed debounce window
    // meant a completed turn hidden mid-debounce was silently lost, never reaching
    // the host copy REHYDRATE_TRANSCRIPT rebuilds from. There is nothing left to
    // grow in a completed message, so it is flushed immediately (no timer to lose);
    // only an actively streaming message still debounces, to avoid a postMessage
    // per token — its own up-to-400ms gap on a mid-stream hide is covered
    // separately by the throttled inflightTurn snapshot below (setState-backed,
    // which VS Code's webview API preserves across this exact teardown).
    useEffect(() => {
        const hasActiveStream = messages.some(
            (m): m is ConversationMessage => m.role === 'assistant' && !!(m as ConversationMessage).streaming,
        );
        const persist = () => {
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
                        companions, timeline, turnStartedAt, turnElapsedMs,
                    }) => ({
                        id, role, content, steps, stepsDone, toolCalls, diffBlocks,
                        checkpoint_id, is_abort_savepoint, authorLabel, liveTokens, checklist,
                        // Message-scoped and keyed by emission_id, so a restored
                        // explanation lands back on the turn that produced it — the
                        // pairing hazard that once justified excluding it no longer
                        // exists, while losing it on restart left the transcript
                        // showing what changed and not why.
                        companions,
                        // Every kind persists, reasoning included — bounded per entry
                        // and with its clock settled, so a reloaded turn shows the
                        // same trace it showed live rather than an amputated one.
                        timeline: timeline ? prepareReasoningForPersist(timeline) : timeline,
                        // Whole-turn duration (DEBT-126a) — unlike thinking*, this is
                        // durable audit evidence (not display-only reasoning), so it
                        // persists like checklist/diffBlocks.
                        turnStartedAt, turnElapsedMs,
                    })),
                nattMessages: nattMessages.map(({ id, role, content }) => ({ id, role, content })),
            });
        };
        if (!hasActiveStream) {
            persist();
            return;
        }
        const handle = setTimeout(persist, 400);
        return () => clearTimeout(handle);
    }, [messages, nattMessages]);

    // Snapshot the active streaming turn (id + content + the plan checklist /
    // companion explanation / activity trace, NO parserState/toolCalls) into the
    // panel-survivable store, throttled. This is the ONLY thing that survives a
    // teardown landing inside the first effect's 400ms debounce window —
    // omitting checklist/companions/timeline here (as this used to) meant a
    // mid-stream tab switch could restore the prose while silently dropping the
    // plan checkmarks and the Planning Explanation card.
    //
    // The reasoning text rides in the timeline entries, not in the message-scoped
    // `thinking` field: they are the same stream at two scopes, and AgentTimeline
    // reads the entry-scoped copy, so persisting both stored it twice — once
    // unreachably, since the entries used to be stripped here. `thinkingTokens`
    // stays because the per-turn token footer sums it; `thinkingStartedAt` is
    // deliberately absent (a performance.now() origin does not survive the
    // teardown this snapshot exists for — see prepareReasoningForPersist).
    //
    // Bodies are dropped and the spine is budget-trimmed because this blob shares
    // one setState slot with every other persisted field, the user's draft
    // included.
    useEffect(() => {
        const inflight = messages.find((m): m is ConversationMessage => m.role === 'assistant' && !!(m as ConversationMessage).streaming);
        const handle = setTimeout(() => {
            setInflightTurn(inflight
                ? {
                    id: inflight.id,
                    role: inflight.role,
                    content: inflight.content,
                    streaming: true,
                    thinkingTokens: inflight.thinkingTokens,
                    thinkingElapsedMs: inflight.thinkingElapsedMs,
                    thinkingOpen: inflight.thinkingOpen,
                    steps: inflight.steps,
                    stepsDone: inflight.stepsDone,
                    checklist: inflight.checklist,
                    companions: inflight.companions,
                    timeline: inflight.timeline
                        ? fitSnapshotBudget(
                            dropHeavyBodiesForSnapshot(prepareReasoningForPersist(inflight.timeline)),
                            MAX_INFLIGHT_SNAPSHOT_CHARS,
                        )
                        : inflight.timeline,
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
