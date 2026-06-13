# Tech Debt Backlog — Continuous Registry Protocol

## The Rule

If you discover a strict-mode error, vulnerability, or typing debt **outside the scope of the
current ticket/subfase**, you MUST:

1. **STOP** — do NOT fix it in-place.
2. **ADD an entry** to this file using the format below (reproduction command + file + context).
3. **CONTINUE** with the current task.

This ensures every fix is atomic, auditable, and in the correct topological order. In-place fixes
of out-of-scope debt create invisible changes that break reviewers' ability to verify the diff.

---

## Entry Format

```
### DEBT-NNN — Short description
- **Date:** YYYY-MM-DD
- **Reproduce:** exact shell command that surfaces the error
- **File(s):** affected path(s) and line numbers if known
- **Error:** mypy error code or free-text description
- **Blocked by:** external dependency / phase prerequisite (if any)
- **Phase:** which Phase 8 subfase will address this
- **Notes:** context for future reader
```

---

## Open Entries

### DEBT-037 — G2 retrieval isolation uses mock.patch, not a production DI seam

- **Date:** 2026-06-12
- **Reproduce:** inspect `tests/benchmark/strategies.py:VectorOnlyRetrievalStrategy.patches()` — it returns `[mock.patch(GRAPH_SEAM, _no_graph)]`, a process-global monkey-patch scoped to one task run.
- **File(s):** `ailienant-core/tests/benchmark/strategies.py`, `ailienant-core/tests/benchmark/arms.py`.
- **Error:** not a runtime defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The patch-based mechanism is the harness-boundary approach established at 8.3.0; no production code is changed. The enterprise alternative is a `RetrievalStrategy` injected via a DI seam in `GraphRAGDynamicExtractor` so the strategy boundary is visible in production profiling and A/B testing, not only in benchmark runs.
- **Blocked by:** requires a production DI refactor of `GraphRAGDynamicExtractor` and its callers (planner + researcher). Out of scope for the 8.3.x measurement track.
- **Phase:** standalone retrieval-DI slice, post-8.5/8.8 when the ablation sweep is complete and the architecture is stable.
- **Notes:** logged at 8.3.3 ship per CLAUDE.md §7.3. The patch-based mechanism is hermetically verified by `test_ablation_verdicts.py` gate tests.

### DEBT-036 — BenchmarkOracle executes candidate patches on the host (no sandbox isolation)

