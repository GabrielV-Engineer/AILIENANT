# shared/logging_filters.py
"""Phase 6.7 — Secrets Scrubber: a process-wide DLP filter for logs.

``SecretsScrubber`` is the central redaction engine: it replaces API keys,
Bearer tokens, JWTs and URL-embedded credentials with ``REDACTED:<hash8>``,
where ``<hash8>`` is the first 8 hex chars of ``blake2b(secret)`` — diagnosable
(the same secret always maps to the same marker) without being reversible.

``SecretsScrubberFilter`` is a :class:`logging.Filter` that runs the engine over
every :class:`logging.LogRecord`. It is installed on the root logger **and on
every root handler** at startup (``main.py`` lifespan) — handler-level filtering
is what catches records propagated from named child loggers.

``core/audit.py`` consumes :class:`SecretsScrubber` directly, so the HITL audit
ledger and the logs share one set of patterns (no drift).

Blueprint reference: §8 (Secrets Scrubber).
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import List

# Redaction patterns (Blueprint §8.2). sk-ant- precedes sk- so an Anthropic key
# is matched whole. The URL pattern uses look-around so only the user:pass
# segment is redacted, leaving the surrounding "://" and "@" intact.
_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?<=://)[^:/\s]+:[^@/\s]+(?=@)"),
]


def _redact(match: re.Match[str]) -> str:
    """Replace a matched secret with ``REDACTED:<hash8>``."""
    h8 = hashlib.blake2b(match.group(0).encode("utf-8")).hexdigest()[:8]
    return f"REDACTED:{h8}"


class SecretsScrubber:
    """Stateless secrets-redaction engine shared by logs and the audit ledger."""

    @staticmethod
    def scrub(text: str) -> str:
        """Return ``text`` with every recognised secret redacted."""
        out = text
        for pattern in _PATTERNS:
            out = pattern.sub(_redact, out)
        return out


class SecretsScrubberFilter(logging.Filter):
    """Root logger/handler filter — redacts secrets in every ``LogRecord``.

    Scrubs ``record.msg`` and the string members of ``record.args`` (a filter
    runs *before* the message is ``%``-formatted, so both must be handled).
    Always returns ``True`` — this filter redacts, it never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = SecretsScrubber.scrub(record.msg)
        args = record.args
        if args:
            if isinstance(args, tuple):
                record.args = tuple(
                    SecretsScrubber.scrub(a) if isinstance(a, str) else a
                    for a in args
                )
            elif isinstance(args, dict):
                record.args = {
                    key: SecretsScrubber.scrub(val) if isinstance(val, str) else val
                    for key, val in args.items()
                }
        return True
