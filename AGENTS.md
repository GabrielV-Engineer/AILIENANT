# AILIENANT — Agent Instructions Pointer

This file exists only for cross-tool agent compatibility (OpenSpec, and any assistant that reads `AGENTS.md` by convention rather than `CLAUDE.md`).

**The canonical operational charter is [`CLAUDE.md`](CLAUDE.md).** Read it in full before proposing or executing any mutation — it covers strategic context anchoring, technical stack commands, domain paradigms, architectural conflict detection, engineering invariants, security posture, mutation protocols, verification gates, dependency governance, contract evolution, technical-debt management, observability, and comment/documentation policy.

A handful of rules are safety-critical regardless of which file an assistant reads, restated here so they can never be missed by a tool that only consults `AGENTS.md`:

1. **Git execution is non-autonomous.** Never run `git commit`, `git push`, or any other Git command yourself. Print the exact command block for the human to run.
2. **No global installs.** Never install packages globally; always use the project's local environments (`ailienant-core`'s venv, `ailienant-extension`'s local `node_modules`, or the repo-root `package.json` for cross-cutting tooling).
3. **No exception hiding.** A bare or broad `except` must either re-raise or log the root cause with `exc_info=True` plus an explicit justification comment. Never swallow an error silently.
4. **No phase/blueprint references in code.** New comments, docstrings, and string literals must be timeless — never mention a phase, sub-phase, ADR, or blueprint number.

For anything else — architecture stances, verification commands, the phase-closure protocol, contract-evolution rules — defer to `CLAUDE.md`, not this file.
