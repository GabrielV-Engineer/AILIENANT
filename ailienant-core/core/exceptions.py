# ailienant-core/core/exceptions.py
#
# Phase 2.22 — Project-wide exception hierarchy.


class PatchError(Exception):
    """Raised by core/patcher.py when a search/replace patch cannot be applied.

    Caught by CoderAgent to feed descriptive error back into the LLM retry loop.
    """


class StaleFileException(Exception):
    """Raised by apply_patch_to_vfs when OCC detects a concurrent file modification.

    Caught by make_patch_file_tool to return a descriptive re-read instruction to the LLM.
    """
