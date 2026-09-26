# Recon: `qdrant/mcp-server-qdrant` upstream + MCP spec + Qdrant Python client

**Date generated:** 2026-09-25 (retrieval window: same day)
**Pinned upstream commit:** `c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41` on branch `master`
**Pinned package version:** `mcp-server-qdrant` v0.8.1 (per `pyproject.toml`)
**Pinned Qdrant Python client:** `qdrant-client>=1.12.0` (resolved against `master` at the same retrieval)
**Methodology:** All raw source files were fetched live from
`https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/<sha>/...` (no clone, no local
archive substituted). MCP spec citations are from `modelcontextprotocol.io` and the official
`modelcontextprotocol/modelcontextprotocol` repo. Qdrant Python client API citations are from
`https://python-client.qdrant.tech/` (mkdocs index) and the live `qdrant-client` source at
`https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/...`.

> Acceptance note: this report cites live URLs only, enumerates every existing tool, and
> states the chosen integration approach for Kuzu in §5.

---

## 1. Executive summary

`qdrant/mcp-server-qdrant` v0.8.1 is a small, focused MCP server: a `FastMCP` subclass
(`QdrantMCPServer`) that exposes exactly **two** MCP tools — `qdrant-find` and
`qdrant-store` — over a Qdrant vector store with FastEmbed embeddings. It is written
against `fastmcp==2.7.0`, `qdrant-client>=1.12.0`, `fastembed>=0.6.0`, and
`pydantic>=2.10.6,<2.12.0`. The only transport wiring is `mcp.run(transport=args.transport)`
in `src/mcp_server_qdrant/main.py`, accepting `stdio` (default), `sse`, and
`streamable-http` (verified against the live `main.py` and the upstream README).

The tool surface is intentionally minimal. There is **no concept of a graph, an entity,
or a relation** anywhere in the upstream — payload is treated as opaque JSON metadata.
This is the seam a Kuzu-backed complement plugs into: post-filter graph expansion over
the entities/names referenced in vector hits.

**Chosen Kuzu integration approach (decided in §5):** ship the Kuzu capability as a
**separate MCP tool** (`qdrant-graph-find`) inside the same FastMCP server process,
not a hybrid retriever fused at scoring time. Rationale, alternatives considered, and
extension points are spelled out in §5.

---

## 2. Upstream recon — `qdrant/mcp-server-qdrant`

### 2.1 Repository facts

| Item | Value | Source |
|---|---|---|
| Default branch | `master` | https://github.com/qdrant/mcp-server-qdrant |
| HEAD commit (pinned) | `c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41` | https://api.github.com/repos/qdrant/mcp-server-qdrant/commits/master |
| HEAD commit date | 2026-08-11 | same |
| HEAD commit message | `feat: Add support to use the MCP server as a Kiro Power (#175)` | same |
| Package version | `0.8.1` | `pyproject.toml` at pinned SHA |
| License | Apache-2.0 | `pyproject.toml` and `LICENSE` |
| Python | `>=3.10` (Dockerfile pins `python:3.11-slim`) | `pyproject.toml`, `Dockerfile` |
| Entry point | `mcp-server-qdrant = "mcp_server_qdrant.main:main"` | `pyproject.toml` |
| Build backend | `hatchling` | `pyproject.toml` |

### 2.2 Dependency story (`pyproject.toml`, verbatim)

```toml
[project]
name = "mcp-server-qdrant"
version = "0.8.1"
description = "MCP server for retrieving context from a Qdrant vector database"
readme = "README.md"
requires-python = ">=3.10"
license = "Apache-2.0"
dependencies = [
    "fastembed>=0.6.0",
    "qdrant-client>=1.12.0",
    "pydantic>=2.10.6,<2.12.0",
    "fastmcp==2.7.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.uv]
dev-dependencies = [
    "ipdb>=0.13.13",
    "isort>=6.0.1",
    "mypy>=1.9.0",
    "pre-commit>=4.1.0",
    "pyright>=1.1.389",
    "pytest>=8.3.3",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.8.0",
]

[project.scripts]
mcp-server-qdrant = "mcp_server_qdrant.main:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
python_functions = "test_*"
asyncio_mode = "auto"
```

Source: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/pyproject.toml

Notes for the Kuzu fork:

- `fastmcp==2.7.0` is pinned exactly. Adding `kuzu` and any helpers must keep the same
  semver constraints; bumps happen via a new upstream release.
