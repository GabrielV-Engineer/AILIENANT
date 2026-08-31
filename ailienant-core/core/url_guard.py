"""Destination validation for outbound agent-initiated HTTP fetches.

An agent that fetches a URL is a Server-Side Request Forgery primitive: the URL
may come from a web page, a file, or any other untrusted surface, and the process
making the request sits inside the operator's network with the backend's own
loopback interface reachable. This module is the single chokepoint that decides
whether a destination may be contacted at all.

Deny-by-default for every non-public destination. Loopback alone has an opt-in
(``WEB_FETCH_ALLOW_LOOPBACK``): reading a local dev server is a legitimate coding
task, but it must be a conscious operator choice. Private, link-local, and
cloud-metadata ranges have no escape hatch — no coding task needs them and the
metadata endpoint is a credential-disclosure vector.

Pure and synchronous apart from name resolution, so it is exhaustively testable
without a network.

DNS rebinding is closed by pinning rather than by re-checking: ``resolve_fetch_target``
returns the address it approved, and the caller connects to that literal address while
keeping the original hostname for the ``Host`` header and TLS SNI. The client therefore
never performs a second, unchecked resolution, and certificate verification still runs
against the real hostname (empirically confirmed: without the SNI override the same
request is rejected by verification, so this is not a weakening of ``verify``).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import ParseResult, urlparse, urlunparse

from shared.config import WEB_FETCH_ALLOW_LOOPBACK

# Only these two schemes can carry a document worth reading. Everything else
# (file, ftp, gopher, data) either reads local disk or is a classic SSRF pivot.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Hostname suffixes that resolve inside container/orchestrator networks rather
# than on the public internet. Checked textually because they frequently resolve
# to a public-looking address from outside the cluster.
_INTERNAL_SUFFIXES: tuple[str, ...] = (
    ".internal",
    ".local",
    ".localdomain",
    ".cluster.local",
)

# Redirect chains are followed manually so every hop is re-validated; this bounds
# the walk. Matches httpx's own default ceiling, so behaviour on legitimate URLs
# is unchanged.
MAX_REDIRECT_HOPS: int = 20


def _classify_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Optional[str]:
    """Return a deny reason for a non-public address, or ``None`` if it is routable.

    Loopback is separated from the other private classes because it is the only
    one an operator may deliberately re-enable.
    """
    if ip.is_loopback:
        if WEB_FETCH_ALLOW_LOOPBACK:
            return None
        return "loopback address (set AILIENANT_WEB_FETCH_ALLOW_LOOPBACK=1 to allow)"
    if ip.is_link_local:
        # Covers 169.254.0.0/16, which carries the cloud instance-metadata
        # endpoint (169.254.169.254) on every major provider.
        return "link-local address (cloud instance metadata range)"
    if ip.is_private:
        return "private network address"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved, multicast, or unspecified address"
    return None


def _resolve_all(host: str) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to every address it maps to.

    Every result is validated, not just the first: a host publishing both a public
    and a private record must not pass on the strength of whichever the resolver
    happened to order first.
    """
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(str(info[4][0])) for info in infos]


@dataclass(frozen=True)
class FetchTarget:
    """A destination approved for one connection, carrying the address checked.

    ``connect_url`` addresses the validated IP literally while ``host`` remains the
    name for the ``Host`` header and TLS SNI, so the connection lands on exactly the
    address this module approved and the certificate is still verified against the
    real hostname.
    """

    host: str
    address: str
    connect_url: str


def _pinned_url(parsed: "ParseResult", address: str) -> str:
    """Rebuild ``parsed`` addressing ``address`` literally, preserving everything else."""
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def resolve_fetch_target(url: str) -> Tuple[Optional[str], Optional[FetchTarget]]:
    """Validate ``url`` and return ``(deny_reason, None)`` or ``(None, target)``.

    Every address the host maps to is checked; the first is pinned for the actual
    connection. Pinning is what closes DNS rebinding: without it this module and the
    HTTP client resolve the name independently, and a name server the attacker
    controls can answer the two lookups differently — public for the check, private
    for the connection.

    Never raises: a malformed URL or a failed resolution is itself a denial, since a
    destination that cannot be verified must not be contacted.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"malformed URL ({exc})", None

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return (
            f"scheme {parsed.scheme or '(none)'!r} is not allowed; use http or https",
            None,
        )

    host = parsed.hostname
    if not host:
        return "URL has no host", None

    lowered = host.lower().rstrip(".")
    if lowered.endswith(_INTERNAL_SUFFIXES):
        return f"host {lowered!r} resolves inside a private network namespace", None

    # A literal IP skips resolution entirely — parsing it directly also prevents a
    # resolver from being consulted for an address the caller already supplied.
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _classify_ip(literal)
        if reason is not None:
            return reason, None
        return None, FetchTarget(host=lowered, address=str(literal), connect_url=url)

    try:
        addresses = _resolve_all(lowered)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        return f"host {lowered!r} could not be resolved ({exc})", None
    if not addresses:
        return f"host {lowered!r} resolved to no addresses", None

    for address in addresses:
        reason = _classify_ip(address)
        if reason is not None:
            return f"{reason} for host {lowered!r}", None

    pinned = str(addresses[0])
    return None, FetchTarget(
        host=lowered, address=pinned, connect_url=_pinned_url(parsed, pinned)
    )


def validate_fetch_url(url: str) -> Optional[str]:
    """Return a human-readable deny reason, or ``None`` when the URL may be fetched."""
    return resolve_fetch_target(url)[0]


def redact_url(url: str) -> str:
    """Strip credentials and query values so a URL is safe to write to a log.

    A URL an agent was handed may carry an API key in its query string or basic-auth
    credentials in its userinfo; the path alone is enough to diagnose a failure.
    Parameter NAMES are preserved because they are diagnostic and not secret.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<unparseable url>"

    netloc = parsed.hostname or ""
    if parsed.username:
        netloc = f"<redacted>@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    query = parsed.query
    if query:
        keys = sorted({pair.split("=", 1)[0] for pair in query.split("&") if pair})
        query = "&".join(f"{key}=<redacted>" for key in keys)

    return urlunparse((parsed.scheme, netloc, parsed.path, "", query, ""))
