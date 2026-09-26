"""End-to-end tests for the qdrant-kuzu-mcp server over the stdio MCP transport.

These tests spawn the real server (``python -m qdrant_kuzu_mcp``) as a
subprocess and drive it with the official ``mcp`` client SDK — no mocks, no
in-process shortcuts. Both backends are embedded, so no external services are
required:

- Qdrant runs in-process via ``QDRANT_URL=":memory:"``.
- Kuzu runs embedded against a throwaway directory under pytest's per-test
  tmp dir (``KUZU_DB_PATH``), removed with it.

Structural note: the server lifecycle (``stdio_client`` + ``ClientSession``)
is opened and closed INSIDE each test body via the ``e2e_server`` context
manager, not via an async fixture. ``stdio_client`` internally uses anyio
cancel scopes, which must be exited in the same task that entered them;
pytest-asyncio finalizes async fixtures from a different task, which makes
fixture-based lifecycles raise ``RuntimeError: Attempted to exit cancel scope
in a different task`` at teardown even when every test passes. Keeping the
whole lifecycle inside the test task is the clean, documented-safe pattern.

Covered categories (one or more tests each, per the task acceptance criteria):

1. MCP handshake: ``initialize`` + ``tools/list`` advertises BOTH a Qdrant
   vector tool and a Kuzu graph tool.
2. Vector tool roundtrip: ``qdrant-store`` then ``qdrant-find`` returns a
   non-empty result against the in-memory Qdrant collection seeded by the test.
3. Graph tool roundtrip: ``qdrant-graph-find`` and
   ``qdrant-graph-expand-from-hits`` return non-empty envelopes against the
   ephemeral Kuzu graph seeded by the test (via ``qdrant-store`` with
   ``entities``/``relations``).
"""

import json
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]

COLLECTION = "e2e_memories"

# Server startup includes importing the app and loading the fastembed model
# (fast with a warm cache; a cold cache also downloads the model). One
# generous read timeout covers the worst case.
READ_TIMEOUT = timedelta(seconds=300)

# Seeded content. The entities/relations build a small chain in Kuzu:
#   PyTorch -[depends_on]-> CUDA -[depends_on]-> GPU driver stack
SEED_INFORMATION = "PyTorch builds on CUDA which depends on the GPU driver stack."
SEED_ENTITIES = ["PyTorch", "CUDA", "GPU driver stack"]
SEED_RELATIONS = [
    {"subject": "PyTorch", "predicate": "depends_on", "object": "CUDA"},
    {"subject": "CUDA", "predicate": "depends_on", "object": "GPU driver stack"},
]
# Second memory carries explicit metadata.entities for the
# qdrant-graph-expand-from-hits harvest path.
HARVEST_INFORMATION = "TensorFlow also depends on CUDA for GPU acceleration."
HARVEST_METADATA_ENTITIES = ["TensorFlow", "CUDA"]


def _text(result) -> str:
    """Concatenate all text blocks of a CallToolResult."""
    return "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )


def _first_json(result):
    """Parse the first JSON text block of a CallToolResult.

    fastmcp serializes a ``list`` tool return as one aggregated JSON text
    block, so ``qdrant-find``'s ``list[str]`` arrives as a single JSON array
    and graph dicts arrive as single JSON objects.
    """
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    raise AssertionError(
        f"no JSON block in tool result: {[getattr(b, 'text', b) for b in result.content]}"
    )


@asynccontextmanager
async def e2e_server(tmp_path: Path, *, seed: bool = True):
    """Run the real server over stdio, handshake, optionally seed both
    backends, and yield the connected client session.

    Must be used inside a test body (see module docstring). ``tmp_path`` is
    the pytest fixture value, giving each test a fresh Kuzu directory.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qdrant_kuzu_mcp", "--transport", "stdio"],
        env={
            # In-process Qdrant (no server, no disk).
            "QDRANT_URL": ":memory:",
            # Ephemeral on-disk Kuzu under the per-test pytest tmp dir.
            "KUZU_DB_PATH": str(tmp_path / "kuzu"),
            # Keep stdout/stderr byte-clean regardless of host locale.
            "PYTHONIOENCODING": "utf-8",
        },
        cwd=str(PROJECT_ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=READ_TIMEOUT
        ) as session:
            init_result = await session.initialize()
            server_info = init_result.serverInfo
            assert server_info.name == "qdrant-kuzu-mcp", (
                f"unexpected serverInfo: {server_info}"
            )
            if seed:
                await _seed_backends(session)
            yield session


async def _seed_backends(session: ClientSession) -> None:
    """Seed Qdrant and Kuzu through the server's own write path."""
    store_result = await session.call_tool(
        "qdrant-store",
        {
            "information": SEED_INFORMATION,
            "collection_name": COLLECTION,
            "entities": SEED_ENTITIES,
            "relations": SEED_RELATIONS,
        },
    )
    assert not store_result.isError, (
        f"seeding qdrant-store failed: {_text(store_result)}"
    )
    confirmation = _text(store_result)
    assert SEED_INFORMATION in confirmation, (
        f"store confirmation missing stored text: {confirmation!r}"
    )
    # A graph-sync failure is reported inline, not as isError (Qdrant is the
    # source of truth), so check for the skip marker explicitly.
    assert "graph sync skipped" not in confirmation, (
        f"Kuzu graph sync failed during seeding: {confirmation!r}"
    )

    harvest_store_result = await session.call_tool(
        "qdrant-store",
        {
            "information": HARVEST_INFORMATION,
            "collection_name": COLLECTION,
            "metadata": {"entities": HARVEST_METADATA_ENTITIES},
        },
    )
    assert not harvest_store_result.isError, (
        f"seeding harvest memory failed: {_text(harvest_store_result)}"
    )


