# shared/config.py

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Application home — the stable per-user root for all global runtime stores.
# Mirrors how a CLI tool keeps its state under a dotfolder in the user's home,
# so the stores no longer depend on the process working directory (launching
# from a different CWD would otherwise orphan the catalog / vector index).
# Created at import so the very first store connection finds the directory.
# ---------------------------------------------------------------------------
AILIENANT_HOME: Path = Path.home() / ".ailienant"
AILIENANT_HOME.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# LiteLLM Proxy (Phase 1.6) — all agent LLM calls route through this endpoint
# ---------------------------------------------------------------------------
LITELLM_PROXY_BASE_URL: str = os.getenv("LITELLM_PROXY_BASE_URL", "http://localhost:4000")
LITELLM_PROXY_API_KEY: str = os.getenv("LITELLM_PROXY_API_KEY", "sk-ailienant-local")

# ---------------------------------------------------------------------------
# Local engine base URLs (Phase 7.9.B.12) — shared by config_generator,
# the embedding resolver and the indexer preflight so all probes agree.
# ---------------------------------------------------------------------------
OLLAMA_API_BASE: str = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")
LM_STUDIO_API_BASE: str = os.getenv("LM_STUDIO_API_BASE", "http://localhost:1234")

# Ailienant model alias tiers — mapped to real models inside LiteLLM proxy config.yaml.
# Override via env to switch providers without touching code (Phase 1.6.2).
MODEL_SMALL: str = os.getenv("AILIENANT_MODEL_SMALL", "ailienant/small")
MODEL_MEDIUM: str = os.getenv("AILIENANT_MODEL_MEDIUM", "ailienant/medium")
MODEL_BIG: str = os.getenv("AILIENANT_MODEL_BIG", "ailienant/big")


def get_litellm_config() -> dict[str, str]:
    """Base kwargs injected into every litellm.completion() / acompletion() call."""
    return {
        "base_url": LITELLM_PROXY_BASE_URL,
        "api_key": LITELLM_PROXY_API_KEY,
    }


# ---------------------------------------------------------------------------
# VRAM gating thresholds (effective GB) — configurable, not frozen constants.
# The hardware detector reads the swarm gates from here so an operator can tune
# the local/cloud frontier per machine without a code change. The cloud floor is
# the point below which the routing engine bypasses local inference to the cloud
# (graceful degradation): below it, even a small local model cannot run safely.
# A malformed override degrades to the documented default rather than raising at
# import time, since this is a foundational module on every startup path.
# ---------------------------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def session_budget_usd() -> float:
    """Session spend ceiling — shared source of truth for on-demand FinOps gates
    (Dreaming, project-init) that must read the *current* env value on every
    call rather than a value baked in at import time, so a runtime override or
    a test's ``monkeypatch.setenv`` takes effect without a reimport. Distinct
    from ``core/supervisor.py::_default_budget`` (a graph-node fallback used
    only when ``state["session_max_budget_usd"]`` is absent) — that one stays
    separate on purpose.
    """
    return _env_float("AILIENANT_MAX_SESSION_BUDGET_USD", 5.00)


VRAM_MICRO_SWARM_GB: float = _env_float("AILIENANT_VRAM_MICRO_SWARM_GB", 4.0)
VRAM_FULL_SWARM_GB: float = _env_float("AILIENANT_VRAM_FULL_SWARM_GB", 12.0)
VRAM_CLOUD_FLOOR_GB: float = _env_float("AILIENANT_VRAM_CLOUD_FLOOR_GB", 4.0)

# ---------------------------------------------------------------------------
# Blast-radius pre-apply gate — the transitive-dependent count above which a
# pending diff escalates to human review before touching disk. Tunable per repo
# (a large monorepo tolerates a wider radius than a tightly-coupled service).
# ---------------------------------------------------------------------------
BLAST_RADIUS_THRESHOLD_FILES: int = _env_int("AILIENANT_BLAST_RADIUS_THRESHOLD_FILES", 25)


