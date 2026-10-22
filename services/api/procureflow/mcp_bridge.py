"""Read-only stdio bridge backend. Talks only to one configured ProcureFlow API.

No database, ERP, model, login, refresh, arbitrary URL or HTTP write capability.
Every call re-reads the same existing advice receipt through business auth.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from .domain import digest
from .errors import DomainError
from .read_tools import invoke_snapshot, validate_tool_arguments

MAX_RESPONSE_BYTES = 256_000
MAX_TOOL_SECONDS = 8.0
MAX_CALLS = 64
ID = re.compile(r"^adv_[a-f0-9]{32}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
SAFE_CODES = frozenset({
    "TOOL_POLICY_DENIED", "TOOL_ARGUMENTS_INVALID", "TOOL_SCOPE_DENIED",
    "MCP_CONFIG_INVALID", "MCP_UNAUTHENTICATED", "MCP_ACCESS_DENIED",
    "MCP_UPSTREAM_UNAVAILABLE", "MCP_UPSTREAM_INVALID", "MCP_RESPONSE_LIMIT",
    "MCP_TIMEOUT", "MCP_CONTEXT_STALE", "MCP_CONTEXT_INVALID", "MCP_CALL_LIMIT",
})
EMPTY_ERROR = "MCP_UPSTREAM_INVALID"


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code if code in SAFE_CODES else EMPTY_ERROR
        super().__init__(self.code)


def _base_url(value: str, approved_host: str, token: str) -> str:
    try:
        url = urlsplit(value)
        port = url.port
        host = url.hostname or ""
        if (not value.isascii() or any(c.isspace() for c in value) or
                url.username is not None or url.password is not None or
                url.path not in ("", "/") or url.query or url.fragment or
                not host or "\\" in value or "%" in value or
                (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        loopback = host in {"127.0.0.1", "::1"}
        if url.scheme == "http":
            if not loopback:
                raise ValueError()
        elif url.scheme == "https":
            if not approved_host or host != approved_host or token.startswith("demo-"):
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host):
                    raise ValueError()
                if "." not in host or host.endswith((".local", ".localhost")) or ".." in host:
                    raise ValueError()
            else:
                if not address.is_global:
                    raise ValueError()
        else:
            raise ValueError()
        return value.rstrip("/") + "/"
    except (ValueError, TypeError) as error:
        raise BridgeError("MCP_CONFIG_INVALID") from error


@dataclass(frozen=True)
class BridgeConfig:
    base_url: str
    token: str = field(repr=False)
    advice_run_id: str
    approved_host: str = ""
    expected_input_hash: str = ""

    def __post_init__(self):
        if (type(self.base_url) is not str or type(self.approved_host) is not str or
                type(self.expected_input_hash) is not str or
                type(self.token) is not str or not 1 <= len(self.token) <= 4096 or
                any(ord(c) < 33 or ord(c) > 126 for c in self.token) or
                type(self.advice_run_id) is not str or not ID.fullmatch(self.advice_run_id) or
                (self.expected_input_hash and not HASH.fullmatch(self.expected_input_hash))):
            raise BridgeError("MCP_CONFIG_INVALID")
        object.__setattr__(self, "base_url", _base_url(self.base_url, self.approved_host, self.token))

    @classmethod
    def from_environment(cls, environment):
        # Deliberately do not instantiate Settings/read DB, ERP or LLM variables.
        return cls(base_url=environment.get("PF_MCP_API_BASE_URL", "http://127.0.0.1:8000"),
                   token=environment.get("PF_MCP_BEARER_TOKEN", ""),
                   advice_run_id=environment.get("PF_MCP_ADVICE_RUN_ID", ""),
                   approved_host=environment.get("PF_MCP_APPROVED_HOST", ""),
                   expected_input_hash=environment.get("PF_MCP_INPUT_HASH", ""))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(_):
    raise ValueError("invalid JSON constant")


def checked_snapshot(receipt: dict, config: BridgeConfig, previous_hash: str | None) -> tuple[dict, str]:
    """Check the API's scoped current receipt; this does not certify source semantics."""
    if type(receipt) is not dict:
        raise BridgeError("MCP_CONTEXT_INVALID")
    if receipt.get("id") != config.advice_run_id:
        raise BridgeError("MCP_CONTEXT_INVALID")
    if receipt.get("current") is not True or receipt.get("stale_reason") is not None:
        raise BridgeError("MCP_CONTEXT_STALE")
    snapshot = receipt.get("input_snapshot")
    fingerprint = receipt.get("input_hash")
    if (type(snapshot) is not dict or type(fingerprint) is not str or not HASH.fullmatch(fingerprint)
            or not all(k in snapshot for k in ("request", "quotes", "documents", "policy", "quote_collection_hash"))):
        raise BridgeError("MCP_CONTEXT_INVALID")
    request, quotes, docs, policy = (snapshot[k] for k in ("request", "quotes", "documents", "policy"))
    if (type(request) is not dict or type(quotes) is not list or type(docs) is not dict or
            type(policy) is not dict or len(quotes) > 20 or len(docs) > 20 or
            request.get("id") != receipt.get("request_id") or
            type(request.get("version")) is not int or
            request["version"] != receipt.get("request_version") or
            any(type(q) is not dict or q.get("document_id") not in docs for q in quotes) or
            any(type(d) is not dict or d.get("id") != k or type(d.get("fragments")) is not list
                or d.get("trust") != "untrusted_source_content" for k, d in docs.items())):
        raise BridgeError("MCP_CONTEXT_INVALID")
    if digest(snapshot) != fingerprint or digest(quotes) != snapshot["quote_collection_hash"]:
        raise BridgeError("MCP_CONTEXT_INVALID")
    if (snapshot["quote_collection_hash"] != receipt.get("quote_collection_hash") or
            policy.get("policy_hash") != receipt.get("policy_hash") or
            policy.get("version") != receipt.get("policy_version")):
        raise BridgeError("MCP_CONTEXT_INVALID")
    if (previous_hash and fingerprint != previous_hash) or (
            config.expected_input_hash and fingerprint != config.expected_input_hash):
        raise BridgeError("MCP_CONTEXT_STALE")
    return snapshot, fingerprint