- `pydantic` is constrained `>=2.10.6,<2.12.0`. Kuzu's Python wheels are pydantic-light
  (Kuzu 0.11.x ships its own Python bindings without a pydantic dependency), so this
  should not collide, but verify before pinning.

### 2.3 Source tree (pinned SHA)

```
.
├── .github/{dependabot.yml, workflows/{pre-commit,pypi-publish,pytest}.yaml}
├── .gitignore
├── .pre-commit-config.yaml
├── .python-version
├── Dockerfile
├── LICENSE
├── README.md
├── kiro-power/{mcp.json, plugin.json, skills/setup/SKILL.md}
├── pyproject.toml
├── src/mcp_server_qdrant/
│   ├── __init__.py                (empty)
│   ├── main.py                    (24 lines — argparse → mcp.run)
│   ├── server.py                  (12 lines — wires QdrantMCPServer)
│   ├── mcp_server.py              (199 lines — TOOL DEFINITIONS LIVE HERE)
│   ├── qdrant.py                  (170 lines — QdrantConnector)
│   ├── settings.py                (115 lines — pydantic-settings)
│   ├── common/
│   │   ├── filters.py             (194 lines — make_filter, make_indexes)
│   │   ├── func_tools.py          (28 lines — make_partial_function)
│   │   └── wrap_filters.py        (150 lines — filter → signature rewriting)
│   └── embeddings/
│       ├── base.py                (25 lines — EmbeddingProvider ABC)
│       ├── factory.py             (17 lines — create_embedding_provider)
│       ├── fastembed.py           (50 lines — FastEmbedProvider)
│       └── types.py               (5 lines — EmbeddingProviderType enum)
├── tests/
│   ├── __init__.py
│   ├── test_fastembed_integration.py
│   ├── test_qdrant_integration.py
│   └── test_settings.py
└── uv.lock
```

Source: https://api.github.com/repos/qdrant/mcp-server-qdrant/git/trees/dc6a6cfc205a379273aa92d5de841f126e32b161?recursive=1

There is **no `tests/test_mcp_server.py`** — tool-level integration tests are not
exercised at the FastMCP layer in this upstream; coverage is at the connector level
(`tests/test_qdrant_integration.py`) and the settings layer (`tests/test_settings.py`).

### 2.4 Transport wiring (verified live)

`src/mcp_server_qdrant/main.py` (24 lines, full file):

```python
import argparse


def main():
    """
    Main entry point for the mcp-server-qdrant script defined
    in pyproject.toml. It runs the MCP server with a specific transport
    protocol.
    """

    # Parse the command-line arguments to determine the transport protocol.
    parser = argparse.ArgumentParser(description="mcp-server-qdrant")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
    )
    args = parser.parse_args()

    # Import is done here to make sure environment variables are loaded
    # only after we make the changes.
    from mcp_server_qdrant.server import mcp

    mcp.run(transport=args.transport)
```

Source: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/main.py

Observations:

- The transport flag is the **only** CLI argument; everything else is env-driven.
- `from mcp_server_qdrant.server import mcp` is deferred until after `argparse` to
  ensure env vars take effect before `pydantic_settings` reads them — this is the
  pattern a Kuzu-injected connector must preserve (load Kuzu settings after arg
  parsing, not at module import time).
- The Dockerfile hard-codes `CMD ["uvx", "mcp-server-qdrant", "--transport", "sse"]`
  and exposes `EXPOSE 8000`. Source:
  https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/Dockerfile

### 2.5 Full existing MCP tool surface (enumerated)

There are exactly **two** MCP tools registered by `setup_tools()` in
`src/mcp_server_qdrant/mcp_server.py` (lines 88–199). Both are auto-derived from the
Python function signatures via FastMCP's introspection; the optional `query_filter`
parameter is replaced by typed keyword/integer/float/boolean arguments via the
`wrap_filters` decorator at server construction time when
`QdrantSettings.filterable_fields` is configured.

#### Tool 1 — `qdrant-find`