# ---------------------------------------------------------------------------
# Outbound LLM concurrency ceiling — the maximum number of simultaneously
# in-flight gateway calls to the LiteLLM proxy. Client-side backpressure so a
# fan-out (parallel coder clones / subagent dispatch) is admission-controlled
# here rather than discovered as a provider-side rate-limit rejection. This is a
# transport-layer runtime gate, deliberately distinct from any plan-time
# fan-out width ceiling. Floored at 1 so a malformed override can never wedge
# the gateway shut.
# ---------------------------------------------------------------------------
LLM_MAX_CONCURRENCY: int = max(1, _env_int("AILIENANT_LLM_MAX_CONCURRENCY", 8))


# ---------------------------------------------------------------------------
# Token hygiene — hard ceiling on observation/digest text folded back into a
# prompt or a dispatch result envelope. Single source of truth shared by the
# tool-dispatch loop and the subagent result schema so the two truncation
# ceilings can never drift apart. A fixed constant, not env-tunable: it is a
# correctness/attention bound, not a per-machine knob.
# ---------------------------------------------------------------------------
MAX_OBSERVATION_CHARS: int = 4000

# ---------------------------------------------------------------------------
# Event-loop safety ceiling on a tool observation before it is parsed as JSON
# for state-channel promotion (core/tool_dispatch.py::promote_tool_state). This
# check runs on the *untruncated* text — deliberately ahead of the
# MAX_OBSERVATION_CHARS clamp above, since a promoted payload (e.g. a TODO
# list) must not be corrupted by mid-JSON truncation. That ordering means an
# adversarial or malfunctioning model could otherwise hand json.loads an
# unbounded string; this ceiling is checked with a plain len() before any
# parse is attempted, so a huge payload is rejected in O(1) rather than paying
# an O(L) synchronous parse on the FastAPI event loop.
# ---------------------------------------------------------------------------
MAX_JSON_PARSE_CHARS: int = 50_000

# ---------------------------------------------------------------------------
# Dynamic subagent dispatch — the maximum number of subagent workers that fan
# out concurrently in a single wave. A plan wider than this is split into
# sequential waves at dispatch time (bounded fan-out, not a runtime semaphore).
# Floored at 1 so a malformed override can never wedge dispatch shut.
# ---------------------------------------------------------------------------
MAX_CONCURRENT_SUBAGENTS: int = max(1, _env_int("AILIENANT_MAX_CONCURRENT_SUBAGENTS", 4))

# ---------------------------------------------------------------------------
# Dynamic subagent dispatch — production wiring gate and recursion/fan-out
# ceilings. The ENABLE flag is read once at graph-construction (module import)
# time: when off, the compiled engine graph is topologically identical to a
# deployment without this feature. The depth/width/round caps are re-checked in
# code (defense in depth) beyond the Pydantic bounds already on DispatchPlan, so
# a caller that bypasses schema validation still cannot exceed a reviewed ceiling.
# ---------------------------------------------------------------------------
ENABLE_DYNAMIC_DISPATCH: bool = os.getenv("AILIENANT_ENABLE_DYNAMIC_DISPATCH", "0") != "0"
MAX_DISPATCH_DEPTH: int = max(0, _env_int("AILIENANT_MAX_DISPATCH_DEPTH", 2))
MAX_DISPATCH_WIDTH: int = max(1, _env_int("AILIENANT_MAX_DISPATCH_WIDTH", 32))
MAX_DISPATCH_ROUNDS: int = max(1, _env_int("AILIENANT_MAX_DISPATCH_ROUNDS", 3))
# Named product ceiling (depth × width) — a single edit to either cap can never
# silently blow past this reviewed bound; asserted in the division checkpoint gate.
MAX_TOTAL_DISPATCH_FANOUT: int = max(1, _env_int("AILIENANT_MAX_TOTAL_DISPATCH_FANOUT", 64))


