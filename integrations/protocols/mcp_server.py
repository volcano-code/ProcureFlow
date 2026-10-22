"""Local stdio MCP server. No HTTP MCP listener, remote OAuth or write tools."""
from __future__ import annotations

import asyncio
import json
import os
import sys

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import Server

from .readonly import BridgeError, ReadOnlyGateway, TOOLS, read_token


def build_server(gateway: ReadOnlyGateway) -> Server:
    server = Server("procureflow-readonly", version="0.1.0")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.schema(),
            annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                idempotentHint=True, openWorldHint=False)) for t in TOOLS]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict | None):
        try:
            result = await gateway.call(name, arguments)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                        structuredContent=result, isError=False)
        except BridgeError as error:
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"error": {"code": error.code}}))], isError=True)
        except Exception:
            # Do not reflect user input, bearer tokens, traceback or raw upstream errors.
            return types.CallToolResult(content=[types.TextContent(type="text", text='{"error":{"code":"INTERNAL_ERROR"}}')], isError=True)

    return server


async def run():
    gateway = ReadOnlyGateway(os.environ.get("PF_READ_API_ORIGIN", ""),
        read_token(os.environ.get("PF_READ_TOKEN_FILE", "")), os.environ.get("PF_READ_REQUEST_ID", ""))
    try:
        server = build_server(gateway)
        async with mcp.server.stdio.stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await gateway.aclose()


def main():
    try:
        asyncio.run(run())
    except BridgeError as error:
        print(error.code, file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
