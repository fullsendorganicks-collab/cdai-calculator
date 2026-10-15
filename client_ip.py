"""Real client IP behind Render's reverse proxy, for security-sensitive rate limiting only.

Same semantics as the module of the same name in the engine repo (Oct 14 2026): last X-Forwarded-For entry, IPv6 reduced to its /64."""
from fastapi import Request


def real_client_ip(request: Request) -> str:
    """Never for anything IP-allowlist-like.

    Render does not clear or reset a client-supplied X-Forwarded-For header —
    it only appends the true connecting IP as the last entry (confirmed:
    https://feedback.render.com/features/p/send-the-correct-xforwardedfor).
    That means every entry except the last can be attacker-supplied, so only
    the last entry is trustworthy. `request.client.host` alone would be
    Render's own internal load balancer, not the caller, since Render proxies
    over HTTP rather than passing the raw TCP connection through."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def ip_bucket(ip: str) -> str:
    """Rate-limit key for an address: IPv4 as is, IPv6 reduced to its /64 (one subscriber can rotate addresses freely inside a /64,
    so a per-address cap would never bind). Anything that does not parse is returned unchanged."""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return ip
