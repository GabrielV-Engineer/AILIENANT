/**
 * Inbound WS/IPC dispatch controller for the workspace panel.
 *
 * Owns the entire "turn a server/host event into a state mutation" concern: the
 * message listener + 45-branch dispatch switch, the two rAF-coalescing stream
 * buffers and their flushers, the unmount cleanup, and the stall watchdog. It is
 * dependency-free — every read and write goes through `useChatStore.getState()` /
 * `useWorkspaceStore.getState()`, so the listener registers ONCE and the handler
 * never closes over stale component state. `getState().messages` is the
 * synchronous source of truth, so a burst of events can't read a render-lagged
 * transcript and corrupt ordering.
 */
import { useEffect, useRef, useState, useCallback } from 'react';
import type {
    WsConnectionStatus, DiffBlockShape, PlanDocumentShape, PlanWBSStep,
    TelemetryFrame, TokenSnapshot, ASTToken, BudgetLimitMode, ToolCallShape,
} from '../../shared/config';
import type { AilienantConfig } from '../../shared/types';
import { DEFAULT_ANALYST_NAME } from '../../shared/types';
import type { Message, NattMessage, ConversationMessage, SystemMessage, ToastLevel } from '../types';
import type { HITLIntervention } from '../components/HITLInterventionCard';
import type { CheckpointEntry } from '../components/CheckpointPicker';
import { useChatStore } from '../chatStore';
import { useWorkspaceStore } from '../workspaceStore';
import { vscode } from '../vscode_bridge';
import { useTpsCalculator } from '../components/TelemetryHUD';
import {
    mkId, authorLabelFor, normalizeStuckChips, mergeById, requestCodeTokens,
    attachOrUpdateToolCall, attachOrUpdateCellRun, attachOrUpdateChecklist, appendPtyLines,
    DEFAULT_STREAM_WATCHDOG_MS, STREAM_WATCHDOG_TICK_MS, MAX_TOOL_OUTPUT_LINES,
    STREAM_ACTIVITY_EVENTS,
} from '../utils/messageDispatchHelpers';
import { accumulateThinking, newThinkingTurn, freezeThinkingOnText, bumpLiveTokens } from '../utils/thinkingReducer';
import { INITIAL_STATE as MD_INITIAL_STATE, pushToken as mdPushToken } from '../utils/StreamingMarkdownParser';
import { mergeStreamEmits, type StreamLineEmit } from '../utils/streamTokenBuffer';
import { sanitizePtyChunk } from '../utils/sanitizePty';

