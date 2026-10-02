"""Small, bounded HTTP helpers for trusted server-configured integrations.

This is not a general URL fetcher. URLs never come from model/tool arguments.
HTTP is allowed only for literal loopback development endpoints; otherwise TLS
is required. Read caps apply to decoded response bytes before JSON parsing.
"""
from __future__ import annotations
import ipaddress
import json
import time
from urllib.parse import urlparse
import httpx


class ResponseLimitError(ValueError):
    pass


class ResponseDeadlineError(TimeoutError):
    pass


def validate_endpoint(value: str) -> str:
    try:
        p = urlparse(value)
        _ = p.port  # Force validation of malformed/out-of-range ports.
        if (p.scheme not in {"http", "https"} or not p.hostname or p.username is not None
                or p.password is not None or p.query or p.fragment or any(c.isspace() for c in value)
                or "\\" in value):
            raise ValueError
        if p.scheme == "http":
            loopback = p.hostname == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(p.hostname).is_loopback
            except ValueError:
                pass
            if not loopback:
                raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Use HTTPS, or literal loopback HTTP, without credentials/query/fragment") from error
    return value.rstrip("/") + "/"


def request_json(client: httpx.Client, method: str, path: str, *, limit: int,
                 deadline: float | None = None, parse_float=float, **kwargs):
    """Return status and parsed JSON. Do not parse/log bodies of non-2xx responses.

    A deadline is checked between reads, not a promise to preempt arbitrary
    blocking code. Callers must also set transport timeouts and bound tools.
    """
    def check_time():
        if deadline is not None and time.monotonic() >= deadline:
            raise ResponseDeadlineError("Response deadline exceeded")
    check_time()
    with client.stream(method, path, **kwargs) as response:
        if not 200 <= response.status_code < 300:
            return response.status_code, None
        length = response.headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError as error:
                raise ResponseLimitError("Invalid response size") from error
            if declared < 0 or declared > limit:
                raise ResponseLimitError("Response too large")
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=8192):
            check_time()
            if len(body) + len(chunk) > limit:
                raise ResponseLimitError("Response too large")
            body.extend(chunk)
        check_time()
    return response.status_code, json.loads(body, parse_float=parse_float)
