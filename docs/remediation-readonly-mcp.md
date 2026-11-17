# Read-only MCP interoperability (stdio v1)

This increment adds an actual MCP server using the official Python SDK, not a
renamed REST or SSE endpoint. It exposes only `get_comparison`, `search_policy`
and `get_evidence`, bound to ONE existing immutable advice receipt.

It does **not** add a remote MCP HTTP/OAuth service, vector document RAG,
AG-UI, model sampling, autonomous approval, ERP writes or model-quality results.
`search_policy` returns the captured structured tenant policy; it is not vector
search. Sources remain untrusted data, and a valid citation is not semantic proof.

## Data and authority path

```
trusted local MCP host
  -> official SDK stdio subprocess
  -> GET /api/v1/advice-runs/{one configured id}
  -> existing business authentication / pilot session validation
  -> current immutable captured request, policy, quotes and sources
  -> shared read-only dispatcher
```

`read_tools.py` is the single schema/dispatcher registry for Function Calling
and MCP. The original LangGraph agent's definitions are re-exported unchanged.
The existing AdviceService continues revalidating principal authority before
each tool. The MCP side performs a fresh authenticated GET for EVERY call:
no cached receipt or bearer-authority check is reused. No new business API route,
schema migration or front-end feature is required.

The bridge never instantiates application Settings, a database, an ERP client
or a model client. It cannot login, renew sessions or reserve/process advice.
An operator or buyer must first reserve an advice receipt through the existing
business UI/API. Reserving captures context; it does not call a model. The
reader may use an auditor pilot session for least privilege. Never use a company
or ERP API key as a bridge bearer token.

The first successful read binds the input hash for the process. Optionally pin
`PF_MCP_INPUT_HASH` before launch. Changed/invalid/stale context is rejected, not
silently refreshed. Cross-tenant access is denied by the API; documents from
another request (including in the same tenant) are rejected by the shared
dispatcher. Pilot logout/revocation/expiry is checked by the API on every read.
An already-authorized response in flight cannot be retroactively recalled.

## Install and run

Python 3.13 is the validated CI target. The SDK is pinned to `mcp==1.30.0`,
the maintained v1 API; current v2 is a different API, not silently installed.
The optional requirement does not change the API/Worker default dependencies.

```bash
python -m pip install -r services/api/requirements.txt -r services/api/requirements-mcp.txt
```

Configure only in a trusted local MCP host's process environment, never in Git,
tool arguments or a pasted chat message:

- `PF_MCP_API_BASE_URL`: existing API origin; default `http://127.0.0.1:8000`.
- `PF_MCP_ADVICE_RUN_ID`: existing `adv_...` receipt; never supplied by tool arguments.
- `PF_MCP_BEARER_TOKEN`: current business session/static token, NOT a model key.
- `PF_MCP_INPUT_HASH`: optional exact captured input hash.
- `PF_MCP_APPROVED_HOST`: required exact hostname for a trusted HTTPS API.

Use the following command/arguments in the MCP host. Substitute the real absolute
path locally. Do not add `--transport http`; no HTTP listener is implemented.

```json
{"command": "/absolute/path/to/.venv/bin/python",
 "args": ["/absolute/path/to/ProcureFlow/scripts/start_mcp.py"]}
```

The host must inherit/inject the above environment without saving real tokens to
a tracked config. Do not type a bearer value as an argument. In a POSIX terminal
a trusted operator can use a non-echoed prompt (`read -rs PF_MCP_BEARER_TOKEN;
export PF_MCP_BEARER_TOKEN`), then unset it after the child starts/finishes.

Only literal `127.0.0.1` / `::1` allow plaintext HTTP. HTTPS requires an exact
operator-approved origin; URL userinfo, paths, queries, fragments, redirects and
ambient HTTP proxy configuration are rejected/disabled. No tool can choose an
upstream URL or request ID. This is not an OS-level egress sandbox or a defense
against a compromised trusted API/DNS/host. Use stdio only with an explicitly
trusted local host; it can see the token, returned procurement data and sources.
Do not expose the bridge as an unauthenticated network service.

## Limits and errors

8-second total deadline per call (including serialized wait), one HTTP request
in flight, 256000 response bytes, 64 validated calls per bridge process, no
redirect/fallback/retry. SDK stdio framing is not a hostile-client resource cage;
use host process limits for public/untrusted workloads. The existing advice
capture limits also apply (20 quotations and its bounded source snapshot).
Only fixed error codes are exposed; no remote errors, keys, SQL or provider
reasoning are added to logs. Stdout contains MCP frames only.

Tool annotations are discovery metadata, not authorization. The allowlisted
dispatcher and existing API identity checks enforce access. This implementation
does not claim MCP HTTP authorization or OAuth conformance.

## Tests (no paid model or production ERP)

```bash
python -m pip install -r services/api/requirements-dev.txt -r services/api/requirements-mcp.txt
python -m pytest -q tests/test_mcp_bridge.py integrations/mcp/tests
```

Offline bridge cases test registry, scoped reads, malformed/oversized replies,
timeouts, cancellation, call budgets and non-network rejection.
Protocol cases use official `ClientSession` and `stdio_client` to launch the
actual server subprocess against a real localhost FastAPI API with disposable
SQLite/pilot accounts, documents and advice records. They test initialize,
discovery, all three tools, ping, forbidden tools, argument injection, other
workspace/request documents, revoked sessions and stale quotes. Table counts
before/after successful reads prove zero business writes in that fixture.
They do not establish remote HTTP transport, all MCP hosts, PostgreSQL protocol
acceptance, real human usage or a real LLM score.

The dedicated workflow fails if the SDK or runtime is missing; no skip fallback.
Test/source identity and original XML are archived. Local absence of the SDK
must be reported separately from remote SDK evidence. Earlier whole-project
CI numbers are not proof for this new source tree.

## Primary protocol references checked 2026-10-22

- https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
- https://pypi.org/project/mcp/1.30.0/
- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://modelcontextprotocol.io/specification/2025-11-25/server/tools

No SDK source or font is bundled. Official MCP SDK: MIT license.
