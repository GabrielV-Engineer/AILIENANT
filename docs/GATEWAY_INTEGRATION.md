# AILIENANT External Capability Gateway — Integration Guide

The gateway lets an external agent (Claude Code, Codex, or any MCP client) drive AILIENANT over the
**Model Context Protocol** on a stdio transport. It is a thin adapter over AILIENANT's existing task-submit,
memory, and benchmark substrate: it adds no orchestration and owns no graph state. This guide is for the
engineer wiring an external agent to that surface.

---

## 1. Launch

The gateway runs as a module over stdio:

```
python -m gateway
```

It boots standalone and answers `list_tools` from a static catalog with no host connection. A running host
(the AILIENANT VS Code extension / FastAPI engine) is required **only** when an EXECUTE-tier verb needs the
loopback substrate; READ_ONLY verbs answer in-process.

A typical MCP client registers it like any other stdio server:

```json
{
  "mcpServers": {
    "ailienant": {
      "command": "python",
      "args": ["-m", "gateway"],
      "env": {
        "AILIENANT_GATEWAY_TOKEN": "your-caller-token"
      }
    }
  }
}
```

At startup the gateway logs one line — the advertised protocol version and whether a caller token was seen
(`Token-Secured` vs `Anonymous (Shared Pool)`). The token value itself is never logged.

---

## 2. Authentication & caller identity

Each external agent authenticates with **its own** gateway caller token. The token is hashed into a stable
`caller_id`; all rate and budget accounting is keyed by it.

| Env var | Effect |
|---|---|
| `AILIENANT_GATEWAY_TOKEN` | Hashed into a private `caller_id` with its own durable ceilings. **Recommended.** |
| `AILIENANT_GATEWAY_CALLER_ID` | Explicit caller id; takes precedence over the token hash. |
| *(neither set)* | Calls fall into a single shared `anonymous` pool that contends one rate bucket and budget. |

This caller token is distinct from the host's loopback **service** token (an ephemeral secret the gateway reads
from `~/.ailienant/run.json` to satisfy the host's own auth on EXECUTE loopbacks). A user's UI token is never
reused, and no secret is ever surfaced to the caller.

> **Ergonomics:** set `AILIENANT_GATEWAY_TOKEN` so your agent gets an isolated ceiling. Running anonymous means
> sharing limits with every other unauthenticated caller.

---

## 3. Per-caller ceilings (DoS guard)

The gateway enforces a **durable** per-caller token-bucket rate limiter and a cumulative budget. The ledger is
disk-backed, so limits survive a reconnect (an ephemeral in-memory counter would reset on every restart). Under
lock contention the guard **fails closed** (denies rather than letting a call slip through).

| Env var | Default | Meaning |
|---|---|---|
| `AILIENANT_GATEWAY_RATE_CAP` | `60` | Token-bucket capacity (max burst). |
| `AILIENANT_GATEWAY_RATE_REFILL_PER_S` | `1.0` | Tokens refilled per second. |
| `AILIENANT_GATEWAY_BUDGET` | `1000000` | Cumulative budget ceiling per caller. |
| `AILIENANT_GATEWAY_BENCHMARK_COST` | `1.0` | Budget charged per `run_benchmark`. |
| `AILIENANT_GATEWAY_BENCH_CONCURRENCY` | `1` | Max concurrent benchmark runs (single-flight). |

Every call is metered — READ_ONLY included — so a probing flood is throttled regardless of tier.

---

## 4. Capability catalog (v1)

| Capability | Tier | Async | Arguments |
|---|---|---|---|
| `run_task` | EXECUTE | yes | `prompt`, `workspace_root` |
| `run_benchmark` | EXECUTE | yes | `suite` *(optional, default `v1`)* |
| `check_task_status` | READ_ONLY | no | `task_id` |
| `get_report` | READ_ONLY | no | `task_id` |
| `query_memory` | READ_ONLY | no | `query`, `workspace_root` |
| `get_dependents` | READ_ONLY | no | `symbol`, `workspace_root` |
| `get_workspace_graph` | READ_ONLY | no | `workspace_root` |

Each tool's `_meta` advertises `protocol_version`, `schema_version`, `tier`, `async`, and `deprecated` — read
them from `list_tools()` to discover the contract without an out-of-band channel.

### Async poll-pair pattern

Long-running EXECUTE verbs return a handle immediately; you poll for completion:

```
run_task / run_benchmark   →  { task_id, status: "submitted", poll: "check_task_status", then: "get_report" }
check_task_status(task_id) →  { status: "running" | "completed" | "unknown" | ... }
get_report(task_id)        →  { status: "running" | "completed" (+report) | "failed" | "not_found" }
```

For a benchmark: call `run_benchmark`, poll `check_task_status` until it is no longer `running`, then read
`get_report` for the machine-readable report (validated against `report.schema.json`).

---

## 5. Response envelopes

Every call returns a JSON envelope. A caller must handle these machine-readable shapes:

| `status` | `reason` (when present) | Meaning |
|---|---|---|
| `ok` | — | Success; the payload is under `result`. |
| `error` | `invalid_arguments` | A required argument was missing (`missing` lists them). |
| `error` | `host_unavailable` | An EXECUTE verb needs the host and none is running — open VS Code. |
| `error` | `unknown_capability` | No such verb. |
| `error` | `handler_error` | The handler faulted (`detail` carries the cause). |
| `denied` | `rate_exceeded` | The caller's rate bucket is empty. |
| `denied` | `budget_exceeded` | The caller's cumulative budget is spent. |
| `denied` | `permission_denied` | The verb's tier is not permitted. |
| `denied` | `requires_human_approval` | A DANGEROUS/HITL verb — see HITL-degrade below. |
| `busy` | `benchmark_busy` | A benchmark is already in flight (single-flight). |

Errors and denials are always structured JSON — never a transport exception.

---

## 6. HITL-degrade (the impedance mismatch)

AILIENANT's core value is a long-running, streaming, **human-in-the-loop** task. An external caller has no human
in its loop. So a tier-`DANGEROUS` / approval-gated action **degrades to an immediate, structured deny-report**
(what was blocked and why) and **never hangs** waiting for a click that will never come:

```json
{
  "status": "denied",
  "reason": "requires_human_approval",
  "capability": "...",
  "would_have_required": "human_approval",
  "message": "... drive this action through the AILIENANT VS Code extension."
}
```

The full-fidelity interactive path (streaming + real HITL approval) is the AILIENANT VS Code extension over its
REST + WebSocket surface — for clients that *do* have a human.

---

## 7. Versioning & deprecation policy

The gateway surface is a **permanent public contract** governed by semver:

- The surface advertises a `protocol_version` (semver) and each capability advertises a `schema_version`, both in
  every tool's `_meta` from `list_tools()`.
- A **breaking** change to a capability's argument schema bumps the **major** version. The old capability is
  marked `deprecated: true` in its `_meta`, with `deprecated_since` and a `sunset_version`.
- A deprecated capability is supported for **at least two minor releases** after deprecation before it is removed
  at its `sunset_version`.
- **Pin a version.** Read `_meta.protocol_version` and code against it; watch `_meta.deprecated` to migrate
  ahead of a sunset.

---

## 8. Host requirement

READ_ONLY verbs answer in-process and need no host. EXECUTE verbs (`run_task`, `run_benchmark`) reach the live
engine over loopback; if no host is running they fail fast with `host_unavailable` ("open VS Code to start the
AILIENANT engine") rather than hanging or raising.
