# Division 8.13 Blueprint — Polyglot Devcontainer Execution Layer

**Status:** Ratified — binding (Division 8.13 active).
**Resolves:** DEBT-035 (TypeScript sandbox) and the broader TS/Python runtime bias.
**ADR:** ADR-762.

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

**Illustrative wire shape** (non-normative — the authoritative, version-tagged contract is defined in
8.13.4 in `SCHEMA_EVOLUTION.MD`, additive-only per §10; field names here may change):

```
exec_request   { session_id, cmd, cwd }
exec_stream    { session_id, stream: out|err, chunk }
exec_exit      { session_id, code }
provisioning   { state: up|ready|timeout }
```

Secret **values** are deliberately **not** wire fields. The realized contract (`SCHEMA_EVOLUTION.MD §26`)
carries `env_keys` — allowlisted variable **names only, never values**: the host resolves the values from
its own environment when invoking `devcontainer exec`, so no secret value transits the loopback bridge
(the host already holds the user's environment — see §3.4). Each event also carries a `request_id` (UUID4)
correlation key so concurrent commands on one session never cross-talk and a retried inbound frame is
idempotent (Charter §5.3).

### 3.3 Async / non-blocking
`devcontainer up` is minutes-long. It runs **once per workspace** (lazy, idempotent, single-flight — the
same lazy-init discipline as the Docker daemon probe), emits a `provisioning` status event, and never
blocks the event loop (subprocess + async streaming). A build timeout degrades gracefully (DLQ / HITL
notice), never hangs — the never-hang contract from the gateway HITL-degrade work.

### 3.4 Security model (trusted tier)
Even trusted, AILIENANT keeps guardrails: the command's environment still passes through the adapter's
`env_whitelist` (host API keys never leak); the adapter never runs untrusted model output (that stays in
the locked cage). **Tier selection is the hard guard:** `select`/`resolve` route only *trusted* project
execution (`run_command`, the user's own tests, interactive sessions) to this adapter — untrusted oracle
output can never resolve here; it stays in the locked `DockerSandboxAdapter` cage (§2). The trusted
boundary is the user's own `devcontainer.json` / `postCreateCommand` / repo: those lifecycle scripts run
as the user intends — an explicitly chosen boundary, not an oversight.

**Selective HITL fallback (unavailable devcontainer):** when the devcontainer infrastructure is unavailable
*before a command runs* (no bridge, provisioning failed, no `devcontainer.json`), trusted execution does not
hard-fail and does not silently use the locked cage — it delegates to the HITL-gated **Native** tier
(`NativeHITLSandboxAdapter`): the command is proposed to the operator, suspended until explicit consent, then
run host-native (or DLQ'd on timeout/decline). This preserves loop continuity without compromising host
integrity. Two invariants keep it correct: (1) the fallback is the Native tier, never the untrusted-code cage
(§2); (2) it engages only *pre-execution* — a mid-execution failure degrades rather than re-run on the host
(idempotency). The `devcontainer.json` scaffold restores isolated execution and retires the prompts.

### 3.5 Dependency governance (§9)
`@devcontainers/cli` is the reference implementation of the open devcontainer spec — justified by
"standardization over invention." **Distribution model (host-prerequisite, ratified 8.13.4):** the
runnable `devcontainer` CLI is a **host prerequisite** sourced from the user's PATH or the installed Dev
Containers extension; it is **not** bundled in the `.vsix`. The `@devcontainers/cli` package is a
dev/test-only `devDependency` (the build excludes `node_modules`), chosen over vendoring the CLI's
transitive runtime tree per §9 (lightest viable dependency, no supply-chain bloat) and because trusted
execution already requires a local Docker daemon. The provisioner probes at runtime and degrades with an
actionable remediation message naming both supported sources when neither is present. Nothing is added to
Python `requirements`.

## 4. Work Breakdown (mirrors PROJECT_MANIFEST §8.13)

- **8.13.1** Blueprint + ADR — ✅ ratified (ADR-762).
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
