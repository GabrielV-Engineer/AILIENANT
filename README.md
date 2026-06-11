<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

<img src="assets/logo.svg" alt="AILIENANT" width="340" />

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

- **🧠 It plans before it codes.** A dedicated *Planner* turns your request into a concrete spec and task list, freezes the scope, and watches for "drift" so the agent can't quietly wander off and rewrite half your project. A separate *Coder* carries that plan out. Two heads, each doing one job well.
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
| Plans, then codes (two-headed) | ✅ Planner + Coder, with drift guard | ❌ One model, one shot |
| Smart local↔cloud routing | ✅ Picks the cheapest tier that can do the job | ❌ Fixed |
| Shows cost in real time | ✅ Token ledger + budget ceiling | ⚠️ Usually hidden |
| Time-travel / branch a run | ✅ Durable checkpoints | ❌ Stateless |
| Sandboxed execution | ✅ Docker / Wasm / approval-gated | ⚠️ Often runs on host |
| Vendor lock-in | ✅ None — swap providers freely | ❌ Locked to one |

A fuller technical comparison lives in **[HowItWorks.md](HowItWorks.md)**.

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

Then open the project in VS Code and press **F5** to launch the extension. The first time you open an AILIENANT session it will start the backend for you and begin indexing your workspace. Configure your models from the in-app **BYOM** panel, type a request, and you're off.

---

## How it works (the short version)

```
You ask  ─▶  Planner  ─▶  drift guard  ─▶  Coder  ─▶  sandbox runs it
            (writes a            (scope          (edits        ▲      │
             spec + plan)         locked)         files)       │      ▼
                                                          fix it  ◀─ read the result
```

Behind the scenes, a stateful **LangGraph** engine routes each task between local and cloud models using a context-and-complexity score, retrieves the right files with **GraphRAG** (vector search + a one-hop dependency walk), and checkpoints every step so nothing is lost. The deep version — diagrams, the routing math, the execution loop, and the security model — is in **[HowItWorks.md](HowItWorks.md)**.

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
