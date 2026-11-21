"""Deterministic stdio MCP fixture, not a live model."""
import json
from pathlib import Path
import sys

schema = {"type": "object", "properties": {"message": {"type": "string"}},
          "required": ["message"], "additionalProperties": False}
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": schema},
                            {"name": "mutate", "description": "Mutation fixture", "inputSchema": schema}]}
    elif method == "tools/call":
        if len(sys.argv) > 1:
            with Path(sys.argv[1]).open("a", encoding="utf-8") as stream:
                stream.write(request["params"]["name"]+"\n")
        result = {"content": [{"type": "text", "text": "echo:"+request["params"]["arguments"]["message"]}],
                  "isError": request["params"]["name"] == "mutate"}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
