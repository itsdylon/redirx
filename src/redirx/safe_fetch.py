"""
SSRF-hardened fetching primitives.

Every server-side fetch of a user-supplied URL (domain discovery, the demo
site auditor, pipeline scraping) must go through a session built with
create_safe_connector(). The guarantees:

- DNS resolution and validation happen at connect time: the connector opens
  connections only to the exact IPs the validating resolver returned, so a
  hostname cannot resolve to a public IP for the check and a private IP for
  the connection (DNS rebinding).
- Redirect hops go through the same connector, so a public domain that 302s
  to an internal address fails at the connect step for that hop.
- Denied ranges: RFC1918, loopback, link-local (incl. cloud metadata
  169.254.169.254), unique-local IPv6, reserved/unspecified/multicast,
  0.0.0.0/8, and any operator-supplied CIDRs (SSRF_DENY_CIDRS env var).

validate_public_url() adds the string-level rules (http/https only, ports
80/443 only) that the resolver cannot see.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlparse

import aiohttp
from aiohttp.resolver import DefaultResolver

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443)
MAX_REDIRECTS = 5

_DENY_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",      # carrier-grade NAT
        "127.0.0.0/8",
        "169.254.0.0/16",     # link-local / cloud metadata
        "172.16.0.0/12",
        "192.168.0.0/16",
        "198.18.0.0/15",      # benchmarking
        "::1/128",
        "fc00::/7",           # unique-local IPv6
        "fe80::/10",          # link-local IPv6
        "::ffff:0:0/96",      # IPv4-mapped — checked again after unwrapping
    )
]

# Operator-supplied extra ranges (e.g. our own VPC CIDRs), comma-separated.
for _raw in os.getenv("SSRF_DENY_CIDRS", "").split(","):
    _raw = _raw.strip()
    if _raw:
        try:
            _DENY_NETWORKS.append(ipaddress.ip_network(_raw))
        except ValueError:
            pass


class SSRFBlockedError(Exception):
    """Raised when a fetch target fails SSRF validation. Fail closed."""

    user_message = "Cannot reach that host."


def is_forbidden_ip(ip_str: str) -> bool:
    """Whether an IP (v4 or v6, as a string) is in a denied range."""
    try:
        addr = ipaddress.ip_address(ip_str.split("%")[0])  # strip zone id
    except ValueError:
        return True  # not a parseable IP — refuse

    # Unwrap IPv4-mapped IPv6 (::ffff:10.0.0.1) so v4 rules apply.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped

    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    ):
        return True
    return any(addr in net for net in _DENY_NETWORKS)


def validate_public_url(url: str) -> None:
    """
    String-level SSRF rules: http/https only, standard ports only, and an
    early rejection when the host is a raw IP in a denied range. Raises
    SSRFBlockedError on violation.
    """
    try:
        parsed = urlparse(url)
        # urllib checks invalid/out-of-range ports lazily. Keep those failures
        # inside the same bounded rejection contract as every other URL rule.
        host = parsed.hostname or ""
        explicit_port = parsed.port
    except Exception as exc:
        raise SSRFBlockedError() from exc

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SSRFBlockedError()
    if not host:
        raise SSRFBlockedError()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal"):
        raise SSRFBlockedError()
    port = explicit_port if explicit_port is not None else (443 if parsed.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise SSRFBlockedError()

    # Raw IP literal? Validate immediately — no DNS involved.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    if is_forbidden_ip(host):
        raise SSRFBlockedError()


async def resolve_and_validate_host(host: str, port: int = 443) -> list[str]:
    """
    Resolve a hostname and validate every resolved IP before any connection
    is opened. Stricter than SafeResolver on purpose: if ANY answer is in a
    denied range the host is refused outright, since a mixed public/private
    answer set is a rebinding signature rather than a legitimate config.

    Use this for an early, clean rejection at entry points. It does not
    replace the connect-time validation in SafeResolver — a hostname can
    return different answers between this call and the actual connection.

    Raises:
        SSRFBlockedError: on resolution failure or any forbidden address.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise SSRFBlockedError() from exc

    ips = [info[4][0] for info in infos if info[4]]
    if not ips:
        raise SSRFBlockedError()
    for ip in ips:
        if is_forbidden_ip(str(ip)):
            raise SSRFBlockedError()
    return ips


class SafeResolver(DefaultResolver):
    """
    Resolver that refuses to return IPs in denied ranges. Because aiohttp's
    TCPConnector connects to exactly the addresses the resolver returns,
    this validates at connect time for the original request and every
    redirect hop, defeating DNS rebinding.
    """

    async def resolve(self, host, port=0, family=socket.AF_INET):
        # Literal IPs skip the parent resolver's getaddrinfo indirection.
        infos = await super().resolve(host, port, family)
        safe = []
        for info in infos:
            if not is_forbidden_ip(str(info.get("host", ""))):
                safe.append(info)
        if not safe:
            raise SSRFBlockedError()
        # Return ONLY validated addresses — never let a mixed answer through.
        return safe


def create_safe_connector(**kwargs) -> aiohttp.TCPConnector:
    """
    TCPConnector wired with the validating resolver. use_dns_cache=False so
    every new connection re-resolves and re-validates (rebinding via a
    poisoned cache entry is off the table); connection keep-alive still
    amortizes most of the cost.
    """
    kwargs.setdefault("use_dns_cache", False)
    return aiohttp.TCPConnector(resolver=SafeResolver(), **kwargs)