| Field | Value |
|---|---|
| Tool name | `qdrant-find` |
| Description source | `ToolSettings.tool_find_description` (env `TOOL_FIND_DESCRIPTION`, default `"Look up memories in Qdrant. Use this tool when you need to: \n - Find memories by their content \n - Access memories for further analysis \n - Get some personal information about the user"`) |
| Always-on parameters | `query: str` — natural-language query string; `collection_name: str` — collection to search in |
| Optional parameters (default mode) | `query_filter: ArbitraryFilter \| None` — opaque dict the server converts to `qdrant_client.models.Filter` |
| Optional parameters (filterable fields configured) | one parameter per `FilterableField` with `condition` set; type derived from `field_type` (`str`/`int`/`float`/`bool`; `list[T]` for `any`/`except`); `required` controls whether the field is mandatory |
| Internals | embeds `query` via the configured `EmbeddingProvider.embed_query`, then `AsyncQdrantClient.query_points(collection_name, query=query_vector, using=vector_name, limit=qdrant_settings.search_limit, query_filter=...)` |
| Returns | `list[str] \| None` — `None` when no results, otherwise `["Results for the query '<query>'", "<entry><content>…</content><metadata>{…}</metadata></entry>", …]` (format produced by `format_entry`) |
| Disabled when | never (always available, even in `read_only` mode) |

#### Tool 2 — `qdrant-store`

| Field | Value |
|---|---|
| Tool name | `qdrant-store` |
| Description source | `ToolSettings.tool_store_description` (env `TOOL_STORE_DESCRIPTION`, default `"Keep the memory for later use, when you are asked to remember something."`) |
| Always-on parameters | `information: str` — text to embed and store; `collection_name: str` — collection name |
| Optional parameters | `metadata: dict[str, Any] \| None` — any JSON; note the upstream comment: *"The `metadata` parameter is defined as non-optional, but it can be None. If we set it to be optional, some of the MCP clients, like Cursor, cannot handle the optional parameter correctly."* |
| Internals | embeds `information` via `EmbeddingProvider.embed_documents`, then `AsyncQdrantClient.upsert(collection_name, points=[models.PointStruct(id=uuid4().hex, vector={vector_name: emb[0]}, payload={"document": entry.content, "metadata": entry.metadata})])` |
| Returns | `str` — `"Remembered: <information> in collection <collection_name>"` (no `collection_name` suffix when default-collection mode is on) |
| Disabled when | `QdrantSettings.read_only` is `True` (`self.qdrant_settings.read_only`) — the tool is not registered at all in that case |

These are the **only** tools. There are no resources, no prompts, no resource
templates. The `FastMCP` subclass `QdrantMCPServer` only calls `self.tool(...)` twice.

### 2.6 Connector layer (`src/mcp_server_qdrant/qdrant.py`)

The tools delegate to `QdrantConnector`, a thin async wrapper around
`AsyncQdrantClient`. Three methods matter for extension:

| Method | Signature | Used by | Notes |
|---|---|---|---|
| `get_collection_names` | `async () -> list[str]` | (unused at the tool layer; available for future use) | wraps `client.get_collections()` |
| `store` | `async (entry: Entry, *, collection_name: str \| None = None) -> None` | `qdrant-store` | embeds, then `client.upsert(...)`; `_ensure_collection_exists` is called first and creates the collection on first write |
| `search` | `async (query: str, *, collection_name: str \| None = None, limit: int = 10, query_filter: models.Filter \| None = None) -> list[Entry]` | `qdrant-find` | wraps `client.query_points(collection_name, query=query_vector, using=vector_name, limit=limit, query_filter=query_filter)` and rehydrates `Entry` from `result.payload["document"]` + `result.payload.get("metadata")` |

Storage shape (verbatim from `store`):

```python
payload = {"document": entry.content, METADATA_PATH: entry.metadata}
await self._client.upsert(
    collection_name=collection_name,
    points=[
        models.PointStruct(
            id=uuid.uuid4().hex,
            vector={vector_name: embeddings[0]},
            payload=payload,
        )
    ],
)
```

`METADATA_PATH = "metadata"` (set in `settings.py`), so payload is
`{"document": "...", "metadata": {...}}`. The Qdrant point id is a random
`uuid4().hex`, **not** a deterministic key derived from content — important for a
graph layer that needs to map entities back to source chunks (see §5).

### 2.7 Settings surface (`src/mcp_server_qdrant/settings.py`)

Three `BaseSettings` groups, all env-driven (no config file in upstream):

