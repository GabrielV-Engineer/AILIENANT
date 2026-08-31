"""Workspace confinement for agent-supplied filesystem paths.

A path an agent names is untrusted input: it may come from a plan, a fetched web
page, a parsed document, or a model's own guess. The backend process runs with the
operator's own credentials, so an unconfined read reaches `~/.ssh`, `.env` files,
and anything else on the machine — a Local File Inclusion primitive with the same
shape as the SSRF one ``core.url_guard`` exists to stop, pointed at disk instead of
the network.

This module is the single predicate that answers "may this path be touched at all".
It is deliberately separate from a content firewall: whether a file is ignored,
binary, or oversized is a different question from whether it is *inside the
workspace*, and answering only the former is what let paths escape.

Symlinks are followed before the comparison, so a link inside the workspace pointing
outside it is denied rather than admitted on its literal location.

Two limitations, stated rather than implied:

* **No root, no confinement.** A caller that supplies no workspace root cannot be
  confined by definition; such calls are admitted. Every agent-facing read path
  supplies one — the root-less callers are internal, non-agent paths.
* **Time-of-check to time-of-use.** The path is resolved here and opened by the
  caller afterwards; a symlink swapped in between is not detected. Closing that
  needs the check and the open to share one file handle, which the callers'
  read-then-stat shape does not currently allow.
"""

from __future__ import annotations

import pathlib
from typing import Optional


def confine_to_root(path: str, root: Optional[str]) -> Optional[str]:
    """Return a deny reason, or ``None`` when ``path`` resolves inside ``root``.

    Never raises: an unresolvable path is itself a denial, since a location that
    cannot be verified must not be opened.
    """
    if not root:
        return None
    try:
        resolved = pathlib.Path(path).resolve()
        jail = pathlib.Path(root).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        return f"path {path!r} could not be resolved ({exc})"

    if resolved == jail or _is_within(resolved, jail):
        return None
    return f"path {path!r} resolves outside the workspace root"


def _is_within(resolved: pathlib.Path, jail: pathlib.Path) -> bool:
    """True when ``resolved`` sits under ``jail``.

    ``is_relative_to`` raises nothing on a different Windows drive — it returns
    False — which is the answer we want for a cross-drive path.
    """
    try:
        return resolved.is_relative_to(jail)
    except (OSError, ValueError):
        return False