# ---------------------------------------------------------------------------
# Sandbox container pool — the Docker tier leases one container per
# (workspace mount root, session) pair rather than sharing a single global
# container, so concurrent sessions cannot contend for one /work tmpfs or share
# a blast radius. The cap bounds how many containers may exist at once; idle
# leases past the TTL are reclaimed on the next acquire (no background timer).
# LEASE_WAIT_S is how long an admission blocks for an idle slot before the pool
# degrades — and it only ever degrades by sharing a container mounted at the
# SAME root, since sharing across roots would execute against the wrong project.
# Floored so a malformed override can never wedge the pool shut.
# ---------------------------------------------------------------------------
SANDBOX_MAX_CONTAINERS: int = max(1, _env_int("AILIENANT_SANDBOX_MAX_CONTAINERS", 4))
SANDBOX_IDLE_TTL_S: int = max(1, _env_int("AILIENANT_SANDBOX_IDLE_TTL_S", 900))
SANDBOX_LEASE_WAIT_S: float = max(0.0, _env_float("AILIENANT_SANDBOX_LEASE_WAIT_S", 30.0))

# Bounded FIFO admission queue depth (DEBT-151). Once the pool is at capacity
# with no idle lease, a new acquirer queues rather than racing every other
# waiter for the next release — see _ContainerPool's head-of-queue predicate.
# A queue already at this depth refuses ADMISSION immediately (raises
# SandboxResourceExhausted rather than joining the queue) instead of piling an
# unbounded backlog behind SANDBOX_LEASE_WAIT_S; the default is generous
# relative to SANDBOX_MAX_CONTAINERS so the refusal is unreachable at any
# realistic concurrency. Floored so a malformed override can never wedge
# admission shut entirely.
SANDBOX_MAX_QUEUED: int = max(1, _env_int("AILIENANT_SANDBOX_MAX_QUEUED", 2 * SANDBOX_MAX_CONTAINERS))

# Per-container resource ceilings (not reservations — usage stays demand-driven).
# Under cgroup v2 the container's tmpfs pages are charged to its own cgroup, so
# the /work tmpfs size sits inside this memory ceiling rather than beside it. A
# process killed for exceeding it surfaces as exit 137, which the Docker adapter
# translates into a message naming this knob so the model can react to it. No CPU
# ceiling is exposed: `nano_cpus` is absent from the SDK's create_host_config, and
# throttling would distort the benchmark oracle's measurements.
SANDBOX_MEM_LIMIT: str = os.getenv("AILIENANT_SANDBOX_MEM_LIMIT", "2g")
SANDBOX_PIDS_LIMIT: int = max(1, _env_int("AILIENANT_SANDBOX_PIDS_LIMIT", 512))

# Socket-level timeout for ordinary Docker SDK calls (ping, inspect, run, stop,
# remove, image lookup). The SDK's transport is synchronous `requests`, so this
# is what turns an unresponsive daemon into a prompt ReadTimeout on the worker
# thread — releasing it in O(1) instead of parking it indefinitely. Image build
# and pull use their own, much longer budget; a one-shot exec uses a client
# scoped to that command's own timeout, since exec blocks until it completes.
DOCKER_OP_TIMEOUT_S: float = max(1.0, _env_float("AILIENANT_DOCKER_OP_TIMEOUT_S", 30.0))

# ---------------------------------------------------------------------------
# Native HITL interrupt abandonment window — how long a session may sit paused
# on an unanswered clarification/approval card before a new submit is allowed
# to reclaim it. Generous by default (a human may legitimately step away for
# hours), but bounded so a dismissed or lost card can never wedge a session
# "busy" for the life of the process. Floored so a malformed override can
# never wedge admission shut entirely.
# ---------------------------------------------------------------------------
PAUSED_INTERRUPT_TTL_S: float = max(60.0, _env_float("AILIENANT_PAUSED_INTERRUPT_TTL_S", 6 * 3600.0))

# ---------------------------------------------------------------------------
# LangGraph super-step ceiling for one coding turn. No value was ever set on
# the production run config, so LangGraph's own default of 25 applied — the
# RELAY multi-step loop already spends ~7 super-steps per WBS step plus ~6
# prologue steps, and per-step incremental approval (13.0.9) adds one more
# node per step, making a modest multi-step WBS a real risk of hitting the
# ceiling mid-turn. Generous by default; env-overridable for a deployment
# running unusually long WBS plans.
# ---------------------------------------------------------------------------
GRAPH_RECURSION_LIMIT: int = max(25, _env_int("AILIENANT_GRAPH_RECURSION_LIMIT", 150))