| Group | Env var(s) | Default | Purpose |
|---|---|---|---|
| `QdrantSettings.location` | `QDRANT_URL` | `None` | URL of the Qdrant server |
| `QdrantSettings.api_key` | `QDRANT_API_KEY` | `None` | API key |
| `QdrantSettings.collection_name` | `COLLECTION_NAME` | `None` | Default collection; if set, `collection_name` becomes a hidden arg (auto-bound via `make_partial_function`) |
| `QdrantSettings.local_path` | `QDRANT_LOCAL_PATH` | `None` | Mutually exclusive with `QDRANT_URL` (validated by `check_local_path_conflict`) |
| `QdrantSettings.search_limit` | `QDRANT_SEARCH_LIMIT` | `10` | Max results from `qdrant-find` |
| `QdrantSettings.read_only` | `QDRANT_READ_ONLY` | `False` | Hides `qdrant-store` |
| `QdrantSettings.filterable_fields` | (in-code only) | `None` | List of `FilterableField` dicts; powers both `wrap_filters` and payload indexes |
| `QdrantSettings.allow_arbitrary_filter` | `QDRANT_ALLOW_ARBITRARY_FILTER` | `False` | If `True` and no filterable fields, `qdrant-find` exposes a free-form `query_filter: dict` |
| `EmbeddingProviderSettings.provider_type` | `EMBEDDING_PROVIDER` | `"fastembed"` | Only `fastembed` supported upstream; `types.py` defines a 1-element enum |
| `EmbeddingProviderSettings.model_name` | `EMBEDDING_MODEL` | `"sentence-transformers/all-MiniLM-L6-v2"` | Any FastEmbed dense model |
| `ToolSettings.tool_store_description` | `TOOL_STORE_DESCRIPTION` | (see above) | Override the `qdrant-store` tool description |
| `ToolSettings.tool_find_description` | `TOOL_FIND_DESCRIPTION` | (see above) | Override the `qdrant-find` tool description |

`FilterableField` (from `settings.py`) is the per-field schema:

```python
class FilterableField(BaseModel):
    name: str
    description: str
    field_type: Literal["keyword", "integer", "float", "boolean"]
    condition: Literal["==", "!=", ">", ">=", "<", "<=", "any", "except"] | None = None
    required: bool = False
```

A field with `condition=None` gets a Qdrant payload index but is **not** exposed to
MCP. A field with `condition` set becomes a typed parameter of `qdrant-find`. This is
how the server maps a `qdrant_client.models.Filter` to an MCP-visible schema at
construction time.

### 2.8 Embedding provider layer (`src/mcp_server_qdrant/embeddings/`)

`EmbeddingProvider` is a 3-method ABC: `embed_documents`, `embed_query`,
`get_vector_name`, `get_vector_size`. `FastEmbedProvider` is the only impl,
returning a vector name like `fast-all-minilm-l6-v2` (slugified model name, prefixed
`fast-`). The `qdrant_client.models.VectorParams` is built with `Distance.COSINE`
and the model dim. For the Kuzu fork, **embedding is a no-op for the graph tool** —
it operates on already-stored entities — so the existing provider is reusable only if
you also want entity embedding (see §5).

---

## 3. MCP spec grounding (live)

Source: https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro (redirects
to `getting-started/intro`) and
https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2025-06-18/server/tools.mdx

### 3.1 What MCP gives us

- **Tools** are "schema-defined interfaces that LLMs can invoke. MCP uses JSON Schema
  for validation. Each tool performs a single operation with clearly defined inputs
  and outputs." Discovered via `tools/list`, invoked via `tools/call`. (Source:
  https://modelcontextprotocol.io/docs/2026-07-28/learn/server-concepts)
- Tools are **model-controlled** — the LLM picks when to call them. (Source:
  https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2025-06-18/server/tools.mdx)
- Servers that support tools **MUST** declare the `tools` capability; `listChanged`
  indicates dynamic tool list updates. (Same source.)
- "Resources" are passive, read-only data (think file contents, schemas). "Prompts"
  are user-controlled pre-built instruction templates. Neither is used by the
  upstream Qdrant server.

### 3.2 Implications for the Kuzu extension

- The MCP tool spec does **not** prescribe a particular scoring fusion or query
  routing shape — each tool is an independent function. This makes both
  integration approaches (separate tool vs hybrid retriever) conformant; the
  difference is implementation, not protocol. (Source: same.)
- A tool may have any JSON-Schema-compatible input. There is no built-in notion of
  multi-step composition across tools — composition is the model's job. So "vector
  search → graph expansion" is naturally expressed as two tools the model can call
  in sequence, or as one tool that internally calls both backends. Both are legal.
- Transport is fully the server's choice. The upstream picks `stdio`, `sse`, and
  `streamable-http` via the `--transport` flag on `FastMCP.run(...)`. Anything we
  add has to use the same `FastMCP.run(transport=...)` path — there is no extra
  transport plumbing in upstream.

---

## 4. Qdrant Python client grounding (live)

Source: https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/async_qdrant_client.py
and `https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/http/models/models.py`.

