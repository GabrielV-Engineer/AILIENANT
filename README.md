<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/icon-color.svg" alt="AILIENANT" width="340" />

<h1>AILIENANT</h1>

<p><strong>The AI coding teammate that plans before it codes — and runs on your machine, your models, your terms.</strong></p>

<p>
  <strong>English</strong> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh.md">中文</a> ·
  <a href="README.hi.md">हिन्दी</a> ·
  <a href="README.ru.md">Русский</a> ·
  <a href="README.it.md">Italiano</a>
</p>

<p>
  <a href="LICENSE"><img alt="License: AGPL-3.0" src="https://img.shields.io/badge/License-AGPL%20v3-blue.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-5.9-3178C6?logo=typescript&logoColor=white">
  <img alt="VS Code" src="https://img.shields.io/badge/VS%20Code-Extension-007ACC?logo=visualstudiocode&logoColor=white">
  <img alt="Status" src="https://img.shields.io/badge/status-active%20development-success">
</p>

<!-- TODO: add a live CI badge once the public repo slug is final, e.g.
     ![CI](https://github.com/<owner>/<repo>/actions/workflows/docker-publish.yml/badge.svg) -->

</div>

---

## What is AILIENANT?

**AILIENANT is an autonomous coding agent that lives inside VS Code.** You describe what you want in plain language; AILIENANT writes a real plan, makes the edits, runs the code in a sandbox, reads the results, and fixes its own mistakes — while showing you every step of its thinking.

What makes it different from the popular AI assistants is **where it runs and how it decides.** AILIENANT is **local-first**: it can run entirely on your own machine with open models (Ollama, LM Studio, and others), reaching for the cloud only when a task genuinely needs it — and it tells you, in dollars, when it does. Your code doesn't have to leave your laptop, and you are never locked into a single vendor.

> **In one line:** a private, cost-aware, plan-first AI engineer for your codebase — open source, with no vendor lock-in.

<!-- TODO: drop a short demo GIF here, e.g. assets/demo.gif -->

---

## Why people use it

- **🧠 It plans before it codes.** A real team of specialized agents — a *Researcher* maps your code, a *Planner* turns the request into a concrete spec and task list and freezes the scope, an *Orchestrator* drives the steps, a *Coder* (in one of 8 expert roles) makes the edits, and an *Analyst* you can chat with explains the codebase. A drift guard keeps the agent from quietly wandering off and rewriting half your project.
- **🔒 Your code stays yours.** Run 100% locally with your own models. No mandatory cloud, no telemetry phone-home, no training on your repository.
- **💸 You see the cost.** Every task has a live token ledger and a hard budget ceiling. Local vs. cloud usage and estimated savings are shown, not hidden.
- **🪟 You see the thinking.** A live "Thought Box" streams the model's reasoning, and a step-by-step trace shows every file read, command run, and patch proposed.
- **⏪ You can rewind.** Every step of a task is a durable checkpoint. Branch from any point to explore an alternative — true time-travel debugging for an agent.
- **🛡️ It runs code safely.** Generated commands execute inside a sandbox (Docker, with WebAssembly and human-approval fallbacks), never blindly against your machine.
- **🔌 No lock-in.** Bring your own model and provider — Ollama, LM Studio, vLLM, llama.cpp, OpenAI, Anthropic, Google, DeepSeek, Mistral, and more — and switch any time.

---

## How is it different?

| | **AILIENANT** | Typical cloud assistant |
| --- | --- | --- |
| Runs fully on your machine | ✅ Local-first, BYO model | ❌ Cloud-only |
| Plans, researches, codes, self-checks | ✅ A 5-agent team with a drift guard | ❌ One model, one shot |
| Smart local↔cloud routing | ✅ Picks the cheapest tier that can do the job | ❌ Fixed |
| Shows cost in real time | ✅ Token ledger + budget ceiling | ⚠️ Usually hidden |
| Time-travel / branch a run | ✅ Durable checkpoints | ❌ Stateless |
| Sandboxed execution | ✅ Docker / Wasm / approval-gated | ⚠️ Often runs on host |
| Vendor lock-in | ✅ None — swap providers freely | ❌ Locked to one |

A fuller technical comparison lives in **[HowItWorks.md](HowItWorks.md)**.

---

## The team inside

AILIENANT isn't one model doing everything — it's a small team of specialists, each with one job, wired together by a stateful **LangGraph** engine:

| Agent | What it does |
| --- | --- |
| 🔭 **Researcher** | Builds a "skeleton map" of your codebase — signatures and cross-module relationships — so the Planner reasons over real structure, not guesses. |
| 🧭 **Planner** | Turns your request into a concrete, schema-validated spec and task list (a WBS), then **freezes the scope** so the work can't sprawl. |
| 🎛️ **Orchestrator** | Drives the plan step by step, coordinating state and routing each step to the right model tier. |
| 🛠️ **Coder** | Makes the actual edits — adopting one of **8 expert roles** per task. |
| 💬 **Analyst (Natt)** | A read-only tutor you can chat with. It explains your code and AILIENANT itself, but never touches files — the *voice*, not the *hand*. |

The Coder specializes into the role each task needs: **core-dev, architect/refactor, devops/infra, secops, qa-tester, doc-manager, vcs-manager, data/ML engineer** — each with its own tools, guardrails, and approval triggers (e.g. a `.env` edit always pauses for you).

When a step fails, a **self-healing** loop reads the error and proposes a corrected patch before giving up; for open-ended steps, a bounded **ReAct cell** works against a live terminal until the job is done. The full per-agent breakdown is in **[HowItWorks.md](HowItWorks.md)**.

---

## Security & safety, by design

AILIENANT assumes that an autonomous agent will eventually try to do something it shouldn't — and is built to contain it.

- **Sandboxed by default.** Commands run in an isolated Docker container (read-only workspace, no network, non-root) with WebAssembly and human-in-the-loop fallbacks when Docker isn't available.
- **Fail-closed permissions.** Every tool is classified by privilege; anything unrecognized is treated as **dangerous until proven safe**, never the other way around.
- **Human approval where it matters.** Risky actions and budget overruns pause for your explicit approval.
- **Tamper-evident audit trail.** Approvals are recorded in a cryptographically chained (blake2b) ledger you can verify.
- **Multi-tenant isolation.** Every piece of indexed memory is namespaced to its workspace, so projects never leak into each other.

---

## Quick start

> Full walkthrough: **[HowToUseIt.md](HowToUseIt.md)**

**Prerequisites:** Python 3.10+ (3.13 recommended), Node.js 20+, VS Code 1.85+, and at least one model source (a local Ollama/LM Studio install, a [LiteLLM](https://docs.litellm.ai/docs/simple_proxy) proxy, or cloud API keys).

```powershell
# 1. Backend (the orchestration engine)
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix: source venv/bin/activate
pip install -r requirements.txt
copy ..\.env.example ..\.env     # Unix: cp ../.env.example ../.env

# 2. Extension (the VS Code UI)
cd ..\ailienant-extension
npm install
npm run compile
```

Then open the project in VS Code and press **F5** to launch the extension. The first time you open an AILIENANT session it **starts the backend for you on an automatically-assigned local port** (a free `127.0.0.1` port, e.g. `http://127.0.0.1:59247/`) and wires the UI to it — there's no port to configure. It then begins indexing your workspace. Configure your models from the in-app **BYOM** panel, type a request, and you're off.

> Running the backend by hand (headless / CI)? Launch it with `uvicorn main:app --port 8000` and point the extension's `backendUrl` setting at it. The auto-assigned port is only for the normal in-VS-Code flow.

---

## How it works (the short version)

```
You ask ─▶ Researcher ─▶ Planner ─▶ drift guard ─▶ Coder ─▶ sandbox runs it
           (maps the     (spec +      (scope         (edits      ▲      │
            codebase)     plan)        locked)        files)      │      ▼
                                                            self-heal ◀─ read the result
```

Behind the scenes, a stateful **LangGraph** engine routes each task between local and cloud models using a context-and-complexity score — always picking the **cheapest tier that can do the job** and only reaching for the cloud when a task genuinely needs it.

It retrieves the right files with **GraphRAG**: instead of dumping whole files into the prompt, it indexes your code as a dependency graph (Tree-sitter) with vector embeddings, then pulls just the relevant slice via vector search + a k-hop dependency walk ranked by importance (PageRank). That keeps prompts small — a **~70 % mean reduction in prompt size** — which is exactly what lets AILIENANT **run well on modest hardware**: per-tier budgets keep context inside a small local model's window (as little as a 4 K-token window), and the index lives in a fast, RAM-first store. Every step is checkpointed so nothing is lost. The deep version — diagrams, the routing math, the execution loop, and the security model — is in **[HowItWorks.md](HowItWorks.md)**.

---

## Chat with your codebase: the Analyst

Not every question needs the agent to *do* something — sometimes you just want to understand. The **Analyst (Natt)** is a chat companion living in a side panel: ask it *"how does auth flow through this service?"*, *"what would break if I change this function?"*, or even *"how does AILIENANT's routing actually work?"* and it answers in plain language.

It's a **read-only tutor — the voice, never the hand.** It explains, traces, and teaches, but it never edits your files, so you can explore freely without it changing anything.

What makes its answers trustworthy is **what it's grounded in** — three sources at once: your code's **knowledge graph** (so it cites the real structure, not a hallucination), your **workspace README** (so it knows your project's intent), and **AILIENANT's own product docs** (so it can explain the tool itself). And because explaining is cheaper than coding, you **pick the answer model** from a small selector — a fast local model for quick questions, a stronger one for a deep architectural walkthrough — without affecting retrieval quality.

---

## Memory you can see

AILIENANT's understanding of your codebase isn't a black box. The built-in **dashboard** renders the GraphRAG index as an **interactive knowledge graph** — a force-directed map of your files and their dependencies, where the most-connected "hub" files stand out, related modules share a color, and importance (PageRank) drives the layout. A companion 2D **vector map** projects how the engine *semantically* groups your code. It's a living picture of what the agent knows, and how it decides what to read.

---

## An open ecosystem

- **🧩 MCP servers.** AILIENANT speaks the **Model Context Protocol**, with a curated registry of vetted servers (GitHub, Brave Search, Docker, Postgres) you can enable in a click. Every MCP tool is **privilege-classified** — unknown tools are treated as dangerous until proven safe — and trusted only for the session after you approve them.
- **⚡ Skills.** Save reusable instruction snippets — global or per-workspace — and drop them into any prompt. Your own command templates, versioned with the project.
- **🧰 Tools.** Agents act through a typed, role-gated tool registry: reading and tracing code, editing files transactionally, running commands in the sandbox, and asking you when they're unsure. The catalog is **growing toward ~56 role-assigned tools** (see the roadmap in **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)**); the full table — which agent uses which tool — is in **[HowItWorks.md](HowItWorks.md)**.

---

## Dreaming: it improves while you're away

Coding is bursty — you step out for lunch, you log off for the night. **Dreaming Mode** turns that idle time into progress. You point AILIENANT at what to think about — *architecture & patterns*, *refactoring & technical debt*, *bug fixes*, the whole workspace, or a theme you type — and while you're away it works that focus autonomously: studying the code, **consolidating what it learns into long-term memory**, and exploring improvements. It self-corrects as it goes and **stops on its own if errors start to compound**.

Crucially, it **never wakes on a timer to ambush your machine** — *you* decide when to spend the resources by starting it when you step away. It's **budget-capped** (it refuses once a session's spend ceiling is hit) and safe: if you come back and save a file mid-pass, that pass aborts cleanly without writing.

Pick the **profile** that fits the break you're taking — they trade speed, cost, and depth:

| Profile | Best for | Roughly |
| --- | --- | --- |
| **Medium** | A lunch break — light, fully local | 1 task · 3 files · ~60 min |
| **Big** | Overnight — deeper, more files, local | 3 tasks · 10 files · nightly |
| **Cloud** | Top-quality reasoning, bounded by tokens | 1 task · 5 files · token-capped |
| **Hybrid** | Cloud *plans*, local model *edits* — quality at lower cost | 2 tasks · 6 files |

The full mechanism — what each profile can actually achieve, the time envelopes, and how the offline tree-search (MCTS) vets candidate changes — is in **[HowItWorks.md](HowItWorks.md)**.

---

## Live terminal & control panel

The agent works against a **persistent, interactive terminal** — a real shell session that remembers its working directory and environment across commands, streams output live, and can be interrupted — all inside the sandbox. The **control panel** (an in-app dashboard, served locally) gives you eleven views over a running session: cost and routing telemetry, hardware and runtime status, the memory graph, BYOM models, MCP servers and skills, governance rules, a staging area to review pending patches, a tamper-evident audit ledger, and crash recovery.

---

## Documentation

| Doc | For whom |
| --- | --- |
| **[HowToUseIt.md](HowToUseIt.md)** | Anyone — install, configure, and run your first task, step by step |
| **[HowItWorks.md](HowItWorks.md)** | The curious — architecture, routing, and the safety model explained |
| **[DEVELOPERS.md](DEVELOPERS.md)** | Core developers — deep internals, diagrams, pseudocode, code map |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | Contributors — setup, standards, and how to send a great PR |
| **[docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md)** | The full phase-by-phase roadmap |

---

## Contributing

AILIENANT is open source and contributions are welcome — from fixing a typo to closing a roadmap item. Please start with **[CONTRIBUTING.md](CONTRIBUTING.md)**.

One thing to know up front: because the project is dual-licensed (see below), every contributor signs a quick **[Contributor License Agreement](CLA.md)** before their first PR is merged. It's a one-time step and you keep the copyright to your work.

---

## License

AILIENANT is **open-core and dual-licensed**:

- **Community Edition — [GNU AGPL-3.0](LICENSE).** Free to use, study, modify, and share. If you distribute it or run a modified version as a network service, you share your source under the same license.
- **Commercial / Enterprise Edition.** For organizations that can't accept the AGPL's terms or want enterprise features and support.

See **[LICENSING.md](LICENSING.md)** for the full picture and how to obtain a commercial license.

> The **AILIENANT** name and logos are trademarks of the project and are not covered by the AGPL.

---

<div align="center">

**Built for engineers who want an AI teammate they can actually trust — and audit.**

Standing on the shoulders of <a href="https://github.com/langchain-ai/langgraph">LangGraph</a> · <a href="https://lancedb.com/">LanceDB</a> · <a href="https://tree-sitter.github.io/">Tree-sitter</a> · <a href="https://github.com/BerriAI/litellm">LiteLLM</a> · <a href="https://docs.pydantic.dev/">Pydantic</a>.

</div>
