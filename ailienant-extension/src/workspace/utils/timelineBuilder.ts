/**
 * Glass-Box Timeline — pure, order-agnostic builder functions.
 *
 * The backend delivers two kinds of frames for one turn: lightweight, seq-ordered
 * `server_activity_event` markers (the spine) and heavy bodies on their EXISTING
 * channels (reasoning deltas, diff blocks) correlated to a marker by `ref`. WS
 * delivery is ordered per-channel but NOT guaranteed ordered across channels, so a
 * body event may arrive before its marker or after it. Every upsert here is keyed
 * by identity (marker's `ref`, or a synthetic `seq:<n>` key for a ref-less marker)
 * so either arrival order produces the same final entry — see `upsertActivityMarker`
 * and each `upsertXBody` function's docstring for the specific adoption rule.
 *
 * Pure on inputs: every function returns a NEW array, never mutates `entries`.
 * No React, no module state — trivially unit-testable and reusable by the eventual
 * `AgentTimeline` renderer.
 */
import type { ActivityEventPayload } from '../../api/contracts';
import type {
    CellIterationShape, DiffBlockShape, ExecutionDetailShape, ToolCallShape,
    TimelineEntry, TimelineEntryStatus,
} from '../../shared/config';

/** Insert `entry` keeping the array sorted ascending by `seq` (an unresolved
 *  placeholder's `Number.POSITIVE_INFINITY` sorts last until a marker resolves it). */
function insertSorted(entries: TimelineEntry[], entry: TimelineEntry): TimelineEntry[] {
    const next = [...entries, entry];
    next.sort((a, b) => a.seq - b.seq);
    return next;
}

/**
 * Apply one `server_activity_event` marker: create the entry, or — if a body
 * event already created a placeholder under the same key — adopt it (resolve
 * seq/ts/kind/target/metric/ref onto the placeholder, preserving any body already
 * attached). Keyed by `payload.ref` when present, else a synthetic `seq:<n>` (a
 * marker with no ref is always self-contained — read/edit/understanding/
 * planning/reviewing/heal/retrieval, or a ref-less 'command' — e.g. a
 * "blocked" outcome that never reached an adapter — so it can never collide
 * with a later one).
 *
 * A ref-carrying 'command' marker starts 'active': it fires BEFORE execution
 * (core/exec_log.py::record_execution), and the matching `server_activity_detail`
 * resolves it via `upsertExecutionBody`. When that detail arrives first (WS
 * delivery is ordered per-channel but not across channels), the merge branch
 * below deliberately leaves `status` untouched — the placeholder it adopts is
 * already resolved to its final 'done'/'failed'.
 */
export function upsertActivityMarker(
    entries: TimelineEntry[],
    payload: ActivityEventPayload,
): TimelineEntry[] {
    const key = payload.ref ?? `seq:${payload.seq}`;
    const idx = entries.findIndex(e => e.id === key);
    if (idx === -1) {
        const isOpenSpan = payload.kind === 'reasoning' || payload.kind === 'cell'
            || (payload.kind === 'command' && payload.ref !== undefined && payload.ref !== null);
        const entry: TimelineEntry = {
            id: key,
            seq: payload.seq,
            ts: payload.ts,
            kind: payload.kind,
            target: payload.target ?? undefined,
            metric: payload.metric ?? undefined,
            ref: payload.ref ?? undefined,
            status: isOpenSpan ? 'active' : 'done',
        };
        return insertSorted(entries, entry);
    }
    const prev = entries[idx];
    const merged: TimelineEntry = {
        ...prev,
        seq: payload.seq,
        ts: payload.ts,
        kind: payload.kind,
        target: payload.target ?? prev.target,
        metric: payload.metric ?? prev.metric,
        ref: payload.ref ?? prev.ref,
    };
    return insertSorted(entries.filter((_, i) => i !== idx), merged);
}

/**
 * Append a streamed reasoning delta, keyed by its `ref` (the same id the matching
 * `reasoning` activity marker carries). If no entry exists yet under that key
 * (the delta arrived before its marker), create an unresolved placeholder — the
 * later marker adopts it via `upsertActivityMarker`'s id-match path above. A
 * missing `ref` (an older server, or the rare frame with none) falls into one
 * shared bucket — the same "one span per turn" scoped simplification the backend
 * `_ThinkingStreamer` applies when it mints a single ref per instance.
 */
export function upsertReasoningDelta(
    entries: TimelineEntry[],
    delta: string,
    ref: string | null | undefined,
    ts: number = Date.now() / 1000,
): TimelineEntry[] {
    const key = ref ?? '__reasoning_no_ref__';
    const idx = entries.findIndex(e => e.id === key);
    if (idx === -1) {
        const placeholder: TimelineEntry = {
            id: key,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'reasoning',
            ref: ref ?? undefined,
            status: 'active',
            thinking: delta,
        };
        return insertSorted(entries, placeholder);
    }
    const next = [...entries];
    next[idx] = { ...next[idx], thinking: (next[idx].thinking ?? '') + delta };
    return next;
}