- **Date:** 2026-06-12
- **Reproduce:** call `BenchmarkOracle.run_oracle(problem, candidate_patch)` with live LLM output — the patched files are written to a host `TemporaryDirectory` and run via `SubprocessPythonExecutor` (inherits the parent process environment).
- **File(s):** `ailienant-core/tests/benchmark/oracle.py` (`BenchmarkOracle.run_oracle`, `SubprocessPythonExecutor`).
- **Error:** not a runtime defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The hermetic gate uses trusted golden/wrong fixtures; the AST pre-flight (`_check_patch_safety`) limits the blast radius for this MVP path. A fully isolated oracle (Docker sandbox + read-only corpus mount) is the Enterprise target.
- **Blocked by:** requires a sandbox tier that allows writing the corpus snapshot into a container-local temp dir (the current `SandboxCodegenExecutor` writes to the Docker ro mount's host side, which is sufficient for codegen but not for multi-file oracle isolation).
- **Phase:** standalone benchmark-runtime hardening slice (before the definitive ablation sweep, post-8.5/8.8 when the system is feature-complete).
- **Notes:** logged at 8.3.2 ship per CLAUDE.md §7.3. AST pre-flight (`_BLOCKED_IMPORTS` + Level-1 reflexivity blocklist) is the in-place mitigation.

### DEBT-035 — MultiPL-E TypeScript execution needs a Node-capable sandbox runtime

- **Date:** 2026-06-12
- **Reproduce:** run a TypeScript codegen problem through `SandboxCodegenExecutor.run(program, Language.TYPESCRIPT, …)` — it returns `ExecOutcome(passed=False, exit_code=-2, stderr="[unsupported_runtime: ...]")` instead of executing.
- **File(s):** `ailienant-core/tests/benchmark/executors.py` (`SandboxCodegenExecutor`); `ailienant-core/core/sandbox.py` (`_DOCKERFILE_TEXT`, `python:3.13-slim`).
- **Error:** not a defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The shared sandbox image is Python-only (no Node/tsc), so MultiPL-E TS cannot be executed in-container. 8.3.1 ships the full TS *adapter* (loader, prompt, extraction, assembly, Pass@1 wiring); only the TS *execution backend* is deferred. Python (HumanEval) Pass@1 is real.
- **Blocked by:** nothing technical — needs a Node-capable sandbox tier (extend the image with Node/tsx, or a vetted host-Node executor behind explicit opt-in) without compromising the locked Docker security profile.
- **Phase:** a standalone benchmark-runtime slice (before the definitive cross-language run, post-8.5/8.8).
- **Notes:** logged at 8.3.1 ship per CLAUDE.md §7.3. Until then TS Pass@1 is `unsupported_runtime`; the 8.3.1 DoD is met on the Python subset.

### DEBT-034 — Gateway project_id hashing is path-format-fragile (no normalization)

- **Date:** 2026-06-12
- **Reproduce:** call a gateway READ_ONLY verb (`query_memory`/`get_dependents`/`get_workspace_graph`) with a `workspace_root` whose casing/separators/trailing-slash differ from the exact path VS Code opened the folder under — e.g. `c:\projects\app\` vs `C:\Projects\app`. The derived `project_id = sha256(workspace_root)` mismatches the indexed key and the verb returns empty results.
- **File(s):** `ailienant-core/gateway/handlers.py` (`project_id_for`); `ailienant-extension/src/core/PathResolver.ts` (`computeProjectId`).
- **Error:** not a defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The gateway intentionally mirrors the extension's *raw* `sha256(uri.fsPath)` so it hits the existing on-disk LanceDB/sqlite keys. Normalizing only in the gateway would diverge and orphan that data, so it was rejected.
- **Blocked by:** nothing technical, but the fix is **cross-cutting**: it must apply `os.path.normcase(os.path.normpath(...))` in BOTH `project_id_for` and the extension's `computeProjectId` simultaneously, and it re-keys every existing index (the lazy indexer rebuilds them on next workspace open).
- **Phase:** a standalone coordinated-normalization slice (extension + core, with a one-time re-index).
- **Notes:** logged at 8.5.4 ship per CLAUDE.md §7.3. Until then the contract is "the caller passes the exact `uri.fsPath`"; documented in the 8.5.4 manifest row.

### DEBT-032 — Coder-side skill injection (planner-only shipped in 8.4.5)

- **Date:** 2026-06-11
- **Reproduce:** submit a task with a saved skill active — the skill directive block appears in the planner system prompt (and therefore in the `mission_spec` the coder receives) but is **not** re-injected into the coder's own system prompt. For skills that express a coding style or pattern constraint, the planner-mediated injection is sufficient; for skills that must survive across multi-step coder turns or whose instruction is structurally important to coder generation, a coder-side injection would be more robust.
- **File(s):** `ailienant-core/agents/planner.py` (injection seam, 8.4.5) — the coder (`agents/coder.py`) has no equivalent injection.
- **Error:** not a defect — a **declared MVP trade-off (CLAUDE.md §7.2)**. The planner bakes skill directives into the `mission_spec` that the coder receives, so one seam shapes the whole task with zero TypedDict churn and the lowest blast radius for 8.4.5.
- **Blocked by:** nothing; requires reading `state.get("active_skills")` in the coder's system-prompt construction and calling `build_skill_directive_block` there (same seam pattern as the planner).
- **Phase:** a standalone coder-side injection slice (8.4.x or a bundled polish pass).
- **Notes:** logged at 8.4.5 ship as required by CLAUDE.md §7.3 (every MVP must surface a tracked follow-up). The planner-only path covers the vast majority of skill-directive use cases.

### DEBT-033 — config.json ↔ MCP secret-store `key_ref` round-trip (fresh-machine import prompt)

- **Date:** 2026-06-11
- **Reproduce:** export `.ailienant/config.json` on a machine with installed credentialed servers, then import it on a fresh machine — the server rows reconcile but their secrets do not travel (correctly: secrets never enter the JSON), and there is no flow that re-prompts for the missing credential. The dashboard also has no import/export buttons surfacing `core/mcp_config.py`.
- **Error:** not a defect — a deferred polish slice. 8.4.6 landed the secret VALUE store + connect-time env injection (closing the load-bearing half of the former DEBT-031), and `core/mcp_config.py` already redacts on export + emits `key_ref` placeholders. What remains is wiring the placeholder round-trip to the new `mcp_secrets` store: on import, detect a `key_ref` whose value is absent locally and prompt for it; expose import/export in the dashboard.
- **Blocked by:** nothing.
- **Phase:** a standalone config-portability slice (8.4.x or a later polish pass).
- **Notes:** logged at 8.4.6 ship per CLAUDE.md §7.3. Export already prevents credential leakage (userinfo redaction + no secret in JSON), so this is a usability gap, not a security one. Substrate is the backend-mask store (`core/config/mcp_secrets.py`), consistent with the ADR-757 amendment.

### DEBT-031 — MCP secret-value store + connect-time env injection + config.json file/UI surface — ✅ RESOLVED (8.4.6, load-bearing half)

- **Date:** 2026-06-10
- **Reproduce:** inspect the `mcp_servers` schema (`core/db.py`) — there is no secret/env column; a server's credential (e.g. a Postgres connection string) can only be typed inline into the `uri`. There is no mechanism to inject a secret as an environment variable into the spawned MCP process at connect time, and no on-disk `.ailienant/config.json` reader/writer or import/export UI.
- **Error:** not a defect — a deliberately deferred slice. 8.4.3 shipped the backend config projection (`core/mcp_config.py`: export with credential redaction + `key_ref` placeholders, idempotent name-keyed import) but **the secret VALUE has nowhere to live and is never injected at connect**. The `config.json` `key_ref` convention and the `needs_secret` import signal are in place, waiting for the store + injection + UI.
- **Blocked by:** nothing functionally; naturally pairs with the connect/dispatch wiring (8.4.4) and the registry UX (8.4.6).
- **Phase:** secret store + env injection → **8.4.6** (connect-time); the config.json file write + dashboard import/export buttons + fresh-machine secret prompt re-tracked as **DEBT-033**.
- **Resolution (8.4.6, 2026-06-11):** new `core/config/mcp_secrets.py` — a backend-masked secret store (`mcp_secrets.json`, atomic + `0600` + UTF-8, mirroring BYOM) keyed by server name, with mask-on-read and masked-resubmit guard. `tools/mcp_adapter._build_stdio_params` injects a server's stored secrets as process `env` at connect time, merged on top of the SDK's `get_default_environment()` (platform-critical vars inherited, full host env NOT leaked to the child). `POST /api/v1/mcp/registry/install` collects + validates secrets; `DELETE` wipes them. `mcp_secrets.json` added to `.gitignore`. Proof: `tests/test_mcp_secrets.py` (7) + install/secret cases in `test_command_menu_config.py` + env-injection cases in `test_mcp_handshake.py`; `mypy .` 0/280, full pytest green. **Remaining (DEBT-033):** the config.json `key_ref` round-trip + import/export UI.
- **Notes:** per the ADR-757 amendment (`docs/PHASE_8_BENCHMARK_MCP_BLUEPRINT.md`), the secret-value substrate is the codebase-consistent backend-mask (BYOM `byom_config.json` `0600` + mask-on-read), **not** VS Code SecretStorage. The `key_ref` placeholder convention is preserved end-to-end.

### DEBT-030 — BYOM dashboard has no Google preset + `_ensure_v1` mangles native cloud endpoints — ✅ RESOLVED (8.4.8)

- **Date:** 2026-06-10
- **Reproduce:** dashboard → BYOM → add endpoint: the provider dropdown offered only `ollama/lmstudio/vllm/openai/openrouter/anthropic/custom` — no Google. Putting Google's OpenAI-compat endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`) under `custom` triggered `api/byom.py::_ensure_v1`, appending `/v1` → `…/v1beta/openai/v1` (invalid).
- **File(s):** `ailienant-core/api/byom.py` (`_ensure_v1`, `_connection_for_provider`, `_normalize_chat_model`, `_KNOWN_PROVIDERS`), `core/config/byom_config.py` (`Provider` Literal), `ailienant-extension/src/dashboard/panels/BYOMPanel.tsx` (`PROVIDER_DEFAULTS` + hardcoded dropdown). Per-provider knowledge was scattered across 6 sites.
- **Error:** UX + correctness gap — users with a Google AI Studio key (or any Chinese-provider key) could not configure a cloud tier from the UI; also blocked the Division 8.3 benchmark cloud tier.
- **Blocked by:** none.
- **Phase:** **8.4.8** (closes this debt).
- **Resolution (8.4.8, 2026-06-10):** new `core/config/provider_registry.py` (single source of truth: routing/base-url/env-key/hints per provider). `api/byom.py` resolvers consult it; native cloud providers (Gemini/DeepSeek/Mistral) route via litellm prefix with **no api_base** (bypassing `_ensure_v1`), OpenAI-compatible providers (Qwen/Moonshot/Zhipu) use the registry base verbatim, generic `custom` keeps `_ensure_v1`. New `GET /api/v1/byom/providers` (no secrets) drives a data-driven dashboard (hide base URL for cloud, "get your key" link). 6 providers added. **Follow-up (8.4.9, 2026-06-10):** real defects surfaced in field use were fixed — re-test 404 (masked key not restored in `/test`; now restored by `endpoint_id`), OpenRouter test double-`/v1` 404 (missing `test_models_url`; OpenRouter switched to native `openrouter/` routing), tested cloud models never persisted (now cached per-endpoint in `BYOMConfig.model_cache` and surfaced in the pool), and `suggested_models` removed from the registry (the system carries no model preferences). Added a low-token `POST /ping` health check. Proof: `tests/test_provider_registry.py` (12) + `tests/test_byom_model_cache.py` (8); `mypy .` 0/267, `pyright` 0, `npm run compile` 0, 35 BYOM tests unregressed. **Note:** API keys remain plaintext-`0600` + masked — the `key_ref`→VS Code SecretStorage migration stays scoped to ADR-757 / Division 8.4.3.

### DEBT-026 — 🔴 SECURITY: MCP-discovered tools hardcoded to READ_ONLY (privilege fail-open) — ✅ RESOLVED (8.4.1)

