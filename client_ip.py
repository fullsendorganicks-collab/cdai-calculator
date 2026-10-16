"""Real client IP behind Render's reverse proxy, for security-sensitive rate limiting only.

Public socket address first, last X-Forwarded-For hop as fallback; IPv6 reduced to its /64, IPv4-mapped IPv6 to IPv4."""
from fastapi import Request


def _parse(value):
    import ipaddress
    try:
        a = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a


def real_client_ip(request: Request) -> str:
    """The address of the visitor, for rate limiting only (never IP-allowlist-like).

    Evidence (Render logs of this service, Oct 15 2026): uvicorn's `request.client.host` is already the visitor's true public IP and matches
    Render's own clientIP request log field. So:
      1. If client.host is a PUBLIC address (ipaddress `is_global`: not private, loopback, link-local, unspecified, CGNAT or documentation space),
         use it and IGNORE X-Forwarded-For entirely: a forged header changes nothing.
      2. Otherwise (host missing, not an IP such as the TestClient's 'testclient', or an internal address that means "a proxy in front of us"),
         use the LAST X-Forwarded-For hop if it parses as an IP (Render appends the true peer last; every earlier entry is attacker-controlled).
      3. Otherwise client.host, or 'unknown'."""
    host = request.client.host if request.client else None
    a = _parse(host)
    if a is not None and a.is_global:
        return str(a) if a.version == 4 else host
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops and _parse(hops[-1]) is not None:
            return hops[-1]
    return host or "unknown"


def ip_bucket(ip: str) -> str:
    """Rate-limit key for an address: IPv4 as is, IPv6 reduced to its /64 (one subscriber can rotate addresses freely inside a /64,
    so a per-address cap would never bind). Anything that does not parse is returned unchanged."""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped is not None:       # ::ffff:1.2.3.4 is the IPv4 caller 1.2.3.4 seen through a dual-stack socket: it must not collapse into ::/64 with everyone else
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return ip