/**
 * Attach a settled diff to its timeline entry, keyed by `file_path` (the same
 * value the backend's `diff` activity marker uses as `ref` — a real, stable wire
 * id known to both sides, so no placeholder-adoption trick is needed here; a
 * diff arriving first simply creates the entry outright).
 */
export function upsertDiffBody(
    entries: TimelineEntry[],
    filePath: string,
    diff: DiffBlockShape,
    ts: number = Date.now() / 1000,
): TimelineEntry[] {
    const idx = entries.findIndex(e => e.id === filePath);
    if (idx === -1) {
        const entry: TimelineEntry = {
            id: filePath,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'diff',
            target: filePath,
            ref: filePath,
            status: 'done',
            diff,
        };
        return insertSorted(entries, entry);
    }
    const next = [...entries];
    next[idx] = { ...next[idx], diff };
    return next;
}

/**
 * Attach a tool-chip artifact to its timeline entry, keyed by `tool_call_id` (a
 * real wire id, symmetric with `upsertDiffBody`). Exported and unit-tested for
 * forward use; not yet wired into a live handler — no activity marker emits a
 * `ref: tool_call_id` this slice, so there is nothing for it to correlate against
 * yet (paired with a future tool/cell activity-marker emission).
 */
export function upsertToolBody(
    entries: TimelineEntry[],
    toolCallId: string,
    tool: ToolCallShape,
    ts: number = Date.now() / 1000,
): TimelineEntry[] {
    const idx = entries.findIndex(e => e.id === toolCallId);
    if (idx === -1) {
        const entry: TimelineEntry = {
            id: toolCallId,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'command',
            target: tool.tool_name,
            ref: toolCallId,
            status: tool.status === 'pending' ? 'active' : 'done',
            tool,
        };
        return insertSorted(entries, entry);
    }
    const next = [...entries];
    next[idx] = { ...next[idx], tool, status: tool.status === 'pending' ? 'active' : 'done' };
    return next;
}

/**
 * Attach an agentic-cell iteration to its timeline entry, keyed by `cell:{iteration}`
 * (the same value the backend's `cell` activity marker uses as `ref`). Symmetric with
 * `upsertToolBody`: an iteration's tool calls / PTY output / AST diffs / governor tick
 * all update in place as they stream in, rather than creating a new entry per delta.
 */
export function upsertCellBody(
    entries: TimelineEntry[],
    ref: string,
    cell: CellIterationShape,
    ts: number = Date.now() / 1000,
): TimelineEntry[] {
    const idx = entries.findIndex(e => e.id === ref);
    if (idx === -1) {
        const entry: TimelineEntry = {
            id: ref,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'cell',
            target: cell.tools[cell.tools.length - 1]?.tool_name,
            ref,
            status: 'active',
            cell,
        };
        return insertSorted(entries, entry);
    }
    const next = [...entries];
    next[idx] = {
        ...next[idx],
        cell,
        target: cell.tools[cell.tools.length - 1]?.tool_name ?? next[idx].target,
    };
    return next;
}

/**
 * Attach the I/O detail to a 'command' timeline entry, keyed by the execution
 * id both `server_activity_event` (the marker, kind='command') and
 * `server_activity_detail` (this body) carry as `ref`. Order-agnostic like
 * every other upsert here: if the detail arrives before its marker, it
 * creates an already-resolved placeholder that the later marker's merge path
 * (in `upsertActivityMarker`) simply adopts without touching `status` — the
 * outcome is already final by the time the marker shows up. Status resolves
 * to 'failed' on a non-zero exit code or a reported `error`, 'done' otherwise.
 */
export function upsertExecutionBody(
    entries: TimelineEntry[],
    ref: string,
    execution: ExecutionDetailShape,
    ts: number = Date.now() / 1000,
): TimelineEntry[] {
    const status: TimelineEntryStatus =
        execution.error !== undefined || (execution.exit_code !== undefined && execution.exit_code !== 0)
            ? 'failed'
            : 'done';
    const idx = entries.findIndex(e => e.id === ref);
    if (idx === -1) {
        const entry: TimelineEntry = {
            id: ref,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'command',
            ref,
            status,
            execution,
        };
        return insertSorted(entries, entry);
    }
    const next = [...entries];
    next[idx] = { ...next[idx], execution, status };
    return next;
}

/**
 * The persisted view of a turn's timeline: 'reasoning' entries dropped entirely
 * (display-only, matching the pre-existing `thinking`/`thinkingTokens`/etc. fields'
 * exclusion from PERSIST_TRANSCRIPT — reasoning has never survived a reload). Every
 * other kind (plan/diff/read/edit/command/…) is durable audit evidence and persists
 * unchanged, taking over the role `checklist`/`diffBlocks` played before AgentTimeline.
 */
export function stripReasoningForPersist(entries: TimelineEntry[]): TimelineEntry[] {
    return entries.filter(e => e.kind !== 'reasoning');
}