- **Date:** 2026-06-10
- **Reproduce:** inspect `ailienant-core/tools/mcp_adapter.py:344` — every tool harvested from an external MCP server is registered with `privilege_tier=ToolPrivilegeTier.READ_ONLY`, unconditionally.
- **File(s):** `ailienant-core/tools/mcp_adapter.py:344` (the hardcoded tier at registration).
- **Error:** not a type error — a **fail-open security hole**. A mutating external tool (e.g. `github.create_pull_request`, `github.merge_pull_request`, `docker.run`) is classified READ_ONLY, so `evaluate_action`/`rbwe_guard` grant it ALLOW and the Asymmetric Friction HITL gate **never fires**. The permission engine itself (`core/permissions.py`) is sound; only the classification at registration is wrong.
- **Blocked by:** nothing — fixable now; the fix is `classify_tool_privilege(tool_name, description, server_name)` with precedence curated-catalog > verb-heuristic > DANGEROUS default (fail-closed).
- **Phase:** **8.4.1** (closes this debt). Also the LOCK anchor for every EXECUTE-tier tool in División 8.5.
- **Resolution (8.4.1, 2026-06-10):** `classify_tool_privilege(tool_name, description, server_name)` added to `core/permissions.py` (precedence catalog > verb heuristic > DANGEROUS; description elevates only via `max` severity, never downgrades; whole-token match with a camelCase-aware tokenizer). The `mcp_adapter.py` hardcode now calls it. Curated catalog ships as an empty, load-bearing seam (populated by 8.4.2). Proof: `test_mcp_handshake.py` now asserts `RemotePing → DANGEROUS` (no read verb → fail-closed) where it previously defaulted READ_ONLY; new `tests/test_classify_tool_privilege.py` (24 cases). `mypy .` 0/264, full pytest 1063 passed. **Deferred to DEBT-029:** the dispatch-time guard + session "trust-once" valve (the HITL *firing* path).
- **Notes:** highest-urgency entry in this backlog — a known, live fail-open at a security boundary. Registered today regardless of close date per the Continuous Registry Protocol. Verb map + catalog overrides specified in ADR-757 (`docs/PHASE_8_BENCHMARK_MCP_BLUEPRINT.md`).

### DEBT-029 — MCP tool dispatch consults no permission guard + no session "trust-once" valve ✅ RESOLVED (8.4.7)

- **Date:** 2026-06-10
- **Resolution (8.4.7, 2026-06-11):** `McpToolAdapter._arun` now resolves session context from ambient `ContextVar`s set by `task_service` before the LangGraph run, so the gate fires for every LangChain-orchestrated call without requiring explicit kwarg injection. Trust-once valve: `_session_trust` dict (module-level, per `(session_id, tool_name)`) records HITL approvals; subsequent calls to the same tool in the same task session skip the prompt. Default approval channel: when `request_approval` is None but a session is known, `_arun` lazily imports `vfs_manager.request_human_approval` (same pattern as sandbox.py). Context reset + trust clear in `task_service` `finally` block. FE: `request_kind="MCP_TOOL_CALL"` added to `WARNING_KINDS` in `hitlNotifier.ts`; `HITLIntervention` interface gains `request_kind` so the card renders an MCP-specific header and trust-session hint. 15 dispatch-guard tests green, mypy 0/280.
- **Blocked by:** none (8.4.1 classification is the prerequisite and is now done).
- **Phase:** **8.4.4** (dispatch guard wiring, alongside auto-connect) / **8.4.7** (the gate DoD: "HITL fires on a WRITE tool"). The session valve lands with the dispatch wiring.
- **Notes:** carved out of 8.4.1 by explicit scope decision — 8.4.1's DoD is classification-only. The permission engine and `request_human_approval` infrastructure already exist; this is integration, not a rebuild.

### DEBT-027 — MCP servers testable but not auto-connected at task launch

- **Date:** 2026-06-10
- **Reproduce:** `POST /api/v1/mcp/test` probes a server successfully, but starting a new task does not open sessions to `enabled` servers — their tools are absent from the task's `ToolRAGStore` selection.
- **File(s):** `ailienant-core/api/mcp_servers.py` (test endpoint exists, no auto-connect hook); `ailienant-core/tools/mcp_adapter.py::bootstrap_mcp_session` (not invoked at task launch); `ailienant-core/core/task_service.py` (task entry, no MCP bootstrap pass).
- **Error:** coverage/wiring gap — a configured-and-enabled MCP server contributes no tools until manually bootstrapped. The user configures a server, expects its tools live, gets nothing.
- **Blocked by:** none.
- **Phase:** **8.4.4** (closes this debt).
- **Notes:** surfaced during the Phase 8 MCP-ecosystem exploration. The bootstrap function is complete; only the auto-invocation at task start is missing.

### DEBT-028 — Hooks persisted but never executed *(skills half ✅ RESOLVED — 8.4.5)*

- **Date:** 2026-06-10
- **Reproduce:** create a `pre_patch`/`post_patch` hook via `POST /api/v1/hooks`; it is stored in the catalog DB (`hooks` table) and listed back, but is never run around task mutations.
- **File(s):** `ailienant-core/core/db.py` (hooks table + CRUD — storage only); `ailienant-core/api/customizers.py` (~line 215 — hooks saved, not executed); no execution wiring in `core/task_service.py` or `tools/execution_tools.py`.
- **Error:** wiring gap — the persistence + UI half exists; the hooks runtime application half does not.
- **Blocked by:** none.
- **Phase:** dedicated hooks-execution sub-phase (8.4.x or standalone).
- **Notes:** skills half closed by **8.4.5** (dual-mode resolver + planner injection + frontend chip). Hooks deferred at user request; scope confirmed as `pre_patch`/`post_patch` command execution. Complements DEBT-027 (both are "configured-but-inert" gaps).

### DEBT-025 — Docker persistent-PTY backend has no daemon integration test

- **Date:** 2026-06-09
- **Files:**
  - `ailienant-core/core/sandbox.py` — `_DockerPtyBackend` (`exec_create`/`exec_start(socket=True, tty=True)` persistent shell) and `DockerSandboxAdapter.open_session`.
- **Error:** not a type error — a coverage gap. The directed suite (`tests/test_phase7_19_0_pty_session.py`) verifies the session contract through a stub backend and the real Unix `openpty` backend (Unix-only, skipped on Windows). The **Docker** session backend's real exec-socket framing (raw-stream `tty=True` semantics, socket detach on container stop, `exec_inspect` exit-code reap) is exercised only structurally via the shared `_PtySession` machinery — no test attaches to a live `ailienant-sandbox-daemon` container.
- **Blocked by:** a Docker daemon in CI (the broader sandbox-integration gap; `test_execution_tools` Docker failures are already environmental per project notes).
- **Phase:** the Phase 7.19 Docker-session integration pass (or whichever sub-phase wires the dispatcher onto a live container), once a daemon-backed CI lane exists.
- **Notes:** declared MVP during 7.19.0. The host PTY path (Native Direct) — the tier the 7.19.2 dispatcher will actually drive first — is fully covered (stub + real openpty). The Docker backend is implemented for parity but unverified end-to-end against a container; treat its first live use as integration-test-gated.

### DEBT-024 — HITL inline-diff transport ships full file content (O(N)) instead of a unified diff (O(Δ))

- **Date:** 2026-06-08
- **Files:**
  - `ailienant-core/api/ws_contracts.py` — `ProposedFile.new_content` (full post-edit content).
  - `ailienant-core/core/task_service.py` — HITL branch builds `proposed_files` from `pending_contents`.
  - `ailienant-extension/src/core/PatchActuator.ts` — `preview()` / `apply()` build `PatchedFileDiff` with full `old_content` + `new_content`.
- **Error:** not a type error — a space/latency trade-off. The pre-apply inline-diff approval (and the existing post-apply `RENDER_DIFF`) transport **full file content per file** over the WebSocket. For a large file this is O(N) on the wire and the client-side `diffLines(old, new)` is O(N) work; a multi-thousand-line file risks WS-buffer pressure and a main-thread/event-loop stall during diffing.
- **Blocked by:** nothing — but it must convert **both** the pre-apply (this fix) and the post-apply (`PatchActuator.apply` → `RENDER_DIFF`) paths together, since they share `PatchedFileDiff`/`DiffBlockShape`.
- **Phase:** future performance/transport sub-phase (own ticket — do not smuggle into a feature fix).
- **Notes:** declared MVP during the ASK-mode inline-approval fix (one-atomic-event design). Bounded by the existing `DIFF_RENDER_LINE_CAP=400` *mount* cap in `DiffBlock.tsx`, but the **wire payload** is still uncapped. Target design: compute the unified diff server-side with `difflib`, transport unified diffs only (O(Δ)), and reconstruct both sides in the host via the already-present `diff` lib (`applyPatch`). Safe to defer under the bounded-file-size assumption.

