# ailienant-core/core/exceptions.py
#
# Phase 2.22 — Project-wide exception hierarchy.


class PatchError(Exception):
    """Raised by core/patcher.py when a search/replace patch cannot be applied.

    Caught by CoderAgent to feed descriptive error back into the LLM retry loop.
    """
