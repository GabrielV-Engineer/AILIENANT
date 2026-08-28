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

Known limitation — DNS rebinding: this guard resolves the hostname, and the HTTP
client then resolves it again independently when it connects. A name server the
attacker controls can answer the two lookups differently (public first, private
second), which no amount of validation here can detect. Closing it requires
pinning the validated address for the connection itself, which is tracked
separately; the guard still stops every literal-address and single-resolution
attempt, which is the entire class an untrusted page or file can express.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

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


def validate_fetch_url(url: str) -> Optional[str]:
    """Return a human-readable deny reason, or ``None`` when the URL may be fetched.

    Never raises: a malformed URL or a failed resolution is itself a denial, since
    a destination that cannot be verified must not be contacted.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"malformed URL ({exc})"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"scheme {parsed.scheme or '(none)'!r} is not allowed; use http or https"

    host = parsed.hostname
    if not host:
        return "URL has no host"

    lowered = host.lower().rstrip(".")
    if lowered.endswith(_INTERNAL_SUFFIXES):
        return f"host {lowered!r} resolves inside a private network namespace"

    # A literal IP skips resolution entirely — parsing it directly also prevents a
    # resolver from being consulted for an address the caller already supplied.
    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None
    if literal is not None:
        return _classify_ip(literal)

    try:
        addresses = _resolve_all(lowered)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        return f"host {lowered!r} could not be resolved ({exc})"
    if not addresses:
        return f"host {lowered!r} resolved to no addresses"

    for address in addresses:
        reason = _classify_ip(address)
        if reason is not None:
            return f"{reason} for host {lowered!r}"
    return None


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
