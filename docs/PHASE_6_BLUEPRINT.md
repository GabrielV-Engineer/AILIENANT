# PHASE 6 — Master Architectural Blueprint (Enterprise Refactor)

> **Mandatory read** during every Phase 6 task. Survives session compactions: re-derive intent from this document. Any deviation from the binding decisions tagged **[ADR-xxx]** requires an explicit blueprint amendment in the same PR.

## Context

Phase 5 (Permission Engine, Tool RAG, Perception · Mutation · Execution · Control tool bundles, Adversarial Gate) closed the **interface layer** between cognition and the outside world: the system knows *who can call what tool* (3-axis matrix), *which schemas to inject just-in-time* (Tool RAG top-K=5, ≥70 % prompt reduction), *how to mutate the workspace safely* (RBWE + OCC + AST validation), and *how to ask the human* (regex-driven friction + WebView round-trip).

Phase 6 closes the **operational-safety layer** under that interface: how subprocesses are actually isolated from the host, how runaway cloud spend is throttled mid-run, how an OOM inside a local model swings the system into cloud fallback without losing the turn, how a node crash hands the task to a Dead Letter Queue rather than dropping it on the floor, and how every HITL approval becomes a cryptographically auditable line in a SOC2-ready ledger.

Phase 6 must deliver **four guarantees**:

1. **Host-grade isolation by default, never silently degraded.** Subprocess execution dispatches through a pluggable Sandbox Adapter resolved once at startup. Default tier is `DOCKER` (long-lived `ailienant-sandbox` container, read-only project mount, no network). When the Docker daemon is offline, the runtime degrades to `NATIVE_HITL` — every `EXECUTE`-tier call routes through `vfs_manager.request_human_approval(...)` *before* the spawn — and surfaces a coloured badge to the VS Code sidebar so the user can never *not* know their posture has dropped.
2. **FinOps with a hard ceiling, not a vibe.** A deterministic `Supervisor` node, spliced between `finops_gate` and `apply_patch`, reads `core/token_ledger.snapshot()` every pass (closing the long-standing decoupling bug between the global ledger and `state["current_cost_usd"]`), enforces session-level budget triggers (soft HITL gate at 1.00×, hard kill at 1.10×, single-turn token-spike trip at 64 K), and verifies the audit chain head on every visit.
3. **Crash-resilient state machine.** A `dead_letter_decorator` wrapping every LangGraph node catches the unhandled exception, promotes L1→L2 via the existing `HybridCheckpointer.promote()` (Phase 2.7/2.15), persists a row to `dead_letter_tasks`, and re-raises. `POST /api/v1/task/resume/{task_id}` hydrates the L2 checkpoint and continues execution — idempotent and load-bearing for the Phase 7 sidebar's "Resume Task" affordance.
4. **Cryptographically auditable HITL.** Every `request_human_approval(...)` call writes a row to `hitl_audit_log` whose `chain_hash = blake2b(prev_chain_hash ‖ audit_id ‖ state_snapshot_hash ‖ resolution ‖ resolved_at)`. The chain is verifiable end-to-end; the Supervisor verifies `state["hitl_audit_chain_head"]` matches the DB head on every pass and crashes loudly (`AuditChainBrokenError`) on divergence. Defence in depth: a Secrets Scrubber filter mutates every `proposed_content` payload through a blake2b-truncated redaction *before* the row is INSERTed, so a leaked API key in a HITL prompt cannot persist in the audit chain.

This blueprint is the contract that future Phase 6 PRs (6.1 → 6.10) must conform to; later compactions can drop session memory and still re-derive intent from this document.

---

## 1. The Unified State Contract (`AIlienantGraphState`)

Phase 6 **extends** the existing TypedDict in [ailienant-core/brain/state.py](../ailienant-core/brain/state.py). It does **not** replace it, and it does **not** modify any Phase 1–5 channel. The 6 new channels are all scalar overwrite with safe defaults, so Phase 5.7 checkpoints deserialise unchanged.

```python
class AIlienantGraphState(TypedDict):
    # ... (all Phase 1–5 channels unchanged — see PHASE_5_BLUEPRINT §1)

    # ===========================================================
    # === Phase 6 ADD — Operational Safety Layer Channels =======
    # ===========================================================

    accumulated_session_cost: float
    # USD cost accumulated across the WHOLE WebSocket session (multiple tasks),
    # not per-graph-invocation. Owner: core/supervisor.py — written from
    # token_ledger.snapshot() every Supervisor pass. Bridge that closes the
    # historic decoupling bug between core/token_ledger.py (process-global)
    # and state["current_cost_usd"] (per-fan-out delta). Default 0.0.

    session_max_budget_usd: float
    # Hard ceiling injected once at graph construction time from the env var
    # AILIENANT_MAX_SESSION_BUDGET_USD (default 5.00). Owner: task_service.
    # Supervisor triggers HITL above 1.00×, hard kill above 1.10×.

    oom_fallback_active: bool
    # Set True by tools/llm_gateway.py::ainvoke() when it catches an OOM-class
    # exception (ContextWindowExceededError, CUDA OOM, provider-specific OOM).
    # Read by core/supervisor.py and brain/nodes/circuit_breaker.py to skip
    # the local cascade and route directly to the cloud fallback model.
    # Default False; reset by Supervisor once the fallback turn completes.

    sandbox_tier_active: Literal["DOCKER", "WASM", "NATIVE_HITL"]
    # Mirror of core.sandbox.ACTIVE_TIER injected once at graph start. Lets
    # graph nodes (and the AnalystAgent post-mortem) reason about the posture
    # of the run without importing the global. Default "NATIVE_HITL" — the
    # safest assumption if the resolver has not run.

    hitl_audit_chain_head: Optional[str]
    # blake2b chain_hash of the LAST resolved hitl_audit_log row for this
    # session. Written by core/audit.py::audit_logger.log_resolution(...).
    # Supervisor verifies state[head] == DB.head on every pass — divergence
    # means out-of-band DB mutation. None until the first HITL.

    dead_letter_episode_id: Optional[str]
    # When the current graph invocation is a resume from a DLQ episode, this
    # carries the episode_id of the failed turn (uuid4 hex, primary key of
    # dead_letter_tasks). AnalystAgent renders this in the final report so
    # the user knows they are looking at a resumed mission. None for fresh
    # tasks. Set by main.py::resume_task() before graph.ainvoke().
```

### Field Provenance Map

