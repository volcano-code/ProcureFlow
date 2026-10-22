# Read-only MCP and AG-UI adapters

## Scope

This optional integration extends the verified `3401ace` base without changing its procurement service, authorization rules, existing frontend, migrations, or core OpenAPI export. It is an implementation of **local stdio MCP tools** and a **bounded AG-UI read-observation endpoint**, not remote MCP OAuth, an autonomous approval agent, or a replacement for the existing UI. Review the exact-commit `protocol-interop` CI evidence before claiming interoperability passed.

The adapters never request a model key or ERP credential. Normal tests use synthetic request data, the real application HTTP API, a real MCP child process and the official SDK client, plus official AG-UI event-model decoding. Mock transport tests separately cover malformed/hostile upstream data; they do not substitute for the subprocess/HTTP cases.

## Install

From the repository root, in a private virtual environment:

```bash
python -m pip install -r services/api/requirements-dev.txt -r integrations/protocols/requirements.txt
export PYTHONPATH="$PWD/services/api:$PWD"
python -m pytest -q integrations/protocols/tests
```

The SDK versions are pinned: MCP 1.30.0 (maintained 1.x API, not the 2.x API) and ag-ui-protocol 1.0.0. This optional requirements file does not silently alter the default API/Worker runtime. CI installs both application and protocol dependencies and runs `pip check`.

## MCP: one selected request, five read-only tools

`get_request`, `get_quotes`, `get_comparison`, `get_policy`, `get_evidence` are the complete allowlist. `get_comparison` reads at most ten previously stored evaluations; it does not calculate a new proposal. `get_policy` returns the existing structured policy, **not document RAG**. Evidence is restricted to current quotes in the selected request. Missing evidence or saved comparisons are not fabricated.

An operator configures a fixed API origin, one request ID and a token-file path. Tools cannot accept a different tenant, role, token, URL or request ID. A caller of this local process has the operator-configured credential's read access to that request; do not hand the process to untrusted users. Use a dedicated auditor account/session. The original API remains the tenant/session authority on every call.

Example for a LOCAL DEMO ONLY (the published demo identity is not a real credential):

```bash
umask 077
mkdir -p "$HOME/.config/procureflow"
printf '%s' 'demo-auditor' > "$HOME/.config/procureflow/reader.token"
export PF_READ_API_ORIGIN='http://127.0.0.1:8000'
export PF_READ_REQUEST_ID='REPLACE_WITH_AN_EXISTING_REQUEST_ID'
export PF_READ_TOKEN_FILE="$HOME/.config/procureflow/reader.token"
python -m integrations.protocols.mcp_server
```

Use an MCP client to launch that command as a stdio server. For private accounts, populate the token file through a trusted secret-management process, not a checked-in config, command argument or chat. The POSIX token file must be a same-owner regular file with no group/other permissions; symlinks and oversized/whitespace credentials are rejected. The current token-file loader intentionally requires POSIX protections.

Only GET is sent upstream. Redirects and ambient HTTP proxy settings are disabled; plaintext URLs must use a literal loopback IP, all other configured origins require HTTPS. Bounded response size, request time, duplicate JSON keys, invalid IDs and compressed/unexpected responses fail closed. Upstream errors expose fixed codes, never raw bodies or credentials. Source contents remain untrusted data even though they come from an authenticated application. TLS trust, origin provisioning and the trusted local host remain operator responsibilities; this is not an OS network sandbox.

One registry derives the new MCP and function-tool schemas. It does not replace the application's earlier internal bound-data agent tools. No resource/file browsing, sampling, arbitrary URL fetch, approval, import, confirmation, job enqueue or ERP tool is exposed. HTTP/Streamable-HTTP MCP and OAuth audience/scope handling remain a separate unimplemented scope.

## AG-UI: observe, never execute

Start the optional API factory instead of the standard factory, on the same trusted application configuration:

```bash
python -m uvicorn integrations.protocols.agui:create_protocol_app --factory --host 127.0.0.1 --port 8000
```

The extra endpoint is `GET /api/v1/requests/{request_id}/ag-ui/events`. It uses the existing bearer authentication, workspace checks, request-body guard and security headers. Its runtime OpenAPI documents this route; the default application OpenAPI remains unchanged. This does not switch the native frontend to a new AG-UI client.

Events use the official `ag_ui` models and `EventEncoder`: `RUN_STARTED`, `STATE_SNAPSHOT`, `CUSTOM` (`procureflow.audit`), then `RUN_FINISHED`; revoked authority or read failure yields `RUN_ERROR`. A run here is **the bounded observation session**, not an extraction/approval/ERP operation. `RUN_FINISHED.result.observation_only` is true and must not be interpreted as procurement success. No fake model tokens, tool calls or hidden reasoning are emitted.

Only request ID/version/status/proposal-current and audit ID/kind/time are exported. Raw audit payloads, document text and provider reasoning are deliberately omitted. Use the existing authenticated business APIs to inspect detailed evidence. `Last-Event-ID` or `after` resumes audit IDs; a fresh state snapshot is still supplied. At most twenty polls per connection, one hundred audit records per poll and eight concurrent streams per API process. Reconnect using the last received audit ID. These are per-process bounds, not a distributed quota guarantee. An event committed before a revocation may already be in flight; subsequent service reads revalidate the durable identity and stop the stream.

## Acceptance and remaining work

The protocol test suite checks official client discovery/calls, exact schema export, unknown/write-tool denial, fixed request scope, cross-workspace rejection, evidence association, sanitized upstream errors/redirects/size/time bounds, token-file protection, SDK-decoded AG-UI events, replay cursor behavior, real durable pilot-session revocation, and zero new procurement operations. The revocation test changes actual session rows between two polls; it is a controlled timing test, not a production security certification.

CI produces JUnit, package versions, exact Git identities and a Git-tracked-only source archive. No model trial, real ERP acceptance, performance result or full R1/R2 completion is inferred. Document vector retrieval, remote authenticated MCP transport, native UI AG-UI adoption, actual model quality and external human evaluation remain explicit follow-ups.

Official references checked for this integration:
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0
- https://py.sdk.modelcontextprotocol.io/v1/
- https://docs.ag-ui.com/sdk/python/core/events
- https://pypi.org/project/ag-ui-protocol/1.0.0/
