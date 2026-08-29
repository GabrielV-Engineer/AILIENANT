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
    CellIterationShape, DiffBlockShape, ExecutionDetailShape,
    TimelineEntry, TimelineEntryStatus,
} from '../../shared/config';
import {
    MAX_LIVE_EXEC_FIELD_CHARS, MAX_PERSISTED_REASONING_CHARS,
} from '../../shared/config';

/** Head+tail clamp for a live-accumulating exec field (DEBT-134) — mirrors the
 *  backend's own middle-truncation shape (core/redaction.py::truncate_middle)
 *  so both ends stay readable under the client-side retention ring. Pure. */
function clampLiveField(text: string, cap: number): string {
    if (text.length <= cap) { return text; }
    const half = Math.floor(cap / 2);
    const dropped = text.length - half * 2;
    return `${text.slice(0, half)}\n…[${dropped} chars truncated]…\n${text.slice(text.length - half)}`;
}

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
        const isRefCarryingSpan = payload.kind === 'command' || payload.kind === 'tool';
        const isOpenSpan = payload.kind === 'reasoning' || payload.kind === 'cell'
            || (isRefCarryingSpan && payload.ref !== undefined && payload.ref !== null);
        const entry: TimelineEntry = {
            id: key,
            seq: payload.seq,
            ts: payload.ts,
            kind: payload.kind,
            target: payload.target ?? undefined,
            metric: payload.metric ?? undefined,
            ref: payload.ref ?? undefined,
            role: payload.role ?? undefined,
            modelTier: payload.model_tier ?? undefined,
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
        role: payload.role ?? prev.role,
        modelTier: payload.model_tier ?? prev.modelTier,
    };
    return insertSorted(entries.filter((_, i) => i !== idx), merged);
}

/**
 * Freeze every still-open 'reasoning' entry's elapsed clock at `now`
 * (performance.now()-based, matching `thinkingStartedAt`) — idempotent, a no-op
 * for an entry that already has `thinkingElapsedMs` or never started. Called
 * both when a NEW reasoning span begins (the previous one implicitly ended) and
 * when the turn's answer text begins (mirrors the message-scoped
 * `freezeThinkingOnText`, but per-entry so several spans in one turn each keep
 * their own honest duration instead of sharing the turn's single clock).
 */
export function freezeActiveReasoningEntries(
    entries: TimelineEntry[],
    now: number,
): TimelineEntry[] {
    let changed = false;
    const next = entries.map(e => {
        if (e.kind !== 'reasoning' || e.thinkingElapsedMs !== undefined) { return e; }
        changed = true;
        const elapsed = e.thinkingStartedAt !== undefined ? Math.max(0, now - e.thinkingStartedAt) : 0;
        return { ...e, thinkingElapsedMs: elapsed };
    });
    return changed ? next : entries;
}

/**
 * Append a streamed reasoning delta, keyed by its `ref` (the same id the matching
 * `reasoning` activity marker carries). If no entry exists yet under that key
 * (the delta arrived before its marker), create an unresolved placeholder — the
 * later marker adopts it via `upsertActivityMarker`'s id-match path above. A
 * missing `ref` (an older server, or the rare frame with none) falls into one
 * shared bucket — the same "one span per turn" scoped simplification the backend
 * `_ThinkingStreamer` applies when it mints a single ref per instance.
 *
 * Per-entry chronometry (`thinkingTokens`/`thinkingStartedAt`) mirrors
 * `thinkingReducer.ts::accumulateThinking`'s message-scoped semantics, scoped to
 * this entry: a new key starting freezes any other still-open reasoning entry
 * first (§ freezeActiveReasoningEntries) — a later span beginning is proof the
 * earlier one ended, letting several reasoning blocks in one turn each render
 * with their own independent, correctly-settled clock.
 *
 * `ts` stays the 4th positional parameter for existing callers; the new
 * per-entry chronometry is bundled into a trailing options object rather than
 * inserted positionally, so no existing call site's argument order shifts.
 */
