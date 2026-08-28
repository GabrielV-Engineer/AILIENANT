import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import * as Popover from '@radix-ui/react-popover';
import { vscode } from './vscode_bridge';
import { useWorkspaceStore } from './workspaceStore';
import { useChatStore } from './chatStore';
import { DreamingProfile, OrchestrationMode, DiffBlockShape, BudgetLimitMode } from '../shared/config';
import type { ExecutionMode } from '../shared/types';
import { DEFAULT_ANALYST_NAME } from '../shared/types';
import { Icon } from '../shared/Icon';
import { Tooltip as AiTooltip } from '../shared/Tooltip';
import { WorkspaceHeader } from './components/WorkspaceHeader';
import { TelemetryHUD } from './components/TelemetryHUD';
import { CSSAlertBanner } from './components/CSSAlertBanner';
import { PromptBar } from './components/PromptBar';
import { ActiveTaskHeader } from './components/ActiveTaskHeader';
import { SessionSummaryCard } from './components/SessionSummaryCard';
import { NattCanvas } from './components/NattCanvas';
import { MarkdownRenderer } from './components/MarkdownRenderer';
import { ToolChip } from './components/ToolChip';
import { AgentTimeline } from './components/AgentTimeline';
import { CoderCompanionCard } from './components/CoderCompanionCard';
import { AgentTodoPanel } from './components/AgentTodoPanel';
import { MessageActions } from './components/MessageActions';
import { CheckpointPicker } from './components/CheckpointPicker';
import { IndexingStatus } from './components/IndexingStatus';
import { PlanAcceptancePanel } from './components/PlanAcceptancePanel';
import { ActionLog } from './components/ActionLog';
import { HITLInterventionCard } from './components/HITLInterventionCard';
import { ClarificationGrillCard } from './components/ClarificationGrillCard';
import { BriefReviewCard, BRIEF_REVIEW_KIND } from './components/BriefReviewCard';
import { useHitlResponder } from './utils/useHitlResponder';
import { getPresetConfig } from './hooks/useReasoningPreset';
import { ErrorBoundary } from './components/ErrorBoundary';
import { ToastStack } from './components/ToastStack';
import { useWSMessageHandler } from './hooks/useWSMessageHandler';
import { useSessionPersistence } from './hooks/useSessionPersistence';
import {
    mkId, authorLabelFor, attachOrUpdateCellRun, attachOrUpdateTimeline,
    readMergedCellIteration, appendPtyLines,
} from './utils/messageDispatchHelpers';
import { upsertCellBody } from './utils/timelineBuilder';
import { timelineEntryLabel } from './utils/activityLabels';
import type { Message, ConversationMessage, SystemMessage, InitialState } from './types';
import { MESSAGE_COMPACTION_THRESHOLD } from './types';

/**
 * Inline fallback for a single message row that throws during render. A malformed
 * turn degrades to this quiet placeholder instead of crashing the whole transcript.
 */
function MessageRowFallback(): JSX.Element {
    return (
        <div className="ws-error-row" role="alert">
            ⚠ This message could not be displayed.
        </div>
    );
}

/**
 * The literal phrase that flips the backend Socratic loop into synthesis.
 * `analyst._is_agreement` does a case-insensitive substring match against its
 * `_AGREEMENT_SIGNALS` frozenset; this phrase matches both "looks good" and
 * "proceed". It is the sole frontend↔backend coupling point for the Planner
 * handoff — keep it in sync with that set.
 */
const AGREEMENT_SIGNAL = 'Looks good, proceed.';

/** Single source of truth for the Socratic-grill, read-only planning mode's
 * literal — every comparison/assignment below goes through this constant or
 * `isPlanMode()` so a future mode-picker change has one place to update. */
const PLAN_MODE: ExecutionMode = 'plan_mode';
const isPlanMode = (mode: ExecutionMode): boolean => mode === PLAN_MODE;