### DEBT-023 — Miscellaneous single-site strict suppressions (5 ignores) — ✅ RESOLVED (8.1.F)

- **Date:** 2026-06-08
- **Files:**
  - `main.py:221` `[no-untyped-def]` — `_require_token(request, call_next)` FastAPI middleware missing return type.
  - `main.py:944` `[list-item]` — `DirtyBuffer` DTO duck-typed across transport/core boundary (commented as pre-existing structural duplication).
  - `api/sessions.py:128` `[assignment]` — LangGraph `tup.checkpoint` typed as `Any` in stubs; cast to `Dict[str, Any]`.
  - `core/resource_manager.py:214` `[return-value]` — return type should be `Literal["WAIT", "SWITCH_TO_CLOUD", "CANCEL"]` but `.upper()` chain is not narrowed by mypy.
  - `tools/llm_gateway.py:609` `[assignment]` — `on_thinking` Callable reassigned to narrower alias.
- **Resolution (8.1.F, 2026-06-08):** (1) `_require_token` fully typed: `call_next: Callable[[Request], Awaitable[Response]] -> Response`; added `Awaitable`, `Callable` to typing import; added `from starlette.responses import Response`. (2) `cast(List[VfsDirtyBuffer], [...])` with explicit target type import `from core.vfs_middleware import DirtyBuffer as VfsDirtyBuffer`. (3) `cast(Dict[str, Any], tup.checkpoint)`; added `cast` to sessions.py import. (4) `cast(Resolution, raw)` in resource_manager; added `cast` to import. (5) Explicit `if on_thinking is None: return ""` guard before the `sink` assignment in llm_gateway.py — no cast needed, mypy narrows directly. `mypy --strict` → 0 on all 5 files; `mypy .` 0/248; pytest green.

---

### DEBT-022 — api/websocket_manager.py: 4 × arg-type on enum literals — ✅ RESOLVED (8.1.E)

- **Date:** 2026-06-08
- **File:** `api/websocket_manager.py:324,494,600,711`
- **Error:** `[arg-type]` — keyword args (`tier=`, `kind=`, `status=`, `mode=`) pass `str` literals where event dataclass fields expect a narrower `Literal[...]` or `Enum` type.
- **Resolution (8.1.E, 2026-06-08):** Narrowed the 4 broadcast method parameters from `str` to `Literal[...]` matching the payload field (`tier: Literal["small","medium","big"]`; `kind: Literal["INSERT","DELETE","ABORT"]`; `status: Literal["success","error"]`; `mode: Literal["autonomous","supervision"]`). One cascading caller (`task_service.py:1326`) required `cast(Literal["success","error"], ...)` + `Literal` added to its import. `mypy --strict api/websocket_manager.py` → 0; `mypy .` 0/248; pytest green.

---

### DEBT-021 — core/io_coalescer.py: bare `Callable` missing type parameters (5 × type-arg) — ✅ RESOLVED (8.1.D)

- **Date:** 2026-06-08
- **File:** `core/io_coalescer.py:48,49,50,52,56`
- **Error:** `[type-arg]` — `Optional[Callable]` and `fn: Callable` without parameter types.
- **Resolution (8.1.D, 2026-06-08):** `asyncio.Task` → `asyncio.Task[None]`; `Optional[Callable]` / `fn: Callable` → `Optional[Callable[..., Any]]` / `fn: Callable[..., Any]`; added `Any` to `typing` import. 5 `type-arg` ignores eliminated. `mypy --strict core/io_coalescer.py` → 0; `mypy .` 0/248; 932 pytest passed.

---

### DEBT-020 — tree-sitter stubs incomplete (6 × attr-defined, 1 × union-attr) — ✅ RESOLVED (8.1.C)

- **Resolution (8.1.C, 2026-06-08):** **7 ignores eliminated** with two complementary fixes. In
  `brain/prompt_builder.py`: retyped `node: object` → `node: Any` in `_function_signature` (removes
  4 `attr-defined` suppresses) and `tree: object` → `tree: Any` in `_extract_python_skeleton` (removes
  2 `attr-defined` suppresses); added `Any` to the `typing` import; extracted `start: int =
  node.start_point[0]` in the fallback branch to satisfy `--strict`'s `no-any-return` rule. In
  `brain/memory.py`: replaced the `# type: ignore[union-attr]` on `_worker_ast.parse()` with a
  local-variable guard (`ast_engine = _worker_ast; if ast_engine is None: return error_result`) so
  mypy narrows the local to `Any` after the guard, consistent with the function's "never raises"
  contract. `mypy --strict brain/prompt_builder.py brain/memory.py` → 0; `mypy .` → 0/248;
  `pytest` green.
- **Date:** 2026-06-08
- **Files:** `brain/prompt_builder.py:67,69,70,74,93,130` (`attr-defined` — `child_by_field_name`, `start_point`, `root_node`); `brain/memory.py:76` (`union-attr` — `_worker_ast.parse()`).
- **Error:** tree-sitter Python bindings (`tree-sitter`, `tree-sitter-languages`) ship no typed stubs for node attributes.
- **Phase:** 8.1.C

---

### DEBT-019 — api/websocket_manager.py: async request-buffer leak (orphaned late responses) — ✅ RESOLVED (8.1.A)

- **Resolution (8.1.A, 2026-06-08):** closed with **guard-at-store + disconnect sweep**. `resolve_patch_ack` /
  `resolve_human_approval` now store the result only when a waiter is still pending — the *primary* leak was the
  late-arrival orphan, and since every key is a single-use UUID whose waiting coroutine has already returned,
  dropping a late result is provably safe (no consumer can ever want it). Two `session_id`-keyed reverse indexes
  (`_client_pending_hitl` / `_client_pending_acks`) are maintained in `request_human_approval` / `wait_patch_ack`
  (entry + finally); `wait_patch_ack` now takes `session_id`. `disconnect(client_id)` sweeps the four maps **and
  wakes** each suspended waiter (`event.set()` after emptying the result buffer) so its coroutine returns `None`
  in O(1) instead of idling as a zombie task until its timeout. Note: the originally-listed proposed fix (b) alone
  (a reverse index removed in `finally`) does **not** catch the late-arrival orphan — the index entry is already
  gone by the time the late result lands — which is why guard-at-store (a) is the load-bearing half. Regression:
  `tests/test_ws_buffer_lifecycle.py` (6 cases incl. the wake-on-disconnect no-zombie check). `pytest` 930 passed;
  `mypy .` 0/248.

- **Date:** 2026-06-08
- **Reproduce:** N/A (lifecycle/concurrency gap, not a type or test error). Surfaced when Phase 8.0.6
  typed the two buffers and brought their lifecycle under review.
- **File(s):** `api/websocket_manager.py` — `self._hitl_responses: Dict[str, Dict[str, Any]]` (keyed
  by `approval_id`) and `self._patch_ack_results: Dict[str, Dict[str, Any]]` (keyed by `patch_id`).
- **Lifecycle:** populated when an inbound response/ack arrives (`_hitl_responses[approval_id] = {...}`
  ~line 840; `_patch_ack_results[patch_id] = result` ~line 745); consumed/`pop`ped only by the waiter
  (`wait_*` methods ~lines 796 / 736).
- **Leak (race):** if the waiter has already torn down — `asyncio.wait_for` timeout, task
  cancellation, or the IDE closing / network flicker mid-request — a *late-arriving* response/ack is
  still stored with no consumer left to pop it. `disconnect(client_id)` (~line 184) reaps
  `_inbound_tokens` / `_inbound_refill_at` but **not** these two buffers → orphaned entries accumulate
  `O(H)` (H = interaction requests) over a long-running local IDE server session. Silent memory growth
  that can eventually stall the event loop.
