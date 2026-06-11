# How to Use AILIENANT

A complete, step-by-step guide to installing, configuring, and working with AILIENANT — from a clean machine to your first finished task and beyond.

> New here? Read the [README](README.md) for the big picture first. Want to understand the machinery? See [HowItWorks.md](HowItWorks.md).

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Install the backend (Core)](#2-install-the-backend-core)
3. [Install the VS Code extension](#3-install-the-vs-code-extension)
4. [First launch](#4-first-launch)
5. [Connect your models (BYOM)](#5-connect-your-models-byom)
6. [Run your first task](#6-run-your-first-task)
7. [Execution modes: Plan, Ask, Auto](#7-execution-modes-plan-ask-auto)
8. [Reading what the agent is doing](#8-reading-what-the-agent-is-doing)
9. [Approving and rejecting changes (HITL)](#9-approving-and-rejecting-changes-hitl)
10. [Checkpoints, Rewind, and branching](#10-checkpoints-rewind-and-branching)
11. [Controlling cost](#11-controlling-cost)
12. [The Web Dashboard](#12-the-web-dashboard)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Prerequisites

| Requirement | Notes |
| --- | --- |
| **Python 3.10+** | 3.13 recommended. Runs the orchestration engine. |
| **Node.js 20+** | Builds the VS Code extension. |
| **VS Code 1.85+** | The editor that hosts the UI. |
| **A model source** | One or more of: a local [Ollama](https://ollama.com/) install, [LM Studio](https://lmstudio.ai/), a [LiteLLM](https://docs.litellm.ai/docs/simple_proxy) proxy, or cloud API keys. |
| **Docker** *(recommended)* | Enables the strongest execution sandbox. AILIENANT still runs without it, using safer fallbacks. |

You do **not** need a cloud account to get started — AILIENANT is designed to run entirely on local models.

---

## 2. Install the backend (Core)

The backend ("Core") is a Python service that runs the agent engine.

```powershell
cd ailienant-core

# Create and activate a virtual environment
python -m venv venv
.\venv\Scripts\activate          # Unix/macOS: source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create your environment file
copy ..\.env.example ..\.env     # Unix/macOS: cp ../.env.example ../.env
```

Open the new `.env` and set what you have. Everything has a sane default; the most common settings are:

| Variable | What it does |
| --- | --- |
| `LITELLM_PROXY_BASE_URL` | Where your LiteLLM proxy lives (default `http://localhost:4000`). |
| `AILIENANT_MODEL_SMALL` / `_MEDIUM` / `_BIG` | The model used at each routing tier (e.g. `ollama/qwen2.5-coder:7b`). |
| `AILIENANT_MAX_BUDGET_USD` | A hard per-task spending ceiling. |
| Cloud keys | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY` — only if you want cloud fallback. |

> You can configure models entirely from the UI later (see [§5](#5-connect-your-models-byom)); `.env` is just the headless option.

You normally don't start the server by hand — the extension does it for you. To run it manually for debugging:

```powershell
uvicorn main:app --reload --port 8000
```

---

## 3. Install the VS Code extension

```powershell
cd ailienant-extension
npm install
npm run compile
```

To use it:

- **Development:** open the `ailienant-extension` folder in VS Code and press **F5** to launch an *Extension Development Host* window.
- **Packaged install:** run `vsce package` to produce a `.vsix`, then in VS Code: *Extensions → ⋯ → Install from VSIX…*

---

## 4. First launch

1. Open your **project folder** in the Extension Development Host window.
2. Open the **AILIENANT** sidebar (its icon appears in the Activity Bar).
3. The extension checks whether the Core is running and, if not, **starts it for you** (controlled by the setting `ailienant.autoStartCore`, on by default).
4. A header pill shows the connection and indexing status: *Awaiting → Indexing % → ready*. AILIENANT is building a map of your codebase so it can retrieve the right files later.

> **Monorepo tip:** if your project root isn't the AILIENANT repo, set `ailienant.coreStartCommand` to the command that launches the Core, or use the manual **Start Core** button in the status pill.

---

## 5. Connect your models (BYOM)

BYOM = *Bring Your Own Model*. This is where you tell AILIENANT which engines to use.

1. Open the **Web Dashboard** (see [§12](#12-the-web-dashboard)) and go to the **BYOM** panel — or use the in-sidebar **Models** menu (`/models`).
2. **Add an endpoint.** Examples:
   - Ollama → `http://localhost:11434`
   - LM Studio → `http://localhost:1234`
   - A cloud provider → its base URL + your API key
3. Click **Test**. AILIENANT probes the endpoint and lists the models it actually found, with latency.
4. **Map models to tiers.** AILIENANT routes work across three tiers — *Small* (fast/cheap), *Big* (heavier local), and *Cloud* (flagship). Assign a model to each tier you want to use.
5. **Pick a profile:**

   | Profile | Use it when |
   | --- | --- |
   | **Medium** | You want only a light local model — fastest, lowest VRAM. |
   | **Big** | Refactors and multi-file work on a heavier local model. |
   | **Cloud** | Force cloud for every call (ignores the routing math). |
   | **Hybrid** *(default)* | Let AILIENANT choose per task. Recommended. |

Your settings are saved and applied to the next request — no restart needed.

---

## 6. Run your first task

1. In the sidebar chat, type a request in plain language. Be specific about the file(s) involved. You can reference files and folders with `@`:
   > `Add input validation to @src/api/users.py and a test for the empty-name case.`
2. Press Enter. AILIENANT will:
   - **Plan** — produce a spec and a task list (the WBS).
   - **Retrieve** — pull in the relevant files.
   - **Code** — propose edits as reviewable diffs.
   - **Run & verify** — execute commands/tests in the sandbox and read the results.
   - **Self-heal** — if something fails, it retries and fixes before handing back.
3. Review the proposed diffs and accept or reject them (see [§9](#9-approving-and-rejecting-changes-hitl)).

---

## 7. Execution modes: Plan, Ask, Auto

A mode selector in the composer controls **how much autonomy** the agent has. It maps directly to the permission engine, so it's a real safety control, not a hint.

| Mode | What happens |
| --- | --- |
| **Plan** | The agent thinks and proposes a plan but **makes no changes**. Great for exploring an approach. Accepting a plan re-submits it under a write-capable mode. |
| **Ask** *(default)* | The agent proposes changes and **pauses for your approval** before writing anything or running risky commands. |
| **Auto** | The agent applies changes and runs commands on its own, announcing each before it acts. Use when you trust the task and want speed. |

You can switch modes between turns. In **Plan** mode, AILIENANT runs a short Socratic "clarify the goal" dialogue before producing the plan.

---

## 8. Reading what the agent is doing

AILIENANT is built to be watched, not trusted blindly.

- **Thought Box** — a collapsible panel that streams the model's live reasoning. It auto-expands when reasoning starts and collapses when the answer begins.
- **Pipeline / Execution trace** — a per-turn, collapsible list of every step: files read, commands run, patches proposed. This is *evidence*, kept separate from the chat answer.
- **Execution Checklist** — your WBS task list, ticking from ☐ → 🔄 → ✅/✗ as the agent works through it.
- **Cell Audit (Glass-Box)** — for complex steps that need iteration, an accordion shows each loop: the command, its (sanitized) terminal output, and the resulting code diff, with a budget/iteration footer.
- **Telemetry HUD** — live inference speed, a concurrency ring, and the FinOps cost bar.

---

## 9. Approving and rejecting changes (HITL)

HITL = *Human-In-The-Loop*. When the agent proposes a change (in Ask mode) or hits a risky action or a budget limit, it pauses and surfaces a card:

- **Accept** — apply this diff / allow this action.
- **Reject** — discard it.
- **Comment-as-reject** — reject *and* leave a note explaining what you wanted instead; the agent uses that as feedback.

Approvals can also appear as native VS Code notifications. Every decision is written to a verifiable audit log.

> **Silent feedback:** if you accept a change but then immediately rewrite most of it (≥70% within 3 minutes), AILIENANT notices and learns from the implicit rejection, distilling it into a rule under `.ailienant/rules/`.

---

## 10. Checkpoints, Rewind, and branching

Every step of a task is saved as a durable checkpoint, which makes time-travel possible.

- **Rewind / Branch** — open the checkpoint picker from a message's actions (the ↪ button) to see the chain of checkpoints with timestamps. Pick one to **branch** a new session from that exact point — the parent transcript is preserved, and you explore an alternative without losing the original.
- **Abort savepoints** — if you stop a run mid-flight, that point is marked so you can resume or branch from it later.

This is ideal for "try approach A, then back up and try approach B" without manual `git` gymnastics.

---

## 11. Controlling cost

- **Budget ceiling.** Set `AILIENANT_MAX_BUDGET_USD` (or per task). When a task would exceed it, the FinOps gate halts and asks for approval instead of silently spending.
- **Token ledger.** The HUD and the dashboard show local vs. cloud token usage and an estimated savings figure from staying local.
- **Stay local.** The **Hybrid** profile only escalates to cloud when the routing math says a task genuinely needs it. Use **Medium**/**Big** to forbid cloud entirely.

---

## 12. The Web Dashboard

AILIENANT ships a local web dashboard (served by the Core) for the heavier control surfaces:

| Panel | What it's for |
| --- | --- |
| **Overview** | At-a-glance status. |
| **Hardware** | Live CPU/RAM/VRAM gauges. |
| **BYOM** | Model endpoints, testing, and presets ([§5](#5-connect-your-models-byom)). |
| **Rules** | Edit the agent's persona/governance (`SOUL.md`) and context rules. |
| **Staging Area** | A Monaco-powered diff review surface. |
| **Memory** | Browse the indexed code graph and vector map of your project. |
| **Audit** | The HITL audit ledger with blake2b chain verification. |
| **Telemetry** | Routing decisions and observability. |
| **Runtime** | Sandbox tier status; start Docker; pull the sandbox image. |

---

## 13. Troubleshooting

**The sidebar says the Core is unreachable.**
Check that Python deps installed cleanly and that nothing else holds port 8000. Use the **Start Core** button, or run `uvicorn main:app --port 8000` manually to see the error.

**Models don't appear / "no models found".**
Make sure your local engine is running (e.g. `ollama serve`) and the endpoint URL is right, then hit **Test** again in BYOM. For cloud, confirm the API key is set in `.env` or the BYOM panel.

**Commands aren't actually running / "execution deferred".**
That's the safety design: in **Plan** mode, execution is intentionally deferred. Switch to **Ask** or **Auto**. For full sandboxed execution, start Docker (Runtime panel → *Start Docker*, then *Pull image*); otherwise AILIENANT falls back to an approval-gated host runner.

**Indexing seems stuck.**
Large repos take a moment. The indexing pill shows progress; it's paused while the sidebar is hidden and resumes when visible. A `.ailienantignore` file lets you exclude paths from indexing.

**It's spending more than I expected.**
Lower `AILIENANT_MAX_BUDGET_USD`, switch the profile to **Medium**/**Big** to stay local, and watch the FinOps bar. The gate will stop and ask before crossing your ceiling.

---

*Still stuck? Open an issue — and if you'd like to help improve this guide, see [CONTRIBUTING.md](CONTRIBUTING.md).*
