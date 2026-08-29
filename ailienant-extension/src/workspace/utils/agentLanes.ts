/**
 * Glass-Box Timeline — folds a flat `TimelineEntry[]` into consecutive
 * per-agent lanes (13.1.9).
 *
 * Pure, no React — mirrors `briefReviewLogic.ts`'s shape so this stays testable
 * without a DOM. Lanes replace the 13.0.7 work-loop phase grouping
 * (`activityLabels.ts`'s now-deleted `timelineEntryPhase`): the agent already
 * implies the phase in practice (a researcher gathers, a coder acts), so a
 * second grouping axis over the same handful of rows was redundant, and it
 * made a short turn (two rows, two single-row phase groups) look broken
 * rather than short.
 *
 * Folding is CONSECUTIVE, not a global group-by: a turn that goes
 * researcher → planner → coder → researcher (a second retrieval pass mid-step)
 * renders four lanes in chronological order, never merging the two researcher
 * runs into one — the trace must read as a timeline, not a summary table.
 */
import type { TimelineEntry } from '../../shared/config';
import type { AilienantConfig } from '../../shared/types';

export interface AgentLane {
    /** Stable React key — the id of the lane's first entry. */
    id: string;
    /** Undefined for a run of entries the backend could not attribute (a
     *  graph fan-out anchor, or an entry emitted before role existed) —
     *  renders without a chip, never a fabricated agent name. */
    role?: string;
    /** The first model tier found among the lane's entries. A tier can be
     *  unbound for a node's opening marker (resolved only partway through the
     *  node — e.g. the planner's first "Planning" row fires before routing
     *  resolves) — later entries in the same lane still carry it, so taking
     *  the first non-undefined value is what makes the chip appear at all. */
    modelTier?: string;
    entries: TimelineEntry[];
}

// Acronym words a plain title-case would otherwise render wrong ("Qa" instead
// of "QA"). Narrow and named, not a full role→label table (§5.7): every other
// role string title-cases correctly from its own words, so only the genuine
// exception is listed.
const ACRONYM_WORDS: ReadonlySet<string> = new Set(['qa', 'vcs', 'ml']);

/** Human display name for a lane's `role` string ("core_dev" -> "Core Dev",
 *  "qa_tester" -> "QA Tester"). Derived from the role itself, not a
 *  hand-maintained map — a role added later renders sensibly with no update
 *  needed here. */
export function formatRoleLabel(role: string): string {
    return role
        .split('_')
        .filter(Boolean)
        .map(word => (
            ACRONYM_WORDS.has(word) ? word.toUpperCase() : word[0].toUpperCase() + word.slice(1)
        ))
        .join(' ');
}

const KNOWN_TIERS: ReadonlySet<string> = new Set(['small', 'medium', 'big', 'cloud']);

/**
 * "big · qwen2.5-coder:32b" — the lane badge's tier + real model name.
 *
 * `config.tiers` (the active preset's tier -> real alias map) is already a
 * global, always-available field on `workspaceStore` — no extra round-trip
 * needed to join a tier onto its real name. Degrades to the bare tier when
 * `config` is unavailable or the tier is unrecognized, never a blank badge.
 */
export function formatModelBadge(tier: string, config: AilienantConfig | null): string {
    if (!KNOWN_TIERS.has(tier)) { return tier; }
    const realName = config?.tiers?.[tier as 'small' | 'medium' | 'big' | 'cloud'];
    return realName ? `${tier} · ${realName}` : tier;
}

export function buildAgentLanes(entries: TimelineEntry[]): AgentLane[] {
    const lanes: AgentLane[] = [];
    for (const entry of entries) {
        const prevLane = lanes[lanes.length - 1];
        if (prevLane && prevLane.role === entry.role) {
            prevLane.entries.push(entry);
            if (prevLane.modelTier === undefined && entry.modelTier !== undefined) {
                prevLane.modelTier = entry.modelTier;
            }
            continue;
        }
        lanes.push({
            id: `lane:${entry.id}`,
            role: entry.role,
            modelTier: entry.modelTier,
            entries: [entry],
        });
    }
    return lanes;
}
