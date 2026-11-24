"""
URL Validation and SSRF (Server-Side Request Forgery) Protection Module.

Provides comprehensive defenses against:
- Private, loopback, link-local, multicast, and reserved IP access
- Cloud metadata service access (e.g., 169.254.169.254, metadata.google.internal)
- Dangerous URL schemes (only http and https allowed)
- Alternative IP representations (decimal, hex, octal integers)
- DNS rebinding attacks via resolution inspection
- Private hostname patterns (.local, .internal, localhost, etc.)
"""

import ipaddress
import socket
import urllib.parse
from typing import Optional, Set, Tuple


class SSRFError(ValueError):
    """Raised when a URL or host violates SSRF protection policies."""
    pass


class InvalidURLError(ValueError):
    """Raised when a URL has an invalid format, unsupported scheme, or cannot be parsed."""
    pass


# Disallowed hostnames and suffixes
BLOCKED_HOSTNAMES: Set[str] = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "0.0.0.0",
    "127.0.0.1",
    "::1",
    "[::1]",
    "metadata.google.internal",
    "instance-data",
    "metadata",
}

BLOCKED_SUFFIXES: Tuple[str, ...] = (
    ".local",
    ".internal",
    ".localhost",
    ".lan",
    ".home",
    ".corp",
)

ALLOWED_SCHEMES: Set[str] = {"http", "https"}

# Carrier-Grade NAT network (RFC 6598)
CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")
# Cloud metadata address
CLOUD_METADATA_IP = "169.254.169.254"


def _check_ip_safety(ip_str: str) -> None:
    """
    Validates whether an IP address is public and safe to connect to.
    Raises SSRFError if the IP is private, loopback, link-local, multicast, or reserved.
    """
    clean_ip_str = ip_str.strip("[]")
    try:
        ip = ipaddress.ip_address(clean_ip_str)
    except ValueError as exc:
        raise SSRFError(f"Invalid IP address format: '{ip_str}'.") from exc

    # Unpack IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if str(ip) == CLOUD_METADATA_IP:
        raise SSRFError("Access to cloud metadata IP (169.254.169.254) is prohibited.")

    if ip.is_loopback:
        raise SSRFError(f"Access to loopback IP address ({ip}) is prohibited.")

    if ip.is_private:
        raise SSRFError(f"Access to private network IP address ({ip}) is prohibited.")

    if ip.is_link_local:
        raise SSRFError(f"Access to link-local IP address ({ip}) is prohibited.")

    if ip.is_multicast:
        raise SSRFError(f"Access to multicast IP address ({ip}) is prohibited.")

    if ip.is_reserved:
        raise SSRFError(f"Access to reserved IP address ({ip}) is prohibited.")

    if ip.is_unspecified:
        raise SSRFError(f"Access to unspecified IP address ({ip}) is prohibited.")

    if ip in CARRIER_GRADE_NAT:
        raise SSRFError(f"Access to carrier-grade NAT address ({ip}) is prohibited.")


def _try_parse_numeric_ip(hostname: str) -> Optional[ipaddress.IPv4Address]:
    """
    Detects and parses numeric representations of IPv4 addresses:
    e.g., decimal integer (2130706433), hex (0x7f000001), or octal (017700000001).
    """
    # Decimal integer
    if hostname.isdigit():
        try:
            val = int(hostname)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
        except (ValueError, OverflowError):
            pass

    # Hexadecimal
    if hostname.lower().startswith("0x"):
        try:
            val = int(hostname, 16)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
        except (ValueError, OverflowError):
            pass

    # Octal (e.g. starting with 0 and other octal digits)
    if hostname.startswith("0") and len(hostname) > 1 and all(c in "01234567" for c in hostname):
        try:
            val = int(hostname, 8)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
        except (ValueError, OverflowError):
            pass

    # Shorthand dotted representations like 127.1, 10.1, etc.
    try:
        if all(part.isdigit() or part.lower().startswith("0x") for part in hostname.split(".")):
            packed = socket.inet_aton(hostname)
            return ipaddress.IPv4Address(packed)
    except (OSError, socket.error, ValueError):
        pass

    return None


