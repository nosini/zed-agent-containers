#!/usr/bin/env python3
"""A stand-in for `rea mcp`: logs each start to $MOCK_REA_LOG and answers
initialize, tools/list, prompts/list and tools/call. MOCK_REA_TOOLS names
its tools (comma-separated)."""
import json, os, sys

with open(os.environ["MOCK_REA_LOG"], "a") as f:
    f.write("start\n")
tools = [{"name": n, "inputSchema": {"type": "object"}}
         for n in os.environ.get("MOCK_REA_TOOLS", "open_binary").split(",")]
for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"],
                  "capabilities": {"tools": {"listChanged": True}, "prompts": {}},
                  "serverInfo": {"name": "rea", "version": "mock"}}
    elif method == "server/discover":
        result = {"supportedVersions": ["2026-07-28"],
                  "capabilities": {"tools": {"listChanged": True}, "prompts": {}},
                  "resultType": "complete",
                  "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "rea", "version": "mock"}}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "trace_crash"}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "called " + msg["params"]["name"]}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "error": {"code": -32601, "message": "Method not found"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