export function upsertReasoningDelta(
    entries: TimelineEntry[],
    delta: string,
    ref: string | null | undefined,
    ts: number = Date.now() / 1000,
    opts: { tokenCount?: number; now?: number } = {},
): TimelineEntry[] {
    const { tokenCount, now = Date.now() } = opts;
    const key = ref ?? '__reasoning_no_ref__';
    const idx = entries.findIndex(e => e.id === key);
    if (idx === -1) {
        const withPriorFrozen = freezeActiveReasoningEntries(entries, now);
        const placeholder: TimelineEntry = {
            id: key,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'reasoning',
            ref: ref ?? undefined,
            status: 'active',
            thinking: delta,
            thinkingTokens: tokenCount ?? 0,
            thinkingStartedAt: now,
        };
        return insertSorted(withPriorFrozen, placeholder);
    }
    const next = [...entries];
    const prev = next[idx];
    next[idx] = {
        ...prev,
        thinking: (prev.thinking ?? '') + delta,
        thinkingTokens: tokenCount ?? prev.thinkingTokens ?? 0,
        thinkingStartedAt: prev.thinkingStartedAt ?? now,
    };
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
 * Attach an agentic-cell iteration to its timeline entry, keyed by `cell:{iteration}`
 * (the same value the backend's `cell` activity marker uses as `ref`). An
 * iteration's tool calls / PTY output / AST diffs / governor tick all update in
 * place as they stream in, rather than creating a new entry per delta.
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
 * Append one incremental stdout/stderr fragment to a 'command' timeline
 * entry's execution body (DEBT-134), arriving BEFORE the terminal
 * `server_activity_detail` that `upsertExecutionBody` resolves. Keyed by the
 * same `ref` both channels share. If no entry exists yet (a chunk racing
 * ahead of its own marker — WS delivery is ordered per-channel but not
 * across), creates an 'active' placeholder exactly like every other upsert
 * here.
 *
 * Once the terminal detail lands (status flips to 'done'/'failed'), any
 * further stray chunk for the same ref is a no-op: `upsertExecutionBody`
 * REPLACES the execution body wholesale, so a late chunk appending after that
 * point would corrupt the now-authoritative settled record — "the terminal
 * detail always replaces, never appends to, accumulated chunk text."
 *
 * Clamped client-side (`clampLiveField` / `MAX_LIVE_EXEC_FIELD_CHARS`) as
 * defense-in-depth alongside the backend's own cumulative budget
 * (`api/websocket_manager.py::_LIVE_STREAM_CAP`) — the store can never grow
 * past this bound per field even if a producer misbehaves.
 */
export function upsertExecutionChunk(
    entries: TimelineEntry[],
    ref: string,
    stream: 'stdout' | 'stderr',
    chunk: string,
    ts: number = Date.now() / 1000,
): TimelineEntry[] {
    const idx = entries.findIndex(e => e.id === ref);
    if (idx === -1) {
        const entry: TimelineEntry = {
            id: ref,
            seq: Number.POSITIVE_INFINITY,
            ts,
            kind: 'command',
            ref,
            status: 'active',
            execution: { source: 'unknown', [stream]: chunk },
        };
        return insertSorted(entries, entry);
    }
    const prev = entries[idx];
    if (prev.status !== 'active') { return entries; }  // already settled — a stale chunk
    const prevExecution: ExecutionDetailShape = prev.execution ?? { source: 'unknown' };
    const accumulated = clampLiveField((prevExecution[stream] ?? '') + chunk, MAX_LIVE_EXEC_FIELD_CHARS);
    const next = [...entries];
    next[idx] = { ...prev, execution: { ...prevExecution, [stream]: accumulated } };
    return next;
}

/**
 * The persisted view of a turn's timeline. Every kind survives, reasoning included:
 * a trace that evaporates on reload is not an audit trail, and reasoning is the one
 * body that exists nowhere else (a diff is re-posted by RENDER_DIFF, execution I/O
 * is durable in the backend exec log).
 *
 * Two normalizations make a reasoning entry safe to store:
 *
 *  - **Bounded.** Each `thinking` body is middle-truncated to `maxChars` via the
 *    same `clampLiveField` the live exec fields use, so both ends stay readable.
 *  - **Clock frozen.** `thinkingStartedAt` is a `performance.now()` reading, and
 *    that origin is per-document: a webview teardown resets it, so a restored
 *    still-open span would measure its elapsed time against a foreign epoch. Any
 *    open span is settled first and the origin is dropped, leaving the already
 *    resolved `thinkingElapsedMs` as the only duration the renderer can read.
 */
export function prepareReasoningForPersist(
    entries: TimelineEntry[],
    maxChars: number = MAX_PERSISTED_REASONING_CHARS,
    now: number = performance.now(),
): TimelineEntry[] {
    return freezeActiveReasoningEntries(entries, now).map(e => {
        if (e.kind !== 'reasoning') { return e; }
        const next: TimelineEntry = { ...e };
        delete next.thinkingStartedAt;
        if (next.thinking !== undefined) {
            next.thinking = clampLiveField(next.thinking, maxChars);
        }
        return next;
    });
}

/**
 * Strip the heavy correlated bodies (`diff`/`cell`/`execution`) while keeping the
 * marker spine. For the in-flight `vscode.setState()` snapshot ONLY — the durable
 * transcript keeps them as audit evidence.
 *
 * The asymmetry is deliberate: that slot is size-critical and shared with the
 * user's draft, and each of these bodies is recoverable (RENDER_DIFF re-posts a
 * diff; execution detail lives in the exec log) whereas the reasoning kept beside
 * them is not. So bodies are what gets dropped there, never the reasoning.
 */
export function dropHeavyBodiesForSnapshot(entries: TimelineEntry[]): TimelineEntry[] {
    return entries.map(e => {
        if (e.diff === undefined && e.cell === undefined && e.execution === undefined) {
            return e;
        }
        const next: TimelineEntry = { ...e };
        delete next.diff;
        delete next.cell;
        delete next.execution;
        return next;
    });
}

/**
 * Trim a timeline from the HEAD until its serialized size fits `maxChars` — the
 * end of a turn is what the user is looking at when they switch back, so the
 * oldest rows are the ones to lose.
 *
 * Sizes are measured once per entry rather than re-serializing the whole array
 * inside a shrink loop, which would be O(n²) on a capped-out 500-entry turn.
 */
export function fitSnapshotBudget(
    entries: TimelineEntry[],
    maxChars: number,
): TimelineEntry[] {
    const sizes = entries.map(e => JSON.stringify(e).length + 1);  // +1 for the comma
    let total = sizes.reduce((sum, n) => sum + n, 2);              // 2 for the brackets
    let start = 0;
    while (start < entries.length && total > maxChars) {
        total -= sizes[start];
        start++;
    }
    return start === 0 ? entries : entries.slice(start);
}
