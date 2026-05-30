# ailienant-core/brain/state.py

import operator
from typing import Any, TypedDict, List, Dict, Optional, Annotated, Literal
from pydantic import BaseModel, Field, model_validator

from shared.hardware import HardwareProfile  # noqa: E402 — imported for type annotation

# =====================================================================
# 1. MODELOS DE DATOS (Contratos de Validación Estricta en Tiempo de Ejecución)
# =====================================================================
# Utilizamos Pydantic para aplicar el Fail-Fast Principle. Si el LLM (Planner)
# alucina la estructura del JSON, el sistema fallará y reintentará inmediatamente,
# evitando propagar datos corruptos al Orchestrator o al CoderAgent.


# Phase 4.1.4 — Legacy → new role-name migration (blueprint §3.1).
# Maps the deprecated 5-value vocabulary onto the canonical 8-value vocabulary.
# Consumed by WBSStep.__migrate_legacy_target_role__ as a before-validator so
# every stored target_role is always one of the 8 NEW canonical names. The legacy
# 5 values + this map will be removed one release after Phase 4 closes (logged
# in PROJECT_MANIFEST.md Tech Debt section).
_LEGACY_TO_NEW_ROLE: Dict[str, str] = {
    "Refactor": "architect_refactor",
    "Infra": "devops_infra",
    "Doc": "doc_manager",
    "SecOps": "secops",
    "Test": "qa_tester",
}

# Phase 7.12 — canonical post-migration role vocabulary. An out-of-vocabulary
# target_role string hallucinated by the Planner is coerced to this default rather
# than raising a ValidationError (which would burn a planner retry).
_CANONICAL_ROLES: frozenset[str] = frozenset({
    "core_dev", "architect_refactor", "devops_infra", "secops",
    "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer",
})
_DEFAULT_ROLE = "core_dev"