### 4.1 What `qdrant-client>=1.12.0` gives the upstream server

- `AsyncQdrantClient(location=..., api_key=..., path=...)` — the upstream passes
  `QDRANT_URL` (with trailing `/` stripped), `QDRANT_API_KEY`, and either an in-memory
  `":memory:"` or a local file path. `AsyncQdrantClient` is a 1:1 surface mirror of
  the sync `QdrantClient` for vector ops, plus async/await.
- `client.upsert(collection_name, points=[models.PointStruct(id=..., vector=..., payload=...)])`
  — the upsert path the upstream uses. `id` accepts `str` (UUID), `int`, or `PointId`;
  `vector` accepts a list (single dense) or a `dict[str, list[float]]` (named vectors).
  `payload` is a free-form dict.
- `client.query_points(collection_name, query=..., using=..., limit=..., query_filter=...)`
  is the canonical search call, returning `models.QueryResponse` with `.points`. The
  upstream uses this for `qdrant-find`. Confirmed at
  https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/async_qdrant_client.py
  lines 251–278 (`async def query_points`).
- `client.create_collection(collection_name, vectors_config={name: models.VectorParams(size, distance=models.Distance.COSINE)})`
  plus `client.create_payload_index(collection_name, field_name, field_schema=models.PayloadSchemaType.KEYWORD|INTEGER|FLOAT|BOOL)`
  for the filterable-field indexes.

### 4.2 Models in `qdrant_client.http.models` the upstream depends on

Confirmed at
https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/http/models/models.py:

| Model | Line | Role |
|---|---|---|
| `models.FieldCondition` | 988 | Single payload match — `key`, `match=MatchValue/MatchAny/MatchExcept`, `range=Range` |
| `models.Filter` | 1012 | `must=[...]`, `must_not=[...]` |
| `models.MatchAny` | 1786 | `any=[...]` |
| `models.MatchValue` | 1839 | `value=...` |
| `models.PayloadSchemaType` | 2209 | `KEYWORD`/`INTEGER`/`FLOAT`/`BOOL` enum |
| `models.Range` | 2624 | `gt`/`gte`/`lt`/`lte` |
| `models.VectorParams` | 3912 | `size`, `distance=Distance.COSINE` |
| `models.PointStruct` | (separate file) | `id`, `vector`, `payload` |

This set is the entire surface the upstream touches. A Kuzu-backed tool would not
need any new Qdrant models — it talks to Kuzu, not to Qdrant, and only optionally
back-references Qdrant point ids.

### 4.3 What we need from the client for a graph layer

For the Kuzu extension, we need **two** things from the Qdrant client:

1. The point id of a hit (so a graph node can cite the chunk that mentioned an
   entity). The upstream currently throws the id away — `Entry` only carries
   `content` and `metadata` (see `qdrant.py:132-138`). A Kuzu-backed tool needs
   either (a) the upstream to surface the id (small patch to `Entry` +
   `format_entry`), or (b) to keep a deterministic mapping (e.g. hash of content).
   Option (a) is cleaner; option (b) is a non-invasive fork.
2. The `with_vectors` / `with_payload` knobs on `query_points` are already exposed
   but unused — the upstream uses defaults (`True` payload, `False` vectors). No
   change required.

---

## 5. Kuzu extension — concrete extension points & chosen approach

This section is the deliverable the recon is meant to support. The remainder is
deliberately prescriptive about *where* Kuzu plugs in, not just *that* it can.

### 5.1 Alternatives considered

| Option | Shape | Pros | Cons |
|---|---|---|---|
| **A. Separate MCP tool** (`qdrant-graph-find`) | Add a third tool to `QdrantMCPServer.setup_tools` that opens a Kuzu DB at startup and exposes graph ops | No change to `qdrant-find` semantics; composable (model calls both); isolates Kuzu lifetime; matches MCP's per-tool granularity | Two round-trips for "vector → graph"; model must know to chain them |
| **B. Hybrid retriever fused inside `qdrant-find`** | Internally call Kuzu after `query_points`, fuse hits, return a single merged list | One tool, one round-trip; cleaner client UX | Breaks the existing API contract; mixes scores; harder to debug; couples Kuzu lifetime to every search |
| **C. Kuzu as a *resource*** (`kuzu://schema`) | Expose graph schema as MCP resource, leave tools as-is | Good for discoverability | MCP resources are read-only data — doesn't actually answer graph queries; doesn't solve the problem |
| **D. Kuzu as a *prompt*** | Pre-built template that instructs the model to chain tools | Almost no code | Doesn't add any new capability; pure prompt engineering |

