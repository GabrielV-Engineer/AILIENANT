# AILIENANT: Autonomous Agent Operational Rules & Context Guardrails

## 🎯 1. STRATEGIC CONTEXT & SOURCE OF TRUTH
You are an autonomous contributor to **Project AILIENANT**. Your primary directive is to maintain architectural integrity and prevent technical debt.
- **Mandatory Context Anchoring:** Before proposing or executing any mutation, you MUST read and analyze the `PROJECT_MANIFEST.md`, `README.md` and `DEV_JOURNAL.md` to understand the full roadmap and the current phase status.
- **Supporting Docs:** Cross-reference `SCHEMA_EVOLUTION.MD` for state definitions, `SYSTEM_PROMPTS.md` for agent logic, and `docs/PHASE_7_14_BLUEPRINT.md` for the active Phase 7.16/7.17 contract before modifying core backend/frontend boundaries.
- **🔒 PHASE 7.16 / 7.17 LOCK-IN (active until Phase 7.16 + 7.17 closure):** While any task touches Phase 7.16 (`7.16.0`–`7.16.3`) or Phase 7.17 (`7.17.x`) scope, you MUST read [`docs/PHASE_7_14_BLUEPRINT.md`](docs/PHASE_7_14_BLUEPRINT.md) **before** every task — including read-only research, design questions, and code mutations. It is binding for: persona/identity (ADR-701), the cognitive-transparency + token-throttling contract (ADR-702), analyst context-injection — budget, uuid-tag XML sandboxing, context-tolerant version tagging (ADR-703), structured-JSON envelope handling (ADR-704), the security posture (ADR-705), the VS Code mesh — inline mutations, abort savepoints, streaming markdown parser (ADR-706), and any chat-surface system-prompt edit. Any deviation requires a blueprint amendment in the same PR.

## 🛠️ 2. TECHNICAL STACK & COMMANDS
- **Backend (Python):** Located in `/ailienant-core`. Use `venv/bin/python` and `pytest`.
- **Frontend (TS/VS Code):** Located in `/ailienant-extension`. Use `npm` for builds and linting.
- **Build/Test Commands:**
  - Backend Lint/Type Check: `mypy .`, `flake8` or `npx pyright` (to catch Pylance UI errors in CLI)
  - Backend Tests: `pytest`
  - Frontend Build: `npm run compile`
  - Frontend Lint: `npm run lint`

### 🛡️ STRICT TYPING & LINTING POLICY
To maintain codebase health and eliminate technical debt, you MUST strictly follow these rules during every interaction:
1. **Zero-Degradation (Stop the bleeding):** After modifying any Python code, you MUST execute `npx pyright` and `mypy .`. Your specific changes MUST NOT introduce a single new type error, syntax issue, or Pylance warning.
2. **The Boy Scout Rule (Gradual Healing):** If you modify a file and notice that it already contains existing Pylance/Pyright or syntax errors, you MUST opportunistically fix them alongside your requested changes. Leave the file cleaner than you found it.

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

## 🧹 6. CODE COMMENTS & BLUEPRINT REFERENCES STRICT POLICY
Every time you modify or create code, you MUST strictly adhere to the following rules regarding comments, docstrings, and string literals:
1. **Reference Eradication:** Whenever you encounter existing comments or strings inside a file that make reference to a specific phase, sub-phase, ADR, or blueprint, you MUST actively delete or scrub those project management references to keep the codebase clean.
2. **Zero-Reference Creation:** When writing new comments or strings, you MUST NOT mention any phase, sub-phase, or blueprint of the current process. 
   - *CRITICAL:* Code documentation must be timeless. Your explanations must focus purely on the "why" and "how" of the technical implementation, deliberately omitting any historical project management metadata.
3. **Strict English Standardization:** The codebase must maintain a single language. Whenever you encounter existing comments, docstrings, or string literals written in Spanish during your modifications, you MUST accurately translate them into professional technical English. All newly created documentation and strings MUST be written exclusively in English.

## 🏗️ 7. ENTERPRISE-GRADE STANDARD & TECHNICAL DEBT MANAGEMENT
Every change to the codebase must default to an uncompromising Enterprise-level standard. However, if a tactical tradeoff is required, you MUST manage it strictly:
1. **Enterprise Default:** You must prioritize highly scalable, robust, and Enterprise-grade solutions for every creation or modification. Do not compromise architecture or performance unless strictly constrained by the current environment.
2. **MVP / Patch Declaration:** If you implement an MVP (Minimum Viable Product) version, a localized patch, or a workaround specifically designed to contain the "blast radius" or respect current system constraints, you MUST explicitly declare this tradeoff in your response.
3. **Deferred Refactoring Phase:** Every time an MVP or temporary patch is implemented, you MUST immediately propose and log a future phase or sub-phase (e.g., in `TECH_DEBT_BACKLOG.md` or the project's WBS) dedicated entirely to the "Enterprise Refactor" of that specific implementation. Zero untracked technical debt is allowed.

## 🛑 GIT COMMIT PROTOCOL (WINDOWS / POWERSHELL STRICT)

Whenever you are instructed to generate Git commands for the user to execute, you MUST adhere to the following Zero-Friction formatting rules:

1. **NO MULTILINE STRINGS:** You are strictly PROHIBITED from using Bash Heredocs (`<<EOF`) or PowerShell Here-Strings (`@'` ... `'@`). 
2. **FORMAT:** You MUST use the inline double-flag format: `git commit -m "feat: title here" -m "Brief 1-2 sentence description here."`
3. **NO NOVELS IN COMMITS:** Do not dump exhaustive changelogs, variable lists, or DoD checklists inside the commit message. The commit body (`-m`) must be a concise summary. Verbose architectural details belong ONLY in `DEV_JOURNAL.md` or PR notes.
4. **GRANULAR ADDS:** Always format `git add` commands one per line (e.g., `git add file1.py \n git add file2.ts`) instead of inline chaining or backslash (`\`) continuation, which breaks in some PowerShell environments.

**Violation of this protocol will result in rejected mutations.**