# --- Category 1: MCP handshake + tool advertisement --------------------------


async def test_handshake_advertises_vector_and_graph_tools(tmp_path):
    """initialize + tools/list must expose both Qdrant vector tools and Kuzu
    graph tools on one combined surface."""
    async with e2e_server(tmp_path, seed=False) as session:
        tools_result = await session.list_tools()
        tool_names = {tool.name for tool in tools_result.tools}
        assert tool_names, "tools/list returned no tools"

        vector_tools = {"qdrant-find", "qdrant-store"}
        graph_tools = {"qdrant-graph-find", "qdrant-graph-expand-from-hits"}
        assert vector_tools <= tool_names, (
            f"missing Qdrant vector tools: {sorted(vector_tools - tool_names)}"
        )
        assert graph_tools <= tool_names, (
            f"missing Kuzu graph tools: {sorted(graph_tools - tool_names)}"
        )

        # The graph write path must be advertised too: qdrant-store accepts
        # entities/relations only when the Kuzu layer is live and writable.
        store_tool = next(t for t in tools_result.tools if t.name == "qdrant-store")
        store_params = set(store_tool.inputSchema.get("properties", {}))
        assert "entities" in store_params, (
            f"qdrant-store does not accept entities (graph layer degraded?): "
            f"properties={sorted(store_params)}"
        )
        assert "relations" in store_params, (
            f"qdrant-store does not accept relations (graph layer degraded?): "
            f"properties={sorted(store_params)}"
        )


# --- Category 2: vector tool roundtrip ----------------------------------------


async def test_vector_tool_roundtrip(tmp_path):
    """qdrant-find must return a non-empty result seeded via qdrant-store
    against the in-memory Qdrant collection."""
    async with e2e_server(tmp_path) as session:
        result = await session.call_tool(
            "qdrant-find",
            {
                "query": "Which framework builds on CUDA?",
                "collection_name": COLLECTION,
            },
        )
        assert not result.isError, f"qdrant-find failed: {_text(result)}"
        assert result.content, "qdrant-find returned no content"

        payload = _first_json(result)
        if isinstance(payload, list):
            entries = payload
        else:  # pragma: no cover - defensive: serialized non-list shape
            entries = [payload]
        assert any(
            SEED_INFORMATION in json.dumps(entry) for entry in entries
        ), f"stored memory not found among {len(entries)} find results"


# --- Category 3: graph tool roundtrips -----------------------------------------


async def test_graph_find_roundtrip(tmp_path):
    """qdrant-graph-find must return a non-empty envelope against the Kuzu
    graph seeded by the test."""
    async with e2e_server(tmp_path) as session:
        result = await session.call_tool(
            "qdrant-graph-find",
            {"entity": "PyTorch", "depth": 2, "direction": "any"},
        )
        assert not result.isError, f"qdrant-graph-find failed: {_text(result)}"

        envelope = _first_json(result)
        assert envelope["seed"]["found"] is True, (
            f"seed entity missing: {envelope['seed']}"
        )

        entities = envelope["entities"]
        assert entities, "qdrant-graph-find returned an empty entities list"
        names = {e["name"] for e in entities}
        assert "CUDA" in names, f"CUDA not reachable from PyTorch: {entities}"
        assert "GPU driver stack" in names, (
            f"depth-2 hop to 'GPU driver stack' missing: {entities}"
        )
        by_name = {e["name"]: e for e in entities}
        assert by_name["CUDA"]["depth"] == 1, (
            f"unexpected hop depth: {by_name['CUDA']}"
        )
        assert envelope["count"] == len(entities)
        # Related entities are backed by referencing chunks in Kuzu.
        chunk_entities = {c["entity"] for c in envelope["chunks"]}
        assert chunk_entities, "no referencing chunks returned"


async def test_graph_expand_from_hits_roundtrip(tmp_path):
    """qdrant-graph-expand-from-hits must combine a Qdrant vector search with
    a Kuzu graph expansion and return a non-empty envelope."""
    async with e2e_server(tmp_path) as session:
        result = await session.call_tool(
            "qdrant-graph-expand-from-hits",
            {
                "query": "GPU acceleration frameworks",
                "collection_name": COLLECTION,
            },
        )
        assert not result.isError, (
            f"qdrant-graph-expand-from-hits failed: {_text(result)}"
        )

        envelope = _first_json(result)
        assert envelope["hits_used"] >= 1, (
            f"vector search inside expand-from-hits found nothing: {envelope}"
        )
        seeds = envelope["seeds"]
        assert seeds, "no seed entities harvested from search hits"

        seed_names = {block["seed"]["name"] for block in seeds}
        assert seed_names & set(HARVEST_METADATA_ENTITIES), (
            f"harvested seeds {seed_names} do not include the stored metadata entities"
        )
        # CUDA was related to PyTorch and 'GPU driver stack' during seeding,
        # so the CUDA seed block must expand to at least one related entity.
        cuda_blocks = [b for b in seeds if b["seed"]["name"] == "CUDA"]
        assert cuda_blocks, f"CUDA seed missing from expansion: {seed_names}"
        assert cuda_blocks[0]["count"] >= 1, (
            f"CUDA expansion returned no related entities: {cuda_blocks[0]}"
        )
