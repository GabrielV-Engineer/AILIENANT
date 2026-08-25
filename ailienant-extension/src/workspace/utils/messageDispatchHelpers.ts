/**
 * Pure, stateless helpers and constants for the inbound WS/IPC dispatch.
 *
 * Extracted from the panel component so both the dispatch controller
 * (`useWSMessageHandler`) and the layout host (`Workspace`) can share them
 * without a runtime import cycle. Every function here is pure on its inputs —
 * no React, no module state beyond the monotonic id counter inside `mkId`.
 */
import type { Message } from '../types';
import type {
    ToolCallShape, CellRunShape, CellIterationShape, PlanWBSStep, TimelineEntry,
} from '../../shared/config';
import type { CoderCompanionPayload } from '../../api/contracts';
import { MAX_IPC_CODE_CHARS } from '../../shared/config';
import { vscode } from '../vscode_bridge';
import { extractCodeBlocks } from './StreamingMarkdownParser';

// Pre-first-submit fallback only — the live timeout arrives from the backend via
// STREAM_WATCHDOG_MS (governed per active model: longer for slow local engines).
// It is never a hardcoded product timeout.
export const DEFAULT_STREAM_WATCHDOG_MS = 90_000;
// How often the watchdog checks for a stalled stream.
export const STREAM_WATCHDOG_TICK_MS = 5_000;
// Hard bound on a single tool chip's retained output (OOM guard for a runaway tool).
export const MAX_TOOL_OUTPUT_LINES = 500;
// Server events that count as live stream activity (reset the stall watchdog).
export const STREAM_ACTIVITY_EVENTS = new Set<string>([
    'server_token_chunk', 'server_thinking_chunk', 'server_pipeline_step',
    'server_activity_event', 'server_activity_detail_chunk',
    'server_tool_start', 'server_tool_stream_chunk', 'server_tool_result',
    'server_natt_token',
    'server_cell_tool_start', 'server_cell_pty_chunk', 'server_cell_ast_diff',
    'server_cell_governor_tick', 'server_agent_todos', 'server_graph_mutation',
]);
// Hard cap on retained PTY lines per cell iteration. On overflow the buffer stops
// appending and writes a single truncation sentinel, so the virtualized list's base
// indices never shift under the user's scroll.
export const MAX_CELL_PTY_LINES = 5000;
export const CELL_PTY_TRUNCATED = '[… Output truncated due to length …]';

/**
 * Flip any still-`pending` tool chip on a NON-streaming turn to `error`. A chip
 * left pending at a teardown/stall would otherwise rehydrate spinning forever.
 */
export function normalizeStuckChips<T extends { streaming?: boolean; toolCalls?: ToolCallShape[] }>(msgs: T[]): T[] {
    return msgs.map(m => {
        if (m.streaming || !m.toolCalls || m.toolCalls.length === 0) { return m; }
        const fixed = m.toolCalls.map(tc =>
            (tc.status === undefined || tc.status === 'pending')
                ? { ...tc, status: 'error' as const }
                : tc);
        return { ...m, toolCalls: fixed };
    });
}

// Client-minted stable turn id. `crypto.randomUUID` is available in the webview
// runtime; the fallback keeps types honest on exotic hosts.
export function mkId(): string {
    try { return crypto.randomUUID(); }
    catch { return `m_${Date.now()}_${Math.random().toString(36).slice(2)}`; }
}

// Resolves the display label for a turn at the moment it is first minted.
// Frozen onto Message.authorLabel so the row component stays pure and a later
// settings change never retroactively relabels existing history.
export function authorLabelFor(role: 'user' | 'assistant', agentName: string): string {
    return role === 'user' ? 'You' : agentName;
}

// On stream-end, ask the host to syntax-highlight this turn's fenced code blocks.
// The webview holds no grammar engine, so it ships each block (lang + code) to the
// host lexer and later paints the returned tokens. A pre-IPC circuit breaker skips
// oversized blocks (never serialized — protects the isolate boundary from an O(N)
// copy); blocks with no language hint can't be tokenized and are skipped too.
export function requestCodeTokens(turnId: string, content: string): void {
    const blocks = extractCodeBlocks(content).filter(
        (b) => b.lang.length > 0 && b.code.length > 0 && b.code.length <= MAX_IPC_CODE_CHARS,
    );
    if (blocks.length === 0) { return; }
    vscode.postMessage({
        type: 'TOKENIZE_CODE',
        turn_id: turnId,
        request_id: mkId(),
        blocks: blocks.map((b) => ({ hash: b.hash, lang: b.lang, code: b.code })),
    });
}