# ---------------------------------------------------------------------------
# Vision-attachment payload ceilings — bound the cost of building an
# OpenAI-style image content block on the FastAPI event-loop thread (pure
# string/dict construction, never off-loaded — see LLMGateway.ainvoke). Refusal
# past either ceiling is loud (WARNING + a user-facing routing note), never a
# silent drop.
# ---------------------------------------------------------------------------
VISION_MAX_IMAGES_PER_CALL: int = max(1, _env_int("AILIENANT_VISION_MAX_IMAGES_PER_CALL", 4))
VISION_MAX_TOTAL_BASE64_CHARS: int = max(
    1, _env_int("AILIENANT_VISION_MAX_TOTAL_BASE64_CHARS", 20_000_000)
)


# ---------------------------------------------------------------------------
# Cloud availability detection (used by Phase 2 routing engine)
# ---------------------------------------------------------------------------
# Mirrors the cloud env keys declared in core/config/provider_registry.py
# (kept as a flat list here to avoid an import cycle in this foundational module).
CLOUD_PROVIDER_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "AILIENANT_CUSTOM_CLOUD_ENDPOINT",
]


def check_cloud_availability() -> bool:
    """Returns True if at least one cloud provider key is configured."""
    return any(os.getenv(key) for key in CLOUD_PROVIDER_KEYS)


# ---------------------------------------------------------------------------
# Catalog DB — global command/graph store, separate from LangGraph's
# alienant_memory.sqlite. Per-project rows are isolated by a project_id column;
# the file itself is global (skills / MCP servers / hooks are shared across
# projects), so it lives in the application home.
# ---------------------------------------------------------------------------
DB_CATALOG_PATH: str = os.getenv("AILIENANT_CATALOG_DB", str(AILIENANT_HOME / "catalog.sqlite"))

# Global LanceDB store — home of the cross-project tables (product-doc index and
# trajectory memory). The per-project GraphRAG store (workspace_embeddings) is
# resolved separately by core.storage_paths so each project gets its own index.
LANCEDB_PATH: str = os.getenv("AILIENANT_LANCEDB_PATH", str(AILIENANT_HOME / "lancedb"))

# MCTS episodic audit DB — global, retention-pruned by the janitor.
MCTS_DB_PATH: str = os.getenv("AILIENANT_MCTS_DB", str(AILIENANT_HOME / "mcts.sqlite"))

# Telemetry DB (routing decisions, OOM events, request latency, container
# lifecycle, per-action token usage) — global, retention-pruned by the janitor
# (DEBT-120). core/telemetry.py's own default was a RELATIVE "data/telemetry.sqlite"
# (wherever the process happened to launch from); this is the one production
# code actually initializes with.
TELEMETRY_DB_PATH: str = os.getenv("AILIENANT_TELEMETRY_DB", str(AILIENANT_HOME / "telemetry.sqlite"))
# Phase 7.9.B.12 — advanced override ONLY. When unset, the embedding backend is
# resolved per-provider from the active BYOM preset (core/config/embedding_resolver.py).
# Setting this env var forces a fixed embedding model regardless of the preset.
MODEL_EMBEDDING: str = os.getenv("AILIENANT_MODEL_EMBEDDING", "ailienant/embedding")
MINI_JUDGE_MODEL: str = os.getenv("AILIENANT_MINI_JUDGE_MODEL", MODEL_SMALL)

# Phase 5.2 — MCP transport URI (None → local-only fallback, no MCP session).
# Format expected: "stdio:///absolute/path/to/server[?arg=...]" (only stdio
# supported in 5.2; websocket/http transports deferred).
AILIENANT_MCP_SERVER_URI: str | None = os.getenv("AILIENANT_MCP_SERVER_URI") or None