def check_ssrf_safety(
    url_or_host: str,
    allow_local: bool = False,
    require_resolvable: bool = False,
) -> bool:
    """
    Checks if a target URL or hostname is safe from SSRF attacks.

    Args:
        url_or_host: Full URL (http://example.com) or bare host/IP (example.com, 127.0.0.1)
        allow_local: If True, bypasses private/loopback checks (useful for specific local test harnesses)
        require_resolvable: If True, raises InvalidURLError if DNS resolution fails completely

    Returns:
        True if safe.

    Raises:
        SSRFError: If the target is an internal/private resource.
        InvalidURLError: If the scheme or host is invalid.
    """
    if not url_or_host or not isinstance(url_or_host, str):
        raise InvalidURLError("Target must be a non-empty string.")

    target = url_or_host.strip()

    # Determine if target is a full URL or host
    if "://" in target:
        parsed = urllib.parse.urlsplit(target)
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise InvalidURLError(
                f"Unsupported URL scheme '{scheme}'. Only HTTP and HTTPS are permitted."
            )
        if parsed.username or parsed.password:
            raise InvalidURLError("URLs with embedded user credentials are not permitted.")
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise InvalidURLError(f"Invalid host or port in target: {exc}") from exc
    else:
        # Bare host, possibly host:port
        if "/" in target:
            target = target.split("/")[0]
        if ":" in target and not target.startswith("["):
            parts = target.split(":")
            hostname = parts[0]
            try:
                port = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                raise InvalidURLError(f"Invalid port in target: '{target}'.")
        else:
            hostname = target
            port = None

    if not hostname:
        raise InvalidURLError("Host name is empty or missing.")

    hostname_clean = hostname.strip("[]").lower()

    if port is not None and not (1 <= port <= 65535):
        raise InvalidURLError(f"Port number {port} is out of valid range (1-65535).")

    # If allow_local is set, skip SSRF restrictions
    if allow_local:
        return True

    # 1. Check blocked hostnames and suffixes
    if hostname_clean in BLOCKED_HOSTNAMES:
        raise SSRFError(f"Access to blocked host '{hostname}' is prohibited.")

    for suffix in BLOCKED_SUFFIXES:
        if hostname_clean.endswith(suffix):
            raise SSRFError(f"Access to internal domain suffix '{suffix}' is prohibited.")

    # 2. Check numeric / encoded IP bypasses (decimal, hex, octal)
    numeric_ip = _try_parse_numeric_ip(hostname_clean)
    if numeric_ip is not None:
        _check_ip_safety(str(numeric_ip))

    # 3. Check direct IP address literal (IPv4 or IPv6)
    try:
        ipaddress.ip_address(hostname_clean)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal:
        _check_ip_safety(hostname_clean)
        return True

    # 4. DNS resolution to catch DNS rebinding and internal resolutions
    try:
        addr_info = socket.getaddrinfo(hostname_clean, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _family, _socktype, _proto, _canonname, sockaddr in addr_info:
            resolved_ip = sockaddr[0]
            _check_ip_safety(resolved_ip)
    except socket.gaierror as exc:
        # In offline/sandbox environments or unresolvable domains:
        if require_resolvable:
            raise InvalidURLError(
                f"Unable to resolve host '{hostname}': domain not found."
            ) from exc
    except socket.timeout as exc:
        if require_resolvable:
            raise InvalidURLError(
                f"DNS resolution timed out for '{hostname}'."
            ) from exc

    return True


def is_safe_url(url_or_host: str, allow_local: bool = False) -> bool:
    """
    Non-raising convenience check. Returns True if the target URL or host is SSRF-safe, False otherwise.
    """
    try:
        check_ssrf_safety(url_or_host, allow_local=allow_local)
        return True
    except (SSRFError, InvalidURLError, ValueError):
        return False


def validate_url(
    url: str,
    allow_local: bool = False,
    require_resolvable: bool = False,
) -> str:
    """
    Strictly validates and normalizes an input URL for website link analysis.
    Verifies HTTP/HTTPS scheme, format, and SSRF safety.

    Args:
        url: URL string to validate.
        allow_local: Whether to allow local network/loopback URLs.
        require_resolvable: Whether to enforce that domain must resolve via DNS immediately.

    Returns:
        Normalized URL string.

    Raises:
        InvalidURLError: If the URL is malformed or uses an invalid scheme.
        SSRFError: If the URL targets internal, private, or metadata network addresses.
    """
    if not url or not isinstance(url, str):
        raise InvalidURLError("URL must be a non-empty string.")

    cleaned_url = url.strip()

    if not (cleaned_url.startswith("http://") or cleaned_url.startswith("https://")):
        raise InvalidURLError("URL must begin with 'http://' or 'https://'.")

    parsed = urllib.parse.urlsplit(cleaned_url)

    if not parsed.scheme or parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only HTTP and HTTPS protocols are supported.")

    if not parsed.hostname:
        raise InvalidURLError("URL does not contain a valid hostname.")

    # Disallow embedded credentials (e.g. http://user:pass@example.com) for security
    if parsed.username or parsed.password:
        raise InvalidURLError("URLs with embedded user credentials are not permitted.")

    # Perform SSRF checks
    check_ssrf_safety(
        cleaned_url,
        allow_local=allow_local,
        require_resolvable=require_resolvable,
    )

    return cleaned_url