**Decision: Option A — separate MCP tool, same FastMCP server.**

Rationale:

1. **Zero contract breakage.** `qdrant-find` and `qdrant-store` keep their existing
   signatures and return shapes. Existing clients (Claude Desktop, Cursor, VS Code,
   Windsurf, Claude Code, Kiro Power) keep working unchanged.
2. **MCP-native composition.** MCP's design is for the model to compose tools; the
   `tools/list` + `tools/call` cycle is exactly what makes "find entities, then expand
   relations" natural. (See §3.2 — tools are model-controlled.)
3. **Clean failure modes.** If Kuzu is down or its DB is missing, `qdrant-find` keeps
   working. `qdrant-graph-find` can return a structured error.
4. **Avoids pydantic-version coupling.** We can pin `kuzu` independently of the
   upstream's `pydantic>=2.10.6,<2.12.0` constraint and put it in an optional
   dependency group (`[project.optional-dependencies] graph = ["kuzu>=0.11"]`).
5. **FastMCP supports it directly.** `self.tool(func, name="qdrant-graph-find",
   description=...)` is the same call site already used twice in `mcp_server.py`.

### 5.2 Concrete extension points in the codebase

The minimum viable extension touches exactly five files, all within the existing
package layout. None requires changes to `mcp_server.py` semantics beyond `setup_tools`.

#### Extension point 1 — `src/mcp_server_qdrant/settings.py`

Add a new `BaseSettings` group `KuzuSettings` alongside `QdrantSettings` /
`ToolSettings` / `EmbeddingProviderSettings`:

- `KUZU_DB_PATH` (default `./kuzu_db`) — local on-disk Kuzu database path.
- `KUZU_READ_ONLY` (default `False`) — symmetric with `QDRANT_READ_ONLY`.
- Optional: `KUZU_GRAPH_NAME` (default `default`) — Kuzu supports multiple named
  graphs in one database, so this names the "graph" the MCP tool binds to.

This reuses the `pydantic-settings` pattern already in place; the `mcp_server_qdrant.server`
shim just adds a `KuzuSettings()` to the `QdrantMCPServer(...)` constructor call.

#### Extension point 2 — `src/mcp_server_qdrant/kuzu.py` (new file)

Mirror `qdrant.py`'s shape: an async `KuzuConnector` class wrapping `kuzu.KuzuDatabase`
+ `kuzu.AsyncConnection`. Public methods to expose:

- `get_entity(name: str) -> dict | None` — exact lookup over a `Entity(name, ...)`
  node table.
- `find_related(entity_name: str, relation: str | None = None, depth: int = 1,
  limit: int = 50) -> list[dict]` — Cypher `MATCH (a:Entity {name:$n})-[:RELATES*1..d]->(b)`
  expansion.
- `expand_from_qdrant_hits(hits: list[dict], relation: str | None = None,
  depth: int = 1, limit_per_hit: int = 20) -> list[dict]` — accepts the output of
  `qdrant-find` (or just the metadata of the hits) and returns the union of related
  entities. This is the post-filter graph expansion method.
- `upsert_entity_mention(point_id: str, entity_name: str, collection: str,
  metadata: dict | None = None)` — write-side hook for `qdrant-store` (see
  Extension point 4).

The Kuzu database itself holds three node tables and one relationship table by
default (these are the "natural" tables for this use case, not a hard requirement):

- `Entity(name STRING, type STRING, properties JSON)` — primary key `name`.
- `Chunk(point_id STRING, collection STRING, source_uri STRING, properties JSON)` —
  primary key `(point_id, collection)`. This is the back-reference to Qdrant.
- `Document(uri STRING, ...)` — optional, for documents that own chunks.
- `MENTIONS(Entity -> Chunk)` — `Chunk` mentions `Entity`.
- `RELATES(Entity -> Entity, kind STRING, weight DOUBLE)` — generic relation between
  entities, with a `kind` discriminator (e.g. "owns", "depends_on", "co_occurs").

These are recommendations; actual schema lives in `kuzu.py`'s `_ensure_schema` at
startup, idempotent (Kuzu supports `CREATE NODE TABLE IF NOT EXISTS`).

#### Extension point 3 — `src/mcp_server_qdrant/mcp_server.py`

Two small additions inside `QdrantMCPServer`:

1. New `__init__` parameter `kuzu_settings: Optional[KuzuSettings] = None` and a new
   instance field `self.kuzu_connector = KuzuConnector(...)` constructed only if
   `kuzu_settings is not None`. This preserves the upstream invariant *"Cannot provide
   both embedding_provider_settings and embedding_provider"* — graph is purely
   additive.
2. In `setup_tools`, after the existing `self.tool(...)` calls, register one or two
   more tools:
   - `qdrant-graph-find` — parameters: `entity: str` (required), `relation: str | None`,
     `depth: int = 1`, `limit: int = 50`. Returns `list[str]` of formatted graph
     records, parallel to `qdrant-find`'s `<entity>` envelope style.
   - Optional `qdrant-graph-expand-from-hits` — parameters: `query: str`,
     `collection_name: str`, `relation: str | None`, `depth: int = 1`. Internally:
     `self.qdrant_connector.search(query, ...)` → for each hit's metadata.entities
     → `self.kuzu_connector.expand_from_qdrant_hits(...)`. This is the
     *post-filter graph expansion over vector hits* the original spec asks about,
     exposed as a single tool the model can call in one round trip. It is
     non-breaking — it does not modify `qdrant-find`'s return shape.

#### Extension point 4 — `qdrant-store` integration (optional but recommended)

The upstream `store` method discards the Qdrant point id (it stores it but does not
return it). To make the graph useful, `qdrant-store` should optionally accept an
`entities: list[str] | None` parameter; when provided, after the Qdrant upsert, also
call `self.kuzu_connector.upsert_entity_mention(point_id=<the new uuid>, ...)`. This
requires a tiny refactor in `qdrant.py`: return the new point id from `store`, then
thread it into the tool body.

This is the **only** invasive change to the existing tool. It is opt-in (the
parameter is optional, and Kuzu being absent makes it a no-op).

#### Extension point 5 — `pyproject.toml`

```toml
[project.optional-dependencies]
graph = ["kuzu>=0.11"]

[project.scripts]
# unchanged
mcp-server-qdrant = "mcp_server_qdrant.main:main"
```

The `[graph]` extra is what users opt into with `uvx --with kuzu mcp-server-qdrant`
or `pip install mcp-server-qdrant[graph]`. Keeps the base install identical to
upstream so existing deployments are not affected.

### 5.3 What we explicitly do *not* touch

- `src/mcp_server_qdrant/main.py` — transport wiring is upstream's contract; we
  inherit `--transport` semantics verbatim.
- `src/mcp_server_qdrant/embeddings/` — the graph tool does not embed; entity
  resolution from text is handled by the embedding provider's output *indirectly*,
  via `qdrant-find` returning hits whose metadata carries pre-extracted entity
  names. If we ever want graph-side entity resolution (entity linking at write
  time), that's a separate feature and a new file.
- `src/mcp_server_qdrant/common/filters.py`, `common/wrap_filters.py`,
  `common/func_tools.py` — Kuzu does not need Qdrant filter rewriting. Leave alone.
- `tests/` — keep upstream tests passing; add new tests under `tests/test_kuzu.py`
  and `tests/test_mcp_server_tools.py`.

### 5.4 Verification matrix for the extension

Once implemented, the following must hold (test plan, not implemented here):

1. `uvx mcp-server-qdrant` (no `[graph]` extra) registers **only** `qdrant-find` and
   `qdrant-store` — identical to upstream.
2. `uvx --with kuzu mcp-server-qdrant` registers `qdrant-find`, `qdrant-store`, and
   `qdrant-graph-find` (and optionally `qdrant-graph-expand-from-hits`).
3. `tools/list` on a stdio transport returns the expected JSON Schema for each tool;
   tool descriptions surface the new env-var hooks (`TOOL_GRAPH_FIND_DESCRIPTION`,
   `TOOL_GRAPH_EXPAND_DESCRIPTION`) consistent with the existing `TOOL_*_DESCRIPTION`
   pattern.
4. `qdrant-find` return shape is byte-identical to upstream when Kuzu is not
   configured.
5. The Kuzu DB is created on first run if missing; existing DB is reused.
6. `QDRANT_READ_ONLY=true` hides `qdrant-store`; the analogous `KUZU_READ_ONLY=true`
   (if added) hides any graph-write tool.

---

## 6. Coverage matrix