- **Phase:** Dedicated WebSocket-lifetime hardening sub-phase (post-8.0.x). **Behavioral change — out
  of scope for the typing campaign;** recorded per the Continuous Registry Protocol, not fixed in 8.0.6.
- **Proposed fix:** (a) guard the store so a response/ack is only buffered when a waiter is still
  pending (`approval_id in _hitl_pending` / `patch_id in _patch_acks`), dropping orphans; and/or
  (b) maintain a `client_id → {pending approval_ids, patch_ids}` index and reap those buffers inside
  `disconnect()`. Add a regression test that simulates a timed-out/cancelled waiter followed by a late
  resolve and asserts the buffer is empty.

---

### DEBT-018 — brain/memory.py: networkx GraphRAG has no memory bound (heap-overhead risk) — ✅ RESOLVED (8.1.B)

- **Resolution (8.1.B, 2026-06-08):** bounded with **cap-and-skip + deterministic teardown**. New
  module constant `MAX_GRAPH_EDGES: int = 5000` gates both builders (`calculate_ppr_sync`,
  `calculate_graph_analytics_sync`) with an early-return guard *before* any graph is built — an
  over-cap request returns `PPRResult(scores={}, success=True)` (identical to the empty-graph branch,
  so the caller degrades to no centrality/community data, not an error) plus a `logger.warning`. The
  guard is on `len(req.edges)` (O(1), pre-build); the constant is named for edges, not nodes, to match
  what it compares against. Each builder now binds `G = None` outside the `try` and clears it in a
  `finally` so all return paths (empty / computed / exception) release the dict-of-dict structure
  deterministically instead of waiting on GC in the reused pool worker; `calculate_graph_analytics_sync`
  additionally binds the `G.to_undirected()` projection (which transiently doubles the graph) and clears
  it in its own `finally`. Regression: `tests/test_graph_analytics.py` gains `test_oversized_graph_is_
  skipped_gracefully` + `test_at_cap_boundary_still_computes`. `pytest` 932 passed; `mypy .` 0/248.

- **Date:** 2026-06-08
- **Reproduce:** N/A (capability/scalability gap, not a type or runtime error). Surfaced when Phase
  8.0.5 unsilenced `brain.memory` and brought the networkx usage under review.
- **File(s):** `brain/memory.py` — the PPR / batch-PPR graph builders that do `import networkx as nx`
  then `nx.DiGraph()` (around lines 108 and 158).
- **Risk:** networkx is pure-Python with a dict-of-dict-of-dict backing store. Spatial complexity is
  `O(V + E)`, but the per-node/per-edge heap overhead on the Python heap is large. In long-lived
  VS Code sessions, a GraphRAG graph that accumulates a node per indexed file with **no eviction or
  teardown** can balloon RAM and stall the asyncio event loop (the IT-director concern raised during
  8.0.5 review).
- **Phase:** Future dedicated phase (post-8.0.x). **Not in scope for the typing campaign** — recorded
  per the Continuous Registry Protocol, not fixed in 8.0.5.
- **Proposed fix:** bound the in-memory graph — e.g. an LRU eviction policy on the node set, a hard
  cap on subgraph size, and/or explicit `G.clear()` / teardown on session end. Measure heap growth
  across a long session first to size the cap.

---

### DEBT-001 — tools.patch_tool: LangChain @tool decorator stub mismatch — ✅ CLOSED 2026-06-05

- **Date:** 2026-05-31
- **File:** `tools/patch_tool.py:219`
- **Error:** `unused-ignore[misc]` — `# type: ignore[misc]` was added when LangChain's `@tool`
  decorator lacked stubs.
- **Resolution (2026-06-05, Phase 8.0.1):** **CLOSED — stubs landed.** `mypy --strict
  tools/patch_tool.py` confirmed the ignore is now *unused* (upstream langchain-core stubs caught
  up). Removed the `# type: ignore[misc]` comment and the `[mypy-tools.patch_tool] follow_imports =
  silent` block together. `mypy --strict tools/patch_tool.py` → 0; `mypy .` → 0/247.

---

### DEBT-002 — agents/contract_guard.py: MODEL_MEDIUM not explicitly exported

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict agents/contract_guard.py`
- **File:** `agents/contract_guard.py:100`
- **Error:** `attr-defined` — `Module "tools.llm_gateway" does not explicitly export attribute
  "MODEL_MEDIUM"`
- **Blocked by:** `tools.llm_gateway` silenced (`follow_imports = silent`). The error is invisible
  under the current `mypy .` gate but surfaces under `--strict`.
- **Phase:** 8.2 (resolves automatically when llm_gateway is unsilenced, IF `MODEL_MEDIUM` is
  added to `__all__` or the import is moved to `shared/config.py`).
- **Notes:** `MODEL_MEDIUM` is defined in `tools/llm_gateway.py` but is not in `__all__`. Two
  remediation options: (a) add `MODEL_MEDIUM` (and `MODEL_SMALL`, `MODEL_BIG`) to `__all__` in
  llm_gateway.py; (b) move the constants to `shared/config.py` (where they may semantically
  belong) and update all callers. Option (b) is cleaner long-term.

---

### DEBT-003 — brain/swarms.py: BaseCheckpointSaver missing type args — ✅ CLOSED 2026-06-05

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/swarms.py`
- **File:** `brain/swarms.py:189`
- **Error:** `type-arg` — `Missing type arguments for generic type "BaseCheckpointSaver"`
- **Resolution:** Fixed in Phase 8.0.0. Changed `Optional[BaseCheckpointSaver]` to
  `Optional[BaseCheckpointSaver[Any]]` in `brain/swarms.py:189`.

---

### DEBT-004 — brain/swarms.py: stale unused-ignore comments — ✅ CLOSED 2026-06-05

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/swarms.py`
- **File:** `brain/swarms.py:226` (and 7 other lines)
- **Error:** `unused-ignore` — stale `# type: ignore[type-var]` / `# type: ignore[arg-type]`
  comments removed (stubs improved). 4 targeted node registration ignores retained where the
  underlying `NodeInputT` constraint is a real Tier 2/3 issue (DEBT-014 will fix those).
- **Resolution:** Fixed in Phase 8.0.0. Removed 8 stale ignores; 4 minimal targeted ignores
  retained for coder/planner/analyst/tool_rag_select node registrations (Tier 2/3 root cause).

---

### DEBT-005 — Multiple brain/ + agents/ interior nodes: unknown strict debt