/**
 * Id-keyed transcript merge for REHYDRATE_TRANSCRIPT. The host transcript is the
 * authoritative COMPLETED history; `local` may hold turns the host hasn't
 * persisted yet. Merge by stable `id` — never a length heuristic (fragile under
 * mid-stream tab-switches → state tearing):
 *   • host order is preserved as the spine;
 *   • a still-`streaming` local copy wins for a matching id (live content is
 *     fresher than the debounced host snapshot);
 *   • any local turn with an id absent from host is appended — whether it's
 *     still streaming (brand-new in-flight) or already completed. A completed
 *     local turn the host doesn't know about yet is a race in WHEN the host
 *     learned about it (a hide→teardown that raced the persist flush), not
 *     licence to discard it: dropping it here deletes a finished turn outright
 *     rather than merely reverting a partial one. The very next persist
 *     re-teaches the host once the fresh webview mounts.
 */
export function mergeById<T extends { id?: string; streaming?: boolean }>(host: T[], local: T[]): T[] {
    const hostIds = new Set(host.map(m => m.id).filter(Boolean));
    const spine = host.map(m => {
        const liveLocal = m.id ? local.find(l => l.id === m.id && l.streaming) : undefined;
        return liveLocal ?? m;
    });
    const tail = local.filter(m => m.id && !hostIds.has(m.id));
    return [...spine, ...tail];
}

/**
 * Update the tool-chip artifact for `tool_call_id` on the LAST assistant message
 * (creating a placeholder turn if none exists yet). The updater receives the prior
 * chip (or `undefined` if this is the chip's first event) and returns the next chip
 * shape. Pure on the previous `messages` array — no mutations.
 */