| Question / angle | Source types searched | Usable sources | Confidence |
|---|---|---|---|
| Upstream README + tool surface | Live GitHub `raw.githubusercontent.com` + `api.github.com` | README at `/master`, `pyproject.toml`, `main.py`, `server.py`, `mcp_server.py`, `qdrant.py`, `settings.py`, `common/*.py`, `embeddings/*.py`, `Dockerfile` | High |
| Existing tools enumerated | `setup_tools` in `mcp_server.py:88–199` | Two tools, full parameter shapes verified | High |
| Existing dependencies / transport | `pyproject.toml`, `main.py`, `Dockerfile` | `fastmcp==2.7.0`, `qdrant-client>=1.12.0`, `fastembed>=0.6.0`, `pydantic>=2.10.6,<2.12.0`; `stdio`/`sse`/`streamable-http` | High |
| Pinned commit SHA | GitHub REST `/commits/master` | `c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41` (2026-08-11) | High |
| MCP tools spec | `modelcontextprotocol.io` + official `modelcontextprotocol/modelcontextprotocol` repo | `tools/list` + `tools/call` semantics, JSON Schema inputs, model-controlled | High |
| Qdrant Python client API surface used | `raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/async_qdrant_client.py` and `.../http/models/models.py` | `AsyncQdrantClient.upsert`, `query_points`, `models.PointStruct`, `models.Filter`, `models.FieldCondition`, `models.MatchValue`, `models.MatchAny`, `models.Range`, `models.PayloadSchemaType`, `models.VectorParams`, `models.Distance.COSINE` | High |
| Python client mkdocs landing | `https://python-client.qdrant.tech/` (returned the Qdrant docs index, JS-rendered) | Site exists and is live; primary source is the GitHub `qdrant-client` repo for the API contract | Medium (landing page only — verified via the index, not per-page render) |
| FastMCP transport docs | `https://gofastmcp.com/servers/transports` | Site exists; content was JS-rendered and the curl returned empty body. Transport behaviour is confirmed via `main.py` and the FastMCP source instead | Medium (page itself unrenderable; behaviour confirmed via code) |
| Kuzu Python package | (not consulted in this task — task explicitly says "Do NOT consult any local archive or older checkout as a substitute" for upstream; Kuzu package version pinning is a separate downstream task) | None — out of scope for this recon | n/a |

### Gaps and explicit "could not verify" items

- The `https://python-client.qdrant.tech/` site serves a JS-rendered index in this
  retrieval window; the canonical API reference is therefore sourced from the live
  `qdrant-client` GitHub repo at the matching master SHA rather than from the rendered
  docs page. The API surface cited here is still authoritative — it is the source of
  the wheel that ships to users.
- `https://gofastmcp.com/servers/transports` did not return body content via curl in
  this window. Transport semantics are instead confirmed from the upstream
  `mcp_server_qdrant/main.py` (uses `FastMCP.run(transport=...)`) plus the README
  transport table. The exact `StreamableHTTPSessionManager` semantics on the FastMCP
  side are out of scope for this recon — they are inherited, not redefined.
- I did not consult any local archive or older checkout. All file contents come from
  the pinned SHA URLs.

---

## 7. Citations

Every load-bearing claim above traces to one of the following live URLs:

- Upstream repository: https://github.com/qdrant/mcp-server-qdrant
- Pinned commit: https://github.com/qdrant/mcp-server-qdrant/commit/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41
- `pyproject.toml`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/pyproject.toml
- `main.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/main.py
- `server.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/server.py
- `mcp_server.py` (tools): https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/mcp_server.py
- `qdrant.py` (connector): https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/qdrant.py
- `settings.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/settings.py
- `common/filters.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/common/filters.py
- `common/func_tools.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/common/func_tools.py
- `common/wrap_filters.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/common/wrap_filters.py
- `embeddings/base.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/embeddings/base.py
- `embeddings/factory.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/embeddings/factory.py
- `embeddings/fastembed.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/embeddings/fastembed.py
- `embeddings/types.py`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/src/mcp_server_qdrant/embeddings/types.py
- `Dockerfile`: https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/Dockerfile
- Tests (sampled): https://raw.githubusercontent.com/qdrant/mcp-server-qdrant/c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41/tests/test_qdrant_integration.py
- MCP spec landing: https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro
- MCP server concepts: https://modelcontextprotocol.io/docs/2026-07-28/learn/server-concepts
- MCP tools spec: https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2025-06-18/server/tools.mdx
- Qdrant Python client docs: https://python-client.qdrant.tech/
- Qdrant Python client source (async): https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/async_qdrant_client.py
- Qdrant Python client models: https://raw.githubusercontent.com/qdrant/qdrant-client/master/qdrant_client/http/models/models.py
