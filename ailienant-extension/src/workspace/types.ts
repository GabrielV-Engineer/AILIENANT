/**
 * Shared webview-panel types for the workspace chat surface.
 *
 * Co-located here (rather than in `Workspace.tsx`) so the dispatch controller,
 * the chat store, and the persistence hook can all reference the message shapes
 * without importing back from the component module — keeping the runtime
 * dependency graph acyclic. The `*Shape` artifact types stay in `shared/config`.
 */
import type {
    ASTToken, ToolCallShape, CellRunShape, PlanWBSStep, DiffBlockShape,
    BudgetLimitMode, OrchestrationMode,
} from '../shared/config';
import type { AilienantConfig } from '../shared/types';
import type { ParserState as MdParserState } from './utils/StreamingMarkdownParser';

/** Rich conversation turn (user or assistant). All fields beyond the three required
 *  ones are optional — most are ephemeral display data excluded from PERSIST_TRANSCRIPT. */
export interface ConversationMessage {
    // Stable per-turn id (client-minted via crypto.randomUUID at creation). Keys the
    // REHYDRATE_TRANSCRIPT merge so a tab re-reveal never clobbers a live in-flight turn.
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    streaming?: boolean;
    steps?: string[];      // pipeline node trace for this assistant turn
    stepsDone?: boolean;   // true after server_stream_end → ✓ + auto-collapse
    // Incremental markdown parser state — live only while streaming; cleared on
    // server_stream_end so the renderer's stable fast path takes over. Transient —
    // explicitly stripped before PERSIST_TRANSCRIPT.
    parserState?: MdParserState;
    // Host-tokenized syntax spans keyed by hashCodeBlock(lang, code). Ephemeral.
    codeTokens?: Record<string, ASTToken[][]>;
    // Per-line streaming AST overlay; superseded by codeTokens on stream-end. Ephemeral.
    streamingCodeTokens?: Record<number, ASTToken[][]>;
    // Tool-chip artifacts built incrementally from server_tool_* events.
    toolCalls?: ToolCallShape[];
    // Glass-box agentic-cell telemetry. Display-only; stripped before PERSIST_TRANSCRIPT.
    cellRun?: CellRunShape;
    // Progressive WBS checklist — durable audit evidence, carried through PERSIST_TRANSCRIPT.
    checklist?: PlanWBSStep[];
    // Inline diff blocks surfaced by the host after PatchActuator applies. Persisted.
    diffBlocks?: DiffBlockShape[];
    // L2-promoted checkpoint wrapping this completed assistant turn. Persisted.
    checkpoint_id?: string;
    // Flips the branch button's icon to ⏹ for user_abort emergency savepoints.
    is_abort_savepoint?: boolean;
    // Frozen at ingestion; the row component is pure and never reads reactive config.
    authorLabel?: string;
    // Native Thinking (ADR-707) — raw reasoning. Display-only; NEVER persisted.
    thinking?: string;
    thinkingTokens?: number;
    thinkingStartedAt?: number;
    thinkingElapsedMs?: number;
    thinkingOpen?: boolean;
    // Ghost Telemetry (ADR-723) — answer tokens tallied client-side. Persisted.
    liveTokens?: number;
}

/** Transient system notification chip — rendered in-transcript but never persisted.
 *  `streaming` and `toolCalls` are typed `never` (structurally absent) so the union
 *  satisfies the `normalizeStuckChips` / `mergeById` generic constraints without
 *  making these fields accidentally accessible at their sites. */
export interface SystemMessage {
    id?: string;
    role: 'system';
    content: string;
    readonly streaming?: never;
    readonly toolCalls?: never;
}

/** Discriminated union over `role`. System chips are filtered from PERSIST_TRANSCRIPT. */
export type Message = ConversationMessage | SystemMessage;

export interface NattMessage {
    id?: string;   // see Message.id (REHYDRATE_TRANSCRIPT merge key).
    role: 'natt' | 'user';
    content: string;
    streaming?: boolean;
    // Same parser state, applied to analyst-canvas turns.
    parserState?: MdParserState;
}

/** A file/folder added to the turn context via the workspace picker. */
export interface AttachedItem { id: string; path: string; kind: 'file' | 'directory'; }

/** Toast notification severity + payload. */
export type ToastLevel = 'info' | 'warn' | 'error';
export interface ToastItem { id: number; level: ToastLevel; message: string; }

/** Creation-time snapshot handed to the panel by the extension host. */
export interface InitialState {
    sessionId:        string;
    sessionTitle:     string;
    config:           AilienantConfig | null;
    logoUri:          string;
    budgetLimitMode:  BudgetLimitMode;
    budgetWeeklyUsd:  number;
    budgetMonthlyUsd: number;
    activeModelId:    string;
    orchestrationMode: OrchestrationMode;
    workspaceFolder:  string;
    initialMessages?:     Message[];      // restored chat transcript
    initialNattMessages?: NattMessage[];  // restored analyst transcript
}