def _coerce_to_str(item: Any) -> str:
    """Phase 7.12 — flatten a hallucinated list element into a plain string.

    The Planner LLM intermittently emits objects (``{"file": "x", "reason": "y"}``)
    where the MissionSpecification contract requires a ``str``. Rather than fail
    validation, flatten: dicts become ``"key: value"`` pairs; everything else is
    stringified. Already-strings pass through untouched.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return "; ".join(f"{k}: {v}" for k, v in item.items())
    return str(item)


def _coerce_str_list(value: Any) -> Any:
    """Phase 7.12 — normalise a value destined for a ``List[str]`` field.

    Maps each element through :func:`_coerce_to_str`. A bare scalar/dict (LLM
    emitted a single value instead of a list) is wrapped into a one-element list.
    Non-coercible input is returned unchanged so Pydantic still fails loudly.
    """
    if isinstance(value, list):
        return [_coerce_to_str(el) for el in value]
    if isinstance(value, (str, dict)):
        return [_coerce_to_str(value)]
    return value


class WBSStep(BaseModel):
    """
    Un paso individual, atómico y ejecutable de la misión.
    Refactor (Fase 4): Integra su propio 'status' y reemplaza agentes por roles dinámicos.

    Phase 4.1.4 widening: target_role Literal accepts both legacy (5) and new (8)
    values. A model_validator(mode="before") normalises legacy strings to new
    canonical names at construction, so the stored value is always one of the 8 NEW.
    """

    step_number: int = Field(
        description="El orden secuencial de ejecución (1, 2, 3...)."
    )
    target_role: Literal[
        # Legacy 5 (deprecated, auto-migrated to the new vocabulary):
        "Refactor", "Infra", "Doc", "SecOps", "Test",
        # New 8 (canonical Phase 4 vocabulary):
        "core_dev", "architect_refactor", "devops_infra", "secops",
        "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer",
    ] = Field(
        default="core_dev",
        description="The RBAC role the CoderAgent assumes for this step (Phase 4.1.4).",
    )
    action: Literal["read_file", "write_file", "edit_file", "run_command"] = Field(
        description="Tipo de acción estricta permitida para este paso."
    )
    target_file: str = Field(
        description="Ruta exacta del archivo afectado (ej. 'src/routes/auth.py') o comando a ejecutar."
    )
    description: str = Field(
        description="Instrucción detallada de lo que el CoderAgent debe hacer en este paso."
    )
    status: Literal["pending", "in_progress", "completed", "failed"] = Field(
        default="pending",
        description="Estado actual de la tarea. El Orchestrator muta esto durante la ejecución.",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_target_role(cls, data: Any) -> Any:
        """Map legacy 5-value role names to new 8-value canonical names.

        Runs BEFORE Pydantic field validation so the Literal narrows cleanly to
        the post-migration set. Idempotent: already-new values pass through
        unchanged. Tolerates non-dict input (Pydantic internal construction).
        """
        if isinstance(data, dict):
            legacy = data.get("target_role")
            if isinstance(legacy, str) and legacy in _LEGACY_TO_NEW_ROLE:
                data["target_role"] = _LEGACY_TO_NEW_ROLE[legacy]
            # Phase 7.12 — coerce a hallucinated, out-of-vocabulary role to the
            # safe default instead of raising a Literal ValidationError.
            role = data.get("target_role")
            if isinstance(role, str) and role not in _CANONICAL_ROLES:
                data["target_role"] = _DEFAULT_ROLE
        return data


class MissionSpecification(BaseModel):
    """
    EL MACRO-CONTRATO (Spec-Driven Development).
    Forza al PlannerAgent a definir la arquitectura completa antes de escribir una línea de código.
    """

    outcome: str = Field(
        description="El resultado final esperado y el valor aportado por esta misión."
    )
    scope: List[str] = Field(
        description="Definición estricta de lo que está DENTRO y FUERA del alcance. Qué archivos tocar y cuáles NO."
    )
    constraints: List[str] = Field(
        description="Limitaciones técnicas (ej. sin librerías externas, complejidad O(n), convenciones del proyecto)."
    )
    decisions: List[str] = Field(
        description="Decisiones de diseño o arquitectura adoptadas para resolver este problema en particular."
    )
    tasks: List[WBSStep] = Field(
        description="Work Breakdown Structure (WBS). La lista secuencial y estricta de pasos a ejecutar."
    )
    checks: List[str] = Field(
        description="Criterios de aceptación técnicos. ¿Cómo sabrá el micro-enjambre de Testing que la tarea fue un éxito?"
    )
    # Phase 2.21 — DDD Ubiquitous Language + SDD + TDD (optional; defaults preserve backward compat)
    ubiquitous_language: Dict[str, str] = Field(
        default_factory=dict,
        description="DDD: domain terms → definitions extracted from the Socratic session.",
    )
    deep_modules_sdd: Optional[str] = Field(
        default=None,
        description="Architectural SDD for core modules identified during ideation.",
    )
    tdd_criteria: List[str] = Field(
        default_factory=list,
        description="TDD acceptance criteria generated from the Socratic Q&A session.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_hallucinated_str_lists(cls, data: Any) -> Any:
        """Phase 7.12 — coerce dict/scalar hallucinations in the ``List[str]`` fields.

        The contract (immutable per SCHEMA_EVOLUTION.MD) is unchanged: these fields
        remain ``List[str]``. This before-validator only normalises malformed LLM
        output (objects where strings belong) so a structurally-valid plan no longer
        burns a retry. Mirrors WBSStep._migrate_legacy_target_role. Idempotent and
        tolerant of non-dict input (Pydantic internal construction).
        """
        if isinstance(data, dict):
            for _field in ("scope", "constraints", "decisions", "checks", "tdd_criteria"):
                if _field in data and data[_field] is not None:
                    data[_field] = _coerce_str_list(data[_field])
        return data


class ContextMeter(BaseModel):
    """Telemetría para el motor de enrutamiento 3D (Local vs Cloud)."""

    semantic_similarity: float = Field(
        ge=0.0, le=1.0, description="Score de similitud semántica (LanceDB)."
    )
    graph_coverage: float = Field(
        ge=0.0, le=1.0, description="Cobertura del grafo de dependencias (NetworkX)."
    )
    recency_score: float = Field(
        ge=0.0, le=1.0, description="Peso basado en archivos modificados recientemente."
    )
    css_total: float = Field(
        ge=0.0,
        le=100.0,
        description="Context Sufficiency Score (Métrica global de contexto).",
    )
    task_complexity_index: float = Field(
        ge=0.0, le=100.0, description="Índice de complejidad calculado de la tarea."
    )
    routing_decision: str = Field(
        pattern="^(LOCAL_SMALL|LOCAL_BIG|CLOUD)$", description="Decisión del enrutador."
    )
    is_red_alert: bool = Field(
        description="True si el CSS es críticamente bajo (<40%)."
    )


class LLMProfile(BaseModel):
    """Firma del modelo actualmente en ejecución."""

    model_name: str
    parameters_b: float
    context_window: int
    quantization: str


class TokenCounter(BaseModel):
    """Auditoría de uso y costos."""

    local: int = 0
    cloud: int = 0
    total_cost_usd: float = 0.0


class VFSFile(BaseModel):
    """Representa un archivo en memoria con control de concurrencia (Virtual File System).

    Phase 2.2.D: content replaced by blob_hash — file text lives in ContentAddressableStorage
    (core/blob_storage.py). The LangGraph checkpoint only carries the tiny hash, keeping
    serialised state O(hash_length) regardless of file size.
    """

    blob_hash: str = Field(
        ...,
        description="blake2b hex digest of file content stored in ContentAddressableStorage.",
    )
    document_version_id: str = Field(
        ...,
        description="Timestamp o Hash MD5 para OCC (Optimistic Concurrency Control).",
    )
    is_dirty: bool = Field(
        default=False,
        description="True si la IA lo modificó y falta sincronizar al IDE del usuario.",
    )


class ManualAttachment(BaseModel):
    """Contexto multimodal inyectado manualmente por el usuario (imagen o documento)."""

    type: Literal["image", "document"]
    data: Optional[str] = Field(
        None,
        max_length=10_485_760,  # 10 MB ceiling on base64 payload to prevent OOM
        description="Bytes codificados en base64 (solo imágenes).",
    )
    content: Optional[str] = Field(None, description="Texto plano del documento.")
    mime: Optional[str] = Field(None, description="Tipo MIME, e.g. 'image/png'.")
    name: Optional[str] = Field(None, description="Nombre del archivo adjunto.")


# =====================================================================
# 2. ESTADO DEL GRAFO (AIlienant Context) (LangGraph TypedDict)
# =====================================================================


def _merge_generated_code(
    left: Dict[str, "VFSFile"], right: Dict[str, "VFSFile"]
) -> Dict[str, "VFSFile"]:
    """Reducer for parallel CoderAgent output buffers. Keeps the entry with the
    lexicographically later document_version_id so the most recent generated edit
    survives multi-agent fan-out collisions."""
    merged = dict(left)
    for path, file in right.items():
        if path not in merged or file.document_version_id > merged[path].document_version_id:
            merged[path] = file
    return merged


def _merge_vfs(left: Dict[str, "VFSFile"], right: Dict[str, "VFSFile"]) -> Dict[str, "VFSFile"]:
    """Reducer for concurrent CoderAgent VFS writes. Keeps the file with the
    lexicographically later document_version_id (timestamp/hash), preventing
    a slower parallel branch from overwriting a more recent edit."""
    merged = dict(left)
    for path, file in right.items():
        if path not in merged or file.document_version_id > merged[path].document_version_id:
            merged[path] = file
    return merged


def _merge_messages(
    existing: List[Dict[str, str]],
    update: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Default: append (operator.add semantics).
    If update[0] has __replace__=True, replace entire history with the rest of update.
    """
    if update and update[0].get("__replace__"):
        return [m for m in update if not m.get("__replace__")]
    return existing + update


