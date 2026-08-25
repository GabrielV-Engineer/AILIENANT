/**
 * Glass-Box Timeline — the live, chronological, spined transcript of one
 * assistant turn (Phase 11.5.C).
 *
 * Unifies what used to be five separate stacked widgets (PipelineProgress,
 * ReasoningStream, ExecutionChecklist, DiffBlock, CellAuditWidget) into one
 * ordered list, driven by `entries` (built from `server_activity_event` +
 * correlated bodies — see `utils/timelineBuilder.ts`). Rich Tool Chips
 * (`ToolChip`/`ActionLog`) stay a separate, unchanged sibling by deliberate
 * design, not a pending gap: their only live data source is a standalone
 * dev-palette debug command (`execute_tracked_tool`), never part of the
 * agent's turn, so there is no turn-scoped activity marker for them to
 * correlate against (see `docs/TECH_DEBT_BACKLOG.md` DEBT-122).
 *
 * Reasoning and plan rows render their existing, already-tested components
 * directly. Every 'reasoning' entry gets its own `ReasoningStream` instance and
 * toggle — settle/elapsed chronometry is entry-scoped (`entry.thinking*`),
 * falling back to the message-level `thinking*` fields only when an entry has
 * no correlated delta yet, so several reasoning spans in one turn (e.g. a grill
 * round's grounding pass, then its composing pass) each render as their own
 * independently-timed, independently-collapsible block. `ExecutionChecklist` is
 * always compact —
 * `diff` and `cell` rows carry their own entry-scoped body (`entry.diff` /
 * `entry.cell`), the heaviest elements in the trace; `cell` reuses
 * `CellAuditWidget` fed a synthetic single-iteration run, since it already
 * owns its own expand/collapse and live-follow logic per iteration. Self-
 * contained kinds (read/edit/command/understanding/planning/reviewing/heal/
 * retrieval) render one line.
 *
 * While streaming: expanded by default, auto-follows new rows unless the user
 * has scrolled up to inspect history. Unlike the five widgets it replaced, it
 * does NOT collapse the instant its own turn settles — the current turn stays
 * expanded and uncapped (see `isLatestTurn`) exactly as long as it remains the
 * most recent one, matching a chat transcript rather than a disappearing
 * progress bar. It collapses to a single honest summary line — "Worked for
 * Ns · N actions · N files changed" (never a throttled count) — only once a
 * NEWER turn begins; re-expandable afterward at any time. `prefers-reduced-
 * motion` (workspace.css) renders every state instantly, no pings/draws/sweeps.
 */