class AdviceBridge:
    """At most one request in flight. No cached data after token revocation."""

    def __init__(self, config: BridgeConfig, *, transport=None, deadline=MAX_TOOL_SECONDS):
        self.config = config
        self.deadline = deadline
        self._lock = asyncio.Lock()
        self._input_hash = None
        self._calls = 0
        self._client = httpx.AsyncClient(
            base_url=config.base_url, transport=transport, trust_env=False,
            follow_redirects=False, timeout=deadline,
            headers={"Authorization": f"Bearer {config.token}", "Accept": "application/json",
                     "Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    async def close(self):
        await self._client.aclose()

    async def _read_receipt(self):
        # All URL components are operator-configured/validated, never model arguments.
        path = f"api/v1/advice-runs/{self.config.advice_run_id}"
        async with self._client.stream("GET", path) as response:
            if response.status_code == 401:
                raise BridgeError("MCP_UNAUTHENTICATED")
            if response.status_code in (403, 404):
                raise BridgeError("MCP_ACCESS_DENIED")
            if response.status_code != 200:
                raise BridgeError("MCP_UPSTREAM_UNAVAILABLE")
            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise BridgeError("MCP_UPSTREAM_INVALID")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise BridgeError("MCP_UPSTREAM_INVALID")
            parts, count = [], 0
            async for chunk in response.aiter_bytes():
                count += len(chunk)
                if count > MAX_RESPONSE_BYTES:
                    raise BridgeError("MCP_RESPONSE_LIMIT")
                parts.append(chunk)
        return json.loads(b"".join(parts), object_pairs_hook=_unique_object, parse_constant=_bad_constant)

    async def call(self, name: str, arguments: dict) -> dict:
        try:
            arguments = validate_tool_arguments(name, arguments)
            # Counting even rejected upstream attempts prevents unbounded reads.
            async with asyncio.timeout(self.deadline):
                async with self._lock:
                    if self._calls >= MAX_CALLS:
                        raise BridgeError("MCP_CALL_LIMIT")
                    self._calls += 1
                    receipt = await self._read_receipt()
                    snapshot, fingerprint = checked_snapshot(receipt, self.config, self._input_hash)
                    result = invoke_snapshot(snapshot, name, arguments)
                    payload = {"advisory_only": True, "advice_run_id": self.config.advice_run_id,
                               "request_id": receipt["request_id"], "input_hash": fingerprint,
                               "tool": name, "result": result}
                    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
                        raise BridgeError("MCP_RESPONSE_LIMIT")
                    self._input_hash = fingerprint
                    return payload
        except BridgeError:
            raise
        except DomainError as error:
            raise BridgeError(error.code) from error
        except (TimeoutError, httpx.TimeoutException) as error:
            raise BridgeError("MCP_TIMEOUT") from error
        except httpx.HTTPError as error:
            raise BridgeError("MCP_UPSTREAM_UNAVAILABLE") from error
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise BridgeError("MCP_UPSTREAM_INVALID") from error
