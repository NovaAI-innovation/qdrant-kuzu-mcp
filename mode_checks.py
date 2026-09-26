"""Mode checks: persistence across restart, degradation, read-only, disabled.

Each check spawns the real server (python -m qdrant_kuzu_mcp) over stdio and
inspects tools/list (+ a graph call where relevant).
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

failures = []


def drive(env_extra, calls=()):
    """Start the server, return (server_info, tools_dict, call_results)."""
    tmp = tempfile.mkdtemp(prefix="qdrant-kuzu-mode-")
    env = dict(os.environ)
    env.update({"QDRANT_LOCAL_PATH": os.path.join(tmp, "qdrant_storage")})
    env.update(env_extra)
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

    def read_msg(timeout=240):
        result = {}

        def reader():
            result["line"] = proc.stdout.readline()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        return result.get("line")

    def read_response(req_id):
        start = time.time()
        while time.time() - start < 240:
            line = read_msg()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                return msg
        return None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "mode-check", "version": "0"}}})
    init = read_response(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools_resp = read_response(2)
    tools = sorted(
        t["name"] for t in (tools_resp or {}).get("result", {}).get("tools", [])
    )
    store_params = {}
    for t in (tools_resp or {}).get("result", {}).get("tools", []):
        if t["name"] == "qdrant-store":
            store_params = sorted(t.get("inputSchema", {}).get("properties", {}))
    results = []
    for i, (name, args) in enumerate(calls, start=10):
        send({"jsonrpc": "2.0", "id": i, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        results.append(read_response(i))
    proc.terminate()
    stderr = ""
    try:
        stderr = proc.stderr.read()[:1500]
    except Exception:
        pass
    return init, tools, store_params, results, stderr


# --- 1) persistence across restart (D3) ---
kuzu_path = os.path.join(tempfile.mkdtemp(prefix="qkm-persist-"), "kuzu_db")
init1, tools1, sp1, res1, err1 = drive(
    {"KUZU_DB_PATH": kuzu_path},
    calls=[("qdrant-store", {
        "information": "Rust powers Tokio which powers Axum.",
        "collection_name": "memories",
        "entities": ["Rust", "Tokio", "Axum"],
        "relations": [
            {"subject": "Tokio", "predicate": "built_with", "object": "Rust"},
            {"subject": "Axum", "predicate": "built_on", "object": "Tokio"},
        ],
    })],
)
print("RUN1 tools:", tools1, "store params:", sp1)
if "qdrant-graph-find" not in tools1:
    failures.append(f"run1: graph tools missing: {tools1} stderr={err1}")

init2, tools2, sp2, res2, err2 = drive(
    {"KUZU_DB_PATH": kuzu_path},
    calls=[("qdrant-graph-find", {"entity": "Axum", "depth": 2, "direction": "out"})],
)
print("RUN2 tools:", tools2)
envelope = None
if res2 and res2[0] and not res2[0].get("result", {}).get("isError"):
    envelope = json.loads(res2[0]["result"]["content"][0]["text"])
    print("RUN2 graph-find:", json.dumps(envelope.get("entities")))
else:
    print("RUN2 graph-find raw:", json.dumps(res2[0])[:500] if res2 else None)
if not envelope:
    failures.append("run2: graph-find failed after restart")
else:
    names = {e["name"] for e in envelope["entities"]}
    if not {"Tokio", "Rust"} <= names:
        failures.append(f"run2: persistence broken, got {names}")

# --- 2) degradation: unwritable/invalid Kuzu path (D7) ---
bad_target = os.path.join(tempfile.mkdtemp(prefix="qkm-bad-"), "blocker")
with open(bad_target, "w") as f:
    f.write("not a directory")
init3, tools3, sp3, res3, err3 = drive({"KUZU_DB_PATH": bad_target})
print("RUN3 (bad path) tools:", tools3, "| stderr head:", err3[:200].replace("\n", " "))
if tools3 != ["qdrant-find", "qdrant-store"]:
    failures.append(f"run3: degradation wrong tool list: {tools3}")
if sp3 != ["collection_name", "information", "metadata"]:
    failures.append(f"run3: store params should match upstream: {sp3}")

# --- 3) read-only Kuzu (existing DB) ---
init4, tools4, sp4, res4, err4 = drive(
    {"KUZU_DB_PATH": kuzu_path, "KUZU_READ_ONLY": "true"},
    calls=[("qdrant-graph-find", {"entity": "Tokio", "depth": 1})],
)
print("RUN4 (read-only) tools:", tools4, "store params:", sp4)
if "qdrant-graph-find" not in tools4:
    failures.append(f"run4: read-only should keep graph read tools: {tools4} err={err4[:300]}")
if "entities" in sp4:
    failures.append(f"run4: read-only should hide graph write params: {sp4}")
if res4 and res4[0] and res4[0].get("result", {}).get("isError"):
    failures.append("run4: graph-find should work in read-only mode")

# --- 4) disabled via env ---
init5, tools5, sp5, _, _ = drive({"KUZU_ENABLED": "false"})
print("RUN5 (disabled) tools:", tools5, "store params:", sp5)
if tools5 != ["qdrant-find", "qdrant-store"]:
    failures.append(f"run5: disabled should list upstream tools only: {tools5}")
if sp5 != ["collection_name", "information", "metadata"]:
    failures.append(f"run5: store params should match upstream: {sp5}")

if failures:
    print("MODE CHECK FAILURES:", failures)
    sys.exit(1)
print("ALL MODE CHECKS PASSED")