| Field | Written by | Read by | Reducer |
|---|---|---|---|
| `accumulated_session_cost` | `core/supervisor.py` (each pass; ledger sync) | `core/supervisor.py` (triggers); `AnalystAgent` post-mortem | scalar overwrite |
| `session_max_budget_usd` | `core/task_service.py` (graph entry, from env) | `core/supervisor.py` | scalar overwrite |
| `oom_fallback_active` | `tools/llm_gateway.py::ainvoke` (catch handler); `core/supervisor.py` (reset) | `brain/nodes/circuit_breaker.py`; `core/supervisor.py` | scalar overwrite |
| `sandbox_tier_active` | graph factory (from `core.sandbox.ACTIVE_TIER`) | `tools/execution_tools.py` (informational); `AnalystAgent` | scalar overwrite |
| `hitl_audit_chain_head` | `core/audit.py::log_resolution` | `core/supervisor.py` (chain verify) | scalar overwrite |
| `dead_letter_episode_id` | `main.py::resume_task()` | `AnalystAgent` | scalar overwrite |

Phase 6 **does not** modify any of the Phase 5.1 audit channels (`permission_audit_log`, `rbwe_violations`) or any Phase 4 channel (`error_streak`, `circuit_breaker_tripped`, `cloud_surgeon_invocations`, `style_gate_status`, …). All Phase 6 channels are additive.

`SCHEMA_EVOLUTION.MD` gets a new section: *"Phase 6 — Sandbox + Supervisor + Audit channels: `accumulated_session_cost`, `session_max_budget_usd`, `oom_fallback_active`, `sandbox_tier_active`, `hitl_audit_chain_head`, `dead_letter_episode_id`; no removals, no renames, no Literal narrowing."*

---

## 2. Pluggable Sandbox Adapter (`core/sandbox.py` — NEW)

### 2.1 [ADR-001] Hybrid Graceful Degradation (binding)

The verdict, restated for posterity: **Strict Mandatory Docker is rejected** — it breaks the Phase 10.2 single-binary install promise. **Hybrid Graceful Degradation is adopted** with three tiers, resolved once at startup and immutable for the session. The user always knows their posture (badge in the sidebar). The default is `DOCKER`; the safety net is `NATIVE_HITL`; the opt-in optimisation is `WASM` (pure-compute only, see [ADR-002]).

```python
# ailienant-core/core/sandbox.py — Phase 6.1 NEW

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

SandboxTier = Literal["DOCKER", "WASM", "NATIVE_HITL"]

@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    sandbox_tier: SandboxTier
    audit_id: Optional[str]  # populated when the call traversed an HITL gate

class SandboxAdapter(ABC):
    tier: SandboxTier

    @abstractmethod
    async def execute(
        self,
        command: str,
        *,
        timeout_s: float,
        cwd: Optional[str] = None,
        env_whitelist: Optional[frozenset[str]] = None,
    ) -> SandboxResult: ...

# Process-global resolution; mutated ONCE during FastAPI lifespan startup.
ACTIVE_TIER: SandboxTier = "NATIVE_HITL"        # safe default before resolve()
ACTIVE_ADAPTER: Optional[SandboxAdapter] = None
```

### 2.2 The three adapters

| Tier | Adapter | Isolation guarantee | Latency cost | Failure mode |
|---|---|---|---|---|
| `DOCKER` | `DockerSandboxAdapter` | cgroups + namespaces; `--read-only` rootfs; project mount **read-only**; tmpfs `/work`; no network | +30–80 ms per `docker exec` against a long-lived container | Daemon stop mid-session → next call promotes to HITL with a sandbox-degraded banner |
| `WASM` | `WasmSandboxAdapter` | WASI-preview1; no `--mapdir`; fuel-metered (5 M instructions cap); imports allow-list (`math`, `re`, `json`, `dataclasses`, `typing`) | <5 ms cold; ~1 ms warm | `WasmScopeError` raised on disallowed import; never spawns OS process |
| `NATIVE_HITL` | `NativeHITLSandboxAdapter` | None on the process; **every call** routes through `request_human_approval` first | +1 HITL RTT (user-bound) | Rejection → `SandboxResult(exit_code=-1, stderr="[hitl_denied]")`; timeout → same + DLQ enqueue |

#### `DockerSandboxAdapter` operating principles

- One long-lived container per session, created lazily on the first `EXECUTE`-tier call. Image is built locally on first run from a pinned `Dockerfile.sandbox` (no Docker Hub pull at runtime). The image's blake2b digest is recorded in `hitl_audit_log` the first time the adapter spawns it.
- Project tree mounted at `/workspace` **read-only**. Patches do **not** land here — they go through the Phase 5.4 ACID write-buffer (`BatchSemanticEditTool` / `AtomicCodePatchTool`) and only the post-commit results are streamed back to the VFS by the host process.
- `tmpfs` at `/work` for ephemeral scratch (pytest cache, ruff cache, mypy cache). Cleared between sessions; never persists.
- No network capability by default (`--network=none`). The Phase 5.3 `WebFetchTool` runs on the **host** (it is `READ_ONLY` and outbound-only); inside the sandbox network egress is a deliberate next-phase capability and stays disabled in 6.1.
- Environment-variable allow-list — only `PYTHONPATH`, `NODE_OPTIONS`, `RUFF_CACHE_DIR`, `MYPY_CACHE_DIR` are forwarded. API keys never enter the container.

#### `NativeHITLSandboxAdapter` operating principles

