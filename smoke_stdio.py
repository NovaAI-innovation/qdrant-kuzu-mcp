"""Live stdio MCP smoke test: initialize + tools/list + tool calls.

Runs the real server as a subprocess (python -m qdrant_kuzu_mcp) and drives
it over stdio JSON-RPC per the MCP spec. Uses a local (embedded) Qdrant via
QDRANT_LOCAL_PATH and an on-disk Kuzu DB under the system temp dir.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

root = r"C:/Users/casey/code/qdrant-kuzu-mcp"
py = os.path.join(root, ".venv", "Scripts", "python.exe")

tmp = tempfile.mkdtemp(prefix="qdrant-kuzu-smoke-")
env = dict(os.environ)
env.update(
    {
        "QDRANT_LOCAL_PATH": os.path.join(tmp, "qdrant_storage"),
        "KUZU_DB_PATH": os.path.join(tmp, "kuzu_db"),
        # keep the default fastembed model; cache is warm from the scaffold run
    }
)

proc = subprocess.Popen(
    [py, "-m", "qdrant_kuzu_mcp", "--transport", "stdio"],
    cwd=root,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    env=env,
)


def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def read_msg(timeout=300):
    result = {}

    def reader():
        result["line"] = proc.stdout.readline()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("line")


def read_response(req_id, timeout=300):
    """Read stdout lines until the JSON-RPC response with the given id."""
    start = time.time()
    while time.time() - start < timeout:
        line = read_msg(timeout=timeout)
        if not line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == req_id:
            return msg
    return None


def read_err():
    try:
        return proc.stderr.read()[:3000]
    except Exception:
        return ""


def brief(msg, n=400):
    if msg is None:
        return "NONE"
    return json.dumps(msg)[:n]


failures = []

# 1) initialize
send(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-check", "version": "0.0.1"},
        },
    }
)
resp = read_response(1)
print("INITIALIZE:", brief(resp, 300))
if not resp:
    print("STDERR:", read_err())
    sys.exit(1)
server_info = resp.get("result", {}).get("serverInfo", {})
print("serverInfo:", server_info)

send({"jsonrpc": "2.0", "method": "notifications/initialized"})

# 2) tools/list
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
tools_resp = read_response(2)
tools = {
    t["name"]: sorted(t.get("inputSchema", {}).get("properties", {}).keys())
    for t in (tools_resp or {}).get("result", {}).get("tools", [])
}
print("TOOLS:", json.dumps(tools, indent=1))

expected = {"qdrant-find", "qdrant-store", "qdrant-graph-find", "qdrant-graph-expand-from-hits"}
missing = expected - set(tools)
if missing:
    failures.append(f"missing tools: {missing}")
if "entities" not in tools.get("qdrant-store", []):
    failures.append("qdrant-store missing entities param")

# 3) qdrant-store with entities/relations (vector + graph write path)
send(
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "qdrant-store",
            "arguments": {
                "information": "PyTorch builds on CUDA which depends on the GPU driver stack.",
                "collection_name": "memories",
                "entities": ["PyTorch", "CUDA", "GPU driver stack"],
                "relations": [
                    {"subject": "PyTorch", "predicate": "depends_on", "object": "CUDA"},
                    {"subject": "CUDA", "predicate": "depends_on", "object": "GPU driver stack"},
                ],
            },
        },
    }
)
resp = read_response(3)
print("STORE RESULT:", brief(resp))
if not resp or resp.get("result", {}).get("isError"):
    failures.append(f"qdrant-store call failed: {resp}")

# 4) qdrant-graph-find
send(
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "qdrant-graph-find",
            "arguments": {"entity": "PyTorch", "depth": 2, "direction": "any"},
        },
    }
)
resp = read_response(4)
print("GRAPH-FIND RESULT:", brief(resp, 700))
if not resp or resp.get("result", {}).get("isError"):
    failures.append(f"qdrant-graph-find call failed: {resp}")
else:
    envelope = json.loads(resp["result"]["content"][0]["text"])
    if envelope.get("count", 0) < 2:
        failures.append(f"graph-find expected >=2 entities, got {envelope.get('count')}")

# 5) store with entity metadata, then qdrant-graph-expand-from-hits
send(
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "qdrant-store",
            "arguments": {
                "information": "TensorFlow also depends on CUDA for GPU acceleration.",
                "collection_name": "memories",
                "metadata": {"entities": ["TensorFlow", "CUDA"]},
            },
        },
    }
)
resp = read_response(5)
print("STORE2 RESULT:", brief(resp, 300))

send(
    {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "qdrant-graph-expand-from-hits",
            "arguments": {"query": "GPU acceleration frameworks", "collection_name": "memories"},
        },
    }
)
resp = read_response(6)
print("GRAPH-EXPAND RESULT:", brief(resp, 700))
if not resp or resp.get("result", {}).get("isError"):
    failures.append(f"qdrant-graph-expand-from-hits call failed: {resp}")

# 6) qdrant-find (upstream vector tool, unchanged)
send(
    {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "qdrant-find",
            "arguments": {"query": "GPU", "collection_name": "memories"},
        },
    }
)
resp = read_response(7)
print("FIND RESULT:", brief(resp))
if not resp or resp.get("result", {}).get("isError"):
    failures.append(f"qdrant-find call failed: {resp}")

proc.terminate()
print("STDERR:", read_err())

if failures:
    print("SMOKE FAILURES:", failures)
    sys.exit(1)
print("SMOKE TEST PASSED")