- **Date:** 2026-05-31
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | head -60`
- **Files:** `brain/engine.py`, `brain/ideation.py`, `brain/guardrails.py`,
  `brain/intent_router.py`, `agents/coder.py`
- **Error:** Various — `type-arg`, `no-any-return`, `no-untyped-def` (full count hidden behind
  silenced transitive deps; cannot be measured accurately until Phases 8.1–8.5 remove the wall)
- **Blocked by:** Silenced modules (`tools.llm_gateway`, `agents.analyst`, `core.db`, etc.)
  masking the true error count.
- **Phase:** 8.4 and 8.7 (assess after each unsilencing step)
- **Notes:** Do NOT attempt to fix these preemptively. Run an exploratory `mypy --strict` after
  each Phase 8.1–8.5 step to update this entry's error count. The topological order guarantees
  these errors only become actionable after their upstream deps are clean.

---

### DEBT-006 — Inline diff has no syntax highlighting (shiki deferred)

> **Note on numbering:** this is the item the 7.14.2 directive referred to as "DEBT-003"; that id was
> already taken by the `brain/swarms.py` entry above, so it is filed here as DEBT-006. Treat
> "DEBT-003 (shiki)" as an alias for this entry.

- **Date:** 2026-06-01
- **Reproduce:** `cd ailienant-extension && node esbuild.js --production; (Get-Item dist/workspace.js).Length`
  (with shiki re-added inline, the bundle exceeds the 550 KB ceiling).
- **File(s):** `src/workspace/components/DiffBlock.tsx` (diff cells render as themed monospace, no
  token spans).
- **Error:** Feature gap, not a type error. The Elite Diff Engine ships without VS Code-identical
  syntax tokenization. Measured: shiki's JS regex engine (~160 KB minified) + the smallest usable
  grammar `tsx` (~172 KB) exceed the bundle headroom; `react-diff-viewer-continued` alone already
  brings the base to ~537 KB.
- **Blocked by:** The IIFE webview bundle cannot lazily code-split (esbuild `iife` has no
  `splitting`). Reintroducing shiki requires runtime asset externalization (Mechanism A:
  `external:[shiki]` + ship assets to `media/` + `webview.asWebviewUri` loader) or a worker — both
  deferred as out-of-scope CSP/plumbing risk for 7.14.2.
- **Phase:** A future 7.14.x or Phase 11 polish slice.
- **Resolution (2026-06-05):** **CLOSED — token layer shipped & gated.** Phase 7.16 moved the grammar
  engine to the host (7.16.1) and the webview now paints the host AST as scope-colored `<span>`s —
  diffs (`DiffBlock.tsx` per-line `renderContent`) and chat code blocks (`MarkdownRenderer.tsx` via the
  stream-end tokenize round-trip), styled only with `--vscode-*` CSS vars (`scopeColor.ts`). The
  webview gained **zero** grammar deps and `dist/workspace.js` stayed under the 550 KB ceiling
  (548.2 KB). The **7.16.3 checkpoint gate** (`src/test/phase7_16_checkpoint_gate.test.ts`, 10/10)
  asserts the ceiling held + no webview leak + engine host-side + scope→CSS-var theme-flip +
  highlighting renders; `esbuild.js::assertWebviewBundleUnderCeiling()` makes the ceiling a permanent
  build gate. Spawned **DEBT-012** (the `disableWordDiff` trade-off).
- **Notes:** The dormant shiki contract (no-WASM JS engine, fine-grained core, lazy-load) is preserved
  in `docs/PHASE_7_14_0_STACK_CONTRACT.md` §3 for whenever this is picked up. ADR-722's *theming*
  half is already honored — diff colors bind to `--vscode-diffEditor-*` CSS vars today; only the
  token layer is missing.
- **Audit cross-ref:** the Phase 7.15 pre-checkpoint audit's "code blocks render as plain white text"
  complaint maps to THIS entry — it is the known, deliberate shiki deferral, not a new defect. The 7.15
  remediation does **not** re-open it (see Fase 7.15 in `PROJECT_MANIFEST.md`); highlighting stays here
  until the bundle/externalization plumbing is funded.
- **Funded by:** **Phase 7.16** (Host-Delegated Tokenization) is the remediation — it takes the
  host-side escape hatch this entry already names: a grammar engine (shiki/textmate) runs in the **Node
  Extension Host** (no bundle ceiling) and ships a token AST over IPC to the dumb webview, so
  `dist/workspace.js` gains no parsing deps. This entry moves to **Closed** when **7.16.2** ships the
  webview AST renderer. The *streaming* half (progressive highlight without flicker) is owned by
  **Phase 7.17**, not this entry.

### DEBT-007 — Auto-accept low-risk edits pays a full HITL round-trip (shift-left candidate)

- **Date:** 2026-06-02
- **Reproduce:** N/A (latency, not an error). With auto-accept ON, every low-risk approval still flows
  backend → WS `server_hitl_approval_request` → webview → `HITL_RESPONSE` → host → WS
  `client_hitl_response` → backend before the edit applies.
- **File(s):** `ailienant-extension/src/workspace/Workspace.tsx` (auto-accept gate in the
  `server_hitl_approval_request` handler); `ailienant-extension/src/workspace/workspaceStore.ts`
  (`autoAcceptLowRisk`).
- **Error:** Per-step network RTT for actions the user pre-authorized. `O(1)` per step but one full
  round-trip each — avoidable.
- **Blocked by:** A client→host→backend channel that carries the auto-accept preference (none exists
  today; the setting is webview-local). Adding it is out of scope for the UI slice that introduced the
  toggle.
- **Phase:** A future shift-left optimization (Phase 11 or a later 7.14.x): the backend reads the
  auto-accept setting and, for low-risk edits, **omits emitting the approval event altogether** — the
  edit applies server-side with no round-trip. Trade-off accepted now for implementation simplicity
  (reuse `useHitlResponder`'s wire message, zero new Python logic).
- **Notes:** The conservative risk gate (any medium/high metric forces the manual card) must be
  preserved if/when this moves server-side.

### DEBT-008 — Coding turns stream node-level narration, not LLM tokens (graph agents don't stream)

- **Date:** 2026-06-02
- **Reproduce:** N/A (UX/perception, not an error). After the Engine Re-Spine, a coding turn drives the
  compiled graph via `astream(stream_mode="values")`; live progress is **node-level narration**
  (`NarrationGate` + `broadcast_pipeline_step`), but the proposed-diff summary still arrives in one
  block — the chat path (`_stream_chat_answer`) streams token-by-token, the coding path does not.
- **File(s):** `ailienant-core/agents/planner.py` (`run_planner_node`), `ailienant-core/agents/coder.py`
  (`run_coder_node`) — both `ainvoke` the model and return complete results; `ailienant-core/core/task_service.py`
  (`_run_coding_task` consumes the final graph state).
- **Error:** Feature gap, not a type error. True token-by-token streaming from inside the graph would
  require the agent nodes themselves to stream their LLM deltas, which they do not.
- **Blocked by:** Nothing technical — it is a **deliberate scope cut** of the Re-Spine to keep that
  change foundational and low-risk, and to avoid touching every agent + the gateway in the same PR
  (event-loop / regression risk).
- **Phase:** Owned by **Phase 7.17** (WBS **7.17.0-B**, ADR-739). **CLOSED 2026-06-05** — but the
  resolution is *thinking*, not the answer tokens: `stream_mode="messages"` was rejected (the nodes use
  the LiteLLM gateway directly, not a LangChain chat model, so that mode captures nothing). Instead the
  nodes stream the model's **native reasoning** to the Thought Box during inference via a dedicated
  `config.configurable["stream_thinking"]` sink (twin of the `narrate` seam), through the new
  `LLMGateway.acomplete_with_thinking`; the structured JSON answer is still buffered → parsed → diffed.
  So the *freeze* is gone (reasoning fills the gap) while the answer diff remains a single block by
  design — structured JSON can't be shown token-by-token usefully. **Distinct from DEBT-006** (frontend
  syntax-highlighting). The residual (true answer-token streaming, blocked by the `response_format`
  drop) is tracked as **DEBT-013**.
- **Notes:** The `NarrationGate` is honored trivially — thinking rides `server_thinking_chunk`, a
  different channel than `server_pipeline_step`, so it never charges the gate. FastAPI event-loop
  protection is honored by the `_ThinkingStreamer` 60 ms coalescer (no WS frame per token).

### DEBT-013 — Thinking-stream coding turns drop hard JSON-mode (`response_format`)

- **Date:** 2026-06-05
- **Reproduce:** N/A (reliability trade-off, not an error). On a reasoning-capable model with native
  thinking ON, `acomplete_with_thinking` takes the streaming branch, which **cannot** pass
  `response_format={"type":"json_object"}` (no `astream*` gateway method supports it). The planner/coder
  answer is therefore prompt-enforced JSON recovered via `_sanitize_json_response`, not provider-enforced
  JSON-mode.
- **File(s):** `ailienant-core/tools/llm_gateway.py` (`acomplete_with_thinking` streaming branch),
  `ailienant-core/agents/coder.py` / `agents/planner.py` (callers).
- **Error:** Not a defect — a **declared trade-off per CLAUDE.md §7.** Blast-radius is bounded: only
  thinking-capable + thinking-ON turns take this path; every other turn keeps the exact
  `ainvoke(response_format=json)` call (zero regression). Residual risk: marginally higher parse-failure
  odds on those turns, already absorbed as **soft errors** (planner actor-critic retry; coder
  step-failed → `error_correction`), and the fence-strip + the 7.18.2/ADR-742 adaptive sanitizer
  recover the JSON.
- **Blocked by:** Nothing technical — a deliberate scope cut to keep 7.17.0-B low-risk.
- **Phase:** Spawned by **7.17.0-B (ADR-739)**. Enterprise refactor candidate: a gateway path that
  streams reasoning **and** keeps `response_format` for providers that support streaming structured
  outputs (e.g. OpenAI), falling back to the sanitizer only where unsupported.
- **Notes:** Re-open if telemetry shows a material rise in planner/coder parse failures on reasoning
  models; otherwise the soft-error handling makes this low-priority.

### DEBT-014 — brain/swarms.py: NodeInputT add_node type-var — ⚠️ REDUCED (3 residual ignores) 2026-06-08

- **Date:** 2026-06-05
- **Root cause:** LangGraph's `add_node` binds `NodeInputT` with `bound=StateLike`
  (`TypedDictLikeV1 | TypedDictLikeV2 | DataclassLike | BaseModel`, per
  `langgraph/typing.py:45`). A node function typed `(state: Dict[str, Any]) -> ...` infers
  `NodeInputT = dict[str, Any]`, which is **not** a TypedDict and violates the bound →
  `type-var` error at the `add_node` call site.
- **Partial resolution (2026-06-08, Phase 8.0.4):** `tool_rag_select_node` (the node defined
  locally in `swarms.py`) was retyped `(state: AIlienantGraphState)`. `AIlienantGraphState` IS a
  TypedDict → satisfies the bound → `swarms.py:155` no longer needs an ignore (removed). This
  closed the strict/non-strict discrepancy that was the last `mypy --strict main.py` residual.
  **`mypy --strict main.py` → 0 as of 8.0.4.**
- **Residual (3 ignores still required):** `swarms.py:156` (`run_coder_node`), `:218`
  (`run_planner_node`), `:227` (`run_analyst_node`), and `ideation.py:215` (`run_analyst_node`)
  retain `# type: ignore[type-var]`. These are **USED** (suppress real errors) under both `mypy .`
  and `mypy --strict` — they cause no `unused-ignore`, so all gates stay green.