- Wraps the existing `asyncio.create_subprocess_shell` path verbatim (no regression to Phase 5.5 truncation, timeout, or `proc.kill()` discipline at [tools/execution_tools.py:157-173](../ailienant-core/tools/execution_tools.py#L157-L173)).
- Pre-spawn it emits `await vfs_manager.request_human_approval(session_id, action_description="SANDBOX_DEGRADED_EXEC", proposed_content=<command + cwd + env>, timeout_s=300.0)`. On approve → spawn + audit row. On reject → returns deny-shaped `SandboxResult`. On timeout → deny-shaped result **plus** DLQ row (the user walked away from a degraded session — that is a state worth recovering from later).
- The friction-asymmetric WebView (Phase 5.6) is **reused unchanged**. The `DANGEROUS_COMMANDS_REGEX` from [tools/control_tools.py:62-73](../ailienant-core/tools/control_tools.py#L62-L73) still runs *first* — if it matches, the action_description sentinel becomes `DANGEROUS_COMMAND_INTERCEPT` and the WebView applies the retype-the-verb friction rule.

#### `WasmSandboxAdapter` operating principles

- [ADR-002] Wasm Scope Guard. Wasm executes **only** pure-compute payloads: algorithm validation, parser unit tests, regex compile, math kernels. Disallowed imports (`os`, `subprocess`, `socket`, `pathlib`, `shutil`, anything that touches FS/net) cause `WasmScopeError` *before* fuel is consumed. Any test that needs `pytest` discovery, `npm install`, `tsc`, or `mypy` falls through to Docker (or, degraded, to HITL).
  - **Two-layer model (clarified at 6.1.3 landing).** The Scope Guard operates at *two* layers. (1) **Module-import layer** — implemented in `WasmSandboxAdapter._inspect_module_scope` (Phase 6.1.3): the loaded `.wasm` module's import section is inspected and any import whose module is outside the WASI-preview1 allow-list raises `WasmScopeError`. A pre-compiled `.wasm` has wasm imports, not Python imports, so this is the correct mechanism for the executor. (2) **Python-source layer** — the `os`/`subprocess`/`socket`/`pathlib`/`shutil` allow-list above is a *complementary* check that belongs with the `RunPureLogicTool` consumer, which compiles source → wasm and can therefore see Python-level imports. Both raise the same public `WasmScopeError`.
- Backing: `wasmtime-py>=20.0.0`, `Config.consume_fuel(True)`, `Store.set_fuel(5_000_000)`. Out-of-fuel maps to `SandboxResult(exit_code=137, stderr="[wasm_fuel_exhausted]")`. Non-fuel traps map to `SandboxResult(exit_code=-1, stderr="[wasm_trap: memory_violation]")`.

### 2.3 Resolution at startup

```python
# core/sandbox.py — pseudocode for resolve_default_adapter()
async def resolve_default_adapter() -> SandboxAdapter:
    global ACTIVE_TIER, ACTIVE_ADAPTER
    preferred = os.getenv("AILIENANT_SANDBOX_PREFERRED_TIER", "DOCKER")
    if preferred == "DOCKER":
        adapter = await _try_docker(timeout_s=2.0)
        if adapter is not None:
            ACTIVE_TIER, ACTIVE_ADAPTER = "DOCKER", adapter
            return adapter
    if preferred in ("DOCKER", "WASM"):
        adapter = _try_wasm()
        if adapter is not None and preferred == "WASM":
            ACTIVE_TIER, ACTIVE_ADAPTER = "WASM", adapter
            return adapter
    ACTIVE_ADAPTER = NativeHITLSandboxAdapter()
    ACTIVE_TIER = "NATIVE_HITL"
    logger.warning("Sandbox degraded to NATIVE_HITL — Docker probe failed in 2.0s")
    return ACTIVE_ADAPTER
```

Called once from `main.py` `lifespan` startup, *after* `checkpoint_manager.initialize()` (so a failed probe still writes to the WAL via the audit hook) and *before* the WebSocket accept. The startup payload emitted to a newly-connected extension client carries `sandbox_tier: SandboxTier` for the badge.

### 2.4 Tool dispatch swap

[tools/execution_tools.py](../ailienant-core/tools/execution_tools.py) tools (`SandboxBashTool`, `TaskCreateTool`, `CheckTypeIntegrityTool`) keep their public `BaseTool` signatures **byte-identical**. The change is internal: their `_arun` methods replace direct `asyncio.create_subprocess_shell` / `create_subprocess_exec` calls with:

```python
result = await core.sandbox.ACTIVE_ADAPTER.execute(
    command, timeout_s=timeout, cwd=self.cwd, env_whitelist=ALLOWED_ENV,
)
```

Phase 5 callers (the 5 RBAC roles whitelisted in `tools/execution_tools._EXECUTE_ROLES`) are blind to the change. Tool RAG selection, RBWE enforcement, and the 5.6 dangerous-command interceptor all keep firing in the same order, before dispatch.

---

## 3. HITL Bridge & Asymmetric Friction (contract, no new transport)

### 3.1 [ADR-003] Reuse the canonical channel

Phase 6 introduces **no new HITL transport**. Every fricción event (sandbox degraded, dangerous command, budget overflow, drift, resource contention) routes through `vfs_manager.request_human_approval(...)` (the function defined at [api/websocket_manager.py:217-256](../ailienant-core/api/websocket_manager.py#L217-L256), already in production for `finops_gate`, `drift_monitor`, and `ResourceBroker`).

Distinction is purely semantic, via the `action_description` sentinel:

| Sentinel | Phase that emits it | Distinction in the WebView |
|---|---|---|
| `BUDGET_OVERFLOW` | Phase 6.5 Supervisor | shows ledger snapshot + last 3 nodes; user can override or cancel |
| `SANDBOX_DEGRADED_EXEC` | Phase 6.1 `NativeHITLSandboxAdapter` | shows full command + cwd + env; banner reminds user the sandbox is offline |
| `DANGEROUS_COMMAND_INTERCEPT` | Phase 5.6 regex match → 6.1 dispatcher | retype-the-verb friction rule (Phase 5.6 unchanged) |
| `DRIFT_DETECTED` | Phase 2.16 DriftMonitor (unchanged) | unchanged |
| `RESOURCE_CONTENTION` | Phase 2.27 ResourceBroker (unchanged) | unchanged |

No change to `ws_contracts.py`. No new event types. The extension's WebView dispatcher branches on the sentinel.

### 3.2 Hooks for the audit chain

Both `request_human_approval` and `resolve_human_approval` gain audit hooks (Phase 6.6, §7):

```python
# api/websocket_manager.py — minimal patch
async def request_human_approval(self, session_id, action_description, proposed_content=None, timeout_s=300.0):
    approval_id = uuid.uuid4().hex
    # Phase 6.6 ADD — pre-emission audit
    await audit_logger.log_request(
        audit_id=approval_id,
        session_id=session_id,
        request_kind=_classify(action_description),
        action_description=action_description,
        proposed_content=proposed_content,  # scrubbed inside audit_logger (Phase 6.7)
    )
    # ... existing flow unchanged ...

async def resolve_human_approval(self, approval_id, response):
    # ... existing flow unchanged ...
    # Phase 6.6 ADD — finalise the chain link
    await audit_logger.log_resolution(approval_id, response)
```

---

## 4. OOM Cascade & Inference Resilience (Phase 6.3 / 6.8)

### 4.1 The single chokepoint

Today every LLM call exits the codebase through `tools/llm_gateway.py::ainvoke` (lines 127-189), which calls `litellm.acompletion(...)` and posts the result back to `core.token_ledger` (lines 172-187). Phase 6 wraps the call in a hierarchy of catches:

```python
# tools/llm_gateway.py — Phase 6.3 patch (pseudocode)
async def ainvoke(self, messages, *, model, tier, ...):
    try:
        response = await litellm.acompletion(...)
    except litellm.exceptions.ContextWindowExceededError:
        return await _oom_cascade(messages, model, reason="context_overflow", state=state)
    except litellm.exceptions.APIConnectionError as e:
        if _looks_like_oom(e):  # /cuda|out of memory/i in str(e)
            return await _oom_cascade(messages, model, reason="cuda_oom", state=state)
        raise
    except _PROVIDER_OOM_EXCEPTIONS as e:
        return await _oom_cascade(messages, model, reason=f"provider:{type(e).__name__}", state=state)
    # ... existing token-ledger sync + return ...
```

### 4.2 The cascade

```python
async def _oom_cascade(messages, model, *, reason: str, state):
    # 1. Purge local KV cache (Phase 4.4 lifecycle hook, no signature change)
    pid = state.get("workspace_pid")
    if pid is not None:
        await lifecycle_manager.release_vram_on_mode_switch(pid)

    # 2. Mark state
    state["oom_fallback_active"] = True
    state.setdefault("security_flags", []).append(f"OOM_FALLBACK_ENGAGED:{reason}")

    # 3. Summarise context (Phase 2.10 / 4.5 summariser, untouched)
    trimmed_messages = await summarizer.compress(messages, budget_tokens=12_000)

    # 4. Re-emit to cloud Haiku-class model (env-configurable)
    fallback_model = os.getenv("AILIENANT_OOM_CLOUD_FALLBACK_MODEL", "claude-haiku-4-5-20251001")
    response = await litellm.acompletion(model=fallback_model, messages=trimmed_messages, ...)

    # 5. Audit
    await telemetry.log_oom_event(reason, original_model=model, fallback_model=fallback_model,
                                  tokens_at_failure=_count(messages), state=state)
    return response
```

### 4.3 Orthogonality with the Cloud Surgeon

OOM and the Phase 4.5 Cloud Surgeon (`error_streak ≥ 3 → swap to MODEL_CLOUD`) are **orthogonal signals**. The Surgeon counts syntax/style failures; OOM is a hardware/context-window failure. The branch added to `brain/nodes/circuit_breaker.py` reads `state["oom_fallback_active"]`:

```python
# brain/nodes/circuit_breaker.py — Phase 6.3 branch
def evaluate_circuit_breaker(state):
    if state.get("oom_fallback_active"):
        # OOM already swung us to cloud; do NOT count this against error_streak
        # and do NOT consume the single Cloud Surgeon shot.
        return {"provider": "CLOUD", "active_llm_profile": _OOM_CLOUD_PROFILE,
                "oom_fallback_active": False}  # reset for next turn
    # ... existing error_streak / cloud_surgeon logic unchanged ...
```

`cloud_surgeon_invocations` is **not** incremented by an OOM cascade. The Phase 4.5 `MAX_CLOUD_SURGEON=1` ceiling still applies to genuine quality failures, not to OOM rescues.

### 4.4 Double-fault fallback

If the cloud Haiku call **also** OOMs (`ContextWindowExceededError` on a 200 K-context model is real — think 500 K prompts), the cascade raises a sentinel exception which the `dead_letter_decorator` (§5) catches. The DLQ row carries `failed_node="oom_cascade"` and the original model identifier, so the user can either trim the prompt manually and resume, or change the fallback model.

---

## 5. ACID Transactions, DLQ, Resume API (Phase 6.4 / 6.9)

### 5.1 Tables (catalog DB, WAL discipline reused)

```sql
CREATE TABLE IF NOT EXISTS dead_letter_tasks (
    episode_id TEXT PRIMARY KEY,           -- uuid4 hex
    task_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,                -- LangGraph thread_id
    failed_node TEXT NOT NULL,              -- "coder_agent" | "supervisor" | "oom_cascade" | …
    exception_class TEXT NOT NULL,
    exception_message TEXT NOT NULL,
    state_snapshot_blob_hash TEXT NOT NULL, -- blake2b via core/blob_storage.py
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dlq_task_id ON dead_letter_tasks(task_id);
```

The `state_snapshot_blob_hash` reuses the existing CAS at [core/blob_storage.py](../ailienant-core/core/blob_storage.py) (Phase 2.17 blake2b LRU). No new persistence infrastructure.

### 5.2 The decorator

```python
# core/dead_letter.py — Phase 6.4 NEW
def dead_letter_decorator(node_name: str):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(state, *args, **kwargs):
            try:
                return await fn(state, *args, **kwargs)
            except Exception as exc:
                # 1. Promote L1 → L2 (idempotent, Phase 2.7/2.15)
                await checkpoint_manager.promote(state["task_id"])
                # 2. Persist DLQ row
                blob_hash = blob_storage.put(json.dumps(_dumpable(state)))
                episode_id = uuid.uuid4().hex
                await _dlq_insert(
                    episode_id=episode_id, task_id=state["task_id"],
                    thread_id=state.get("thread_id", state["task_id"]),
                    failed_node=node_name, exception_class=type(exc).__name__,
                    exception_message=str(exc)[:2000],
                    state_snapshot_blob_hash=blob_hash,
                )
                # 3. Re-raise so LangGraph still observes the failure
                raise
        return wrapper
    return decorator
```

Applied to **7 entrypoints** in [brain/swarms.py](../ailienant-core/brain/swarms.py): `planner_agent`, `researcher_agent`, `orchestrator_agent`, `coder_agent`, `apply_patch`, `validate_output`, `supervisor_node`. `verify_environment` is too cheap to bother (single subprocess check, no graph state to lose).

### 5.3 Resume API

```http
POST /api/v1/task/resume/{task_id}
Response: { "resumed": true, "from_episode": "<episode_id>", "node_resumed_at": "<node_name>" }
       |  { "resumed": false, "reason": "already_completed" | "no_dlq_episode" }
```

Implementation in `main.py`:
1. SELECT the latest `dead_letter_tasks` row for `task_id`.
2. If none or task already in `completed` state → 200 OK with `resumed: false` (idempotent).
3. Otherwise: hydrate the L2 checkpoint via `HybridCheckpointer.get(thread_id)`, set `state["dead_letter_episode_id"] = episode_id`, and call `graph.ainvoke(state)` again.

The extension's WebSocket startup payload reports pending DLQs for the workspace; the sidebar offers "Resume Task" as an actionable item (Phase 7.5 surface).

### 5.4 Why this replaces the original 6.4 stub

The original spec said `commit_on_completion=True` on the LangGraph Saver. That is a knob that does not exist on `HybridCheckpointer` and would not survive an uncaught exception inside a node anyway (the exception propagates **before** the Saver commits). The decorator pattern is the correct hook: it sits inside each node, catches everything, and uses the **existing** L1→L2 promotion (which is itself an UPSERT and therefore safe to call mid-flight).

---

## 6. FinOps Supervisor & Graph Health Monitor (`core/supervisor.py` — NEW, Phase 6.5)

### 6.1 Position in the graph

```
… → coder_agent → finops_gate → supervisor_node → apply_patch → validate_output → …
```

Spliced *after* `finops_gate` (which handles the per-task `current_cost_usd` vs `max_budget_usd` decision from Phase 2.18) and *before* `apply_patch`. Supervisor is deterministic — no LLM call, zero token cost when nothing trips. Same engineering precedent as Phase 4.2 deterministic gates and Phase 2.13 output-parser guardrails.

### 6.2 The triggers (priority order)

```python
# core/supervisor.py — Phase 6.5 NEW (decision sketch)
async def run_supervisor_node(state) -> dict:
    # === 0. Sync ledger → state (closes the historic decoupling bug) ===
    ledger_snapshot = token_ledger.snapshot()
    session_cost = _ledger_delta_for_session(state["session_id"], ledger_snapshot)
    state_patch: dict = {"accumulated_session_cost": session_cost}

    # === 1. Audit chain verify (cheapest, hardest fail) ===
    db_head = await audit_logger.get_chain_head(state["session_id"])
    if state.get("hitl_audit_chain_head") not in (None, db_head):
        raise AuditChainBrokenError(state=state, db_head=db_head)

    # === 2. Hard kill (no recovery, write DLQ for continuity) ===
    budget = state["session_max_budget_usd"]
    if session_cost > budget * 1.10:
        state_patch["security_flags"] = ["SESSION_BUDGET_HARD_KILL"]
        await _force_dlq(state, "supervisor", "budget_hard_kill")
        return state_patch | {"__route__": END}

    # === 3. Soft HITL gate ===
    if session_cost > budget:
        approval = await vfs_manager.request_human_approval(
            session_id=state["session_id"],
            action_description="BUDGET_OVERFLOW",
            proposed_content=_format_budget_breach(state, ledger_snapshot),
            timeout_s=300.0,
        )
        if approval is None or not approval.get("approved"):
            state_patch["security_flags"] = ["SESSION_BUDGET_HARD_KILL"]
            return state_patch | {"__route__": END}
        state_patch["session_max_budget_usd"] = budget * 2.0  # user raised the ceiling

    # === 4. Token-spike trip (independent of budget) ===
    max_per_turn = int(os.getenv("AILIENANT_MAX_TOKENS_PER_TURN", "64000"))
    if _last_turn_tokens(ledger_snapshot) > max_per_turn:
        # Same HITL flow with action_description="TOKEN_SPIKE"
        ...

    return state_patch
```

### 6.3 Why the bridge from `token_ledger` to state matters

Today `core/token_ledger.py` accumulates LOCAL/CLOUD tokens process-wide (`token_ledger.record_local`, `record_cloud` at `tools/llm_gateway.py:172-187`), but **nothing writes it back into `state["current_cost_usd"]`**. `state["current_cost_usd"]` is `Annotated[float, operator.add]` and aggregates only across fan-out branches **within a single graph invocation**; between tasks in the same WebSocket session it resets to 0.0. So a user can burn $50 in five sequential tasks of $10 each and the finops gate (which only sees per-task cost) never fires.

The Supervisor closes the loop by reading `token_ledger.snapshot()`, computing the session-scoped delta, and publishing it to the new `accumulated_session_cost` channel. The session_id is the WebSocket client_id (already passed end-to-end since Phase 1.4), so the bookkeeping is unambiguous.

---

## 7. Append-Only HITL Audit Chain (`core/audit.py` — NEW, Phase 6.6)

### 7.1 Table

```sql
CREATE TABLE IF NOT EXISTS hitl_audit_log (
    audit_id TEXT PRIMARY KEY,            -- uuid4 hex; matches approval_id
    session_id TEXT NOT NULL,
    task_id TEXT,
    request_kind TEXT NOT NULL,            -- enum: BUDGET_OVERFLOW |
                                           --   DANGEROUS_COMMAND_INTERCEPT |
                                           --   SANDBOX_DEGRADED_EXEC |
                                           --   DRIFT_DETECTED | RESOURCE_CONTENTION
    action_description TEXT NOT NULL,
    proposed_content_hash TEXT NOT NULL,   -- blake2b(scrubbed_payload)
    state_snapshot_hash TEXT NOT NULL,     -- blake2b(state at emission)
    prev_chain_hash TEXT,                  -- NULL only for the session's genesis row
    chain_hash TEXT NOT NULL,              -- see formula below
    requested_at INTEGER NOT NULL,
    resolved_at INTEGER,                   -- NULL until log_resolution() runs
    resolution TEXT,                       -- approved | rejected | timeout | <comment>
    operator_user_email TEXT               -- CLAUDE.md userEmail, best-effort
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON hitl_audit_log(session_id, requested_at);
```

### 7.2 The chain formula

```
chain_hash = blake2b(
    (prev_chain_hash or '')
    || audit_id
    || state_snapshot_hash
    || (resolution or '')
    || str(resolved_at or 0)
).hexdigest()
```

The chain is finalised at **resolution time**, not request time, so it covers the full lifecycle (request → resolution). The request row is INSERTed with `chain_hash = blake2b(prev_chain_hash || audit_id || state_snapshot_hash || '' || '0').hexdigest()` and UPDATEd at resolution with the final hash. The Supervisor's chain-verify step (§6.2 step 1) reads the **finalised** head.

### 7.3 Why blake2b

Already battle-tested in the codebase (`core/blob_storage.py`, Phase 2.17). No new crypto dependency. Faster than SHA-256 on the hot path. 32-byte digest (64 hex chars) is plenty for tamper detection at the audit-log row count we expect (< 10⁶ rows over the lifetime of a workspace).

### 7.4 Defence in depth — scrubber runs before INSERT

The `proposed_content` payload is **scrubbed** (§8) before its blake2b hash is computed and before the row is INSERTed. A leaked `sk-ant-…` key in a HITL prompt does not enter the audit chain in cleartext; only the `**REDACTED:<hash8>**` form does. Tampering: an attacker who somehow rewrites the cleartext into the row's payload also breaks the `proposed_content_hash` (which was computed over the scrubbed form), which breaks the next row's `chain_hash`.

---

## 8. Secrets Scrubber (`shared/logging_filters.py` — NEW, Phase 6.7)

### 8.1 Filter installation

A `logging.Filter` subclass attached to the **root logger** during the FastAPI `lifespan` startup, immediately after `logging.basicConfig(...)`. Covers every per-module child logger (`AILIENANT_RESOURCE_BROKER`, `LIFECYCLE_MANAGER`, `WAL_CHECKPOINTER`, `HYBRID_CHECKPOINTER`, `TELEMETRY`, etc.) without per-module changes.

### 8.2 Patterns

| Provider / shape | Regex |
|---|---|
| OpenAI key | `sk-[A-Za-z0-9]{20,}` |
| Anthropic key | `sk-ant-[A-Za-z0-9-]{20,}` |
| Generic Bearer | `Bearer\s+[A-Za-z0-9._-]{20,}` |
| JWT-shape | `eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}` |
| URL embedded creds | `://[^:]+:[^@]+@` |

Replacement format: `**REDACTED:<hash8>**` where `<hash8> = blake2b(secret).hexdigest()[:8]` — diagnosable across log lines (same key → same hash8) without disclosure.

### 8.3 Application surface

The same scrubber callable is exported (`scrub(text: str) -> str`) and is invoked from `core/audit.py::log_request(...)` before computing `proposed_content_hash`. Two-tier defence: leaks in **logs** are scrubbed, and leaks in the **audit chain** are scrubbed.

---

## 9. File inventory

### 9.1 New files

| Path | Phase | Purpose |
|---|---|---|
| `ailienant-core/core/sandbox.py` | 6.1 | `SandboxAdapter` ABC + 3 concrete adapters + `resolve_default_adapter` |
| `ailienant-core/core/audit.py` | 6.6 | `AuditLogger` (log_request, log_resolution, get_chain_head, verify_chain) |
| `ailienant-core/core/supervisor.py` | 6.5 | `run_supervisor_node` + ledger sync + 4 triggers |
| `ailienant-core/core/dead_letter.py` | 6.4 / 6.9 | DLQ table init, `dead_letter_decorator`, `resume_task` helper |
| `ailienant-core/shared/logging_filters.py` | 6.7 | `SecretsScrubberFilter` + `install_root_scrubber()` |
| `ailienant-core/Dockerfile.sandbox` | 6.1 | Alpine + `python:3.13-slim` derivative for `DockerSandboxAdapter` image |
| `ailienant-core/tests/test_phase6_checkpoint_gate.py` | 6.10 | 12 adversarial tests A1–G2 (see §10) |
| `ailienant-core/tests/test_sandbox_adapters.py` | 6.1 | per-adapter unit tests (Docker mock, Wasm scope, Native HITL flow) |
| `ailienant-core/tests/test_oom_cascade.py` | 6.3 / 6.8 | ContextWindowExceeded, CUDA OOM mock, double-fault |
| `ailienant-core/tests/test_dead_letter.py` | 6.4 / 6.9 | decorator → DLQ row; Resume idempotency; resume from coder_agent crash |
| `ailienant-core/tests/test_audit_chain.py` | 6.6 | chain integrity (E1) + tamper detection (E2) |
| `ailienant-core/tests/test_secrets_scrubber.py` | 6.7 | 5 patterns × log/audit application surface |

### 9.2 Modified files

| Path | Change |
|---|---|
| [tools/execution_tools.py](../ailienant-core/tools/execution_tools.py) | `_arun` dispatch through `core.sandbox.ACTIVE_ADAPTER.execute(...)`; BaseTool signatures untouched |
| [tools/llm_gateway.py](../ailienant-core/tools/llm_gateway.py) | OOM catch hierarchy around `litellm.acompletion`; call `_oom_cascade` on hit |
| [brain/state.py](../ailienant-core/brain/state.py) | ADD 6 channels (§1); all scalar overwrite; defaults safe |
| [brain/swarms.py](../ailienant-core/brain/swarms.py) | Splice `supervisor_node` between `finops_gate` and `apply_patch`; wrap 7 node entrypoints with `dead_letter_decorator` |
| [brain/nodes/circuit_breaker.py](../ailienant-core/brain/nodes/circuit_breaker.py) | One extra branch: OOM signal → immediate cloud swap, does NOT consume Cloud Surgeon shot |
| [api/websocket_manager.py](../ailienant-core/api/websocket_manager.py) | `request_human_approval` calls `audit_logger.log_request`; `resolve_human_approval` calls `log_resolution` |
| [core/db.py](../ailienant-core/core/db.py) | Idempotent `CREATE TABLE IF NOT EXISTS` for `hitl_audit_log` + `dead_letter_tasks` |
| [core/lifecycle_manager.py](../ailienant-core/core/lifecycle_manager.py) | (no signature change) `release_vram_on_mode_switch` invoked by OOM cascade as well as mode switch |
| [main.py](../ailienant-core/main.py) | lifespan: probe Docker → set `core.sandbox.ACTIVE_TIER`; install `SecretsScrubberFilter` on root logger; register `POST /api/v1/task/resume/{task_id}`; carry sandbox tier in WS startup payload |
| [shared/config.py](../ailienant-core/shared/config.py) | Add `AILIENANT_MAX_SESSION_BUDGET_USD`, `AILIENANT_MAX_TOKENS_PER_TURN`, `AILIENANT_OOM_CLOUD_FALLBACK_MODEL`, `AILIENANT_SANDBOX_PREFERRED_TIER` |
| `ailienant-core/requirements.txt` | Add `docker>=7.0.0` (optional — adapter falls through if import fails), `wasmtime>=20.0.0` (optional, same) |
| `ailienant-extension/src/webview/MasterToggle.tsx` | Third state "degraded" with explainer link; coloured badge |
| `ailienant-extension/src/api/ws_client.ts` | Read `sandbox_tier` from startup payload; surface DLQ pending count |
| [docs/SCHEMA_EVOLUTION.MD](SCHEMA_EVOLUTION.MD) | APPEND "Phase 6 — Sandbox + Supervisor + Audit channels" |
| [docs/SYSTEM_PROMPTS.md](SYSTEM_PROMPTS.md) | No change |
| [docs/PROJECT_MANIFEST.md](PROJECT_MANIFEST.md) | Already updated (Phase 6 §6.1–6.10 + ADRs) |
| [docs/DEV_JOURNAL.md](DEV_JOURNAL.md) | APPEND one entry per sub-phase (6.A · 6.B · 6.C · 6.D) |
| [README.md](../README.md) | Update Repository Layout (new files); update Roadmap; remove now-delivered items from "Honest list of what is NOT implemented" |

### 9.3 Reused (not modified) artifacts

- [shared/rbac.py](../ailienant-core/shared/rbac.py) — `PermissionMode` / `AgentIdentity` unchanged.
- [core/permissions.py](../ailienant-core/core/permissions.py) — byte-identical. `ToolPrivilegeTier`, `evaluate_action`, `rbwe_guard` consumed as-is.
- [core/tool_rag.py](../ailienant-core/core/tool_rag.py) — unchanged. Tool RAG selection still runs upstream of Sandbox dispatch.
- [tools/control_tools.py](../ailienant-core/tools/control_tools.py) — `DANGEROUS_COMMANDS_REGEX` reused verbatim; `AskUserQuestionTool` / `TogglePlanModeTool` untouched.
- [tools/validation/lsp_filter.py](../ailienant-core/tools/validation/lsp_filter.py) — in-venv ruff/eslint stay native (the user's interpreter cannot be containerised); they remain bounded to the project tree by design.
- [brain/checkpoint.py](../ailienant-core/brain/checkpoint.py) — `HybridCheckpointer.promote()` consumed by the DLQ decorator as-is.
- [core/blob_storage.py](../ailienant-core/core/blob_storage.py) — blake2b CAS reused for `state_snapshot_blob_hash`.
- [core/token_ledger.py](../ailienant-core/core/token_ledger.py) — read by Supervisor; **not** modified. The bridge from ledger → state lives in the Supervisor, not in the ledger.
- [brain/personality.py](../ailienant-core/brain/personality.py) (SoulManager) — unchanged.

---

## 10. Verification Plan (Phase 6.10 Checkpoint Gate)

End-to-end adversarial checks executed via `pytest ailienant-core/tests/test_phase6_checkpoint_gate.py`. Twelve named tests + static gates + regression sweep.

| # | Test | Assertion |
|---|---|---|
| A1 | Docker tier reachable | startup probe selects `DOCKER`; `SandboxBashTool("echo hi")` runs in the container; host PID tree never sees the `sh` process |
| A2 | Docker daemon offline | probe fails → `NATIVE_HITL`; webview badge says "degraded"; mock HITL approval → command runs and is audited |
| B1 | Wasm scope guard | `RunPureLogicTool` accepts pure-compute payload; rejects with `WasmScopeError` on import of `os` / `subprocess` / `socket` |
| C1 | Budget hard kill | seed `accumulated_session_cost = 11.0`, `session_max_budget_usd = 10.0` → Supervisor halts; DLQ row exists; `security_flags` contains `SESSION_BUDGET_HARD_KILL` |
| C2 | Token-spike HITL | single LLM call with 70 000 tokens → HITL emitted even under sub-budget condition |
| D1 | OOM cascade happy path | mock LiteLLM raising `ContextWindowExceededError` → `oom_fallback_active = True`, cloud Haiku call succeeds, audit row written, `cloud_surgeon_invocations` NOT incremented |
| D2 | Double OOM → DLQ | both local and cloud raise → DLQ row written with `failed_node="oom_cascade"`, graph halts gracefully |
| E1 | Audit chain integrity | three HITL events in sequence → `chain_hash[i] == blake2b(chain_hash[i-1] ‖ …)` for each i |
| E2 | Audit tamper detection | manually UPDATE one historical row in the DB → next Supervisor pass raises `AuditChainBrokenError` |
| F1 | Secrets scrubber | log line containing `sk-ant-AAAAAAAAAAAAAAAAAAAA` → record reaches handler with `**REDACTED:<hash8>**`; same key → same hash8 across lines |
| G1 | DLQ + Resume | force-raise in `coder_agent` → DLQ row created; `POST /api/v1/task/resume/{task_id}` → graph resumes from L2 checkpoint and completes |
| G2 | Resume idempotency | second resume on already-completed `task_id` → 200 OK, `resumed: false`, no DB mutation |

**Static gates:** `mypy --strict ailienant-core/core/sandbox.py ailienant-core/core/audit.py ailienant-core/core/supervisor.py ailienant-core/core/dead_letter.py ailienant-core/shared/logging_filters.py` exit 0. `ruff check` exit 0.

**Regression:** full existing suite (496 tests at Phase 5.7 close) + Phase 6 additions; target ≥ 540 passing tests, **zero regressions**.

**Manual smoke (after pytest green):**
- `docker stop ailienant-sandbox-<session>` mid-run → next exec call promotes to HITL with a sandbox-degraded banner.
- Toggle MasterToggle to AUTO; deliberately overspend → confirm HITL still fires for `BUDGET_OVERFLOW` (AUTO does NOT bypass the FinOps gate — only PLAN bypasses WRITE/EXECUTE).
- Kill the FastAPI process mid-coder_agent turn. Restart. Confirm the sidebar offers "Resume Task". Click → graph resumes from the L2 checkpoint and the AnalystAgent renders the run with `dead_letter_episode_id` shown.
- `echo "API key sk-ant-XXXXXXXXXXXXXXXXXXXX is leaking" | logger -t ailienant` (or equivalent in-app log) → grep the log file: only `**REDACTED:<hash8>**` should be visible.

---

## 11. Roadmap Impact Analysis (Strategic Auditor checklist)

| Future Phase | Risk introduced by Phase 6 plan | Mitigation in this blueprint |
|---|---|---|
| Phase 7 (VS Code Extension) | Sidebar UI must surface 3 new states: sandbox tier badge, DLQ pending count, supervisor HITL flow. Schema bumps would break the WebView silently. | All three pieces of data land in the existing WebSocket startup + event channels (`ServerSandboxTierEvent` + reuse of `ServerHITLApprovalRequestEvent` with sentinel). Phase 6.10 test G1 asserts the round-trip. UI lib check at 7.x against this schema is mechanical. |
| Phase 8 (Pruebas, refinamiento) | Phase 8.2 promises LangSmith / OpenTelemetry traces. The Supervisor's deterministic decisions are tokens-free but invisible to tracing without instrumentation. | Supervisor emits one `telemetry.log_supervisor_decision(...)` row per pass (reuses Phase 2.23 telemetry table). Phase 8.2 can pull this directly without touching the Supervisor. |
| Phase 8.3 (Hardware fallbacks) | Phase 8.3.1's "Context OOM Predictor" was supposed to *predict* OOM and pre-empt; Phase 6.3 reactively *catches* it. Risk: the two layers fight. | Phase 6.3 emits `OOM_FALLBACK_ENGAGED:<reason>` security flag; Phase 8.3.1 reads it to calibrate its predictor (a real OOM is ground truth, the predictor learns). Layers are complementary, not redundant. |
| Phase 9 (Onboarding) | Phase 9.2's "Antena" panel needs to explain why the agent paused. New HITL sentinels (`BUDGET_OVERFLOW`, `SANDBOX_DEGRADED_EXEC`) need user-friendly copy. | `core/audit.py` exposes `audit_logger.tail(session_id, n)` returning the most-recent entries with `request_kind` already enum-stringed. Phase 9.2 renders the explainer copy keyed by `request_kind`. |
| Phase 10.1 (Dockerización completa) | Phase 10.1 wants the WHOLE backend in Docker. Phase 6.1's `DockerSandboxAdapter` runs containers FROM the backend — risks Docker-in-Docker if the backend itself is dockerised. | `DockerSandboxAdapter` uses `docker.from_env()` which respects `DOCKER_HOST`; Phase 10.1's compose file mounts `/var/run/docker.sock` into the backend container with `:ro` — DinD via socket-share, not nested daemons. Documented as a Phase 10.1 dependency. |
| Phase 10.2 (Zero-Friction Install) | The whole point of ADR-001. If Phase 6 silently required Docker, Phase 10.2 would have to retract the single-binary promise. | ADR-001 explicitly preserves the binary promise. The default-DOCKER → fallback-NATIVE_HITL flow means a fresh install on a machine without Docker still runs; the user is informed via the sidebar badge. |
| `SCHEMA_EVOLUTION.MD` | 6 new state channels + 2 new SQLite tables. | All channel additions are scalar overwrite with safe defaults; existing checkpoints deserialise unchanged. Tables are idempotent `CREATE TABLE IF NOT EXISTS`. Phase 5.7 regression test loads a frozen Phase 5.7 checkpoint and asserts deserialisation. |
| `core/permissions.py` | Phase 6 must not widen `ToolPrivilegeTier` (no new enum value for "SANDBOX_DEGRADED" or similar). | Tier values are untouched. Phase 6 routes through the existing `EXECUTE` and `DANGEROUS` tiers; the sandbox layer is one level **below** the permission decision, not parallel to it. |
| `mcp_adapter` (Phase 5.2) | MCP tools that run subprocesses on the host bypass the Sandbox. | Phase 6 documents this as an **acknowledged limit**: MCP servers are trusted by the user at registration time (their URI lives in env); the Sandbox protects the *agent's* subprocess intent, not the *MCP server's* internal behaviour. Phase 6.10 manual smoke calls out a "do not register untrusted MCP servers" warning that the Phase 7 UI must surface. |

---

## 12. Anti-patterns (do **not** do this)

- ❌ Re-implement a HITL channel in `core/safety.py`. Use `vfs_manager.request_human_approval`. (Avoids drift from Phase 1.4 / 2.27 contracts.)
- ❌ Add a new `ToolPrivilegeTier` enum value for "sandboxed" / "degraded". The sandbox layer is orthogonal to the permission decision.
- ❌ Modify `core/token_ledger.py` to write into state. The ledger is process-global; the **Supervisor** is the bridge.
- ❌ Apply the `dead_letter_decorator` to `verify_environment`. Too cheap; not worth the audit noise.
- ❌ Use `subprocess.run` / `os.system` anywhere. Phase 5.5 already banned these; Phase 6 reinforces the ban via the Sandbox dispatch.
- ❌ Commit secrets in cleartext to `hitl_audit_log`. The Scrubber runs **before** hashing — verify in test F1.
- ❌ Use `--amend` to fix a pre-commit hook failure during Phase 6 work (CLAUDE.md global rule). Create a new commit.
- ❌ Bypass the Supervisor under PLAN mode. PLAN mode blocks WRITE/EXECUTE at the permission layer (Phase 5.1) but the FinOps trigger still applies to any cloud call made by the Planner (Mini-Judge, ResearcherAgent).

---

## 13. Glossary (terms used throughout this document)

- **Adapter / Tier** — the three sandbox concretes (`DockerSandboxAdapter`, `WasmSandboxAdapter`, `NativeHITLSandboxAdapter`). "Tier" is the `Literal["DOCKER","WASM","NATIVE_HITL"]` selector.
- **Cascade** — the OOM rescue path: catch → release VRAM → summarise → re-emit to cloud Haiku.
- **Chain (audit)** — the blake2b linkage between consecutive `hitl_audit_log` rows.
- **Cloud Surgeon** — Phase 4.5 escalation for `error_streak ≥ 3`. Orthogonal to OOM Cascade.
- **DLQ** — Dead Letter Queue. `dead_letter_tasks` table.
- **Hard kill** — Supervisor decision to halt the graph at 1.10× of the session budget. Writes DLQ for resume continuity.
- **HITL channel** — `vfs_manager.request_human_approval(...)`. Canonical, reused.
- **Resume API** — `POST /api/v1/task/resume/{task_id}`.
- **Scrubber** — `SecretsScrubberFilter`, applied to logs and to audit payloads.
- **Session** — the WebSocket client_id; spans many tasks.
- **Soft gate** — Supervisor decision at 1.00× budget: HITL approval extends the ceiling.
- **Supervisor** — `core/supervisor.py::run_supervisor_node`. Deterministic node spliced between `finops_gate` and `apply_patch`.
- **Tier** — see Adapter.
- **Tier-active state** — `state["sandbox_tier_active"]`. Mirror of process-global `core.sandbox.ACTIVE_TIER`.

---

*End of Phase 6 Blueprint. The next compaction should still be able to re-derive Phase 6 intent from this single document.*
