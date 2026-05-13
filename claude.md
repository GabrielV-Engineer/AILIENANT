# AILIENANT: Autonomous Agent Operational Rules & Context Guardrails

## 🎯 1. STRATEGIC CONTEXT & SOURCE OF TRUTH
You are an autonomous contributor to **Project AILIENANT**. Your primary directive is to maintain architectural integrity and prevent technical debt.
- **Mandatory Context Anchoring:** Before proposing or executing any mutation, you MUST read and analyze the `PROJECT_MANIFEST.md` to understand the full roadmap and the current phase status.
- **Supporting Docs:** Cross-reference `SCHEMA_EVOLUTION.MD` for state definitions and `SYSTEM_PROMPTS.md` for agent logic logic before modifying core backend/frontend boundaries.

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