- **Why not fixed:** two approaches were tried and rejected in 8.0.4:
  1. **Retype signatures to `AIlienantGraphState`** — cascades to **63 `arg-type` errors across 19
     files**: every direct caller (production `agents/logic.py:27` + ~18 test files) passes a plain
     `dict`, which is not assignable to a TypedDict param. Too invasive; churns the test suite.
  2. **`input_schema=AIlienantGraphState` on the `add_node` call** — mypy reports `Cannot infer
     value of type parameter "NodeInputT"` because it cannot reconcile a `Dict[str, Any]`-typed
     action with `StateNode[AIlienantGraphState, ...]`. Does not work.
- **Proposed enterprise refactor (deferred):** when the agent-node call sites are themselves
  hardened (a dedicated phase), retype `run_coder_node` / `run_planner_node` / `run_analyst_node`
  to `AIlienantGraphState` **and** migrate all ~19 direct callers (tests + `logic.py`) to construct
  a typed state (or a small `cast` helper). Alternatively, adopt it when LangGraph ships a stub that
  accepts `Mapping[str, Any]` for `NodeInputT`. Until then the 3 ignores are the correct, minimal,
  gate-green suppression.
- **Notes:** The enforced gate (`mypy .`) and the campaign gate (`mypy --strict main.py`) are both
  **0** with these ignores in place. This is no longer a strict-gate blocker — only a code-cleanliness
  residual.

---

### DEBT-015 — agents/contract_guard.py: MODEL_MEDIUM import — ✅ CLOSED 2026-06-05

- **Date:** 2026-06-05
- **File(s):** `agents/contract_guard.py:100`
- **Error:** `attr-defined` — `Module "tools.llm_gateway" does not explicitly export attribute
  "MODEL_MEDIUM"`. `MODEL_MEDIUM` is defined in `shared.config` and imported into `llm_gateway`
  without an explicit re-export; `contract_guard` was pulling it from `llm_gateway` instead of
  the canonical source.
- **Resolution (2026-06-05, Phase 8.0.2):** Changed the deferred import in `contract_guard.py:100`
  to `from shared.config import MODEL_MEDIUM` directly. `mypy --strict agents/contract_guard.py` → 0.

---

### DEBT-016 — brain/summarizer.py: strict-mode type-arg — ✅ CLOSED 2026-06-05

- **Date:** 2026-06-05
- **File(s):** `brain/summarizer.py:34` (bare `dict`).
- **Error:** `type-arg` × 1 (`run_summarize_node` signature: `state: dict`).
- **Partial resolution (2026-06-05, Phase 8.0.1):** `brain/ideation.py` portion (8 × type-arg) was
  self-contained (not gated by `agents.analyst`) — fixed in 8.0.1. Only `summarizer.py` remained.
- **Resolution (2026-06-05, Phase 8.0.2):** **CLOSED.** `run_summarize_node(state: Dict[str, Any])
  -> Dict[str, Any]` + added `Any` to the `typing` import. `mypy --strict brain/summarizer.py` → 0.

---

### DEBT-009 — MCTS variant-search is offline-only (not wired into the live coder loop) — ✅ CLOSED 2026-06-09

- **Resolution (2026-06-09, Phase 7.19.2 — ADR-749):** **CLOSED.** MCTS now has a sanctioned live home in the autonomous ReAct cell (`brain/agentic_cell.py` → `select_candidate_via_mcts`). It engages **only** when a reasoning turn proposes ≥2 competing fix candidates for the same file; the linear single-fix path pays zero tree-search cost (avoiding the latency/cost regression the original defer warned about). The reward is the cell's **own structured verdict** (exit code + diagnostic severity), not `evaluate_node_reward` (which would re-run fix+surgeon+judge and multiply cost). Candidate evaluation is transactional over the shared surface (push → verify → roll back to the clean base → restore the winner). The single live `brain.mcts.tree` import edge is confined to the cell module; the cheap single-shot spine (`brain/engine.py`, `agents/coder.py`) stays MCTS-free, and the **MCTS-DEFER** gate row was retargeted to pin that enduring invariant instead of the now-obsolete "offline-only" boundary. **Note:** 7.19.2 also delivered `AGENTIC_CELL_MAX_ITERATIONS=6` as an explicit MVP single-axis bound, superseded in full by **7.19.3 (ADR-750)** — the multi-axis governor (`brain/iteration_governor.py`) replaces it with steps ∧ tokens ∧ time, closing the MVP gap declared at DEBT-009 resolution time.
- **Date:** 2026-06-03
- **Reproduce:** N/A (capability gap, not an error). `brain/mcts/` (UCB1 tree, reward eval) + `agents/mcts_coder.py` exist and run only in the parallel dreaming daemon; the live Phase-4 LangGraph coder loop (`run_coder_node`) is single-shot-per-step and never enters MCTS.
- **File(s):** `brain/mcts/tree.py`, `agents/mcts_coder.py`, `brain/engine.py` (no edge into the MCTS search from the live path).
- **Error:** Feature gap. Surfacing UCB1 variant-search inline would multiply LLM calls per step, collide with the 7.18.0 correction budgets, and risk latency/cost regression on the exact loop 7.18.0 makes load-bearing.
- **Blocked by:** **Precondition = Phase 7.18.0 — now SHIPPED (2026-06-04).** MCTS needs a per-variant reward signal; the *structured test/typecheck verdict* introduced by 7.18.0 (`tools/validation/diagnostics.py` → a `healing_required` delta carrying compact `[file,line,code,msg]` diagnostics) is exactly that signal and is now available. The precondition is satisfied; MCTS-live remains deferred by choice (highest risk, lowest marginal value) — it should only be attempted after 7.18.0 *stabilizes* in real turns, and never coupled onto the same edge as another risky change.
- **Phase:** Deferred by **7.18.5 (ADR-745)**. If pursued, scope as a follow-up phase gated behind 7.18.0's green checkpoint; the 7.18.6 gate row **MCTS-DEFER** pins the offline boundary (no live-loop import edge into `brain/mcts`) so an accidental wiring trips the gate.
- **Notes:** Highest risk, lowest marginal value of the six audited techniques. Not a defect — a deliberate sequencing decision.

