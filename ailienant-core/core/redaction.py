# ailienant-core/core/redaction.py
#
# Shared secret-redaction utility. Any free-text field that leaves the server
# (telemetry reads, the command log, dashboard payloads) is passed through
# ``mask_secrets`` first so credentials echoed into stdout, reasons, or command
# strings never round-trip to a client.

import re
from typing import List, Optional

# Cap the text fed to the masking regex so a giant log line cannot trigger
# catastrophic backtracking (ReDoS) and pin a CPU.
_MASK_INPUT_CAP: int = 2_000
_REDACTED: str = "***REDACTED***"

# ReDoS-safe secret patterns — bounded quantifiers, no nesting. The key=value
# pattern uses a non-greedy value capture so a single mask never swallows an
# entire line that holds several secrets.
_KV_SECRET_RE: re.Pattern[str] = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)(\S+?)(?=\s|$)"
)
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),          # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{8,}"),               # AWS access key id
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"),  # bearer tokens
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),           # long hex blobs
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),   # long base64 blobs
]


def truncate_middle(text: str, cap: int) -> str:
    """Keep the head and tail of an over-long string, eliding the middle.

    Middle-truncation preserves both the leading context (a command, a tool's
    args) and the trailing signal (the last lines of output, an error), which
    is where the useful content usually sits — a head-only or tail-only cap
    would silently drop one or the other. Cheap: the slices copy at most
    ``cap`` characters regardless of the source length.

    Shared by every masking site (`core/exec_log.py`'s command/output ring,
    the tool-dispatch activity detail) so truncation shape stays consistent
    wherever free text is capped before it leaves the server; call this
    BEFORE ``mask_secrets`` — that function's own internal cap is head-only,
    so pre-truncating here is what preserves the tail on an over-long input.
    """
    if len(text) <= cap:
        return text
    half = cap // 2
    dropped = len(text) - (half * 2)
    return f"{text[:half]}\n…[{dropped} chars truncated]…\n{text[-half:]}"


def mask_secrets(text: Optional[str]) -> Optional[str]:
    """Redact secrets from a free-text field before it leaves the server.

    ReDoS-safe: input is truncated to ``_MASK_INPUT_CAP`` chars before any
    regex runs, and every pattern uses bounded/non-nested quantifiers. Returns
    the input unchanged when it is empty or ``None``.
    """
    if not text:
        return text
    snippet = text[:_MASK_INPUT_CAP]
    snippet = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", snippet)
    for pat in _SECRET_PATTERNS:
        snippet = pat.sub(_REDACTED, snippet)
    if len(text) > _MASK_INPUT_CAP:
        snippet += "…"
    return snippet