import { Fragment, memo, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import type { PlanWBSStep, TimelineEntry } from '../../shared/config';
import type { HitlRespond } from '../utils/useHitlResponder';
import type { ReasoningSource } from '../utils/thinkingReducer';
import { timelineEntryLabel, timelineEntryPhase, workLoopPhaseLabel, type WorkLoopPhase } from '../utils/activityLabels';
import { ReasoningGlyph } from './ReasoningGlyph';
import { ReasoningStream } from './ReasoningStream';
import { ExecutionChecklist } from './ExecutionChecklist';
import { DiffBlock } from './DiffBlock';
import { CellAuditWidget } from './CellAuditWidget';
import { ExecutionDetail } from './ExecutionDetail';

export interface AgentTimelineProps {
    entries: TimelineEntry[];
    streaming: boolean;
    // Whether this turn is still the most recent one in the transcript — the
    // collapse trigger (see the class docstring above). True for the entire
    // lifetime of the current turn, including after it settles; flips false
    // the moment a newer turn is appended, at which point this one collapses.
    isLatestTurn: boolean;
    // Reasoning body — reuses ReasoningStream's own header/glyph/toggle/settle
    // logic unchanged (message-scoped fields, not per-entry).
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
    thinkingSource?: ReasoningSource;
    // Plan body — reuses ExecutionChecklist (always compact, no extra toggle).
    checklist?: PlanWBSStep[];
    // Diff body — reuses DiffBlock; identical HITL wiring to the pre-timeline render.
    hitlApprovalId?: string;
    onRespondDiff?: HitlRespond;
    onRequestChangesDiff?: (feedback: string) => void;
    // Cell body — reuses CellAuditWidget fed a synthetic single-iteration run.
    onCellStdin?: (iteration: number, line: string) => void;
    // Whole-turn wall-clock duration (DEBT-126a), frozen at server_stream_end
    // from the submit-time marker — spans generation + actuation, unlike the
    // entries[]-derived span below. Preferred whenever present; undefined only
    // for a transcript rehydrated from before this field existed, where the
    // marker-span fallback keeps a pre-12.8 turn from showing no duration at all.
    turnElapsedMs?: number;
}

const STICK_TOLERANCE_PX = 24;

function summarize(entries: TimelineEntry[], turnElapsedMs?: number): string {
    let secs: number;
    if (turnElapsedMs !== undefined) {
        secs = Math.max(0, turnElapsedMs / 1000);
    } else {
        const first = entries[0];
        const last = entries[entries.length - 1];
        secs = first && last ? Math.max(0, last.ts - first.ts) : 0;
    }
    const actions = entries.length;
    const files = new Set(
        entries.filter(e => e.kind === 'diff' && e.target).map(e => e.target),
    ).size;
    return `Worked for ${secs.toFixed(1)}s · ${actions} ${actions === 1 ? 'action' : 'actions'}`
        + (files > 0 ? ` · ${files} ${files === 1 ? 'file' : 'files'} changed` : '');
}

function AgentTimelineImpl({
    entries, streaming, isLatestTurn,
    thinking, thinkingTokens, thinkingStartedAt, thinkingElapsedMs, thinkingOpen,
    checklist, hitlApprovalId, onRespondDiff, onRequestChangesDiff, onCellStdin, turnElapsedMs,
}: AgentTimelineProps): JSX.Element | null {
    const done = !streaming;
    // Initial state is derived from isLatestTurn, not hardcoded true: a
    // rehydrated past turn mounts already-collapsed (no expand-then-collapse
    // flash); the current turn mounts open. The effect below only ever
    // reacts to isLatestTurn flipping false — done settling this turn's own
    // stream does NOT collapse it; only a newer turn starting does.
    const [containerOpen, setContainerOpen] = useState(isLatestTurn);
    useEffect(() => { if (!isLatestTurn) { setContainerOpen(false); } }, [isLatestTurn]);

    const [manualDiffOpen, setManualDiffOpen] = useState<Record<string, boolean>>({});
    const [manualExecOpen, setManualExecOpen] = useState<Record<string, boolean>>({});
    const [manualReasonOpen, setManualReasonOpen] = useState<Record<string, boolean>>({});
    const lastDiffId = useMemo(() => {
        let id: string | null = null;
        for (const e of entries) { if (e.kind === 'diff') { id = e.id; } }
        return id;
    }, [entries]);
    const lastCellId = useMemo(() => {
        let id: string | null = null;
        for (const e of entries) { if (e.kind === 'cell') { id = e.id; } }
        return id;
    }, [entries]);
    // Work-loop phase headers (13.0.7): entry.id → the phase header to render
    // immediately before it. A phase-less entry (reasoning/cell) doesn't break
    // a run of the same phase around it, but a genuine phase change — even a
    // brief one, e.g. a single retrieval interrupting an act run — always gets
    // its own header rather than being silently folded into its neighbours.
    const phaseHeaderBefore = useMemo(() => {
        const map: Record<string, WorkLoopPhase> = {};
        let current: WorkLoopPhase | undefined;
        for (const e of entries) {
            const phase = timelineEntryPhase(e);
            if (phase === undefined) { continue; }
            if (phase !== current) { map[e.id] = phase; }
            current = phase;
        }
        return map;
    }, [entries]);

    const rowsRef = useRef<HTMLDivElement>(null);
    const stuckRef = useRef(true);
    useEffect(() => {
        const el = rowsRef.current;
        if (!el || !streaming || !stuckRef.current) { return; }
        el.scrollTop = el.scrollHeight;
    }, [entries.length, streaming]);

    // A checklist can outlive its own 'plan'-kind marker: stripReasoningForPersist
    // only strips 'reasoning' entries, but a turn whose entries were reasoning-only
    // to begin with (or whose 'plan' marker landed on a different message via a WS-
    // ordering race) round-trips through persistence as `entries:[]` while
    // `checklist` survives — bail only when there is truly nothing to show.
    if (entries.length === 0 && !(checklist && checklist.length > 0)) { return null; }

    const label = done ? summarize(entries, turnElapsedMs) : 'Working…';
    const anyActive = entries.some(e => e.status === 'active');

    // Extracted (not inlined in the .map() below) so the phase-header wrapper
    // can call it uniformly regardless of which kind-specific branch an entry
    // takes — every branch still returns its own `key`-carrying row exactly as
    // before; only its caller changed.
    function renderRow(entry: TimelineEntry, idx: number): JSX.Element {
        const isLast = idx === entries.length - 1;

        if (entry.kind === 'reasoning') {
            // Every reasoning entry gets its own independent
            // ReasoningStream + toggle — several spans in one turn
            // (e.g. a grill round's grounding pass, then its
            // question-composing pass) each render as their own
            // collapsible block instead of collapsing into one.
            // Per-entry fields fall back to the message-scoped
            // thinking* props when absent, so a marker-only entry
            // (no correlated delta yet) still renders as before.
            const entryThinking = entry.thinking ?? thinking ?? '';
            const entryTokens = entry.thinkingTokens ?? thinkingTokens ?? 0;
            const entryStartedAt = entry.thinkingStartedAt ?? thinkingStartedAt;
            const entryElapsedMs = entry.thinkingElapsedMs ?? thinkingElapsedMs;
            const isOpen = entry.id in manualReasonOpen
                ? manualReasonOpen[entry.id]
                : (entry.thinkingElapsedMs === undefined ? true : !!thinkingOpen);
            return (
                <div key={entry.id} className="ws-timeline-row" data-kind="reasoning">
                    <span className="ws-timeline-dot" data-status={entry.status} aria-hidden="true" />
                    <div className="ws-timeline-row-body">
                        <ReasoningStream
                            thinking={entryThinking}
                            tokens={entryTokens}
                            startedAt={entryStartedAt}
                            elapsedMs={entryElapsedMs}
                            open={isOpen}
                            streaming={streaming}
                            onToggle={() => setManualReasonOpen(prev => ({ ...prev, [entry.id]: !isOpen }))}
                        />
                    </div>
                </div>
            );
        }

        // A 'plan'-kind marker no longer renders the checklist inline — the
        // checklist is rendered once, unconditionally, as its own standalone
        // block below (see the `checklist` block just above `entries.map(...)`)
        // so it survives even when `entries` round-trips through persistence
        // empty. The marker itself falls through to the generic self-contained
        // one-liner below (timelineEntryLabel already renders "Planned N steps"
        // for it), keeping its place as a chronological marker in the trace.

        if (entry.kind === 'diff' && entry.diff) {
            // A settled diff (already applied to disk — Auto or a decided
            // Ask-mode approval) is a confirmed record, not a pending
            // decision — default it open regardless of turn-settle so it
            // doesn't vanish the instant the turn finishes (which, in Auto
            // mode, is nearly immediate). An unsettled diff keeps the old
            // "only the last one, only while streaming" default.
            const isOpen = entry.id in manualDiffOpen
                ? manualDiffOpen[entry.id]
                : (entry.diff.settled || (entry.id === lastDiffId && !done));
            const hitlActive = !!hitlApprovalId && entry.diff.patch_id === hitlApprovalId;
            return (
                <div key={entry.id} className="ws-timeline-row" data-kind="diff">
                    <span className="ws-timeline-dot" data-status={entry.status} aria-hidden="true" />
                    <div className="ws-timeline-row-body">
                        <button
                            type="button"
                            className="ws-timeline-row-header"
                            onClick={() => setManualDiffOpen(prev => ({ ...prev, [entry.id]: !isOpen }))}
                            aria-expanded={isOpen}
                        >
                            <span className="ws-timeline-row-label">{timelineEntryLabel(entry)}</span>
                            <Icon
                                name={isOpen ? 'chevron-down' : 'chevron-right'}
                                size={12}
                                className="ws-timeline-chevron"
                            />
                        </button>
                        {isOpen && (
                            <DiffBlock
                                block={entry.diff}
                                hitlActive={hitlActive}
                                onRespond={hitlActive ? onRespondDiff : undefined}
                                onRequestChanges={hitlActive ? onRequestChangesDiff : undefined}
                            />
                        )}
                    </div>
                </div>
            );
        }

        if (entry.kind === 'cell' && entry.cell) {
            const live = entry.id === lastCellId && !done;
            // Derived, not `entry.status`: the marker fires at iteration
            // start and nothing ever flips it back to 'done' in the data
            // model (same latent shape as the reasoning row above) — the
            // dot would pulse forever on re-expand after the turn settles
            // if left following `entry.status` directly. Reuses the same
            // liveness check already driving `streaming` below.
            const cellDotStatus = live ? 'active' : 'done';
            return (
                <div key={entry.id} className="ws-timeline-row" data-kind="cell">
                    <span className="ws-timeline-dot" data-status={cellDotStatus} aria-hidden="true" />
                    <div className="ws-timeline-row-body">
                        <CellAuditWidget
                            run={{ iterations: [entry.cell] }}
                            streaming={live}
                            onStdin={onCellStdin}
                        />
                    </div>
                </div>
            );
        }

        if (entry.kind === 'command' && entry.execution) {
            // A command that actually reached an adapter: expandable,
            // showing the execution envelope + I/O. A 'command' entry
            // with NO `execution` (e.g. a "blocked" outcome that never
            // reached one — no I/O body ever exists for it) falls
            // through to the plain single-line render below instead.
            // Default expanded (not click-per-row) — this IS the record of
            // what actually ran, not a pending decision to hide by default.
            const isOpen = entry.id in manualExecOpen ? manualExecOpen[entry.id] : true;
            return (
                <div key={entry.id} className="ws-timeline-row" data-kind="command">
                    <span className="ws-timeline-dot" data-status={entry.status} aria-hidden="true" />
                    <div className="ws-timeline-row-body">
                        <button
                            type="button"
                            className="ws-timeline-row-header"
                            onClick={() => setManualExecOpen(prev => ({ ...prev, [entry.id]: !isOpen }))}
                            aria-expanded={isOpen}
                        >
                            <span className="ws-timeline-row-label">{timelineEntryLabel(entry)}</span>
                            <Icon
                                name={isOpen ? 'chevron-down' : 'chevron-right'}
                                size={12}
                                className="ws-timeline-chevron"
                            />
                        </button>
                        {isOpen && <ExecutionDetail execution={entry.execution} />}
                    </div>
                </div>
            );
        }

        // Self-contained kinds: understanding/planning/reviewing/read/
        // edit/command/retrieval/heal — a single line, no expand affordance.
        return (
            <div
                key={entry.id}
                className="ws-timeline-row"
                data-kind={entry.kind}
                data-active={isLast && streaming ? 'true' : 'false'}
            >
                <span className="ws-timeline-dot" data-status={entry.status} aria-hidden="true" />
                <span className="ws-timeline-row-label">{timelineEntryLabel(entry)}</span>
            </div>
        );
    }

    return (
        <div
            className="ws-timeline"
            data-open={containerOpen ? 'true' : 'false'}
            data-streaming={streaming ? 'true' : 'false'}
            data-latest={isLatestTurn ? 'true' : 'false'}
        >
            <button
                type="button"
                className="ws-timeline-header"
                onClick={() => setContainerOpen(o => !o)}
                aria-expanded={containerOpen}
            >
                <ReasoningGlyph size={16} still={!(streaming && anyActive)} />
                <span className="ws-timeline-label">{label}</span>
                <Icon
                    name={containerOpen ? 'chevron-down' : 'chevron-right'}
                    size={12}
                    className="ws-timeline-chevron"
                />
            </button>
            {containerOpen && (
                <div className="ws-timeline-rows" ref={rowsRef} onScroll={() => {
                    const el = rowsRef.current;
                    if (!el) { return; }
                    stuckRef.current = el.scrollTop + el.clientHeight >= el.scrollHeight - STICK_TOLERANCE_PX;
                }}>
                    {checklist && checklist.length > 0 && (
                        <div className="ws-timeline-row" data-kind="plan">
                            <span className="ws-timeline-dot" data-status="done" aria-hidden="true" />
                            <div className="ws-timeline-row-body">
                                <ExecutionChecklist tasks={checklist} />
                            </div>
                        </div>
                    )}
                    {entries.map((entry, idx) => {
                        const headerPhase = phaseHeaderBefore[entry.id];
                        if (!headerPhase) { return renderRow(entry, idx); }
                        return (
                            <Fragment key={`group:${entry.id}`}>
                                <div className="ws-timeline-phase-header">
                                    <span className="ws-timeline-phase-label">{workLoopPhaseLabel(headerPhase)}</span>
                                </div>
                                {renderRow(entry, idx)}
                            </Fragment>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

/** Re-render only when a visible input changes — the same discipline as the
 *  widgets this component replaces (ReasoningStream/ExecutionChecklist). */
export const AgentTimeline = memo(AgentTimelineImpl, (a, b) =>
    a.entries === b.entries &&
    a.streaming === b.streaming &&
    a.isLatestTurn === b.isLatestTurn &&
    a.thinking === b.thinking &&
    a.thinkingTokens === b.thinkingTokens &&
    a.thinkingElapsedMs === b.thinkingElapsedMs &&
    a.thinkingOpen === b.thinkingOpen &&
    a.checklist === b.checklist &&
    a.hitlApprovalId === b.hitlApprovalId,
);
