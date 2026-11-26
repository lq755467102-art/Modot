"""Read-only MCP sample. The manifest checker only inspects its declaration."""
import json
import sys


TOOL = {
    "name": "read_note",
    "description": "Read the example review note.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}


def reply(request):
    method = request.get("method")
    if "id" not in request:
        return None
    envelope = {"jsonrpc": "2.0", "id": request["id"]}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "review-notes", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = request.get("params", {})
        if params.get("name") != TOOL["name"] or params.get("arguments", {}) != {}:
            return {**envelope, "error": {"code": -32602, "message": "Expected read_note with no arguments"}}
        result = {"content": [{"type": "text", "text": "Review note from studio v1."}]}
    else:
        return {**envelope, "error": {"code": -32601, "message": "Unknown method"}}
    return {**envelope, "result": result}


if __name__ == "__main__":
    for line in sys.stdin:
        response = reply(json.loads(line))
        if response is not None:
            print(json.dumps(response), flush=True)