### DEBT-010 — OCC version-vectors on the graph state dict: rejected in favor of existing reducers (decision record)

- **Date:** 2026-06-03
- **Reproduce:** N/A (architecture decision, not an error). Architect upgrade #5 requested strict version-vector OCC on the LangGraph state dict (reject-and-retry, idempotent nodes).
- **File(s):** `brain/state.py:241-289` (per-file `document_version_id` OCC), `brain/state.py:458-459` (LangGraph reducers: `operator.add`, last-writer-wins `merge`); `agents/coder.py` (emitted `base_hash` stale-guard).
- **Error:** Not a defect — a **conflict surfaced per CLAUDE.md §3.** OCC already exists at the file granularity that governs mutation safety, and the graph state is managed by **reducers** that *merge* the concurrent `Send()` fan-out a version-vector model would *abort* — opposite strategies for the same contention. A parallel OCC layer would either duplicate the guarantee or serialize (break) the SWARM fan-out.
- **Blocked by:** N/A — **resolved as Option A (Pivot):** the intent (zero state-corruption under concurrency) is treated as already satisfied; the 7.18.6 gate row **OCC1** *asserts* the existing reducer + `base_hash` guarantee rather than adding a mechanism.
- **Phase:** Decision recorded under **7.18 (ADR-746)**. Re-open **only** if a demonstrated corruption bug proves reducers insufficient (then Option B targeted idempotency for async MCP writes, or — last resort — Option C full version-vector re-spine).
- **Notes:** A genuine future risk this entry flags: once 7.18 wires execute-tier dispatch, **async MCP tool calls** mutating state mid-node could warrant Option B (targeted execute-tier write idempotency) — a small hardening, not a global OCC rewrite.

### DEBT-011 — test_v3_tracemalloc heap-baseline ceiling is structurally broken (red in the Phase 3.7 gate)

- **Date:** 2026-06-04
- **Reproduce:** `cd ailienant-core && .\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q`
- **File(s):** `tests/test_phase3_checkpoint_gate.py:418-452` (`test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline`).
- **Error:** Pre-existing test-design defect, not a product bug. The test takes the `tracemalloc`
  baseline snapshot **immediately after** `tracemalloc.start()`, so `baseline_bytes ≈ 0`; the ceiling
  `int(baseline_bytes * 0.05) + 65_536` therefore collapses to a fixed ~64 KB. Allocating 50
  `MCTSNode` + 50 `MissionSpecification` (Pydantic v2) objects retains ~210-240 KB (validator/core-schema
  caches + node dict), so the assertion `delta_bytes < ceiling` always fails (observed 212-237 KB).
  The `prune_branch` + `del tree` cleanup does not (and cannot) reclaim Pydantic's process-wide schema
  caches, so the "returns to baseline" premise is unmeetable as written.
- **Blocked by:** None — fixable now, but **out of scope for 7.18.1** (recency); deliberately deferred
  per the Continuous Registry Protocol rather than fixed in-place. Verified failing on the stashed
  pre-7.18.1 tree, so it is **not** a regression from the recency work.
- **Phase:** A future test-hardening slice (Phase 8 / gate maintenance).
- **Notes:** Remediation options: (a) call `gc.collect()` before *both* snapshots and warm Pydantic by
  constructing one `MissionSpecification` before the baseline so its schema cache is already resident;
  (b) replace the baseline-relative ceiling with a fixed absolute budget sized for 50 nodes (e.g.
  ~512 KB) since the real claim is "bounded growth," not "returns to zero"; (c) measure only the
  `MCTSTree`/RAM-VFS delta via `snapshot.filter_traces` on `brain/mcts/tree.py` + `core/vfs_middleware.py`
  rather than the whole-process `"filename"` sum. Option (b)+(c) together best preserve the original
  intent (heap-leak guard on the MCTS lifecycle) without the brittle near-zero baseline.

### DEBT-012 — diff highlighting disables word-level diff (no intra-line token slicing)

- **Date:** 2026-06-05
- **Reproduce:** Apply an edit that changes part of a line; the line shows full-line syntax color but
  no word-level add/remove shading (the per-word green/red highlight).
- **File(s):** `src/workspace/components/DiffBlock.tsx` (`disableWordDiff={true}` + the per-line
  `renderContent` content→tokens map).
- **Error:** Not a defect — a **declared trade-off (CLAUDE.md §7.2)** taken to ship 7.16.2 syntax
  highlighting. `react-diff-viewer-continued` calls `renderContent(source)` per *word fragment* when
  word-diff is on, which would break the per-line token mapping (the host emits tokens row-aligned to
  full lines, not fragments). Disabling word-diff yields clean full-line syntax color at the cost of
  intra-line word shading. Line-level add/remove backgrounds (the `--vscode-diffEditor-*` palette) are
  unaffected, so the diff still reads correctly.
- **Blocked by:** None — needs a word-diff-aware token slicer: intersect the viewer's per-fragment
  `DiffInformation` offsets with the line's `ASTToken` runs so each fragment carries only its slice of
  scopes. Non-trivial offset math; out of scope for 7.16.2 (static highlight first).
- **Phase:** A future 7.16.x / 7.17 polish slice (best folded into the 7.17 streaming-render work,
  which already owns the token-reconciliation path).
- **Notes:** Alternative: keep word-diff and run a second, fragment-level tokenization pass keyed by
  `(line, fragmentRange)` — heavier; the offset-intersection approach reuses the existing host AST.

---

## Closed Entries

*(Move entries here when their Phase has been executed and verified.)*

- **DEBT-006 — Inline diff / chat code had no syntax highlighting (shiki deferred)** — **CLOSED 2026-06-05**
  by Phase 7.16 (host-delegated tokenization). Engine moved to the host; webview paints scope-colored
  spans with `--vscode-*` CSS vars and zero grammar deps; `dist/workspace.js` 548.2 KB < 550 KB ceiling.
  Verified by the 7.16.3 checkpoint gate (10/10) + a permanent esbuild ceiling guard. Full record under
  the DEBT-006 entry above. Spawned DEBT-012 (`disableWordDiff` trade-off, still open).

---

## Appendix: Reproduction Quick-Reference

```powershell
# Run from: C:\Proyectos\Proyect_Ailienant\ailienant-core

# DEBT-001
.\venv\Scripts\python -m mypy --strict tools/patch_tool.py

# DEBT-002
.\venv\Scripts\python -m mypy --strict agents/contract_guard.py

# DEBT-003 + DEBT-004
.\venv\Scripts\python -m mypy --strict brain/swarms.py

# DEBT-005 (exploratory — count changes over time)
.\venv\Scripts\python -m mypy --strict brain/engine.py 2>&1 | grep "error:" | wc -l

# DEBT-011 (pre-existing red gate test — heap baseline ceiling)
.\venv\Scripts\python -m pytest tests/test_phase3_checkpoint_gate.py::test_v3_tracemalloc_50_node_lifecycle_returns_to_baseline -q
```