export function Workspace({ initial }: { initial: InitialState }): JSX.Element {
    // Seed the live chat store from the host snapshot ONCE, synchronously, before any
    // selector below reads — a lazy initializer (not an effect) so render 1 paints the
    // populated transcript with no empty-then-filled flash.
    useState(() => { useChatStore.getState().hydrate(initial); return null; });

    // ── Live chat state — memory-only store, mutated by the WS controller ──
    const messages = useChatStore((s) => s.messages);
    const setMessages = useChatStore((s) => s.setMessages);
    const isStreaming = useChatStore((s) => s.isStreaming);
    const isTurnActive = useChatStore((s) => s.isTurnActive);
    const wsStatus = useChatStore((s) => s.wsStatus);
    const nattMessages = useChatStore((s) => s.nattMessages);
    const setNattMessages = useChatStore((s) => s.setNattMessages);
    const hitlPending = useChatStore((s) => s.hitlPending);
    const setHitlPending = useChatStore((s) => s.setHitlPending);
    const config = useChatStore((s) => s.config);
    const telemetry = useChatStore((s) => s.telemetry);
    const occStatus = useChatStore((s) => s.occStatus);
    const lockedFiles = useChatStore((s) => s.lockedFiles);
    const snapshot = useChatStore((s) => s.snapshot);
    const indexing = useChatStore((s) => s.indexing);
    const activeTaskId = useChatStore((s) => s.activeTaskId);
    const activeTaskPrompt = useChatStore((s) => s.activeTaskPrompt);
    const activeTaskStartedAt = useChatStore((s) => s.activeTaskStartedAt);
    const setActiveTaskPrompt = useChatStore((s) => s.setActiveTaskPrompt);
    const checkpointPicker = useChatStore((s) => s.checkpointPicker);
    const setCheckpointPicker = useChatStore((s) => s.setCheckpointPicker);
    const budgetLimitMode = useChatStore((s) => s.budgetLimitMode);
    const budgetWeeklyUsd = useChatStore((s) => s.budgetWeeklyUsd);
    const budgetMonthlyUsd = useChatStore((s) => s.budgetMonthlyUsd);
    const setBudgetLimitMode = useChatStore((s) => s.setBudgetLimitMode);
    const setBudgetWeeklyUsd = useChatStore((s) => s.setBudgetWeeklyUsd);
    const setBudgetMonthlyUsd = useChatStore((s) => s.setBudgetMonthlyUsd);
    const workspaceFolder = useChatStore((s) => s.workspaceFolder);
    const attachedItems = useChatStore((s) => s.attachedItems);
    const setAttachedItems = useChatStore((s) => s.setAttachedItems);
    const nattAttachedItems = useChatStore((s) => s.nattAttachedItems);
    const setNattAttachedItems = useChatStore((s) => s.setNattAttachedItems);
    const plan = useChatStore((s) => s.plan);
    const setPlan = useChatStore((s) => s.setPlan);
    const toasts = useChatStore((s) => s.toasts);
    const tps = useChatStore((s) => s.tps);

    const nattName = config?.agent_settings.analyst_name ?? DEFAULT_ANALYST_NAME;
    const budgetUsd = budgetLimitMode === 'weekly'  ? budgetWeeklyUsd
                    : budgetLimitMode === 'monthly' ? budgetMonthlyUsd : 0;

    // ── Persisted UI slice (workspaceStore) ──
    const mode    = useWorkspaceStore((s) => s.mode);
    const setMode = useWorkspaceStore((s) => s.setMode);
    const preset    = useWorkspaceStore((s) => s.preset);
    const setPreset = useWorkspaceStore((s) => s.setPreset);
    // Routing tier is no longer user-selectable (presets drive routing); the value
    // persists with its default and still rides every SUBMIT_TASK payload.
    const tier    = useWorkspaceStore((s) => s.tier);
    const isAborting    = useWorkspaceStore((s) => s.isAborting);
    const setIsAborting = useWorkspaceStore((s) => s.setIsAborting);
    const nattOpen    = useWorkspaceStore((s) => s.nattOpen);
    const setNattOpen = useWorkspaceStore((s) => s.setNattOpen);
    const coreMenuOpen    = useWorkspaceStore((s) => s.coreMenuOpen);
    const setCoreMenuOpen = useWorkspaceStore((s) => s.setCoreMenuOpen);

    // ── Local UI-only state (never touched by the WS dispatch) ──
    const [dreamingActive, setDreamingActive] = useState(false);
    const [dreamingProfile, setDreamingProfile] = useState<DreamingProfile>('Hybrid');
    const [activeModelId, setActiveModelId] = useState<string>(initial.activeModelId ?? '');
    const [orchestrationMode, setOrchestrationMode] = useState<OrchestrationMode>(initial.orchestrationMode ?? 'auto');
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // ── Compaction fold state (session-transcript declutter) ──
    // The last message carrying a `compaction` marker is the fold boundary: everything
    // before it can collapse behind the SessionSummaryCard. Fold state is per-marker and
    // resets when a newer compaction arrives (see the reset effect below).
    let lastCompactionIdx = -1;
    for (let k = messages.length - 1; k >= 0; k--) {
        if ((messages[k] as SystemMessage).compaction) { lastCompactionIdx = k; break; }
    }
    // DEBT-124: the compaction chip is a transient SystemMessage, deliberately
    // excluded from PERSIST_TRANSCRIPT — so a panel hide/reveal leaves NO live
    // marker in `messages` even though the persisted `compactionFold` slice
    // (workspaceStore) survives. Fall back to it, resolving `afterMessageId`
    // against the rehydrated `messages` array by id (stable across reload;
    // an index would not be). `boundaryIsChipRow` distinguishes the two
    // shapes: a live chip occupies its OWN placeholder row (hidden count =
    // its index), while the persisted anchor IS a real bubble that must be
    // folded away too (hidden count = its index + 1).
    const persistedFold = useWorkspaceStore((s) => s.compactionFold);
    let foldBoundaryIdx = lastCompactionIdx;
    let boundaryIsChipRow = lastCompactionIdx >= 0;
    let fallbackSummaryText: string | undefined;
    if (lastCompactionIdx === -1 && persistedFold?.afterMessageId) {
        const anchorIdx = messages.findIndex((m) => m.id === persistedFold.afterMessageId);
        if (anchorIdx >= 0) {
            foldBoundaryIdx = anchorIdx;
            boundaryIsChipRow = false;
            fallbackSummaryText = persistedFold.summaryText;
        }
    }
    const compactionMarkerId = lastCompactionIdx >= 0
        ? messages[lastCompactionIdx].id
        : (foldBoundaryIdx >= 0 ? persistedFold?.markerId : undefined);
    const foldActive = foldBoundaryIdx >= 0 && messages.length > MESSAGE_COMPACTION_THRESHOLD;
    const foldHiddenCount = boundaryIsChipRow ? foldBoundaryIdx : foldBoundaryIdx + 1;
    const [summaryExpanded, setSummaryExpanded] = useState(false);
    const [originalRevealed, setOriginalRevealed] = useState(false);
    // A fresh compaction re-folds the transcript and closes the prose body.
    useEffect(() => {
        setSummaryExpanded(false);
        setOriginalRevealed(false);
    }, [compactionMarkerId]);

    // ── Controllers: inbound WS/IPC dispatch + transcript persistence ──
    useWSMessageHandler();
    useSessionPersistence();

    // Emit Natt visibility to the extension host (drives critical-notif gating).
    useEffect(() => {
        vscode.postMessage({ type: 'NATT_VISIBILITY', open: nattOpen });
    }, [nattOpen]);

    useEffect(() => {
        // Instant while streaming — a smooth animation can't keep pace with a token
        // flood and visibly stutters; smooth is reserved for discrete new turns.
        messagesEndRef.current?.scrollIntoView({
            behavior: isStreaming ? 'auto' : 'smooth',
        });
    }, [messages, isStreaming]);

    // The Active Task Header's live status text: the same real narration
    // AgentTimeline rows already render (server_activity_event → typed
    // kind → timelineEntryLabel), sourced from the latest turn's own
    // timeline entries — never a fabricated status verb. Undefined before
    // the first marker arrives, so the header falls back to "Working…".
    const activeTaskStatusLabel = useMemo(() => {
        const last = messages[messages.length - 1];
        const entries = last && 'timeline' in last ? last.timeline : undefined;
        if (!entries || entries.length === 0) { return undefined; }
        const latest = entries.reduce((a, b) => (b.seq > a.seq ? b : a));
        return timelineEntryLabel(latest);
    }, [messages]);

    // Submit a turn under an explicit execution mode. The mode is passed in
    // rather than read from the `mode` state so callers that flip the mode in
    // the same handler (plan acceptance) submit under the NEW mode immediately,
    // instead of racing React's asynchronous state update (which would resubmit
    // under the stale mode and re-deny the writes).
    const submitWithMode = useCallback((
        text: string, executionMode: ExecutionMode, acceptedPlan = false,
    ) => {
        const presetConfig = getPresetConfig(preset);
        setMessages(prev => [...prev, { id: mkId(), role: 'user', content: text, authorLabel: authorLabelFor('user', nattName) }]);
        // Preserve the submitted prompt in the sticky Active Task Header. The
        // plan-accept hand-off submits a fixed agreement phrase, not a user
        // instruction, so substitute a readable label rather than surfacing it raw.
        const chat = useChatStore.getState();
        chat.setActiveTaskPrompt(text === AGREEMENT_SIGNAL ? 'Applying approved plan' : text);
        chat.setActiveTaskStartedAt(Date.now());
        // Wider than isStreaming (token delivery only) — covers the whole turn,
        // including node execution and an interrupt()/resume pause with no
        // tokens yet, so the Stop button and working indicator never go dark.
        chat.setIsTurnActive(true);
        const storeState = useWorkspaceStore.getState();
        // A cell that finishes without sending an explicit empty todo_write leaves
        // its last list on screen; a new turn always starts a fresh task, so clear
        // it here rather than waiting for a write that may never come.
        storeState.setAgentTodos(initial.sessionId, []);
        vscode.postMessage({
            type: 'SUBMIT_TASK',
            value: text,
            preset,
            tier,
            execution_mode: executionMode,
            ...presetConfig,
            session_id: initial.sessionId,
            // Persisted Native Thinking toggle, read at submit time so the
            // latest value (survives reload) is injected.
            enable_native_thinking: storeState.nativeThinking,
            // Plan mode → route the turn into the backend Socratic loop.
            planner_mode_active: isPlanMode(executionMode),
            // Persisted auto-accept toggle, read at submit time. The backend elides
            // the approval card for edits whose added lines trip no risk pattern.
            auto_accept_low_risk: storeState.autoAcceptLowRisk,
            // Explicit skill chip selected by the user (snake_case, undefined if none).
            invoked_skill_id: storeState.activeSkills?.[initial.sessionId]?.id ?? undefined,
            // The user approved the drafted plan: execute THAT plan rather than
            // re-drafting one they never saw. An explicit flag, never inferred from
            // the agreement phrase — that string coupling has broken before.
            accepted_plan: acceptedPlan,
        });
    }, [preset, tier, initial.sessionId]);

    const handleSubmit = useCallback((text: string) => {
        submitWithMode(text, mode);
    }, [submitWithMode, mode]);

    // A manual mode switch clears any plan left over from a prior turn so it can
    // never resurface as a stale acceptance panel (e.g. an Ask-turn plan popping
    // up the instant the user enters Plan mode). A plan re-appears only when a
    // fresh server_plan_document arrives while already in Plan mode.
    const handleModeChange = useCallback((next: ExecutionMode) => {
        setPlan(null);
        setMode(next);
    }, [setMode]);

    // Plan decision: accept and apply the plan with no further gating. The
    // agreement signal lets the backend's ideation loop synthesize and hand off
    // to the coder; submitting under Auto lifts the read-only plan gate so the
    // patches actually land on disk. Clearing the plan collapses the split panel
    // back to the chat composer as the agreement turn streams.
    const handlePlanAutoAccept = useCallback(() => {
        submitWithMode(AGREEMENT_SIGNAL, 'automatic', true);
        setMode('automatic');
        setPlan(null);
    }, [submitWithMode, setMode]);

    // Plan decision: accept the plan but route every file write through the
    // HITL approval card (Ask). Same agreement hand-off, stricter write gate.
    const handlePlanManualApprove = useCallback(() => {
        submitWithMode(AGREEMENT_SIGNAL, 'ask_before_edits', true);
        setMode('ask_before_edits');
        setPlan(null);
    }, [submitWithMode, setMode]);

    // Plan decision: reject the current plan and keep refining. With feedback,
    // it is a normal Socratic turn — stay in Plan mode so the analyst keeps the
    // read-only stance and the questioning loop continues. With no feedback, just
    // dismiss the acceptance panel back to the composer so the user can type the
    // next instruction (the plan panel must never trap the input).
    const handlePlanKeepPlanning = useCallback((feedback: string) => {
        const trimmed = feedback.trim();
        if (trimmed) {
            submitWithMode(trimmed, PLAN_MODE);
        }
        setPlan(null);
    }, [submitWithMode]);

    // Plan decision: dismiss the panel without submitting anything or changing
    // mode. Deliberately never gated on isTurnActive — the backend may still be
    // narrating the planner call itself, but the panel must always be closeable;
    // it does not call handleAbort, so a still-running turn keeps running behind
    // the composer rather than being force-stopped by a simple dismiss.
    const handlePlanClose = useCallback(() => {
        setPlan(null);
    }, []);

    // Phase 7.11.3 (ADR-706 §4.5b) — Abort Controller Mesh.
    // ABORT_TASK keeps the client-side HTTP AbortController (legacy, harmless).
    // ABORT_MESH is the new path: workspace_panel.ts turns it into a
    // `client_abort_mesh` WS frame the backend resolves to Task.cancel().
    // The `isAborting` guard makes the second click idempotent.
    const handleAbort = useCallback(() => {
        if (isAborting) { return; }
        setIsAborting(true);
        vscode.postMessage({ type: 'ABORT_TASK' });
        vscode.postMessage({ type: 'ABORT_MESH' });
    }, [isAborting, setIsAborting]);

    // Interactive terminal: send a line of stdin to the live cell session and echo
    // it locally (the cell PTY runs echo-off, so the user otherwise sees nothing).
    const handleCellStdin = useCallback((iteration: number, line: string) => {
        vscode.postMessage({ type: 'PTY_STDIN', session_id: initial.sessionId, data: line + '\n' });
        setMessages(prev => {
            const withCell = attachOrUpdateCellRun(prev, iteration, it => appendPtyLines(it, [`› ${line}`]), nattName);
            const merged = readMergedCellIteration(withCell, iteration);
            if (!merged) { return withCell; }
            return attachOrUpdateTimeline(withCell, ts => upsertCellBody(ts ?? [], `cell:${iteration}`, merged), nattName);
        });
    }, [initial.sessionId, nattName]);

    // Phase 7.11.6 — Retry button on a Rich Tool Chip. The backend looks up
    // the historical ToolCallSpec and re-invokes verbatim; a new chip lands
    // alongside the old one (the original stays as record).
    const handleRetryTool = useCallback((toolCallId: string) => {
        vscode.postMessage({ type: 'RETRY_TOOL', tool_call_id: toolCallId });
    }, []);

    const handleDreamingToggle = useCallback((next: boolean, p: DreamingProfile) => {
        setDreamingActive(next);
        setDreamingProfile(p);
    }, []);

    const handleModelPrefChange = useCallback((id: string, m: OrchestrationMode) => {
        setActiveModelId(id);
        setOrchestrationMode(m);
        vscode.postMessage({ type: 'SET_MODEL_PREFERENCE', activeModelId: id, orchestrationMode: m });
    }, []);

    const handleNattSubmit = useCallback((text: string) => {
        setNattMessages(prev => [...prev, { id: mkId(), role: 'user', content: text }]);
        const contextPaths = nattAttachedItems.map(i => i.path);
        vscode.postMessage({
            type: 'NATT_MESSAGE',
            text,
            session_id: initial.sessionId,
            model_tier: useWorkspaceStore.getState().analystTier,
            enable_native_thinking: useWorkspaceStore.getState().nativeThinking,
            ...(contextPaths.length > 0 && { context_paths: contextPaths }),
        });
        setNattAttachedItems([]);
    }, [initial.sessionId, nattAttachedItems]);

    const handleResolveHitl = useCallback((_id: string) => {
        setHitlPending(undefined);
    }, []);

    // ADR-724 — shared responder for the inline per-diff HITL row. Bound to the
    // currently-pending approval; the resolved-guard is reset whenever a new
    // approval arrives so a later request isn't swallowed by the previous one's
    // latched ref. Resolving here clears `hitlPending`, which simultaneously
    // tears down the Natt-pane card — one approval_id, one decision, no double-post.
    const hitlActiveApprovalId = hitlPending?.approval_id ?? '';
    const { respond: respondInlineHitl, resolvedRef: inlineHitlResolved } =
        useHitlResponder(hitlActiveApprovalId, handleResolveHitl);
    useEffect(() => {
        inlineHitlResolved.current = false;
    }, [hitlActiveApprovalId, inlineHitlResolved]);

    // "Request changes" — decline the pending edit AND re-submit the note as a
    // fresh turn so the agent re-proposes against the feedback. The reject carries
    // the comment so the backend acknowledges the hand-off ("Revising…") rather
    // than declaring the work discarded; the re-submit rides the same session id,
    // so the checkpointed thread carries prior context into the new proposal.
    const handleRequestChanges = useCallback((feedback: string) => {
        respondInlineHitl(false, { comment: feedback });
        submitWithMode(feedback, mode);
    }, [respondInlineHitl, submitWithMode, mode]);

    const handleBudgetChange = useCallback((
        mode: BudgetLimitMode, weeklyUsd: number, monthlyUsd: number,
    ) => {
        setBudgetLimitMode(mode);
        setBudgetWeeklyUsd(weeklyUsd);
        setBudgetMonthlyUsd(monthlyUsd);
        vscode.postMessage({ type: 'SET_BUDGET_LIMIT', mode, weeklyUsd, monthlyUsd });
    }, []);

    const rootAttrs: Record<string, string> = {
        'data-dreaming': dreamingActive ? 'true' : 'false',
        'data-tier':     tier,
        'data-alert':    telemetry?.is_red_alert ? 'true' : 'false',
        'data-hitl':     hitlPending ? 'true' : 'false',
        'data-natt':     nattOpen ? 'true' : 'false',
    };

    // A FILE_WRITE approval renders inline as the diff + action row in the main
    // chat (its blocks carry patch_id === approval_id). A diff-less approval
    // (e.g. budget overflow, degraded-sandbox exec) has no inline surface, so it
    // falls back to the authorization card — also in the main column, never the
    // analyst pane.
    const hitlHasDiff = !!hitlPending && messages.some(
        m => m.role !== 'system' && ((m as ConversationMessage).diffBlocks ?? []).some((db: DiffBlockShape) => db.patch_id === hitlPending.approval_id),
    );

    const wsLabel =
        wsStatus === 'connected' ? 'AILIENANT Core · Connected' :
        wsStatus === 'reconnecting' ? 'AILIENANT Core · Reconnecting…' :
        'AILIENANT Core · Offline';
    const wsTip =
        wsStatus === 'connected' ? 'Backend WebSocket online. Streaming, telemetry, and HITL are active.' :
        wsStatus === 'reconnecting' ? 'Reconnecting to the AILIENANT backend. Streaming is paused.' :
        'Backend WebSocket unreachable. Start the core (uvicorn main:app) on the configured port.';

    return (
        <Tooltip.Provider delayDuration={400} skipDelayDuration={150}>
            <div className="ws-root" {...rootAttrs}>
                <WorkspaceHeader
                    sessionTitle={initial.sessionTitle}
                    nattName={nattName}
                    nattOpen={nattOpen}
                    onToggleNatt={() => setNattOpen(!nattOpen)}
                    logoUri={initial.logoUri}
                />

                {/* Phase 7.11.8 (ADR-706 §4.5g) — Time-Travel picker overlay.
                    Mounted only when the host hands us a checkpoint list
                    (CHECKPOINTS_LIST); the host fires the REST fetch when
                    the user picks `/context rewind` in the CommandPalette. */}
                {checkpointPicker !== null && (
                    <div className="ws-checkpoint-picker-overlay"
                         onClick={(e) => {
                             if (e.target === e.currentTarget) {
                                 setCheckpointPicker(null);
                             }
                         }}>
                        <CheckpointPicker
                            entries={checkpointPicker}
                            onCancel={() => setCheckpointPicker(null)}
                            onPick={(entry) => {
                                vscode.postMessage({
                                    type: 'BRANCH_FROM_CHECKPOINT',
                                    session_id: initial.sessionId,
                                    checkpoint_id: entry.checkpoint_id,
                                });
                                setCheckpointPicker(null);
                            }}
                        />
                    </div>
                )}

                {/* Status strip */}
                <div className="ws-status">
                    <Popover.Root open={coreMenuOpen} onOpenChange={setCoreMenuOpen} modal={false}>
                        <AiTooltip content={wsTip}>
                            <Popover.Trigger asChild>
                                <button className="ws-status-pill ws-status-pill--btn" aria-label="Backend options">
                                    <Icon name="network" size={12} />
                                    <span className="ws-status-dot" data-status={wsStatus} />
                                    <span>{wsLabel}</span>
                                </button>
                            </Popover.Trigger>
                        </AiTooltip>
                        <Popover.Portal>
                            <Popover.Content
                                className="ws-core-menu"
                                side="bottom"
                                align="center"
                                sideOffset={6}
                                collisionPadding={8}
                            >
                                <div className="ws-core-menu-head">AILIENANT Core</div>
                                <div className="ws-core-menu-actions">
                                    <button
                                        className="ws-core-menu-btn"
                                        onClick={() => {
                                            vscode.postMessage({ type: 'RESTART_BACKEND' });
                                            setCoreMenuOpen(false);
                                        }}
                                    >
                                        <Icon name="play" size={12} />
                                        Restart Core
                                    </button>
                                    <button
                                        className="ws-core-menu-btn"
                                        onClick={() => {
                                            vscode.postMessage({ type: 'OPEN_DASHBOARD' });
                                            setCoreMenuOpen(false);
                                        }}
                                    >
                                        <Icon name="external-link" size={12} />
                                        Open Dashboard
                                    </button>
                                </div>
                            </Popover.Content>
                        </Popover.Portal>
                    </Popover.Root>
                    {/* Workspace folder pill */}
                    {workspaceFolder ? (
                        <div className="ws-status-pill">
                            <Icon name="folder" size={12} />
                            <span>{workspaceFolder}</span>
                        </div>
                    ) : (
                        <Popover.Root modal={false}>
                            <Popover.Trigger asChild>
                                <button className="ws-status-pill ws-status-pill--btn ws-status-pill--warn" aria-label="Workspace options">
                                    <Icon name="folder" size={12} />
                                    <span>Awaiting Workspace</span>
                                </button>
                            </Popover.Trigger>
                            <Popover.Portal>
                                <Popover.Content className="ws-core-menu" side="bottom" align="center" sideOffset={6} collisionPadding={8}>
                                    <div className="ws-core-menu-head">Workspace Folder</div>
                                    <p className="ws-core-menu-desc">Open a folder so AILIENANT knows which project to work on.</p>
                                    <div className="ws-core-menu-actions">
                                        <button
                                            className="ws-core-menu-btn"
                                            onClick={() => vscode.postMessage({ type: 'OPEN_WORKSPACE' })}
                                        >
                                            <Icon name="folder" size={12} />
                                            Open Folder…
                                        </button>
                                    </div>
                                </Popover.Content>
                            </Popover.Portal>
                        </Popover.Root>
                    )}
                    <IndexingStatus state={indexing} />
                </div>

                {/* Main split-grid */}
                <main className={`ws-main${plan && isPlanMode(mode) ? ' plan-mode-active' : ''}`}>
                    {/* LEFT: chat + bar */}
                    <section className="ws-main-left">
                        <CSSAlertBanner telemetry={telemetry} />

                        {/* Sticky prompt-preservation header — pinned above the
                            scrolling transcript so the active request is never lost
                            (DEBT-058). Expanded while streaming, collapsed on settle,
                            dismissible; a new submit replaces it. */}
                        {activeTaskPrompt !== undefined && activeTaskStartedAt !== undefined && (
                            <ActiveTaskHeader
                                prompt={activeTaskPrompt}
                                startedAt={activeTaskStartedAt}
                                isTurnActive={isTurnActive}
                                statusLabel={activeTaskStatusLabel}
                                onCancel={handleAbort}
                                onDismiss={() => setActiveTaskPrompt(undefined)}
                            />
                        )}

                        <div className="ws-messages">
                            {messages.length === 0 && (
                                <div className="ws-empty">
                                    <Icon name="sparkles" size={24} color="var(--accent-primary)" />
                                    <div>
                                        AILIENANT is ready.<br />
                                        Type a task or press <strong>/</strong> for the command palette.
                                    </div>
                                </div>
                            )}
                            {/* Compaction fold (DEBT-059) — once the transcript is long and the
                                backend has compacted the context, the pre-compaction bubbles
                                collapse behind the SessionSummaryCard. When the user reveals the
                                originals, this slim bar sits above the restored transcript. The
                                fold is non-destructive: the bubbles stay in the store. */}
                            {foldActive && originalRevealed && (
                                <SessionSummaryCard
                                    mode="revealed"
                                    hiddenCount={foldHiddenCount}
                                    summaryText=""
                                    expanded={false}
                                    onToggle={() => { /* n/a in revealed mode */ }}
                                    onRevealOriginal={() => { /* n/a in revealed mode */ }}
                                    onHideOriginal={() => setOriginalRevealed(false)}
                                />
                            )}
                            {(() => {
                                return messages.map((m, i) => {
                                    // Fold: hide the pre-compaction bubbles behind the card and
                                    // render the card once at the boundary. Indices stay intact
                                    // (we return null rather than slicing) so message_index and
                                    // reasoning toggles below keep pointing at the right turn.
                                    if (foldActive && !originalRevealed) {
                                        if (i < foldBoundaryIdx) { return null; }
                                        if (i === foldBoundaryIdx) {
                                            // A live chip carries its own summaryText; the persisted-fold
                                            // fallback (boundaryIsChipRow=false, no chip row survived
                                            // reload) reads it from workspaceStore instead — and in that
                                            // case `m` is a real bubble that must NOT also render below.
                                            const meta = boundaryIsChipRow ? (m as SystemMessage).compaction : undefined;
                                            const summaryText = boundaryIsChipRow ? (meta?.summaryText ?? '') : (fallbackSummaryText ?? '');
                                            return (
                                                <SessionSummaryCard
                                                    key={m.id ?? 'compaction-fold'}
                                                    mode="folded"
                                                    hiddenCount={foldHiddenCount}
                                                    summaryText={summaryText}
                                                    expanded={summaryExpanded}
                                                    onToggle={() => setSummaryExpanded(v => !v)}
                                                    onRevealOriginal={() => setOriginalRevealed(true)}
                                                    onHideOriginal={() => setOriginalRevealed(false)}
                                                />
                                            );
                                        }
                                    }
                                    // System notification chips (state_compacted) bypass the
                                    // full row structure — they render a plain string and have
                                    // no role header, tool chips, token footer, or ErrorBoundary.
                                    if (m.role === 'system') {
                                        return (
                                            <div key={m.id ?? `row-${i}`} className="ws-system-chip" role="status">
                                                {m.content}
                                            </div>
                                        );
                                    }
                                    return (
                                <ErrorBoundary
                                    key={m.id ?? `row-${i}`}
                                    label="message-row"
                                    resetKeys={[m.id, m.content, m.streaming]}
                                    fallback={<MessageRowFallback />}
                                >
                                    {/* Glass-Box Timeline — the living spine unifying phase
                                        narration, reasoning, the plan checklist, diffs, and
                                        the agentic-cell audit into one chronological,
                                        seq-ordered trace. */}
                                    {m.role === 'assistant' && ((m.timeline && m.timeline.length > 0) || (m.checklist && m.checklist.length > 0)) && (
                                        <AgentTimeline
                                            entries={m.timeline ?? []}
                                            streaming={!!m.streaming}
                                            isLatestTurn={i === messages.length - 1}
                                            thinking={m.thinking}
                                            thinkingTokens={m.thinkingTokens}
                                            thinkingStartedAt={m.thinkingStartedAt}
                                            thinkingElapsedMs={m.thinkingElapsedMs}
                                            thinkingOpen={m.thinkingOpen}
                                            thinkingSource={m.thinkingSource}
                                            checklist={m.checklist}
                                            hitlApprovalId={hitlPending?.approval_id}
                                            onRespondDiff={respondInlineHitl}
                                            onRequestChangesDiff={handleRequestChanges}
                                            onCellStdin={handleCellStdin}
                                            turnElapsedMs={m.turnElapsedMs}
                                        />
                                    )}
                                    {/* Ghost Telemetry (ADR-723) — live action-log: the
                                        in-flight tool invocations, shown only while the turn
                                        streams. On stream-end the ToolChip stack below is the
                                        canonical record, so this while-you-wait view drops out. */}
                                    {m.role === 'assistant' && m.streaming && m.toolCalls && m.toolCalls.length > 0 && (
                                        <ActionLog toolCalls={m.toolCalls} />
                                    )}
                                    {(m.role === 'user' || m.content) && (
                                        <div
                                            className="ws-msg"
                                            data-role={m.role}
                                            data-streaming={m.streaming ? 'true' : 'false'}
                                        >
                                            <div className="ws-msg-role">
                                                {m.authorLabel ?? (m.role === 'user' ? 'You' : 'AILIENANT')}
                                            </div>
                                            <div className="ws-msg-content">
                                                {m.role === 'assistant' ? (
                                                    // Anti-flicker streaming markdown — parser state is cleared on stream end.
                                                    <MarkdownRenderer
                                                        content={m.content}
                                                        parserState={m.parserState}
                                                        streaming={!!m.streaming}
                                                        codeTokens={m.codeTokens}
                                                        streamingCodeTokens={m.streamingCodeTokens}
                                                    />
                                                ) : (
                                                    m.content
                                                )}
                                            </div>
                                        </div>
                                    )}
                                    {/* Ghost Telemetry (ADR-723) — per-message token footer.
                                        Ticks live while streaming (answer tokens tallied
                                        client-side + reasoning tokens) and freezes into a quiet
                                        per-turn total on stream-end. Distinct from the global
                                        FinOps HUD cost; zero-token turns render nothing. */}
                                    {m.role === 'assistant' && (() => {
                                        const total = (m.liveTokens ?? 0) + (m.thinkingTokens ?? 0);
                                        if (total === 0) { return null; }
                                        return (
                                            <div
                                                className="ws-turn-footer"
                                                data-streaming={m.streaming ? 'true' : 'false'}
                                                aria-live="off"
                                                aria-atomic="true"
                                            >
                                                {m.streaming
                                                    ? `Thinking… ${total} ${total === 1 ? 'token' : 'tokens'}`
                                                    : `${total} ${total === 1 ? 'token' : 'tokens'}`}
                                            </div>
                                        );
                                    })()}
                                    {/* Phase 7.11.6 — Rich Tool Chips attached to this turn. */}
                                    {m.role === 'assistant' && m.toolCalls && m.toolCalls.length > 0 && (
                                        <div className="ws-tool-chip-stack">
                                            {m.toolCalls.map(tc => (
                                                <ToolChip
                                                    key={tc.tool_call_id}
                                                    tc={tc}
                                                    onRetry={handleRetryTool}
                                                />
                                            ))}
                                        </div>
                                    )}
                                    {/* Diffs now render as 'diff' rows inside AgentTimeline above
                                        (same hitlActive/onRespond/onRequestChanges wiring, relocated).
                                        The companion stack (13.0.7) is no longer gated on diffBlocks —
                                        a Plan-mode or Ask-mode turn produces no diff but can still carry
                                        its own decision-point explanations. */}
                                    {m.role === 'assistant' && (
                                        <CoderCompanionCard entries={m.companions} turnActive={!!m.streaming} />
                                    )}
                                    {/* Phase 7.11.8 (ADR-706 §4.5g) — per-message
                                        Time-Travel branch button. Only rendered
                                        on completed assistant turns that carry
                                        an L2-promoted checkpoint_id. */}
                                    {m.role === 'assistant' && !m.streaming && m.checkpoint_id && (
                                        <MessageActions
                                            checkpoint_id={m.checkpoint_id}
                                            session_id={initial.sessionId}
                                            message_index={i}
                                            is_abort_savepoint={m.is_abort_savepoint}
                                            post={vscode.postMessage.bind(vscode)}
                                        />
                                    )}
                                </ErrorBoundary>
                                    );
                                });
                            })()}
                            {/* Diff-less approval (budget / degraded exec) — the
                                authorization card lives in the main chat, not the
                                analyst pane. FILE_WRITE uses the inline diff row above. */}
                            {hitlPending && !hitlHasDiff && (
                                hitlPending.request_kind === BRIEF_REVIEW_KIND
                                    ? (
                                        // The last step of the grill: the distilled brief,
                                        // shown before it becomes the planner's input.
                                        <BriefReviewCard
                                            intervention={hitlPending}
                                            onResolved={handleResolveHitl}
                                        />
                                    )
                                    : (hitlPending.questions?.length || hitlPending.question)
                                        ? (
                                            <ClarificationGrillCard
                                                intervention={hitlPending}
                                                nattName={nattName}
                                                onResolved={handleResolveHitl}
                                            />
                                        )
                                        : (
                                            <HITLInterventionCard
                                                intervention={hitlPending}
                                                nattName={nattName}
                                                onResolved={handleResolveHitl}
                                            />
                                        )
                            )}
                            <div ref={messagesEndRef} />
                        </div>

                        {/* Attached context chips */}
                        <AgentTodoPanel sessionId={initial.sessionId} />

                        {attachedItems.length > 0 && (
                            <div className="ws-attached-bar">
                                {attachedItems.map(item => {
                                    const label = item.path.split(/[/\\]/).pop() ?? item.path;
                                    return (
                                        <div key={item.id} className="ws-attached-chip" title={item.path}>
                                            <Icon name={item.kind === 'directory' ? 'folder' : 'file'} size={11} />
                                            <span>{label}</span>
                                            <button
                                                className="ws-attached-chip-remove"
                                                aria-label={`Remove ${label}`}
                                                onClick={() => setAttachedItems(prev => prev.filter(i => i.id !== item.id))}
                                            >
                                                <Icon name="x" size={10} />
                                            </button>
                                        </div>
                                    );
                                })}
                            </div>
                        )}

                        {/* Composer + Telemetry sibling cards (matches manifest §7.2).
                            ONE composer in every mode — the HUD mode pill is the only
                            signal of Plan mode. The plan's "Accept" affordance lives on
                            the plan card, not on the input bar (Cursor/Claude-Code shape). */}
                        <div className="ws-bottom">
                            <PromptBar
                                disabled={Boolean(hitlPending)}
                                placeholder={
                                    hitlPending
                                        ? `${nattName} is waiting for your decision`
                                        : isPlanMode(mode)
                                            ? `Describe what you want to build — ${nattName} will grill you into a plan…`
                                            : undefined
                                }
                                activeTaskId={activeTaskId}
                                isAborting={isAborting}
                                config={config}
                                mode={mode}
                                preset={preset}
                                onModeChange={handleModeChange}
                                onPresetChange={setPreset}
                                dreamingActive={dreamingActive}
                                dreamingProfile={dreamingProfile}
                                onDreamingToggle={handleDreamingToggle}
                                activeModelId={activeModelId}
                                orchestrationMode={orchestrationMode}
                                onModelPrefChange={handleModelPrefChange}
                                sessionId={initial.sessionId}
                                onSubmit={handleSubmit}
                                onAbort={handleAbort}
                            />
                            <TelemetryHUD
                                occStatus={occStatus}
                                lockedFiles={lockedFiles}
                                tps={tps}
                                snapshot={snapshot}
                                budgetUsd={budgetUsd}
                                telemetry={telemetry}
                                budgetLimitMode={budgetLimitMode}
                                budgetWeeklyUsd={budgetWeeklyUsd}
                                budgetMonthlyUsd={budgetMonthlyUsd}
                                onBudgetChange={handleBudgetChange}
                            />
                        </div>
                    </section>

                    {/* RIGHT: Natt pane */}
                    {nattOpen && (
                        <NattCanvas
                            nattName={nattName}
                            messages={nattMessages}
                            disabled={Boolean(hitlPending)}
                            nattAttachedItems={nattAttachedItems}
                            onNattRemoveAttached={(id) => setNattAttachedItems(prev => prev.filter(i => i.id !== id))}
                            onClose={() => setNattOpen(false)}
                            onSendMessage={handleNattSubmit}
                            onToggleReasoning={(idx) => setNattMessages(prev => prev.map((mm, j) =>
                                j === idx ? { ...mm, thinkingOpen: !mm.thinkingOpen } : mm))}
                        />
                    )}

                    {/* RIGHT: dedicated plan-acceptance surface. In Plan mode the
                        plan opens as a split panel carrying the three-way decision
                        (auto-accept / manual-approve / keep-planning); the chat
                        composer folds away so the acceptance controls own the input. */}
                    {plan && isPlanMode(mode) && (
                        <aside className="plan-panel-container">
                            <PlanAcceptancePanel
                                plan={plan}
                                onAutoAccept={handlePlanAutoAccept}
                                onManualApprove={handlePlanManualApprove}
                                onKeepPlanning={handlePlanKeepPlanning}
                                onClose={handlePlanClose}
                                isTurnActive={isTurnActive}
                            />
                        </aside>
                    )}
                </main>

                {/* Toasts */}
                <ToastStack toasts={toasts} />
            </div>
        </Tooltip.Provider>
    );
}
