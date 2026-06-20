# Division 8.13 Blueprint — Polyglot Devcontainer Execution Layer

**Status:** Planned (authored during 8.10.6; binding once 8.13 becomes the active phase).
**Resolves:** DEBT-035 (TypeScript sandbox) and the broader TS/Python runtime bias.
**ADR:** to be assigned.

---

## 1. Problem & Intent

The agent's execution substrate is a single Docker image (`python:3.13-slim`). Adding a language
means editing that image — an O(N)-runtime maintenance trap and a TS/Python bias. The intent is a
**language-agnostic, user-extensible** execution layer where the *environment is declared as data the
user owns*, and image build + caching are delegated to the user's local Docker daemon through standard
VS Code infrastructure — not reimplemented inside AILIENANT.

This mirrors how the field provisions agent environments: OpenAI Codex configures a per-task sandbox
from repo config (`AGENTS.md`), Cursor background agents take a per-project Dockerfile/install spec, and
Anthropic ships a reference `.devcontainer` for Claude Code. The common pattern is **environment-as-data,
provisioned per project, built by the host's container runtime** — the open
[devcontainer specification](https://containers.dev) and its reference CLI (`@devcontainers/cli`).

## 2. The Locked Invariant — Split by Trust (Charter §4)

The existing `DockerSandboxAdapter` is a **locked cage for untrusted, LLM-generated code** (the benchmark
oracle, hardened in DEBT-036): no network, read-only mount, non-root `USER sandbox`, env-whitelist, tmpfs.
A user-authored `devcontainer.json` can declare the opposite — root, network, arbitrary `postCreateCommand`
shell. These are **two opposite threat models**: the devcontainer's job is to *reproduce the user's real
environment*; the oracle's job is to *cage untrusted output*. Pointing the oracle at a user devcontainer
would dissolve the cage.

**Resolution (binding):**

| Surface | Threat model | Tier |
|---|---|---|
| Benchmark oracle, codegen Pass@1 | **Untrusted** model output | `DockerSandboxAdapter` (locked) — **UNCHANGED** |
| Agent `run_command`, the user's own tests, interactive sessions | **Trusted** project execution | `DevcontainerSandboxAdapter` (new) |

The devcontainer adapter **never** executes untrusted model output. DEBT-035's TS *benchmark* stays in the
untrusted lane and remains `unsupported_runtime` until a separate locked Node tier is justified — it is not
the devcontainer's job.

## 3. Architecture

```
┌─ ailienant-core (Python) ─────────────┐        ┌─ ailienant-extension (VS Code host) ─┐
│ get_active_adapter()                  │        │ Devcontainer lifecycle owner          │
│   └─ DevcontainerSandboxAdapter       │  WS    │   probe: Dev Containers ext           │
│        execute()/open_session()  ─────┼───────►│        → @devcontainers/cli           │
│        (no Docker shelled here)       │ host-  │        → degrade                      │
│   └─ DockerSandboxAdapter (locked) ◄──┼─bridge │   devcontainer up / exec (local Docker)│
│        oracle — untouched             │        │   status → RuntimePanel.tsx           │
└───────────────────────────────────────┘        └───────────────────────────────────────┘
```

### 3.1 Extension-owned lifecycle (VS Code-native)
The extension drives provisioning. Probe order, with graceful degrade at each step:
1. Official **Dev Containers extension** (`ms-vscode-remote.remote-containers`) if installed.
2. Bundled **`@devcontainers/cli`** child process (`devcontainer up`, `devcontainer exec`).
3. **Degrade** to the existing Docker/Native tiers when neither is present or no `devcontainer.json` exists.

The CLI wraps `docker build` / `docker exec` and applies `devcontainer.json`, **delegating image build and
caching to the user's local Docker daemon** — AILIENANT builds and caches nothing itself.

### 3.2 Backend host-bridge
`DevcontainerSandboxAdapter.execute()` does **not** shell Docker. It routes the command over the existing
host channel to the extension, which runs `devcontainer exec` and streams `stdout`/`stderr`/`exit` back.
This keeps `get_active_adapter()` as the single execution seam (tools call `execute()` unchanged) while the
provisioning lives where the Docker daemon and the user's `devcontainer.json` are — the host. The pattern
mirrors the off-process `NativeHITLSandboxAdapter` (channel + DLQ-on-timeout), which already executes
off the backend process.

### 3.3 Async / non-blocking
`devcontainer up` is minutes-long. It runs **once per workspace** (lazy, idempotent, single-flight — the
same lazy-init discipline as the Docker daemon probe), emits a `provisioning` status event, and never
blocks the event loop (subprocess + async streaming). A build timeout degrades gracefully (DLQ / HITL
notice), never hangs — the never-hang contract from the gateway HITL-degrade work.

### 3.4 Security model (trusted tier)
Even trusted, AILIENANT keeps guardrails: the command's environment still passes through the adapter's
`env_whitelist` (host API keys never leak); the adapter never runs untrusted model output (that stays in
the locked cage). The blueprint documents that user lifecycle scripts (`postCreateCommand`) run as the
user intends — that is the trusted boundary, explicitly chosen.

### 3.5 Dependency governance (§9)
`@devcontainers/cli` is the reference implementation of the open devcontainer spec — justified by
"standardization over invention." It is a **soft/optional** dependency: probed at runtime and degraded
when absent, **pinned** in the extension's `package.json` (Node). Nothing is added to Python
`requirements`.

## 4. Work Breakdown (mirrors PROJECT_MANIFEST §8.13)

- **8.13.1** Blueprint + ADR (this document ratified, ADR assigned).
- **8.13.2** `DevcontainerSandboxAdapter` backend tier + host-bridge `execute()`/`open_session()`.
- **8.13.3** Extension lifecycle owner: probe/degrade, `up`/`exec` driver, non-blocking build + timeout
  degrade, `RuntimePanel` status.
- **8.13.4** Additive host execution-bridge WS contract + provisioning status (`SCHEMA_EVOLUTION.MD §`).
- **8.13.5** `execution_tools`/MCP tier-selection wiring (AST-aware) + `devcontainer.json` scaffold/fallback
  (ties into Division 8.9 `.ailienant/` provisioning).
- **8.13.6** Checkpoint gate: trusted execution routes through the devcontainer when present; the untrusted
  oracle keeps the locked cage; CLI-absent and build-timeout degrade paths covered.

## 5. Definition of Done

`mypy .` 0 · `pytest` green · `npm run compile` 0. Charter checks: the locked oracle cage is provably
untouched; the new tier is reachable only for trusted execution; every provisioning path degrades and
never hangs; the CLI dependency is optional and probed; the wire contract is additive-only (§10).