export function attachOrUpdateToolCall(
    prev: Message[],
    toolCallId: string,
    update: (prior: ToolCallShape | undefined) => ToolCallShape,
    agentName: string,
): Message[] {
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    // Always attach to the last assistant turn — even if its streaming flag
    // has been cleared. The /dev/run-bash smoke command also lands here on a
    // fresh placeholder when no assistant turn is active yet.
    if (last?.role === 'assistant') {
        const calls = last.toolCalls ?? [];
        const idx = calls.findIndex(c => c.tool_call_id === toolCallId);
        const nextCall = update(idx >= 0 ? calls[idx] : undefined);
        const nextCalls = idx >= 0
            ? [...calls.slice(0, idx), nextCall, ...calls.slice(idx + 1)]
            : [...calls, nextCall];
        return [...prev.slice(0, lastIdx), { ...last, toolCalls: nextCalls }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        toolCalls: [update(undefined)],
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}

/**
 * Update the cell-audit iteration for `iteration` on the LAST assistant message
 * (creating a placeholder turn if none exists yet). The updater receives the prior
 * iteration record (or a fresh empty one) and returns the next shape. Pure on the
 * previous `messages` array — no mutations.
 */
export function attachOrUpdateCellRun(
    prev: Message[],
    iteration: number,
    update: (prior: CellIterationShape) => CellIterationShape,
    agentName: string,
): Message[] {
    const empty = (): CellIterationShape => ({ iteration, tools: [], pty: [], diffs: [] });
    const applyTo = (run: CellRunShape | undefined): CellRunShape => {
        const iters = run?.iterations ?? [];
        const idx = iters.findIndex((it) => it.iteration === iteration);
        const nextIter = update(idx >= 0 ? iters[idx] : empty());
        const nextIters = idx >= 0
            ? [...iters.slice(0, idx), nextIter, ...iters.slice(idx + 1)]
            : [...iters, nextIter].sort((a, b) => a.iteration - b.iteration);
        return { iterations: nextIters };
    };
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    if (last?.role === 'assistant') {
        return [...prev.slice(0, lastIdx), { ...last, cellRun: applyTo(last.cellRun) }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        cellRun: applyTo(undefined),
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}

/**
 * Read the just-merged iteration back off the array `attachOrUpdateCellRun`
 * returned, so a caller can feed the SAME merged body into `upsertCellBody`
 * (the Glass-Box Timeline) without duplicating its update logic.
 * `attachOrUpdateCellRun` guarantees the last message is 'assistant' once it
 * returns (appending a fresh one if the prior last message wasn't), so this
 * never misses when called immediately after it on the same array.
 */
export function readMergedCellIteration(
    messages: Message[], iteration: number,
): CellIterationShape | undefined {
    const last = messages[messages.length - 1];
    if (last?.role !== 'assistant') { return undefined; }
    return last.cellRun?.iterations.find((it) => it.iteration === iteration);
}

/**
 * Append sanitized PTY lines to an iteration's buffer with a stop-at-cap policy.
 * Once the cap is hit the buffer is frozen (apart from a one-time truncation
 * sentinel) so the virtualized list's indices never shift mid-scroll.
 */
export function appendPtyLines(it: CellIterationShape, lines: string[]): CellIterationShape {
    if (it._truncated) { return it; }
    if (it.pty.length + lines.length <= MAX_CELL_PTY_LINES) {
        return { ...it, pty: [...it.pty, ...lines] };
    }
    const room = Math.max(0, MAX_CELL_PTY_LINES - it.pty.length);
    return { ...it, pty: [...it.pty, ...lines.slice(0, room), CELL_PTY_TRUNCATED], _truncated: true };
}

/**
 * Update the execution checklist on the LAST assistant message (creating a
 * placeholder turn if none exists). The updater receives the prior checklist (or
 * undefined) and returns the next one. Pure on the previous array — no mutations.
 */
export function attachOrUpdateChecklist(
    prev: Message[],
    update: (prior: PlanWBSStep[] | undefined) => PlanWBSStep[],
    agentName: string,
): Message[] {
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    if (last?.role === 'assistant') {
        return [...prev.slice(0, lastIdx), { ...last, checklist: update(last.checklist) }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        checklist: update(undefined),
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}

/**
 * Attach to (or seed) the active assistant turn's Glass-Box Timeline. Mirrors
 * `attachOrUpdateChecklist`'s shape; `update` receives the prior `timeline` (or
 * `undefined`) and returns the next one via one of `timelineBuilder.ts`'s pure
 * upsert functions. Pure on the previous array — no mutations.
 */
export function attachOrUpdateTimeline(
    prev: Message[],
    update: (prior: TimelineEntry[] | undefined) => TimelineEntry[],
    agentName: string,
): Message[] {
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    if (last?.role === 'assistant') {
        return [...prev.slice(0, lastIdx), { ...last, timeline: update(last.timeline) }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        // A fresh placeholder IS the start of a live turn — without this,
        // AgentTimeline mounts with streaming=false, reads done=true, and
        // auto-collapses on its very first frame.
        streaming: true,
        timeline: update(undefined),
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}

/**
 * Attach an Agent Companion payload (13.0.7) to the last assistant turn, keyed
 * by `emission_id` (falls back to `correlation_id` for an older event) —
 * append-or-replace-by-key, mirroring `workspaceStore`'s own dedupe rule so a
 * retried emission updates in place rather than duplicating. Message-scoped
 * (not session-wide): a companion for turn N attaches to turn N's own row,
 * never bleeding onto an earlier turn the way a session-keyed store would.
 */
export function attachOrUpdateCompanion(
    prev: Message[],
    payload: CoderCompanionPayload,
    agentName: string,
): Message[] {
    const key = payload.emission_id ?? payload.correlation_id;
    const merge = (prior: CoderCompanionPayload[] | undefined): CoderCompanionPayload[] => {
        const existing = prior ?? [];
        const idx = existing.findIndex(p => (p.emission_id ?? p.correlation_id) === key);
        return idx === -1
            ? [...existing, payload]
            : existing.map((p, i) => (i === idx ? payload : p));
    };
    const lastIdx = prev.length - 1;
    const last = lastIdx >= 0 ? prev[lastIdx] : undefined;
    if (last?.role === 'assistant') {
        return [...prev.slice(0, lastIdx), { ...last, companions: merge(last.companions) }];
    }
    return [...prev, {
        id: mkId(),
        role: 'assistant',
        content: '',
        companions: merge(undefined),
        authorLabel: authorLabelFor('assistant', agentName),
    }];
}
