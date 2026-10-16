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


def _hop_ip(hop: str):
    """The address in one forwarded hop: bare IPv4, IPv4:port, bare IPv6, [IPv6], [IPv6]:port, with an optional IPv6 zone id (fe80::1%eth0).
    Returns the normalised address text, or None if it does not parse."""
    h = hop.strip()
    if h.startswith("["):
        end = h.find("]")
        if end == -1:
            return None
        h = h[1:end]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    a = _parse(h.split("%", 1)[0])
    return None if a is None else str(a)


def real_client_ip(request: Request) -> str:
    """Never for anything IP-allowlist-like.

    Render appends the true connecting IP as the LAST X-Forwarded-For entry and does not clear what the client sent, so every earlier entry is
    attacker-controlled: only the last is used. ALL header lines are joined (a second forged line must change nothing) and the RAW header is read
    on purpose. `request.client.host` is NOT trusted when the header is present: uvicorn's proxy-headers handling (always-trust mode, as run on
    Render) rewrites it to the FIRST forwarded entry, which a visitor controls. So: a header with hops decides alone (an unparseable last hop is
    'unknown', one shared bucket, never a fall back to the forgeable host); client.host is used only when the header is absent or has no hops."""
    lines = request.headers.getlist("x-forwarded-for")
    hops = [h.strip() for line in lines for h in line.split(",") if h.strip()]
    if hops:
        ip = _hop_ip(hops[-1]) or "unknown"
    else:
        ip = request.client.host if request.client else "unknown"
    _warn_once_if_shared(ip)
    return ip


def ip_bucket(ip: str) -> str:
    """Rate-limit key for an address: IPv4 as is, IPv6 reduced to its /64 (one subscriber can rotate addresses freely inside a /64,
    so a per-address cap would never bind). Anything that does not parse is returned unchanged."""
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped is not None:       # ::ffff:1.2.3.4 is the IPv4 caller 1.2.3.4 seen through a dual-stack socket: it must not collapse into ::/64 with everyone else
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return ip