export function useWSMessageHandler(): void {
    // TPS is computed here (the only consumer is the streaming dispatch) and
    // mirrored into the store so the HUD can read it as a selector.
    const { recordChunk, tps } = useTpsCalculator();
    const recordRef = useRef(recordChunk);
    recordRef.current = recordChunk;
    useEffect(() => {
        useChatStore.getState().setTps(tps);
    }, [tps]);

    // Streaming-AST hydration buffer. Per-line STREAM_CODE_TOKENS pushes are
    // coalesced into a single setMessages per animation frame, stamped with the
    // target turn id; a frame that lands after a turn boundary is dropped.
    const streamTokenBufferRef = useRef<{ turnId: string; emits: StreamLineEmit[] } | null>(null);
    const streamTokenRafRef = useRef<number | null>(null);
    const flushStreamTokens = useCallback(() => {
        streamTokenRafRef.current = null;
        const buffered = streamTokenBufferRef.current;
        streamTokenBufferRef.current = null;
        if (!buffered || buffered.emits.length === 0) { return; }
        useChatStore.getState().setMessages(prev => {
            const last = prev[prev.length - 1];
            // Only the still-active streaming turn we buffered for is a valid target.
            if (!last || last.role !== 'assistant' || !last.streaming || last.id !== buffered.turnId) {
                return prev;
            }
            return [
                ...prev.slice(0, -1),
                { ...last, streamingCodeTokens: mergeStreamEmits(last.streamingCodeTokens, buffered.emits) },
            ];
        });
    }, []);

    // Cell PTY coalescing — sanitized lines buffered per iteration, flushed into one
    // setMessages per animation frame (mirrors the streaming-AST buffer).
    const cellPtyBufferRef = useRef<Map<number, string[]> | null>(null);
    const cellPtyRafRef = useRef<number | null>(null);
    const flushCellPty = useCallback(() => {
        cellPtyRafRef.current = null;
        const buffered = cellPtyBufferRef.current;
        cellPtyBufferRef.current = null;
        if (!buffered || buffered.size === 0) { return; }
        const cs = useChatStore.getState();
        const nattName = cs.config?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;
        cs.setMessages(prev => {
            let next = prev;
            for (const [iter, lines] of buffered) {
                next = attachOrUpdateCellRun(next, iter, (it) => appendPtyLines(it, lines), nattName);
            }
            return next;
        });
    }, []);

    // Stall-watchdog budget — backend-governed (posted via STREAM_WATCHDOG_MS);
    // `lastStreamActivityRef` is bumped on every live stream event.
    const [streamWatchdogMs, setStreamWatchdogMs] = useState<number>(DEFAULT_STREAM_WATCHDOG_MS);
    const lastStreamActivityRef = useRef<number>(0);

    // ── WS / extension message handler ─────────────────────────
    useEffect(() => {
        const handler = (event: MessageEvent): void => {
            const msg = event.data as { type: string; payload?: unknown; config?: unknown; open?: boolean };
            const cs = useChatStore.getState();
            const ws = useWorkspaceStore.getState();
            const nattName = cs.config?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;

            // Any live stream event keeps the stall watchdog at bay.
            if (STREAM_ACTIVITY_EVENTS.has(msg.type)) {
                lastStreamActivityRef.current = performance.now();
            }

            switch (msg.type) {
                case 'WS_STATUS':
                    cs.setWsStatus(msg.payload as WsConnectionStatus);
                    break;
                case 'STREAM_WATCHDOG_MS':
                    // Backend-governed stall timeout for the active model (zero-config).
                    if (typeof msg.payload === 'number' && msg.payload > 0) {
                        setStreamWatchdogMs(msg.payload);
                    }
                    break;
                case 'server_abort_ack': {
                    const d = msg.payload as { signalled?: boolean };
                    if (!d?.signalled) {
                        // No live task was cancelled (socket down, or it already
                        // finished). Clear the optimistic flag so Stop doesn't stay
                        // frozen, and surface the failure.
                        ws.setIsAborting(false);
                        cs.addToast('error', 'Stop failed — backend unreachable. The task may still be running.');
                    }
                    break;
                }
                case 'server_hitl_ack':
                    cs.addToast('info', 'Decision received.');
                    break;
                case 'RENDER_DIFF': {
                    // Host-enriched inline diff: an approved edit was applied, and
                    // the host surfaced both sides. Attach to the assistant turn
                    // that explained the edit (or a fresh placeholder if the stream
                    // already ended). Built once on edit-arrival — never per token.
                    const d = msg.payload as { patch_id: string; files: Omit<DiffBlockShape, 'patch_id'>[] };
                    if (!d?.files?.length) { break; }
                    const incoming: DiffBlockShape[] = d.files.map(f => ({ ...f, patch_id: d.patch_id }));
                    cs.setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant') {
                            // New array + new message object so React.memo on DiffBlock
                            // only reconciles this turn — composer keystrokes that
                            // re-render Workspace leave every other turn's refs intact.
                            return [...prev.slice(0, -1), {
                                ...last,
                                diffBlocks: [...(last.diffBlocks ?? []), ...incoming],
                            }];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'assistant',
                            content: '',
                            diffBlocks: incoming,
                            authorLabel: authorLabelFor('assistant', nattName),
                        }];
                    });
                    break;
                }
                case 'REHYDRATE_TRANSCRIPT': {
                    // Host re-posts the authoritative per-session transcript when this
                    // hidden panel becomes visible again (retainContextWhenHidden:false
                    // destroys the webview, so the creation-time snapshot is stale).
                    // Merge by id — an in-flight streaming turn is preserved.
                    const rh = msg as unknown as {
                        messages?: Message[]; nattMessages?: NattMessage[];
                    };
                    if (Array.isArray(rh.messages)) {
                        // Normalize chips left spinning at the teardown that triggered
                        // this rehydrate so they don't resurrect as perpetual "pending".
                        cs.setMessages(prev => normalizeStuckChips<Message>(mergeById(rh.messages as Message[], prev)));
                    }
                    if (Array.isArray(rh.nattMessages)) {
                        cs.setNattMessages(prev => mergeById(rh.nattMessages as NattMessage[], prev));
                    }
                    // A transient Stop flag must never survive a hide→reveal teardown,
                    // or the button stays frozen on the remounted panel.
                    ws.setIsAborting(false);
                    break;
                }
                case 'CONFIG_UPDATED':
                    cs.setConfig((msg.config ?? null) as AilienantConfig | null);
                    break;
                case 'OPEN_NATT':
                    ws.setNattOpen(true);
                    break;
                case 'server_token_chunk': {
                    const d = msg.payload as { token: string };
                    recordRef.current();
                    cs.setIsStreaming(true);
                    cs.setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant' && last.streaming) {
                            // Advance the markdown parser state ONCE per arriving
                            // token. O(1) per call.
                            const nextState = mdPushToken(
                                last.parserState ?? MD_INITIAL_STATE,
                                d.token,
                            );
                            // First answer token after a reasoning phase freezes the
                            // elapsed clock and auto-collapses the Thought Box.
                            const thinkingFreeze = freezeThinkingOnText(last, performance.now());
                            // Ghost Telemetry — tally this answer token into the
                            // per-turn live counter (fold into the same immutable
                            // rebuild; no extra state / setState).
                            return [...prev.slice(0, -1), bumpLiveTokens({
                                ...last,
                                content: last.content + d.token,
                                parserState: nextState,
                                ...(thinkingFreeze ?? {}),
                            })];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'assistant',
                            content: d.token,
                            streaming: true,
                            parserState: mdPushToken(MD_INITIAL_STATE, d.token),
                            authorLabel: authorLabelFor('assistant', nattName),
                            liveTokens: 1,
                        }];
                    });
                    break;
                }
                case 'server_thinking_chunk': {
                    // Accumulate raw reasoning into the streaming assistant turn's
                    // Thought Box. Display-only: never touches `content`, stripped
                    // before persist.
                    const d = msg.payload as { delta: string; token_count?: number };
                    recordRef.current();
                    cs.setIsStreaming(true);
                    cs.setMessages(prev => {
                        const now = performance.now();
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant' && last.streaming) {
                            return [...prev.slice(0, -1),
                                accumulateThinking(last, d.delta, d.token_count, now)];
                        }
                        return [...prev, { id: mkId(), ...newThinkingTurn(d.delta, d.token_count, now), authorLabel: authorLabelFor('assistant', nattName) }];
                    });
                    break;
                }
                case 'server_pipeline_step': {
                    // Attach the node trace to the active assistant turn. Steps arrive
                    // before tokens, so create the turn placeholder if needed.
                    const d = msg.payload as { node_name?: string };
                    if (!d?.node_name) { break; }
                    const node = d.node_name;
                    cs.setMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'assistant' && last.streaming) {
                            return [...prev.slice(0, -1), { ...last, steps: [...(last.steps ?? []), node] }];
                        }
                        return [...prev, { id: mkId(), role: 'assistant', content: '', streaming: true, steps: [node], authorLabel: authorLabelFor('assistant', nattName) }];
                    });
                    break;
                }
                case 'server_plan_document': {
                    // The structured plan and its one-line chat pointer arrive in ONE
                    // message, so the docked panel and the conversation bubble update
                    // in a single render transition — no two-message race that could
                    // flash the pointer against an empty panel.
                    const doc = msg.payload as PlanDocumentShape;
                    cs.setPlan(doc);
                    // Seed the in-chat execution checklist from the WBS. The early
                    // (seed-only) broadcast carries an empty summary; the per-step
                    // server_graph_mutation events flip the statuses from here.
                    if (doc.tasks && doc.tasks.length > 0) {
                        cs.setMessages(prev => attachOrUpdateChecklist(prev, () => doc.tasks, nattName));
                    }
                    if (doc.summary) {
                        recordRef.current();
                        cs.setMessages(prev => {
                            const last = prev[prev.length - 1];
                            if (last?.role === 'assistant' && last.streaming) {
                                return [...prev.slice(0, -1), {
                                    ...last,
                                    content: last.content ? `${last.content}\n${doc.summary}` : doc.summary,
                                }];
                            }
                            // Idempotency: the host re-posts the latest plan when a
                            // hidden panel becomes visible again (to restore the docked
                            // panel after a webview teardown). The panel state is
                            // restored by setPlan above; re-appending the summary here
                            // would stack a duplicate chat pointer on every tab switch,
                            // so skip when a turn already carries this exact summary.
                            if (prev.some(m => m.role === 'assistant' && m.content === doc.summary)) {
                                return prev;
                            }
                            return [...prev, {
                                id: mkId(),
                                role: 'assistant',
                                content: doc.summary,
                                streaming: true,
                                authorLabel: authorLabelFor('assistant', nattName),
                            }];
                        });
                    }
                    break;
                }
                case 'server_graph_mutation': {
                    // A WBS step changed status (pending→completed/failed, etc.). Flip
                    // the matching checklist row by step_number on the most recent turn
                    // that carries a checklist — never create a turn.
                    const d = msg.payload as { step_number: number; new_status: string };
                    cs.setMessages(prev => {
                        for (let i = prev.length - 1; i >= 0; i--) {
                            // System chips have no checklist; the cast is safe — only
                            // ConversationMessage rows ever carry checklist entries.
                            const m = prev[i] as ConversationMessage;
                            if (m.checklist && m.checklist.length > 0) {
                                const next = m.checklist.map((t: PlanWBSStep) =>
                                    t.step_number === d.step_number ? { ...t, status: d.new_status } : t);
                                return [...prev.slice(0, i), { ...m, checklist: next }, ...prev.slice(i + 1)];
                            }
                        }
                        return prev;
                    });
                    break;
                }
                case 'server_stream_end': {
                    cs.setIsStreaming(false);
                    // Paint any tokens still buffered for this turn before it finalizes,
                    // so the block doesn't flash back to plain in the gap between
                    // stream-end and the CODE_TOKENS round-trip that supersedes the
                    // overlay. Runs while the turn is still streaming, so the flush
                    // guard accepts it; the finalize below preserves streamingCodeTokens.
                    if (streamTokenRafRef.current !== null) {
                        cancelAnimationFrame(streamTokenRafRef.current);
                        streamTokenRafRef.current = null;
                    }
                    flushStreamTokens();
                    // Disarm the stall watchdog — the stream ended cleanly.
                    lastStreamActivityRef.current = 0;
                    // Back to idle whether the stream ended naturally or we aborted it.
                    ws.setIsAborting(false);
                    // The turn is complete and now lives in the host transcript; drop
                    // the in-flight resilience snapshot so a stale Thought Box can't
                    // resurrect on the next reload.
                    ws.setInflightTurn(null);
                    // Time-Travel: capture the L2-promoted checkpoint_id (when present)
                    // and attach it to the last assistant turn so the per-message ↪
                    // Branch button can target it. The payload field is optional
                    // (older servers don't emit it); absent → no button renders.
                    const _se = (msg.payload ?? {}) as { checkpoint_id?: string };
                    const _cid = typeof _se.checkpoint_id === 'string' ? _se.checkpoint_id : undefined;
                    cs.setMessages(prev => prev.map((m, i) => {
                        // System chips carry no streaming state; skip them.
                        if (i !== prev.length - 1 || m.role === 'system') { return m; }
                        const cm = m as ConversationMessage;
                        return {
                            ...cm,
                            streaming: false,
                            stepsDone: true,
                            parserState: undefined,
                            checkpoint_id: _cid ?? cm.checkpoint_id,
                        };
                    }));
                    // Round-trip the just-finalized turn's code blocks to the host
                    // grammar engine. Content is already fully accumulated (the
                    // finalize above only flips flags), so read it fresh from the store.
                    const finalizedList = useChatStore.getState().messages;
                    const finalized = finalizedList[finalizedList.length - 1];
                    if (finalized?.role === 'assistant' && finalized.id && finalized.content) {
                        requestCodeTokens(finalized.id, finalized.content);
                    }
                    break;
                }
                case 'CODE_TOKENS': {
                    // Host grammar engine answered the stream-end tokenize request.
                    // Merge the spans into the originating turn — but only if it still
                    // exists (cleared/replaced history drops the zombie reply, so an
                    // async reply for a gone turn is a safe no-op). Null results
                    // (unsupported lang / lexer fault) keep the plain fallback.
                    const d = msg.payload as {
                        turn_id?: string;
                        results?: { hash: string; ast_lines: ASTToken[][] | null }[];
                    };
                    if (!d?.turn_id || !Array.isArray(d.results) || d.results.length === 0) { break; }
                    cs.setMessages(prev => {
                        const idx = prev.findIndex(m => m.id === d.turn_id);
                        if (idx < 0) { return prev; }   // zombie reply — turn is gone
                        // Code tokens are only ever attached to ConversationMessage turns.
                        const cm = prev[idx] as ConversationMessage;
                        const merged: Record<string, ASTToken[][]> = { ...(cm.codeTokens ?? {}) };
                        let changed = false;
                        for (const r of d.results!) {
                            if (r.ast_lines) { merged[r.hash] = r.ast_lines; changed = true; }
                        }
                        if (!changed) { return prev; }
                        return [
                            ...prev.slice(0, idx),
                            { ...cm, codeTokens: merged },
                            ...prev.slice(idx + 1),
                        ];
                    });
                    break;
                }
                case 'STREAM_CODE_TOKENS': {
                    // Host pushed a tokenized code line for the active streaming turn.
                    // Rather than reconcile per line, append to a per-turn buffer and
                    // coalesce all pushes that land in one animation frame into a single
                    // setMessages (see flushStreamTokens). Only the last streaming
                    // assistant turn is a valid target; a push with no streaming turn
                    // (e.g. arriving after stream-end) is dropped.
                    const st = msg.payload as { block_seq?: number; line_index?: number; ast?: ASTToken[] };
                    if (
                        typeof st?.block_seq !== 'number' ||
                        typeof st.line_index !== 'number' ||
                        !Array.isArray(st.ast)
                    ) { break; }
                    const liveList = useChatStore.getState().messages;
                    const last = liveList[liveList.length - 1];
                    if (!last || last.role !== 'assistant' || !last.streaming || !last.id) { break; }
                    // Stamp the buffer with this turn; a new turn starts a fresh batch.
                    let buf = streamTokenBufferRef.current;
                    if (!buf || buf.turnId !== last.id) {
                        buf = { turnId: last.id, emits: [] };
                        streamTokenBufferRef.current = buf;
                    }
                    buf.emits.push({
                        block_seq: st.block_seq, line_index: st.line_index, ast: st.ast,
                    });
                    if (streamTokenRafRef.current === null) {
                        streamTokenRafRef.current = requestAnimationFrame(flushStreamTokens);
                    }
                    break;
                }
                // ── Rich Tool Chips ─────────────────────────────────────
                // Each tool call broadcasts (start → stream_chunk*, → result) and
                // optionally a dep_graph attachment. We build the chip up on the LAST
                // assistant message (the same target the token stream is appending to)
                // so the artifact lives alongside the narrative that introduced it.
                case 'server_tool_start': {
                    const d = msg.payload as {
                        tool_call_id: string;
                        tool_name: string;
                        args: Record<string, unknown>;
                        side_effect_free?: boolean;
                    };
                    cs.setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: d.tool_name,
                            args: d.args,
                            output_lines: [],
                            side_effect_free: d.side_effect_free,
                        }),
                        tool_name: d.tool_name,
                        args: d.args,
                        side_effect_free: d.side_effect_free,
                        status: 'pending',
                    }), nattName));
                    break;
                }
                case 'server_tool_stream_chunk': {
                    const d = msg.payload as { tool_call_id: string; chunk: string };
                    cs.setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: '(unknown)',
                            args: {},
                            output_lines: [],
                            status: 'pending' as const,
                        }),
                        // Hard tail-bound the retained output so a runaway tool
                        // cannot OOM the webview.
                        output_lines: [...(tc?.output_lines ?? []), d.chunk].slice(-MAX_TOOL_OUTPUT_LINES),
                    }), nattName));
                    break;
                }
                case 'server_tool_result': {
                    const d = msg.payload as {
                        tool_call_id: string;
                        status: 'success' | 'error';
                        exit_code?: number;
                        duration_ms?: number;
                    };
                    cs.setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: '(unknown)',
                            args: {},
                            output_lines: [],
                            status: 'pending' as const,
                        }),
                        status: d.status,
                        exit_code: d.exit_code,
                        duration_ms: d.duration_ms,
                    }), nattName));
                    break;
                }
                case 'server_tool_dep_graph': {
                    const d = msg.payload as {
                        tool_call_id: string;
                        nodes: { id: string; label: string }[];
                        edges: { from: string; to: string }[];
                    };
                    cs.setMessages(prev => attachOrUpdateToolCall(prev, d.tool_call_id, tc => ({
                        ...(tc ?? {
                            tool_call_id: d.tool_call_id,
                            tool_name: '(unknown)',
                            args: {},
                            output_lines: [],
                            status: 'pending' as const,
                        }),
                        dep_graph: { nodes: d.nodes, edges: d.edges },
                    }), nattName));
                    break;
                }
                case 'server_cell_tool_start': {
                    const d = msg.payload as {
                        iteration: number;
                        tool_name: string;
                        args_scrubbed: Record<string, string>;
                    };
                    cs.setMessages(prev => attachOrUpdateCellRun(prev, d.iteration, it => ({
                        ...it,
                        tools: [...it.tools, { tool_name: d.tool_name, args_scrubbed: d.args_scrubbed }],
                    }), nattName));
                    break;
                }
                case 'server_cell_pty_chunk': {
                    const d = msg.payload as { iteration: number; text: string };
                    const cleaned = sanitizePtyChunk(d.text);
                    if (cleaned.length === 0) { break; }
                    // Split into lines, dropping the empty fragment a trailing newline
                    // produces. Buffer per iteration; the rAF flush appends them.
                    const lines = cleaned.split('\n');
                    if (lines.length > 0 && lines[lines.length - 1] === '') { lines.pop(); }
                    if (lines.length === 0) { break; }
                    const buf = cellPtyBufferRef.current ?? new Map<number, string[]>();
                    buf.set(d.iteration, [...(buf.get(d.iteration) ?? []), ...lines]);
                    cellPtyBufferRef.current = buf;
                    if (cellPtyRafRef.current === null) {
                        cellPtyRafRef.current = requestAnimationFrame(flushCellPty);
                    }
                    break;
                }
                case 'server_cell_ast_diff': {
                    const d = msg.payload as {
                        iteration: number;
                        path: string;
                        search: string;
                        replace: string;
                    };
                    cs.setMessages(prev => attachOrUpdateCellRun(prev, d.iteration, it => ({
                        ...it,
                        diffs: [...it.diffs, { path: d.path, search: d.search, replace: d.replace }],
                    }), nattName));
                    break;
                }
                case 'server_cell_governor_tick': {
                    const d = msg.payload as {
                        iteration?: number;
                        step: number;
                        cost_usd: number;
                        elapsed_s: number;
                        axis: string | null;
                    };
                    // The governor tick keys off step (1-based); the iteration it
                    // belongs to is step - 1.
                    const iter = d.iteration ?? Math.max(0, d.step - 1);
                    cs.setMessages(prev => attachOrUpdateCellRun(prev, iter, it => ({
                        ...it,
                        governor: { step: d.step, cost_usd: d.cost_usd, elapsed_s: d.elapsed_s, axis: d.axis },
                    }), nattName));
                    break;
                }
                case 'server_telemetry': {
                    const frame = msg.payload as TelemetryFrame;
                    cs.setTelemetry(frame);
                    if (frame.is_red_alert && !cs.telemetry?.is_red_alert) {
                        cs.addToast('error', `Context insufficient (${frame.css_total.toFixed(0)}%) — inject more files`);
                    }
                    break;
                }
                case 'server_hitl_approval_request': {
                    const req = msg.payload as HITLIntervention & {
                        files?: DiffBlockShape[];
                        proposed_files?: Array<{ file_path: string; unified_diff?: string; new_content?: string; base_hash?: string | null }>;
                    };
                    // Soft-permission fast path: when the user enabled auto-accept and
                    // every risk metric is low (or none was attached), approve without
                    // ever mounting the card. The conservative gate — ANY medium/high
                    // metric falls through to the manual card — is the whole safety
                    // contract here.
                    const autoAccept = ws.autoAcceptLowRisk;
                    const allLowRisk = (req.risk_metrics ?? []).every(m => m.level === 'low');
                    if (autoAccept && allLowRisk) {
                        vscode.postMessage({
                            type: 'HITL_RESPONSE',
                            approval_id: req.approval_id,
                            approved: true,
                        });
                        cs.addToast('info', 'Auto-accepted low-risk edit');
                        break;
                    }
                    // The proposed diff rides in this same message (FILE_WRITE), so the
                    // inline diff and the Accept/Reject row mount together — attaching
                    // the blocks and setting hitlPending in one handler batches into a
                    // single React commit, so the buttons never appear without their
                    // code. The diff attaches to the awaiting assistant turn; patch_id =
                    // approval_id correlates them exactly.
                    //
                    // `files` is the host's syntax-highlighted both-sides preview. If
                    // it's absent (host preview faulted), fall back to the
                    // `proposed_files` that ride in this same payload and synthesize a
                    // diff from them — base_hash tells edit (existing) from create (new
                    // file). Either way the diff comes from ONE message, never a second
                    // broadcast, so the buttons can't mount without their code.
                    const blocks: DiffBlockShape[] = (req.files && req.files.length > 0)
                        ? req.files.map(f => ({ ...f, patch_id: req.approval_id }))
                        : (req.proposed_files ?? []).map(f => ({
                            patch_id: req.approval_id,
                            file_path: f.file_path,
                            old_content: '',
                            // Host preview faulted: the webview has no old side to
                            // reconstruct the O(Δ) diff against, so the new side may be
                            // empty here. The card still mounts; the user can authorize
                            // and the host's apply-time guard remains authoritative.
                            new_content: f.new_content ?? '',
                            status: (f.base_hash ? 'edit' : 'create') as 'edit' | 'create',
                        }));
                    if (blocks.length > 0) {
                        cs.setMessages(prev => {
                            const last = prev[prev.length - 1];
                            if (last?.role === 'assistant') {
                                return [...prev.slice(0, -1), {
                                    ...last,
                                    diffBlocks: [...(last.diffBlocks ?? []), ...blocks],
                                }];
                            }
                            return [...prev, { id: mkId(), role: 'assistant', content: '', diffBlocks: blocks }];
                        });
                    }
                    cs.setHitlPending(req);
                    cs.addToast('warn', `${nattName} requires your authorization`);
                    break;
                }
                case 'server_natt_message': {
                    const d = msg.payload as { content: string; is_alert?: boolean };
                    cs.setNattMessages(prev => [...prev, { id: mkId(), role: 'natt', content: d.content }]);
                    break;
                }
                case 'server_natt_token': {
                    // Accumulate batched analyst tokens into the active natt bubble,
                    // advancing the markdown parser per token.
                    const d = msg.payload as { token: string };
                    cs.setNattMessages(prev => {
                        const last = prev[prev.length - 1];
                        if (last?.role === 'natt' && last.streaming) {
                            const nextState = mdPushToken(
                                last.parserState ?? MD_INITIAL_STATE,
                                d.token,
                            );
                            return [...prev.slice(0, -1), {
                                ...last,
                                content: last.content + d.token,
                                parserState: nextState,
                            }];
                        }
                        return [...prev, {
                            id: mkId(),
                            role: 'natt',
                            content: d.token,
                            streaming: true,
                            parserState: mdPushToken(MD_INITIAL_STATE, d.token),
                        }];
                    });
                    break;
                }
                case 'server_natt_stream_end':
                    // Finalize the streamed analyst bubble; drop parserState → stable
                    // renderer path.
                    cs.setNattMessages(prev => prev.map((m, i) =>
                        i === prev.length - 1
                            ? { ...m, streaming: false, parserState: undefined }
                            : m));
                    break;
                case 'state_compacted': {
                    const d = msg.payload as { turns_compressed: number; compaction_message?: string };
                    // Use || (not ??) so an empty-string payload also triggers the fallback.
                    const label = d.compaction_message || `Context window compacted — ${d.turns_compressed} turn(s) summarized.`;
                    cs.setMessages(prev => {
                        const chip: SystemMessage = { id: mkId(), role: 'system', content: label };
                        const last = prev[prev.length - 1];
                        // Insert BEFORE any streaming tail so server_token_chunk (which
                        // targets prev.last by role==='assistant'&&streaming) is not orphaned.
                        if ((last as ConversationMessage | undefined)?.streaming) {
                            return [...prev.slice(0, -1), chip, last];
                        }
                        return [...prev, chip];
                    });
                    break;
                }
                case 'server_indexing_started': {
                    const d = msg.payload as { total_files?: number };
                    cs.setIndexing({ state: 'indexing', pct: 0, total_files: d?.total_files, files_indexed: 0 });
                    break;
                }
                case 'server_indexing_progress':
                case 'INDEXING_PROGRESS': {
                    // Backend wire format is { current, total, percentage } (websocket_manager.py).
                    // Completion is signalled as a 100% frame (current === total), so derive 'ready' here.
                    const d = msg.payload as { current?: number; total?: number; percentage?: number };
                    const filesIndexed = d?.current ?? 0;
                    const totalFiles = d?.total ?? 0;
                    const pct = d?.percentage ?? 0;
                    if (pct >= 100) {
                        // The completion signal arrives as a 1/1 frame after the real total frame;
                        // keep the larger count so it doesn't clobber the true node count.
                        const reported = totalFiles || filesIndexed;
                        cs.setIndexing(prev => {
                            const prevCount = prev.state === 'ready' ? prev.node_count : 0;
                            return { state: 'ready', node_count: Math.max(prevCount, reported) };
                        });
                    } else {
                        cs.setIndexing({
                            state: 'indexing',
                            pct,
                            files_indexed: filesIndexed,
                            total_files: totalFiles,
                        });
                    }
                    break;
                }
                case 'server_indexing_complete': {
                    const d = msg.payload as { node_count?: number };
                    cs.setIndexing({ state: 'ready', node_count: d?.node_count ?? 0 });
                    break;
                }
                case 'server_indexing_error': {
                    const d = msg.payload as { reason?: string };
                    const reason = d?.reason ?? 'LLM configuration missing';
                    cs.setIndexing({ state: 'error', reason });
                    cs.addToast('error', reason);   // carries the exact actionable command (e.g. ollama pull …)
                    break;
                }
                case 'server_byom_config_applied': {
                    const d = msg.payload as { preset_id?: string; preset_name?: string };
                    cs.addToast('info', `Preset "${d.preset_name ?? ''}" applied — retrying indexer…`);
                    cs.setIndexing(prev => prev.state === 'error' ? { state: 'idle' } : prev);
                    break;
                }
                case 'server_model_warmup': {
                    const d = msg.payload as { model_name: string; is_local: boolean };
                    cs.addToast('info', `Warming up ${d.model_name} (${d.is_local ? 'local' : 'cloud'})`);
                    break;
                }
                case 'server_oom_engaged': {
                    const d = msg.payload as { failed_model?: string; fallback_model?: string } | undefined;
                    const target = d?.fallback_model ? ` → ${d.fallback_model}` : '';
                    cs.addToast('error', `OOM detected — falling back to cloud model${target}`);
                    break;
                }
                case 'OCC_CONFLICT':
                    cs.setOccStatus('soft_conflict');
                    cs.setLockedFiles(prev => prev + 1);
                    break;
                case 'OCC_CLEAR':
                    cs.setOccStatus('clear');
                    cs.setLockedFiles(0);
                    break;
                case 'TOKEN_SNAPSHOT':
                    // Preserve any context-occupancy fields already merged in by a prior
                    // CONTEXT_OCCUPANCY frame — the cost/savings snapshot and the
                    // window-occupancy read arrive on separate cadences.
                    cs.setSnapshot(prev => ({ ...prev, ...(msg.payload as TokenSnapshot) }));
                    break;
                case 'CONTEXT_OCCUPANCY': {
                    const occ = msg.payload as { context_window: number; context_used_tokens: number };
                    cs.setSnapshot(prev => ({
                        local_tokens: prev?.local_tokens ?? 0,
                        cloud_tokens: prev?.cloud_tokens ?? 0,
                        savings_pct: prev?.savings_pct ?? 0,
                        total_cost_usd: prev?.total_cost_usd ?? 0,
                        ...prev,
                        context_window: occ.context_window,
                        context_used_tokens: occ.context_used_tokens,
                    }));
                    break;
                }
                case 'TASK_STARTED': {
                    const d = msg.payload as { task_id: string };
                    cs.setActiveTaskId(d.task_id);
                    break;
                }
                case 'PARALLEL_SESSION_NOTIFY': {
                    const count = (msg as unknown as { count: number }).count;
                    const label = count === 1 ? 'session is' : `${count} sessions are`;
                    cs.addToast('info', `${count} parallel ${label} running — AILIENANT isolates each independently.`);
                    break;
                }
                case 'MENTION_NOTIFY': {
                    // @-mention expansion outcome (oversize folder skipped / cap hit)
                    // surfaced in-panel rather than as a native popup.
                    const m = msg as unknown as { level: ToastLevel; message: string };
                    cs.addToast(m.level, m.message);
                    break;
                }
                // ── Time-Travel ─────────────────────────────────────────
                case 'CHECKPOINTS_LIST': {
                    // Host fetched GET /api/v1/sessions/{id}/checkpoints and is handing
                    // us the chain. Open the picker overlay.
                    const entries = (msg.payload ?? []) as CheckpointEntry[];
                    cs.setCheckpointPicker(Array.isArray(entries) ? entries : []);
                    break;
                }
                case 'SESSION_BRANCHED': {
                    // The host minted a new session and opened it in the sidebar; here
                    // we just dismiss any open picker and emit a confirmation toast. The
                    // webview's transcript was already replaced by the host's
                    // openSession() flow.
                    cs.setCheckpointPicker(null);
                    cs.addToast('info', '↪ Branched into a new session — original conversation preserved.');
                    break;
                }
                case 'BUDGET_UPDATED': {
                    const d = msg as unknown as { mode: BudgetLimitMode; weeklyUsd: number; monthlyUsd: number };
                    cs.setBudgetLimitMode(d.mode);
                    cs.setBudgetWeeklyUsd(d.weeklyUsd);
                    cs.setBudgetMonthlyUsd(d.monthlyUsd);
                    break;
                }
                case 'WORKSPACE_UPDATED': {
                    const d = msg as unknown as { workspaceFolder: string };
                    cs.setWorkspaceFolder(d.workspaceFolder);
                    break;
                }
                case 'CONVERSATION_CLEARED': {
                    cs.setMessages([]);
                    cs.setPlan(null);
                    break;
                }
                case 'PICKED_PATHS': {
                    const d = msg as unknown as { items: { path: string; kind: 'file' | 'directory' }[] };
                    const stamp = Date.now();
                    for (const item of d.items) {
                        vscode.postMessage({ type: 'ATTACH_CONTEXT', kind: item.kind, payload: item.path });
                    }
                    cs.setAttachedItems(prev => [
                        ...prev,
                        ...d.items.map((item, i) => ({ id: `${stamp}-${i}`, path: item.path, kind: item.kind })),
                    ]);
                    break;
                }
                case 'PICKED_NATT_PATHS': {
                    const d = msg as unknown as { items: { path: string; kind: 'file' | 'directory' }[] };
                    const stamp = Date.now();
                    cs.setNattAttachedItems(prev => [
                        ...prev,
                        ...d.items.map((item, i) => ({ id: `n-${stamp}-${i}`, path: item.path, kind: item.kind })),
                    ]);
                    break;
                }
            }
        };
        window.addEventListener('message', handler);
        return () => window.removeEventListener('message', handler);
        // Registers once: the handler reads all state via getState() and closes over
        // only stable refs / the []-stable flushers, so re-registration is never needed.
    }, [flushStreamTokens, flushCellPty]);

    // Cancel any pending coalesce frame on unmount so a queued flush can't fire
    // against a torn-down panel.
    useEffect(() => () => {
        if (streamTokenRafRef.current !== null) {
            cancelAnimationFrame(streamTokenRafRef.current);
            streamTokenRafRef.current = null;
        }
        if (cellPtyRafRef.current !== null) {
            cancelAnimationFrame(cellPtyRafRef.current);
            cellPtyRafRef.current = null;
        }
        cellPtyBufferRef.current = null;
    }, []);

    // Stream-stall watchdog. While streaming, if no token / tool / natt activity
    // arrives within the backend-governed budget, `server_stream_end` was almost
    // certainly lost — finalize the turn so the UI never hangs on "Streaming…".
    // The budget is dictated by the backend (longer for slow local engines), never
    // a hardcoded product constant.
    const isStreaming = useChatStore((s) => s.isStreaming);
    useEffect(() => {
        if (!isStreaming) { return; }
        if (lastStreamActivityRef.current === 0) {
            lastStreamActivityRef.current = performance.now();
        }
        const interval = setInterval(() => {
            if (performance.now() - lastStreamActivityRef.current <= streamWatchdogMs) { return; }
            lastStreamActivityRef.current = 0;
            const cs = useChatStore.getState();
            const ws = useWorkspaceStore.getState();
            cs.setIsStreaming(false);
            ws.setIsAborting(false);
            ws.setInflightTurn(null);
            cs.setMessages(prev => prev.map((m, i) => {
                if (m.role === 'system') { return m; }
                const cm = m as ConversationMessage;
                if (i !== prev.length - 1 || !cm.streaming) { return m; }
                const calls = cm.toolCalls?.map((tc: ToolCallShape) =>
                    (tc.status === undefined || tc.status === 'pending')
                        ? { ...tc, status: 'error' as const }
                        : tc);
                return { ...cm, streaming: false, stepsDone: true, parserState: undefined, toolCalls: calls };
            }));
            cs.addToast('warn', 'Stream stalled — no response from the backend. Ending this turn.');
        }, STREAM_WATCHDOG_TICK_MS);
        return () => clearInterval(interval);
    }, [isStreaming, streamWatchdogMs]);
}
