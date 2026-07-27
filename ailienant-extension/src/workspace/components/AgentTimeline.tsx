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
 * directly (`ReasoningStream` owns its own settle/elapsed logic via the
 * message-level `thinking*` fields; `ExecutionChecklist` is always compact) —
 * `diff` and `cell` rows carry their own entry-scoped body (`entry.diff` /
 * `entry.cell`), the heaviest elements in the trace; `cell` reuses
 * `CellAuditWidget` fed a synthetic single-iteration run, since it already
 * owns its own expand/collapse and live-follow logic per iteration. Self-
 * contained kinds (read/edit/command/understanding/planning/reviewing/heal/
 * retrieval) render one line.
 *
 * While streaming: expanded by default, auto-follows new rows unless the user
 * has scrolled up to inspect history. The instant the turn settles it collapses
 * to a single honest summary line — "Worked for Ns · N actions · N files
 * changed" (never a throttled count) — re-expandable to the full, quiet trace.
 * `prefers-reduced-motion` (workspace.css) renders every state instantly, no
 * pings/draws/sweeps.
 */
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from '../../shared/Icon';
import type { PlanWBSStep, TimelineEntry } from '../../shared/config';
import type { HitlRespond } from '../utils/useHitlResponder';
import type { ReasoningSource } from '../utils/thinkingReducer';
import { timelineEntryLabel } from '../utils/activityLabels';
import { ReasoningGlyph } from './ReasoningGlyph';
import { ReasoningStream } from './ReasoningStream';
import { ExecutionChecklist } from './ExecutionChecklist';
import { DiffBlock } from './DiffBlock';
import { CellAuditWidget } from './CellAuditWidget';

export interface AgentTimelineProps {
    entries: TimelineEntry[];
    streaming: boolean;
    // Reasoning body — reuses ReasoningStream's own header/glyph/toggle/settle
    // logic unchanged (message-scoped fields, not per-entry).
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
    thinkingSource?: ReasoningSource;
    onReasoningToggle: () => void;
    // Plan body — reuses ExecutionChecklist (always compact, no extra toggle).
    checklist?: PlanWBSStep[];
    // Diff body — reuses DiffBlock; identical HITL wiring to the pre-timeline render.
    hitlApprovalId?: string;
    onRespondDiff?: HitlRespond;
    onRequestChangesDiff?: (feedback: string) => void;
    // Cell body — reuses CellAuditWidget fed a synthetic single-iteration run.
    onCellStdin?: (iteration: number, line: string) => void;
}

const STICK_TOLERANCE_PX = 24;

function summarize(entries: TimelineEntry[]): string {
    const first = entries[0];
    const last = entries[entries.length - 1];
    const secs = first && last ? Math.max(0, last.ts - first.ts) : 0;
    const actions = entries.length;
    const files = new Set(
        entries.filter(e => e.kind === 'diff' && e.target).map(e => e.target),
    ).size;
    return `Worked for ${secs.toFixed(1)}s · ${actions} ${actions === 1 ? 'action' : 'actions'}`
        + (files > 0 ? ` · ${files} ${files === 1 ? 'file' : 'files'} changed` : '');
}

function AgentTimelineImpl({
    entries, streaming,
    thinking, thinkingTokens, thinkingStartedAt, thinkingElapsedMs, thinkingOpen, onReasoningToggle,
    checklist, hitlApprovalId, onRespondDiff, onRequestChangesDiff, onCellStdin,
}: AgentTimelineProps): JSX.Element | null {
    const done = !streaming;
    const [containerOpen, setContainerOpen] = useState(true);
    // Auto-collapse to the summary bar the instant the turn settles; still
    // re-expandable by the user afterward (mirrors PipelineProgress's prior
    // done-state auto-collapse).
    useEffect(() => { if (done) { setContainerOpen(false); } }, [done]);

    const [manualDiffOpen, setManualDiffOpen] = useState<Record<string, boolean>>({});
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

    const rowsRef = useRef<HTMLDivElement>(null);
    const stuckRef = useRef(true);
    useEffect(() => {
        const el = rowsRef.current;
        if (!el || !streaming || !stuckRef.current) { return; }
        el.scrollTop = el.scrollHeight;
    }, [entries.length, streaming]);

    if (entries.length === 0) { return null; }

    const label = done ? summarize(entries) : 'Working…';
    const anyActive = entries.some(e => e.status === 'active');
    // Only the FIRST reasoning entry gets the rich ReasoningStream body (the
    // scoped one-ref-per-turn simplification means a second span is rare this
    // slice) — later ones degrade to a plain label row rather than a duplicate.
    let reasoningRendered = false;

    return (
        <div className="ws-timeline" data-open={containerOpen ? 'true' : 'false'} data-streaming={streaming ? 'true' : 'false'}>
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
                    {entries.map((entry, idx) => {
                        const isLast = idx === entries.length - 1;

                        if (entry.kind === 'reasoning' && !reasoningRendered) {
                            reasoningRendered = true;
                            return (
                                <div key={entry.id} className="ws-timeline-row" data-kind="reasoning">
                                    <span className="ws-timeline-dot" data-status={entry.status} aria-hidden="true" />
                                    <div className="ws-timeline-row-body">
                                        <ReasoningStream
                                            thinking={thinking ?? ''}
                                            tokens={thinkingTokens ?? 0}
                                            startedAt={thinkingStartedAt}
                                            elapsedMs={thinkingElapsedMs}
                                            open={!!thinkingOpen}
                                            streaming={streaming}
                                            onToggle={onReasoningToggle}
                                        />
                                    </div>
                                </div>
                            );
                        }

                        if (entry.kind === 'plan') {
                            return (
                                <div key={entry.id} className="ws-timeline-row" data-kind="plan">
                                    <span className="ws-timeline-dot" data-status={entry.status} aria-hidden="true" />
                                    <div className="ws-timeline-row-body">
                                        <ExecutionChecklist tasks={checklist ?? []} />
                                    </div>
                                </div>
                            );
                        }

                        if (entry.kind === 'diff' && entry.diff) {
                            const isOpen = entry.id in manualDiffOpen
                                ? manualDiffOpen[entry.id]
                                : (entry.id === lastDiffId && !done);
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
    a.thinking === b.thinking &&
    a.thinkingTokens === b.thinkingTokens &&
    a.thinkingElapsedMs === b.thinkingElapsedMs &&
    a.thinkingOpen === b.thinkingOpen &&
    a.checklist === b.checklist &&
    a.hitlApprovalId === b.hitlApprovalId,
);
