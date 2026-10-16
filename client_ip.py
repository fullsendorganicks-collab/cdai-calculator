"""Real client IP behind Render's reverse proxy, for security-sensitive rate limiting only.

Raw X-Forwarded-For, LAST hop; IPv6 reduced to its /64, IPv4-mapped IPv6 to IPv4."""
import ipaddress
import logging
import threading

from fastapi import Request

logger = logging.getLogger("cdai.calculator_leads")

_warned = False
_warn_lock = threading.Lock()


def _parse(value):
    try:
        return ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None


def _warn_once_if_shared(ip: str) -> None:
    """If the resolved caller is 'unknown' or not a public address, every visitor may be sharing one rate-limit bucket (a site-wide outage that
    would otherwise be silent). Say so ONCE per process, naming no address."""
    global _warned
    if _warned:
        return
    a = _parse(ip)
    if a is not None and a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    if a is not None and a.is_global:
        return
    with _warn_lock:
        if _warned:
            return
        _warned = True
    logger.warning("[CALC] caller address resolved to %s: if this repeats for real visitors, all of them share one rate-limit bucket",
                   "'unknown'" if a is None else "a non-public address")


def real_client_ip(request: Request) -> str:
    """Never for anything IP-allowlist-like.

    Render appends the true connecting IP as the LAST X-Forwarded-For entry and does not clear what the client sent, so every earlier entry is
    attacker-controlled: only the last is used. The RAW header is read on purpose. `request.client.host` is NOT trusted: uvicorn's proxy-headers
    handling (always-trust mode, as run on Render) rewrites it to the FIRST forwarded entry, which a visitor controls. client.host is only the
    fallback when the header is absent, empty or its last hop is not an IP; then 'unknown'."""
    ip = None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops and _parse(hops[-1]) is not None:
            ip = hops[-1]
    if ip is None:
        ip = request.client.host if request.client else "unknown"
    _warn_once_if_shared(ip)
    return ip


def ip_bucket(ip: str) -> str:
    """Rate-limit key for an address: IPv4 as is, IPv6 reduced to its /64 (one subscriber can rotate addresses freely inside a /64,
    so a per-address cap would never bind). Anything that does not parse is returned unchanged."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped is not None:       # ::ffff:1.2.3.4 is the IPv4 caller 1.2.3.4 seen through a dual-stack socket: it must not collapse into ::/64 with everyone else
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return ip
