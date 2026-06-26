# Agent System Prompts — Architecture Reference

> This document is the authoritative reference for what each agent node receives as its system prompt and how those prompts are composed. Prompt strings are defined in their respective agent modules — this file describes their content and intent, not their exact runtime value (which includes dynamic state injections). All agent modules live under `ailienant-core/agents/`.

---

## 1. Cognitive Topology

The LangGraph compiled graph contains two categories of nodes:

**Deterministic routing nodes (no LLM call):**
- `OrchestratorAgent` — advances the WBS, assigns roles, trips the circuit breaker
- `ContractGuardNode` — detects cognitive drift and emits persistent contract banners

**LLM-calling nodes (main graph):**
- `ResearcherAgent` — builds the Skeleton Map from GraphRAG
- `PlannerAgent` — converts the Skeleton Map + user intent into a `MissionSpecification`
- `CoderAgent` — mutates code under a dynamically injected role
- `ErrorCorrectionAgent` — surgically diagnoses and patches failed nodes

**Parallel / ephemeral LLM surfaces:**
- `AnalystAgent (Natt)` — Socratic chat copilot on a separate WebSocket channel
- `InlineEditAgent` — ephemeral Cmd+K region editor (no graph state)

**Specialist sub-graph nodes:**
- `MCTSCoderNode` — local/fix/surgeon sampling passes inside the MCTS loop

---

## 2. Deterministic Nodes (No LLM Call)

### 2.1 OrchestratorAgent

> **No system prompt. No LLM call. Pure Python routing logic.**

The OrchestratorAgent is a deterministic state machine. It:

1. Reads `mission_spec.tasks` and advances `current_step_id` to the next `pending` step.
2. Assigns `active_role` from the WBS step's `target_role` and selects `execution_mode` (`SEQUENTIAL` | `MICRO_SWARM` | `FULL_SWARM`) based on TCI and task parallelism.
3. Enforces `MAX_RETRIES = 2` per WBS step: on `error_streak >= MAX_RETRIES` it sets `circuit_breaker_tripped = True`, halting further coder dispatch.
4. Detects scope drift (CoderAgent attempting actions outside `current_step`'s `target_file`) and injects `HITL_APPROVAL_REQUIRED` state via `pending_hitl_request`.
5. Detects `healing_required = True` and routes to `ErrorCorrectionAgent`.

Source: `agents/orchestrator.py`. The node returns a state delta — it never calls an LLM.

### 2.2 ContractGuardNode

> **No system prompt. No LLM call. Three O(1) invariant checks.**

ContractGuardNode evaluates three invariants on every turn:

| Invariant | Condition | Notes |
|---|---|---|
| TCI Delta | `abs(state["tci"] - anchor["tci"]) > 15.0` | Requires a prior anchor |
| CSS at Capacity | `css < 40.0` AND `token_utilisation >= 0.80` | Can fire on first turn |
| Domain Shift | `active_role != anchor["target_role"]` | Requires a prior anchor |

On any trigger, it writes `ui_payload` with `action = "RENDER_PERSISTENT_CONTRACT"` — a `SessionContract` built deterministically from `mission_spec`. A network or parse failure falls back to a skeleton from `mission_spec.outcome / .scope / .constraints`. The banner always renders if the trigger fired.

Source: `agents/contract_guard.py`.

---

## 3. LLM Agents

### 3.1 ResearcherAgent

**Prompt source:** `_SKELETON_INSTRUCTION` in `agents/researcher.py`

**Directive (excerpt):**
> "You are the ResearcherAgent. Output a structured Skeleton Map for the PlannerAgent. List: (a) relevant files, (b) public function/class signatures, (c) cross-module relationships, and (d) topological warnings the Planner should anticipate."

**Permission mode:** `ReadOnly` — no write tools available.

**Authorized tools:** `FileReadTool`, `GrepTool`, `GlobTool`, `query_graphrag`, `DocumentParserTool`

**I/O contract:**
- Input: `user_input`, `workspace_root`, `project_id`
- Output: Markdown Skeleton Map → written to `researcher_skeleton` state channel

**Key constraint:** Never extract full files if a `GrepTool` call can return the function signature. Token efficiency is the primary objective.

---

### 3.2 PlannerAgent

**Prompt source:** Built dynamically in `agents/planner.py` from two directives injected based on state:

**`_SCOPE_DISCIPLINE_DIRECTIVE`:** Only propose edits to files explicitly named in the user request or in `researcher_skeleton`. Editing files not in scope is a contract violation.

**`_WBS_SEED_DIRECTIVE`:** If the user supplies a task list, treat it as an immutable seed. Translate it to `MissionSpecification` format; inject missing infrastructure steps (e.g. dependency installs) silently as step 0, but never alter the business logic steps.

**Planning strategies (injected from `planner_mode_active` / `hitl_response` state):**

| Strategy | Behavior |
|---|---|
| `autonomous` | Planner designs from scratch based on `researcher_skeleton` + `user_input` |
| `manual_override` | Planner acts as a strict WBS compiler from `hitl_response` (human-supplied task list). Logic steps are immutable. |
| `collaborative` | Socratic Grill Me loop (see AnalystAgent §3.4). Planner waits until `shared_understanding_reached = True`, then generates the spec. |

**Permission mode:** `PLAN` — all write and execute tools blocked.

**I/O contract:**
- Input: `user_input`, `researcher_skeleton`, `planner_mode_active`, `hitl_response`
- Output: `MissionSpecification` JSON → `mission_spec` state channel

---

### 3.3 CoderAgent (8 Dynamic Roles)

**Prompt source:** `build_coder_system_prompt(role)` in `agents/coder.py`, composing `BASE_SYSTEM_PROMPT` + role directive from `agents/roles.py`.

**Base prompt (`agents/prompts.py::BASE_SYSTEM_PROMPT`, paraphrased):**
> "You are the CoderAgent. You produce concrete code changes for the active WBS step. Read files before writing. Emit SEARCH/REPLACE blocks when patching. Honor the role-specific rules below."

**All 8 roles (from `agents/roles.py::ROLE_REGISTRY`):**

| Role | System prompt snippet | HITL triggers | Key tool constraint |
|---|---|---|---|
| `core_dev` | "Implement business logic. Prefer existing utilities. No abstractions for hypothetical futures." | — | — |
| `architect_refactor` | "SOLID enforced. MUST use BatchEditTool. Rewriting whole files is a contract violation." | — | `BatchEditTool` mandatory |
| `devops_infra` | "Docker/CI/Bash work. Any `sudo` or `.env` mutation pauses for HITL approval before applying." | `.env`, `sudo ` | `BashTool` available |
| `secops` | "OWASP Top-10 enforced. Run Bandit/Semgrep after every patch. Quote CVE IDs when relevant." | — | — |
| `qa_tester` | "Write tests first. NEVER mark step complete without pytest exit code 0. Always read `stderr` before emitting a patch." | — | `BashTool` for test runs |
| `doc_manager` | "JSDoc, docstrings, and `.md` files ONLY. BashTool disabled. Never touch logic." | — | `BashTool` disabled |
| `vcs_manager` | "Git operations only. Conventional Commits format. Never use `--force` without explicit HITL approval." | `--force` | `BashTool` for git only |
| `data_ml_engineer` | "Tensors, pipelines, analytics. Validate dataframe shapes before any write." | — | `BashTool` available |

**LANGUAGE_MIRROR_DIRECTIVE** (inherited by all roles, defined in `agents/roles.py`):
> "LANGUAGE: Mirror the language of the user's request. Write all prose, explanations, identifiers, comments, and docstrings in that same language."

**Permission mode:** `EDIT_EXECUTE_RBW` — write and execute tools available, subject to RBWE (see §5.4).

---

### 3.4 AnalystAgent (Natt)

The AnalystAgent operates on a **separate WebSocket channel** (`client_analyst_query` → `server_natt_*`), completely independent of the main graph execution. It is AILIENANT's Socratic chat copilot, not a graph node.

**Two distinct LLM surfaces (both in `agents/analyst.py`):**

**`_GRILL_DIRECTIVE` — Socratic Planning Session:**
> "You are running a Socratic 'Grill Me' planning session. Ask EXACTLY ONE focused question this turn to expose hidden constraints or risks in the user's plan. Always end with a concrete recommended default answer the user can accept if they have no strong preference."

Used when the main graph enters `collaborative` planning strategy and `planner_mode_active = True`.

**`_INTENT_SYSTEM_PROMPT` — Pre-Dream Reflection:**
> "You are an AnalystAgent performing Pre-Dream Reflection. Based on the user's recent message and workspace context, produce ONE sentence (≤30 words) summarising the user's primary coding intent."

Used before triggering Manual Dreaming (`client_dreaming_run`) to anchor the memory consolidation pass to the current user goal.

**Context assembly:**

`assemble_analyst_context()` routes its sources onto the shared five-layer `ContextPipeline`
via `build_agent_context` (`brain/context_pipeline.py`, `brain/agent_context.py`):
- **Foundation** — the AILIENANT Codex self-knowledge slice (pinned).
- **Project** — the README digest (`core/readme_digest.py`, 5 KB cap, debounced invalidation)
  and GraphRAG code snippets (`SemanticMemoryManager.search_snippets()`); pinned, dropped
  wholesale only when the pinned layers exhaust the window.
- **Execution** — the indexed product docs (`core/memory/docs_index.py`: `HowItWorks.md`,
  `HowToUseIt.md`, `README.md`) and the active file(s); tail-truncated first under pressure,
  with the G3 sandbox boundary repaired if a file block is cut.

Model tier is user-selectable from Natt HUD (`analystTier` in `workspaceStore.ts`); the
per-tier budget (1500–8000 tokens, `_ANALYST_BUDGET_BY_TIER`) is passed as the pipeline's
`total_token_budget`.

**WS protocol:** `client_analyst_query` → `server_natt_message` (full response) | `server_natt_token` (streaming delta) | `server_natt_stream_end`

---

### 3.5 ErrorCorrectionAgent

**Prompt source:** `ERROR_CORRECTION_SYSTEM_PROMPT` in `agents/prompts.py`

**Full directive:**
> "You are a surgical error-correction engine. A node in an autonomous coding graph raised an exception. Your job is to diagnose the root cause and propose a minimal patch. Respond ONLY with a JSON object: `{\"diagnosis\": \"...\", \"filepath\": \"...\", \"new_content\": \"...\"}`."

**No persona. No empathy. Pure diagnostician.** If the error cannot be fixed by a file patch, `filepath` is `null` and `new_content` is an explanation for the Orchestrator to surface as a fatal failure.

**Trigger conditions (from `agents/orchestrator.py`):**
- `healing_required = True`
- `last_error_trace` is populated
- `correction_attempts < MAX_CORRECTION_ATTEMPTS`

**Output:** JSON parsed by `reflexion_guard` in `core/dead_letter.py`. On success, the corrected file is applied via VFS and the failed node is retried.

Source: `agents/error_correction.py`, `agents/prompts.py`.

---

### 3.6 InlineEditAgent

**Prompt source:** `_INLINE_EDIT_SYSTEM_PROMPT` in `agents/inline_edit.py`

**Full directive:**
> "You are an inline code editor invoked by Cmd+K. The user has selected a region and provided an instruction. Output ONLY the replacement text for that region — no markdown fences, no commentary, no explanation. The output is inserted verbatim into the editor."

**Characteristics:**
- **Ephemeral** — operates with no `AIlienantGraphState` (no graph context).
- Input: selected code region + user instruction + surrounding file context (via `<file_content>` boundary).
- Output: raw replacement text — not a SEARCH/REPLACE block, not JSON.

**WS protocol:** `client_inline_edit_request` → `server_inline_edit_start` + `server_inline_edit_delta` (×N streaming) + `server_inline_edit_end`

Cancellation: `client_inline_edit_cancel` aborts the stream mid-flight.

---

## 4. Specialist Nodes

### 4.1 MCTSCoderNode (Local / Fix / Surgeon)

The MCTSCoderNode operates as a **sub-graph** inside the MCTS sampling loop. It has three distinct LLM surfaces, each triggering at a different failure tier.

**Source:** `agents/mcts_coder.py`

**`_LOCAL_GENERATE_SYSTEM` — Pass@1 Candidate Generation:**
> "You are a code generator. Output ONLY the code, no prose, no markdown fences."

Used for local model fast-path candidate generation. Goal: pass@1 (first attempt, no iteration).

**`_LOCAL_FIX_SYSTEM` — Local Fix Pass:**
> "You are a code-fixer. You will receive a snippet and a static-analysis error. Output ONLY the corrected code."

Triggered when the local model's output fails AST validation or linter check. Remains on the local model.

**`_SURGEON_SYSTEM` — Cloud Escalation:**
> "You are an expert code surgeon. The local model has failed 3 consecutive times on this snippet. Output ONLY the corrected code."

Triggered when `consecutive_style_failures >= 3`. Escalates to the cloud flagship model. Tracked by `cloud_surgeon_invocations` state channel.

### 4.2 LogicAgent

A thin dispatch wrapper in `agents/logic.py` that delegates to `run_coder_node`. It has no independent system prompt — it inherits the role-based prompt from the CoderAgent surface. Exists to satisfy LangGraph node naming requirements for the sequential execution path.

---

## 5. Cross-Cutting Directives

These directives apply across multiple or all agent nodes.

### 5.1 Language Mirror

**Source:** `LANGUAGE_MIRROR_DIRECTIVE` in `agents/roles.py`, inherited by all 8 CoderAgent roles.

> "LANGUAGE: Mirror the language of the user's request. Write all prose, explanations, identifiers, comments, and docstrings in that same language."

This means: if the user writes in Spanish, the CoderAgent writes Spanish comments and docstrings. If the user writes in English, English. The directive does not apply to the agent's structural output (SEARCH/REPLACE block delimiters, JSON keys, etc.), which remain in English.

### 5.2 XML File-Content Boundary

All VFS file reads injected into agent context are wrapped in the XML boundary delimiter:

```xml
<file_content path="relative/path/to/file.py">
... file contents ...
</file_content>
```

**Purpose:** Protects against prompt injection from untrusted file content. An LLM receiving a maliciously crafted source file that contains "Ignore previous instructions" inside a `<file_content>` tag treats that text as data, not as instructions. This is the primary injection defense for the CoderAgent.

Source: `core/vfs_middleware.make_safe_reader`.

### 5.3 SEARCH/REPLACE Edit Block Format

The CoderAgent's primary mutation protocol. No POSIX diff. No line numbers.

```
<<<<<<< SEARCH
[exact existing text to match in the target file]
=======
[replacement text]
>>>>>>> REPLACE
```

**Rules:**
- The SEARCH block must match exactly (after fuzzy normalization) a contiguous region in the target file.
- The REPLACE block is inserted verbatim.
- Multiple SEARCH/REPLACE blocks may appear in one response for multi-site edits within a single file.
- `apply_patch_to_vfs` (`tools/patch_tool.py`) parses these blocks, applies fuzzy matching, then validates the result with Python AST before committing to VFS. A syntactically invalid patch raises `PatchError` and is rejected without writing to disk.

### 5.4 Read-Before-Write Enforcement (RBWE)

`rbwe_guard` in `core/permissions.py` raises `PermissionDeniedError` if the CoderAgent attempts to write a file that has not been read into `read_files_state` during the current session.

Enforcement point: the graph's **Orchestrator node boundary**, upstream of `CoderAgent._arun`. This means the guard runs at the node level, not inside the tool's `_arun` method — it reflects production reality where LangGraph dispatches the guard before the tool runs.

`session_permission_mode` controls the guard's behavior:
- `DEFAULT`: full RBWE — every write requires a prior read.
- `PLAN`: no writes permitted at all (`session_permission_mode == PLAN` blocks the coder node entirely).
- `AUTO`: RBWE active but HITL for DANGEROUS-tier tools is bypassed for the session (External Gateway pre-authorized mode).

### 5.5 Cognitive Isolation Fence (ISO1)

Agents under `agents/` — specifically `coder.py`, `planner.py`, `researcher.py`, `orchestrator.py`, and `error_correction.py` — **MUST NOT** import `brain.personality`.

**Rationale:** `brain.personality` contains the AILIENANT product persona. Importing it inside a cognitive agent leaks product framing into code-generation prompts, degrading output quality and making the agent self-referential. The Analyst surface is exempt (it is the product persona by design).

**Enforcement:** Asserted as ISO1 row in `tests/test_phase7_13_checkpoint_gate.py`. Violation fails the phase gate.

### 5.6 HITL Escalation Pattern

`AskUserQuestionTool` (`tools/control_tools.py`) is tiered `READ_ONLY` so it is available in every `session_permission_mode`. It:

1. Populates `state["pending_hitl_request"]` with `{request_id, kind, question, context, suggested_options, requested_at}`.
2. Returns the sentinel string `[ask_user_question] HITL_PENDING:{request_id}`.
3. The OrchestratorAgent detects the populated `pending_hitl_request` channel on its next dispatch and suspends the turn.
4. The VS Code extension emits `server_hitl_approval_request`; the user responds via `client_hitl_response`.
5. On response, `pending_hitl_request` is cleared and the OrchestratorAgent resumes.

**Pre-interception (bash-level):** `DANGEROUS_COMMANDS_REGEX` in `tools/execution_tools.py` intercepts `SandboxBashTool` calls before `asyncio.create_subprocess_shell`. Any match blocks the subprocess spawn and returns a sentinel string advising the CoderAgent to call `AskUserQuestionTool`. This is a defense-in-depth layer below the permission engine.