class AIlienantGraphState(TypedDict):
    """
    El cerebro compartido del flujo de LangGraph.
    Define estrictamente la memoria y variables que los nodos pueden leer o mutar.
    """

    # --- Identidad de la Misión ---
    task_id: str
    user_input: str

    # --- Workspace Identity & Manual Context (Phase 1.1.0 / 1.1.0.4) ---
    project_id: Optional[str]              # SHA-256 of the VS Code workspace root path
    workspace_root: Optional[str]          # Absolute path of the VS Code workspace; set from _workspace_registry
    explicit_mentions: List[str]           # @-referenced file paths → forced full-file read
    attachments: List[ManualAttachment]    # user-attached images / documents

    # --- Memoria de Mensajes ---
    # Historial acumulativo O(N) para la comunicación conversacional.
    messages: Annotated[List[Dict[str, str]], _merge_messages]

    # --- Contexto y Telemetría ---
    context_metrics: ContextMeter
    active_llm_profile: LLMProfile
    token_usage: TokenCounter

    # --- Control de Flujo (Prompt Swapping) ---
    is_manual_override: bool
    target_role: Optional[
        str
    ]  # Sustituye a 'target_agent'. Define el rol actual del CoderAgent.
    current_step_id: Optional[
        int
    ]  # Puntero a la tarea actual del WBS en ejecución (step_number).

    # --- Human-in-the-Loop & Planner Mode (Phase 1.4 / 2.21) ---
    planner_mode_active: bool       # True when user toggled Planner-only mode via WS event
    hitl_pending: bool              # True while the graph is awaiting human approval
    hitl_response: Optional[str]   # "approved" | "rejected" + optional comment from HITL response
    shared_understanding_reached: bool  # Phase 2.21: True once analyst confirms Socratic dialogue complete

    # --- Planificación Inmutable (SDD) ---
    # Reemplaza 'immutable_wbs' y 'completed_steps'.
    # Todo el estado del plan vive dentro de este único objeto para evitar desincronizaciones.
    mission_spec: Optional[MissionSpecification]

    # --- Sistema de Archivos Virtual (VFS) ---
    # Single Source of Truth para el código.
    read_files_state: Dict[str, VFSFile]
    vfs_buffer: Annotated[Dict[str, VFSFile], _merge_vfs]

    # --- Enrutamiento MoE (Phase 2) ---
    # Shortcuts para que los nodos de orquestación lean TCI/CSS sin navegar context_metrics.
    tci: float          # Task Complexity Index  0–100
    css: float          # Context Sufficiency Score  0–100
    # Payload del fan-out MapReduce: PlannerAgent escribe, route_to_coders lee.
    parallel_tasks: List[WBSStep]

    # --- Routing & Hardware (Phase 2.1) ---
    # True when at least one attachment is type="image" → forces CLOUD via Vision Bypass.
    has_images: bool
    # Set by resolve_provider() when CLOUD is optimal but unavailable; None otherwise.
    routing_warning: Optional[str]
    # Populated by orchestrator node on first invocation; cached in checkpoint state.
    hardware_profile: Optional[HardwareProfile]
    # Active routing decision written by the orchestrator; read by route_to_coders.
    provider: str
    # Parallel CoderAgent output buffer; _merge_generated_code prevents fan-out collisions.
    generated_code: Annotated[Dict[str, VFSFile], _merge_generated_code]

    # --- Resiliencia y Diagnóstico ---
    errors: Annotated[List[str], operator.add]
    retry_count: int
    security_flags: Annotated[List[str], operator.add]
    terminal_output: str
    # Phase 3.4.2: Pre-Dream Reflection — compact summary of last session written by
    # session_delta_aggregator_node before each planner turn. Empty string on first turn.
    session_delta: str
    # Phase 4.1.1: Researcher Skeleton Map — written by run_researcher_node before planner in
    # FULL_SWARM mode. None when the run skips the Researcher (SEQUENTIAL / MICRO_SWARM).
    researcher_skeleton: Optional[str]
    # Phase 4.1.2: PlannerAgent retry telemetry — number of ValidationError retries the
    # planner consumed on the current invocation (0 = first-shot success). Bounded by
    # MAX_PLANNER_RETRIES (= 2). Overwrite semantics; not a reducer.
    planner_retry_count: int

    # Phase 4.2 — Deterministic Validators (blueprint §4.2 + §4.1 thresholds).
    # All additive, overwrite semantics (no reducer). The gate nodes write them;
    # the Give-Up Gate logic (inlined in style_gate_node) reads syntax_gate_status
    # + consecutive_style_failures to decide whether to latch style_bypass_active.
    # Engine wiring lives in Phase 4.3.
    venv_interpreter_path: Optional[str]
    relaxed_typing_mode: bool
    style_bypass_active: bool
    consecutive_style_failures: int
    syntax_gate_status: Literal["pass", "fail", "pending"]
    # ──────────────────────────────────────────────────────────────────────
    # Phase 4.2 TRANSITIONAL — code_under_validation
    # ──────────────────────────────────────────────────────────────────────
    # This field exists ONLY so Phase 4.2 unit tests can inject code into the
    # gate nodes without coupling to vfs_buffer / blob_storage. It DUPLICATES
    # content that already lives in vfs_buffer (Dict[str, VFSFile]) and
    # pending_patches (Dict[str, str] diffs). Persisting it inflates the
    # SQLite WAL + LanceDB checkpoint by O(N) per patch — STATE BLOAT.
    #
    # TODO(phase-4.3): when engine wiring lands, replace gate-node reads of
    # this field with resolution from vfs_buffer (via core/blob_storage) or
    # pending_patches (in-memory diff apply). Then REMOVE this field from the
    # TypedDict and update the tests to point at a RunnableConfig.metadata
    # channel or equivalent ephemeral surface. Tracked in
    # docs/PROJECT_MANIFEST.md Tech Debt section.
    # ──────────────────────────────────────────────────────────────────────
    code_under_validation: Optional[str]
    # Phase 2.5: Workspace Indexing Gate — seeded from lazy_indexer.is_complete at graph invocation
    is_indexing_complete: bool

    # --- Guardrail State (Phase 2.1.14) ---
    guardrail_failed: bool              # True if validate_output detected a schema violation
    validation_feedback: Optional[str]  # Corrective message prepended to CoderAgent on retry

    # --- Shadow Planner (Phase 2.2.C) ---
    # Frozen on the first planner turn; never updated by agents.
    # DriftMonitor compares mission_spec against this baseline to detect semantic drift.
    immutable_wbs: Optional[MissionSpecification]

    # --- Shallow State / Patch Queue (Phase 2.2.D) ---
    # CoderAgent (Phase 4) writes unified diffs here; apply_patch_node consumes them.
    # operator.or_ merges dicts by preferring the right-hand (latest) value per key.
    pending_patches: Annotated[Dict[str, str], operator.or_]  # filepath → unified diff
    # Phase 7.9.B.18 — write pipeline: the coder also emits the full new content and
    # a pre-edit hash per changed file so the approved patch can be actuated via the
    # VS Code applyEdit bridge (content) with a stale-file guard (hash).
    pending_contents: Annotated[Dict[str, str], operator.or_]   # filepath → full new content
    pending_base_hash: Annotated[Dict[str, str], operator.or_]  # filepath → sha256(pre-edit, EOL-normalized)

    # --- FinOps Budget Gate (Phase 2.18) ---
    # operator.add reducer required: parallel Send() fan-out means multiple CoderAgent
    # branches write a cost delta simultaneously; reducer sums them safely.
    # Phase 4 CoderAgent returns {"current_cost_usd": delta_usd} per invocation.
    current_cost_usd: Annotated[float, operator.add]
    # Fixed ceiling injected once at graph invocation from env (AILIENANT_MAX_BUDGET_USD).
    # No reducer: this value is never aggregated, only read by the finops_gate node.
    max_budget_usd: float

    # --- Contract Guard (Phase 2.23) ---
    # Render signal emitted by ContractGuardNode when a drift trigger fires; consumed
    # by the VS Code extension to render a persistent banner. Cleared by the extension
    # on ack. Scalar overwrite — no reducer.
    ui_payload: Optional[Dict[str, object]]
    # Anchor snapshot of the state at the last contract emission. Read-only for all
    # nodes except ContractGuardNode. Holds {"tci": float, "target_role": Optional[str],
    # "turn": int}. None until the first emission. Scalar overwrite — no reducer.
    contract_anchor: Optional[Dict[str, object]]

    # --- Resource Broker (Phase 2.27) ---
    # Blocking-modal render signal emitted by ResourceBroker on cross-session VRAM
    # contention. Distinct from `ui_payload` (Phase 2.23 — non-blocking banner) so
    # the two cannot collide in the same turn. Cleared by the extension once the
    # user picks a resolution. Scalar overwrite — no reducer.
    ui_interrupt: Optional[Dict[str, object]]
    # Telemetry snapshot at the moment of contention: conflicting model, queue
    # position, recommendation. Owned by ResourceBroker; read-only elsewhere.
    contention_status: Optional[Dict[str, object]]
    # User's response to the most recent contention prompt: "WAIT" | "SWITCH_TO_CLOUD"
    # | "CANCEL". Set by the broker from the HitL reply; consumed within the same
    # node invocation. Scalar overwrite — no reducer.
    user_resource_resolution: Optional[Literal["WAIT", "SWITCH_TO_CLOUD", "CANCEL"]]

    # --- Phase 4.3 — Execution Tier Selector ---
    # Written once by process_user_intent() at the routing entry; locked for the
    # lifetime of the run (mode-locked topology per blueprint §2).
    execution_mode: Literal["SEQUENTIAL", "MICRO_SWARM", "FULL_SWARM"]

    # --- Phase 4.3 stage-2 — MICRO_SWARM / FULL_SWARM channels ---
    # active_role: written by Orchestrator (1:1 copy of WBSStep.target_role for the
    #   active step); read by CoderAgent prompt builder and tool-filter.
    # error_streak: written by SyntaxGate/StyleGate on fail; reset on step transition;
    #   read by Circuit Breaker (escalates at CIRCUIT_BREAKER_THRESHOLD = 3).
    # style_gate_status: split from consecutive_style_failures so MICRO_SWARM router
    #   can distinguish "broken code" from "ugly code" without re-deriving from counts.
    # circuit_breaker_tripped: latched True once tripped; read by route_to_coders.
    # cloud_surgeon_invocations: operator.add — bounded to MAX_CLOUD_SURGEON=1 by Circuit Breaker.
    active_role: Optional[Literal[
        "core_dev", "architect_refactor", "devops_infra", "secops",
        "qa_tester", "doc_manager", "vcs_manager", "data_ml_engineer",
    ]]
    error_streak: int
    style_gate_status: Literal["pass", "fail", "pending"]
    circuit_breaker_tripped: bool
    cloud_surgeon_invocations: Annotated[int, operator.add]

    # --- Phase 4.4 — Lifecycle & PID Manager channels ---
    # workspace_pid: VS Code window PID sent by the extension on client_workspace_init.
    # workspace_active: latched False by lifecycle_manager.shutdown_workspace(pid).
    workspace_pid: Optional[int]
    workspace_active: bool

    # --- Phase 5.1 — Permission Engine & Cognitive Quarantine channels ---
    # session_permission_mode: per-mission HITL policy (DEFAULT / PLAN / AUTO).
    # boundary_id: per-turn uuid4().hex used by the Cognitive Quarantine axiom.
    # tool_registry_active: names of tools surfaced to the model this turn (Tool RAG).
    # permission_audit_log: append-only ledger of every permission decision.
    # pending_hitl_request: structured payload awaiting WebView user response.
    # background_tasks: registry of long-running asyncio tasks keyed by task_id.
    # mcp_server_endpoint: active MCP ClientSession URI (None = local-only).
    # rbwe_violations: append-only list of "tool::target" RBWE rejections.
    session_permission_mode: Literal["DEFAULT", "PLAN", "AUTO"]
    boundary_id: Optional[str]
    tool_registry_active: List[str]
    permission_audit_log: Annotated[List[Dict[str, Any]], operator.add]
    pending_hitl_request: Optional[Dict[str, Any]]
    background_tasks: Dict[str, Dict[str, Any]]
    mcp_server_endpoint: Optional[str]
    rbwe_violations: Annotated[List[str], operator.add]

    # --- Phase 6 — Operational Safety Layer channels (PHASE_6_BLUEPRINT §1) ---
    # All scalar overwrite (no reducer) with safe defaults — Phase 5.7 checkpoints
    # deserialise unchanged. Landed in Phase 6.3 (OOM Cascade needs
    # oom_fallback_active); the rest front-load the 6.4/6.5 Supervisor + DLQ work.
    #
    # accumulated_session_cost: USD cost across the WHOLE WebSocket session
    #   (many tasks), written by core/supervisor.py from token_ledger.snapshot().
    # session_max_budget_usd: hard ceiling injected once at graph construction
    #   from AILIENANT_MAX_SESSION_BUDGET_USD; read by the Supervisor triggers.
    # oom_fallback_active: set True by tools/llm_gateway.py::ainvoke when it traps
    #   an OOM-class exception; read by brain/nodes/circuit_breaker.py to skip the
    #   local cascade; reset to False once the fallback turn is acknowledged.
    # sandbox_tier_active: mirror of core.sandbox.ACTIVE_TIER injected at graph
    #   start so nodes can reason about posture without importing the global.
    # hitl_audit_chain_head: blake2b chain_hash of the last resolved
    #   hitl_audit_log row; the Supervisor verifies it against the DB head.
    # dead_letter_episode_id: episode_id of a resumed DLQ turn; None for fresh
    #   tasks; rendered by the AnalystAgent post-mortem.
    accumulated_session_cost: float
    session_max_budget_usd: float
    oom_fallback_active: bool
    sandbox_tier_active: Literal["DOCKER", "WASM", "NATIVE_HITL"]
    hitl_audit_chain_head: Optional[str]
    dead_letter_episode_id: Optional[str]

    # --- Phase 7.11.3 (ADR-706 §4.5b) — Abort Controller Mesh savepoint marker ---
    # Populated by the orchestrator (core/task_service.py) inside the
    # `except asyncio.CancelledError` block when the user clicks Stop. The value
    # is "user_abort" per the blueprint; future fault classes can extend the
    # taxonomy (e.g., "budget_kill" / "timeout"). Cold-serializable string — the
    # next HybridCheckpointer.promote() carries it into SQLite/LanceDB without
    # any schema migration. UI consumers read it on rehydrate to render a
    # "Stopped by user" indicator without breaking graph topology. None on
    # normal completion. Scalar overwrite, no reducer.
    termination_reason: Optional[str]
