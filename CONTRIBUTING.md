# Contributing to AILIENANT

Thanks for your interest in improving AILIENANT! Contributions of every size are welcome — fixing a typo, sharpening the docs, closing a roadmap item, or hardening a subsystem. This guide gets you set up and explains the standards a pull request needs to meet.

> **Before anything else:** every contributor signs a one-time **[Contributor License Agreement (CLA)](CLA.md)**. See [§1](#1-sign-the-cla-required) — it's quick, and it's what keeps AILIENANT both open and sustainable.

---

## 1. Sign the CLA (required)

AILIENANT is **dual-licensed**: the open-core community edition under [AGPL-3.0](LICENSE), and a separate commercial/enterprise edition (see [LICENSING.md](LICENSING.md)). For the project to be able to offer your contribution under both, you grant it that right via the **[CLA](CLA.md)**.

- **You keep the copyright to your work.** The CLA is a license, not a transfer.
- **It's a one-time step.** Signing once covers all your future contributions.
- **No PR is merged until your CLA is on file.** When you open your first pull request, the automated CLA check posts a link; accept it there. If the automation is unavailable, use the manual sign-off in [CLA.md](CLA.md).

Without this, a single un-relicensable contribution would block the commercial edition for the whole project — which is why we can't waive it.

---

## 2. Set up your environment

You'll need **Python 3.10+** (3.13 recommended), **Node.js 20+**, and **VS Code 1.85+**. Docker is recommended for the full execution sandbox. See [HowToUseIt.md](HowToUseIt.md) for the user-level walkthrough; the contributor setup is:

```powershell
# Backend
cd ailienant-core
python -m venv venv
.\venv\Scripts\activate          # Unix/macOS: source venv/bin/activate
pip install -r requirements.txt
copy ..\.env.example ..\.env     # Unix/macOS: cp ../.env.example ../.env

# Extension
cd ..\ailienant-extension
npm install
npm run compile
```

New to the architecture? Read [HowItWorks.md](HowItWorks.md), then [DEVELOPERS.md](DEVELOPERS.md) for the deep internals.

---

## 3. Before you start coding

1. **Read the map.** Skim [docs/PROJECT_MANIFEST.md](docs/PROJECT_MANIFEST.md) to find the current phase and confirm your change fits the roadmap. The manifest is the source of truth for *what* is being built and in *what order*.
2. **Check the contracts.** If you're touching a subsystem governed by a `docs/PHASE_*_BLUEPRINT.md` or `docs/SCHEMA_EVOLUTION.MD`, read it first — those are binding architectural contracts.
3. **Open an issue for anything non-trivial.** Align on the approach before writing a large change. For roadmap items, a good starting point is the [Honest list of what is NOT implemented](DEVELOPERS.md#honest-list-of-what-is-not-implemented).

---

## 4. Coding standards

AILIENANT defaults to an uncompromising, enterprise-grade bar. Concretely:

- **Strict typing & linting — zero new errors.** After changing Python, run `mypy .`, `npx pyright`, and `ruff check .`; your change must not introduce a single new type error or lint warning. After changing TypeScript, `npm run compile` and `npm run lint` must be clean.
- **Boy-Scout rule.** If a file you edit already has type/lint errors, fix them while you're there. Leave it cleaner than you found it.
- **Timeless, English documentation.** Comments and docstrings explain the *why* and *how* of the implementation — not project-management metadata (no phase/ADR/blueprint references inside code). All comments, docstrings, and string literals are in professional English.
- **Granular edits.** Prefer targeted edits over full-file rewrites so existing comments and formatting survive. Prefer editing existing files over creating new ones unless the manifest calls for them.
- **Track tradeoffs.** If you ship an MVP or a deliberate workaround, declare it in the PR and log the follow-up refactor in [docs/TECH_DEBT_BACKLOG.md](docs/TECH_DEBT_BACKLOG.md). Zero untracked technical debt.

---

## 5. Quality gates (Definition of Done)

A change is only done when these return **Exit Code 0**:

```powershell
# Backend
cd ailienant-core
.\venv\Scripts\pytest.exe          # full suite, zero regressions
.\venv\Scripts\mypy.exe .          # the enforced typing gate
.\venv\Scripts\ruff.exe check .    # lint

# Extension
cd ..\ailienant-extension
npm run compile                    # tsc + esbuild
npm run lint                       # ESLint
```

Add or update tests for behavior you change. Phases ship a sibling `test_phase*_checkpoint_gate.py`; if you touch a gated contract, keep its gate green.

---

## 6. Git & pull-request workflow

- **Branch from `main`** in your fork and open a PR against `main`.
- **Conventional Commits.** Format: `type(scope): summary` — e.g. `feat(core): add VRAM fallback threshold`, `fix(byom): correct re-test 404`, `docs(readme): clarify install`. Keep the subject concise; put detail in the PR description, not a novel in the commit body.
- **PR description should include:** a one-line summary, a test plan, and a note on whether the change touches `mypy`-strict modules or any blueprint-governed contract.
- **Keep history clean.** Don't force-push over others' commits or rewrite shared history. Never skip hooks (`--no-verify`) or bypass signing unless explicitly asked.
- **Don't commit secrets.** API keys live in `.env` / secret storage, never in code or config you commit.

---

## 7. Documentation changes

- The **public README is translated into 7 languages** (`README.md` + `README.{es,fr,zh,hi,ru,it}.md`). If you change user-facing content in the English README, please note in your PR which translations need updating — or update them if you can. Keep the language-nav bar and links in sync across all seven.
- User-facing guides are [HowToUseIt.md](HowToUseIt.md) and [HowItWorks.md](HowItWorks.md); deep internals are [DEVELOPERS.md](DEVELOPERS.md). Put content where its audience will look for it.

---

## 8. Reporting bugs & requesting features

Open an issue with: what you expected, what happened, steps to reproduce, and your environment (OS, Python/Node versions, model setup). For security issues, please report privately rather than in a public issue.

---

## Code of conduct

Be respectful, assume good faith, and keep discussion technical and constructive. We want AILIENANT to be a project people are glad they contributed to.

Thank you for helping build it. 🐜
