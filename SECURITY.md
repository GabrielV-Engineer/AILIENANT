# Security Policy

AILIENANT's core surface is a sandboxing and permission engine — vulnerabilities here can have real
impact. Please report privately, not through a public issue.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the **Security** tab on this repository and select
**"Report a vulnerability."** This opens a private advisory visible only to maintainers until a fix is
ready, and lets us coordinate a disclosure timeline with you directly.

Please include:

- What you found and its potential impact.
- Steps to reproduce (a minimal repro is ideal).
- Affected version/commit.

## What to expect

We'll acknowledge a report and work with you on a fix and coordinated disclosure timeline. This is a
solo-developer, pre-launch project — response times are best-effort, not SLA-backed, but security
reports get priority over everything else in the queue.

## Scope

In scope: the FastAPI backend (`ailienant-core/`), the VS Code extension (`ailienant-extension/`), and
the sandboxing/permission engine specifically (Docker/Wasm/NativeHITL adapters, RBAC evaluation, the
HITL approval chain). Out of scope: third-party dependencies (report those upstream) and issues
requiring physical access to a user's machine.
