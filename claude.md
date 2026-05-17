# AILIENANT: Autonomous Agent Operational Rules & Context Guardrails

## 🎯 1. STRATEGIC CONTEXT & SOURCE OF TRUTH
You are an autonomous contributor to **Project AILIENANT**. Your primary directive is to maintain architectural integrity and prevent technical debt.
- **Mandatory Context Anchoring:** Before proposing or executing any mutation, you MUST read and analyze the `PROJECT_MANIFEST.md`, `README.md` and `DEV_JOURNAL.md` to understand the full roadmap and the current phase status.
- **Supporting Docs:** Cross-reference `SCHEMA_EVOLUTION.MD` for state definitions and `SYSTEM_PROMPTS.md` for agent logic logic before modifying core backend/frontend boundaries.
- **🔒 PHASE 4 LOCK-IN (active until Phase 4 closure):** While any task in this session touches Phase 4 scope (`4.1`–`4.5` in `PROJECT_MANIFEST.md`), you MUST read [`docs/PHASE_4_BLUEPRINT.md`](docs/PHASE_4_BLUEPRINT.md) **before** every task — including read-only research, design questions, and code mutations. The blueprint is the load-bearing contract: any deviation (state-channel additions, role-matrix edits, threshold changes, mode-routing logic) requires an explicit blueprint amendment in the same PR. This directive auto-expires once Phase 4.5 Checkpoint Gate is marked `[x]` in the manifest.

## 🛠️ 2. TECHNICAL STACK & COMMANDS
- **Backend (Python):** Located in `/ailienant-core`. Use `venv/bin/python` and `pytest`.
- **Frontend (TS/VS Code):** Located in `/ailienant-extension`. Use `npm` for builds and linting.
- **Build/Test Commands:**
  - Backend Lint/Type Check: `mypy .` or `flake8`
  - Backend Tests: `pytest`
  - Frontend Build: `npm run compile`
  - Frontend Lint: `npm run lint`

## 🛡️ 3. ARCHITECTURAL FORESIGHT & CONFLICT DETECTION (CRITICAL)
You act as a **Strategic Auditor**. Before applying any change:
1. **Roadmap Impact Analysis:** Evaluate if the current modification clashes with future phases defined in `PROJECT_MANIFEST.md`.
2. **Conflict Notification:** If a task fulfillment risks blocking a future milestone or violates `SCHEMA_EVOLUTION.MD` contracts, you **MUST STOP** and notify the user.
3. **Resolution Protocols:** Provide the following options to the user:
   - **Option A (Pivot):** Propose an alternative implementation that satisfies both the current task and future scalability.
   - **Option B (Manifest Update):** Suggest specific modifications to the WBS in `PROJECT_MANIFEST.md` to accommodate the new architectural reality.
   - **Option C (Refactor):** Identify existing technical debt that must be cleared before proceeding.

## 📝 4. MUTATION PROTOCOLS
- **Zero-Trust Execution:** Always use `ls` and `grep` to build a mental AST of the files before editing.
- **Granular Edits:** Prefer targeted edits over full file rewrites to preserve existing comments and formatting.
- **Definition of Done (DoD):** A task is only complete if the verification commands (Lint/Test) return **Exit Code 0**.
- **No Global Installs:** Never attempt to install packages globally. Always use local environments.

## 🏁 5. PHASE CLOSURE & GIT PROTOCOL (MANUAL EXECUTION)
Every time you complete a phase or sub-phase defined in `PROJECT_MANIFEST.md`, you MUST strictly perform the following documentation updates before declaring completion:
1. **`PROJECT_MANIFEST.md` Update:** Mark the corresponding task/sub-phase as completed `[x]`.
2. **`DEV_JOURNAL.md` Update:** Append a concise engineering log entry detailing the date, status, files changed, and explicit architectural outcomes or calibration details using the same structure and language already used in the document.
3. **`README.md` Update:** Reflect any global system changes. 
   - *CRITICAL:* If your implementation created any new file or structural directory, you MUST explicitly find and update the **"Repository Layout"** section inside `README.md` to maintain an accurate structural tree.
4. **Git Command Output (Strictly Non-Autonomous):** Do NOT execute Git commands yourself. At the absolute end of your response, you MUST provide the precise block of `git` commands so the user can manually stage, commit, and push the changes. The commit message must follow conventional commits formats (e.g., `feat(core): ...`, `test(memory): ...`) and include a detailed body outlining the changes.

## 🛑 GIT COMMIT PROTOCOL (WINDOWS / POWERSHELL STRICT)

Whenever you are instructed to generate Git commands for the user to execute, you MUST adhere to the following Zero-Friction formatting rules:

1. **NO MULTILINE STRINGS:** You are strictly PROHIBITED from using Bash Heredocs (`<<EOF`) or PowerShell Here-Strings (`@'` ... `'@`). 
2. **FORMAT:** You MUST use the inline double-flag format: `git commit -m "feat: title here" -m "Brief 1-2 sentence description here."`
3. **NO NOVELS IN COMMITS:** Do not dump exhaustive changelogs, variable lists, or DoD checklists inside the commit message. The commit body (`-m`) must be a concise summary. Verbose architectural details belong ONLY in `DEV_JOURNAL.md` or PR notes.
4. **GRANULAR ADDS:** Always format `git add` commands one per line (e.g., `git add file1.py \n git add file2.ts`) instead of inline chaining or backslash (`\`) continuation, which breaks in some PowerShell environments.

**Violation of this protocol will result in rejected mutations.**