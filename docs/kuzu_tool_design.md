# Design: Kuzu graph tool surface and schema for `qdrant-kuzu-mcp`

**Status:** design only — no code. Handoff target: task `t_f2f6cae6` (coder).
**Inputs:** `docs/recon.md` (upstream recon, pinned SHA `c56ae5adf62bb78d852bf7bbcbc5d7b75e2bbe41`,
mcp-server-qdrant v0.8.1) + live Kuzu sources listed in §10.
**Date:** 2026-09-25.

---

## 1. Goal and scope

**Goal (one sentence):** Add Kuzu-backed graph tools to the forked
`mcp-server-qdrant` FastMCP server so an MCP client can store and traverse an
entity graph derived from Qdrant memories, without changing the existing
`qdrant-find` / `qdrant-store` contracts.

**In scope**

- Concrete MCP tool names, parameters, validation rules, JSON return shapes.
- Kuzu on-disk schema (DDL) and initialization semantics.
- Persistence decision (file path vs per-process) with rationale.
- Exact Cypher pattern each tool executes, and which upstream Qdrant tool each
  graph capability complements.
- Environment-variable settings surface and failure/degradation behavior.

**Out of scope (v1)**

- Any implementation code (next task).
- Entity extraction / NER at write or read time — entity names arrive
  explicitly from the caller (LLM) via the `entities` parameter.
- Kuzu extensions (full-text search, vector index) — core embedded engine only.
- A `Document` node table (noted as a future extension in §5.5).
- Hybrid score fusion inside `qdrant-find` (rejected in recon §5.1; stays
  rejected).

---

## 2. Decisions at a glance

| # | Decision | Choice | Rejected alternatives |
|---|---|---|---|
| D1 | Integration shape | Separate MCP tools in the same FastMCP server (recon Option A) | Fused hybrid retriever; MCP resource; MCP prompt |
| D2 | Tool names | `qdrant-graph-find`, `qdrant-graph-expand-from-hits`, plus an optional `entities` parameter on `qdrant-store` | `graph_traverse` / `graph_search` / `graph_neighbors`; engine-prefixed `kuzu-*` |
| D3 | Kuzu state | **Persisted on disk** at `KUZU_DB_PATH` (default `./kuzu_db`); `":memory:"` supported for tests/ephemeral use | Per-process in-memory only |
| D4 | Schema | 2 node tables (`Entity`, `Chunk`), 2 rel tables (`MENTIONS`, `RELATES`), single-column primary keys | JSON-typed property columns; `Document` table; per-kind rel tables |
| D5 | Kuzu package pin | `kuzu>=0.11.3,<0.12` in `[project.optional-dependencies] graph` | `kuzu>=0.11` (recon's looser floor); unpinned |
| D6 | Write atomicity | Explicit `BEGIN TRANSACTION … COMMIT` on one acquired pooled connection, guarded by a connector-level `asyncio.Lock` | Per-statement autocommit for multi-entity writes |
| D7 | Failure mode | If the Kuzu DB cannot open at startup, log to stderr, register **no** graph tools, keep vector tools working | Fail server startup |
| D8 | Return shapes | Structured JSON dict envelopes (not `list[str]`), all values str/int/bool/list-of-those | Mirroring upstream's `list[str] | None` envelope for graph results |

Each decision is normative for the implementer; rationale follows in the
referenced sections.

---

## 3. Kuzu grounding (verified live, 2026-09-25)

Facts the design depends on, each verified against live sources this session
(full list in §10):

1. **Project status.** `github.com/kuzudb/kuzu` is **archived**; prior releases
   remain usable unchanged. Final release is **`kuzu` 0.11.3** on PyPI
   (2025-10-10, MIT). 0.11.3 "bundles many (but not all) of the extensions" —
   irrelevant to us: **this design uses zero Kuzu extensions**, core engine
   only. (Source: repo README via github.com/kuzudb/kuzu; pypi.org/project/kuzu.)
2. **Python package.** `pip install kuzu`. Wheels exist for CPython 3.10–3.13
   on `win_amd64`, macOS, and manylinux (verified via the PyPI JSON API for
   0.11.3) — Windows, the host platform for this project, is covered.
   `requires_python` is unset (any Python); the project's `>=3.10` floor is
   compatible. Kuzu has **no pydantic dependency**, so it cannot collide with
   upstream's `pydantic>=2.10.6,<2.12.0` constraint.
3. **Core API.**
   - `db = kuzu.Database(path)` — `path` is a directory for on-disk mode; `""`,
     `None`, or `":memory:"` gives an in-memory DB that dies with the process.
     Constructor options include `read_only: bool = False`,
     `buffer_pool_size`, `max_num_threads`.
   - `conn = kuzu.Connection(db)`; `result = conn.execute(query, parameters={...})`
     — parameters are passed as a dict, never string-interpolated. Multi-
     statement strings return `list[QueryResult]`; failures raise `RuntimeError`
     with the engine error message.
   - `QueryResult` iteration: `has_next()` / `get_next()` (rows are lists,
     positionally aligned with `get_column_names()`), `get_all()`, `close()`.
     Result objects hold connection resources and should be closed/consumed.
   - **Async:** `kuzu.AsyncConnection(db, max_concurrent_queries=4,
     max_threads_per_query=0)` wraps a pool of `Connection`s plus a
     `ThreadPoolExecutor`; `await conn.execute(...)`; supports context-manager
     use and `acquire_connection()` / `release_connection()` for temporarily
     holding one specific pooled connection (needed for manual transactions,
     see D6).
4. **Transaction model.** Every statement runs in a transaction; without an
   explicit `BEGIN`, each statement **autocommits** as a serializable txn.
   `BEGIN TRANSACTION` (read-write), `BEGIN TRANSACTION READ ONLY`, `COMMIT`,
   `ROLLBACK` are supported. **At any time there can be multiple read
   transactions but only ONE write transaction** — this drives the connector
   write-lock (D6). Durability is WAL + automatic `CHECKPOINT` (manual
   `CHECKPOINT;` only succeeds when no active transactions).
5. **DDL.** `CREATE NODE TABLE [IF NOT EXISTS] T(col TYPE [PRIMARY KEY], ...)`
   and `CREATE REL TABLE [IF NOT EXISTS] R(FROM A TO B, prop TYPE, ...)`. Node
   tables require a primary key; the schema below assumes **one** PK column per
   node table (the conservative reading of the docs — avoid composite PKs).
   `DROP TABLE [IF EXISTS]` exists for teardown in tests.
6. **Cypher surface.** `MATCH`/`WHERE`/`RETURN`/`ORDER BY`/`LIMIT`/`SKIP`/
   `DISTINCT`; inline property maps in patterns (`{name: 'Adam'}`); `MERGE`
   with `ON CREATE SET` / `ON MATCH SET` (docs: MERGE is intended for small,
   sporadic writes — exactly our write pattern; use `COPY FROM` for bulk, which
   we do not expose via MCP). Variable-length paths `-[:REL*1..3]->` are
   supported **but the relationship type must be named** — Kuzu rejects untyped
   `-[*1..3]->`. Path functions (`nodes(p)`, `rels(p)`, `length(p)`) and
   quantifier predicates (`all(x IN list WHERE ...)`) exist in Kuzu Cypher;
   where a feature is not 100% confirmed for variable-length use, §9 lists a
   verified-safe Python fallback.

---

## 4. Persistence and lifecycle (Decision D3)

**Kuzu state is persisted to a file path, not per-process.**

- `KUZU_DB_PATH` (default `./kuzu_db`) is a directory Kuzu creates and owns.
  On-disk mode writes a WAL and checkpoints automatically — data survives
  process restarts.
- `KUZU_DB_PATH=":memory:"` (or `""`) selects an in-memory DB: schema is
  re-created at startup, all graph state is lost at exit. Intended for tests
  and ephemeral demos, not production memory use.

**Rationale.** The server's value proposition is a *memory* that accumulates:
`qdrant-store` writes today must be traversable by `qdrant-graph-find` next
week. Per-process state would make the write path pointless and the read tools
amnesiac. On-disk is the only mode where the graph complements the Qdrant
collection (which is itself persistent).

**Lifecycle rules for the implementer**

1. `KuzuConnector` is constructed in `QdrantMCPServer.__init__` **iff** the
   `kuzu` package imports AND `KUZU_ENABLED` is not `"false"`. Import is
   guarded (`try: import kuzu`) so the base install (no `[graph]` extra)
   behaves exactly like upstream.
2. On construction: open `kuzu.Database(KUZU_DB_PATH, read_only=KUZU_READ_ONLY,
   buffer_pool_size=...)`, then run `_ensure_schema()` (§5) unless read-only.
   In read-only mode an existing schema is required; if the tables are absent,
   graph read tools return their empty envelope (§7) rather than raising.
3. **Concurrency:** one `AsyncConnection(db, max_concurrent_queries=
   KUZU_MAX_CONCURRENT_QUERIES)` serves reads concurrently. **All writes go
   through a single `asyncio.Lock`** (Kuzu allows one write txn at a time;
   the lock converts an engine-level race into clean serialization).
4. **Multi-write atomicity:** `qdrant-store` with `entities`/`relations` runs
   `BEGIN TRANSACTION` … `COMMIT` as multiple `execute()` calls on **one**
   connection held via `acquire_connection()`/`release_connection()` for the
   whole transaction (pool round-trips between statements would silently break
   txn affinity). On any exception: `ROLLBACK`, then release, then re-raise.
5. **Degradation (D7):** if `kuzu.Database(...)` or `_ensure_schema()` raises
   at startup, log the error to stderr, set the connector to `None`, and
   register no graph tools. `qdrant-find` / `qdrant-store` are unaffected.
   This mirrors recon §5.1 rationale 3 ("clean failure modes").
6. **Shutdown:** there is no server-level shutdown hook in upstream
   `main.py`; rely on Kuzu autocommit + WAL/auto-checkpoint for durability and
   process-exit teardown. Do not add an atexit hook in v1 (unnecessary given
   autocommit-per-write).

**Operational caveats to document in the README (implementer)**

- Kuzu is an **embedded, single-process-per-database** engine: two server
  processes pointing at the same `KUZU_DB_PATH` will fail (second open errors
  → that process degrades per rule 5). One MCP server process per DB path.
- The DB directory is engine-owned; users back it up by copying the directory
  while no server process holds it.
- `KUZU_READ_ONLY=true` requires the DB to already exist (Kuzu cannot create
  in read-only mode); otherwise startup degrades per rule 5.

---

## 5. Kuzu schema (Decision D4)

All timestamps are ISO-8601 strings supplied by Python (`datetime.now(timezone.
utc).isoformat()`), not `DEFAULT` expressions — explicit values are portable
and parameterization-friendly. All string values pass through `$parameters`.

### 5.1 DDL (executed by `_ensure_schema()`, idempotent)

```cypher
CREATE NODE TABLE IF NOT EXISTS Entity (
    name       STRING PRIMARY KEY,
    type       STRING,
    created_at STRING
);

CREATE NODE TABLE IF NOT EXISTS Chunk (
    chunk_id        STRING PRIMARY KEY,   -- "<collection>:<point_id>"
    point_id        STRING,               -- Qdrant point id (uuid4 hex)
    collection      STRING,               -- Qdrant collection name
    content_snippet STRING,               -- first 200 chars of stored info
    created_at      STRING
);

CREATE REL TABLE IF NOT EXISTS MENTIONS (
    FROM Chunk TO Entity,
    first_seen STRING
);

CREATE REL TABLE IF NOT EXISTS RELATES (
    FROM Entity TO Entity,
    kind        STRING,   -- e.g. "depends_on", "works_at", "part_of"
    weight      DOUBLE,   -- 1.0 on create (see §7.3 note on increments)
    created_at  STRING,
    last_seen   STRING
);
```

### 5.2 Design notes

- **Single-column PKs.** `Entity.name` is the identity (v1 does not
  disambiguate same-named entities of different types; `type` is advisory).
  `Chunk.chunk_id` is the deterministic string `f"{collection}:{point_id}"`
  because Qdrant point ids are unique per collection, not globally — the
  composite encodes both without needing a composite PK.
- **Direction.** `MENTIONS` reads "Chunk MENTIONS Entity" — `FROM Chunk TO
  Entity`. (The recon sketch had the arrow backwards; this design corrects
  it.) `RELATES` is `FROM Entity TO Entity`; subject→predicate→object.
- **Generic `RELATES.kind` vs per-kind tables.** Relation kinds are LLM-
  supplied at runtime; a typed table per kind would require DDL per new kind
  (unbounded schema mutation from a tool call). One table with a validated
  `kind` property keeps the schema fixed. Kind filtering at read time uses the
  `kind` property (see §6); Kuzu property scans are fine at memory-graph
  scale.
- **`content_snippet` (200 chars)** lets graph results carry enough context to
  be useful without duplicating Qdrant as a document store. The authoritative
  text always lives in Qdrant; the snippet is a pointer aid.
- **No JSON columns.** Avoids any dependency on Kuzu's JSON type surface and
  keeps every stored value a primitive. Extra per-entity attributes are out of
  scope for v1.

### 5.3 Write Cypher (used by the `entities`/`relations` path of `qdrant-store`)

Executed inside one explicit transaction (D6). `$` values are bound
parameters; the ONLY string interpolation in any query this design emits is
the validated integer depth bound in §6.

```cypher
-- 1. Entity upsert (per entity e in entities ∪ subjects ∪ objects)
MERGE (e:Entity {name: $name})
ON CREATE SET e.type = $type, e.created_at = $ts;

-- 2. Chunk node (once, after the Qdrant upsert succeeded)
MERGE (c:Chunk {chunk_id: $chunk_id})
ON CREATE SET c.point_id = $point_id, c.collection = $collection,
              c.content_snippet = $snippet, c.created_at = $ts;

-- 3. Mention edges (per entity)
MATCH (c:Chunk {chunk_id: $chunk_id}), (e:Entity {name: $name})
MERGE (c)-[m:MENTIONS]->(e)
ON CREATE SET m.first_seen = $ts;

-- 4. Relation edges (per {subject, predicate, object})
MERGE (s:Entity {name: $subject})
MERGE (o:Entity {name: $object})
MERGE (s)-[r:RELATES {kind: $kind}]->(o)
ON CREATE SET r.weight = 1.0, r.created_at = $ts
ON MATCH SET r.last_seen = $ts;
```

Note: `qdrant-store` must be patched to **return the new point id** from
`QdrantConnector.store` (upstream discards it — recon §4.3). The point id is
generated by the tool layer (`uuid4().hex`, same as upstream) and threaded to
both Qdrant and Kuzu, so the `Chunk` node exists before any mention of it.

### 5.4 Read Cypher (used by the graph tools)

Seeded expansion (the core pattern; `:min..max` bound is interpolated as
validated ints, direction per §6.1):

```cypher
-- outgoing
MATCH (a:Entity {name: $seed})-[rs:RELATES*1..3]->(b:Entity)
WHERE ALL(r IN rs WHERE r.kind = $kind)      -- only when kind filter set
RETURN b.name AS name, b.type AS type, size(rs) AS depth, rs[-1].kind AS via
ORDER BY depth, name
LIMIT $limit;

-- undirected (default): replace the pattern with
MATCH (a:Entity {name: $seed})-[rs:RELATES*1..3]-(b:Entity) ...
-- incoming: <-[rs:RELATES*1..3]-
```

Chunk back-reference (provenance, used by both read tools):

```cypher
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {name: $name})
RETURN c.point_id, c.collection, c.content_snippet
LIMIT $limit;
```

`size(rs)` (list length) and `rs[-1]` (list indexing) are the conservative
forms; §9 gives the fallback if either misbehaves on 0.11.3.

### 5.5 Future extensions (explicitly not v1)

- `Document(uri STRING PRIMARY KEY, …)` + `Chunk→Document` ownership edges.
- Per-kind rel tables or kind-specific indexes if `kind` scans ever matter.
- Weight increments (`ON MATCH SET r.weight = r.weight + 1`) — see §9 risk R3.

---

## 6. Tool surface (Decision D2)

Three deliverable pieces: two new MCP tools and one additive parameter on an
existing tool. Names are kebab-case with the `qdrant-` prefix to match the
upstream family (`qdrant-find`, `qdrant-store`) so clients see one coherent
namespace per server.

**Naming decision record.** The task brief floated `graph_traverse`,
`graph_search`, `graph_neighbors`. Those are subsumed: `qdrant-graph-find`
covers neighbors+traversal (depth-parameterized expansion is both);
`graph_search` would duplicate `qdrant-graph-find` with a fuzzier contract.
Engine-prefixed `kuzu-*` names were rejected because the server brand is
qdrant and the LLM-facing surface should not advertise storage engines.
Sticking to the recon-committed names also keeps the two design documents and
all child tasks consistent.

### 6.1 `qdrant-graph-find`

Expand the entity graph from a seed entity.

| Aspect | Specification |
|---|---|
| Description (env-overridable via `TOOL_GRAPH_FIND_DESCRIPTION`) | "Traverse the entity graph derived from stored memories. Use after qdrant-find when the question is about relationships between entities (dependencies, ownership, co-occurrence) rather than similar text. Provide a seed entity name; returns related entities with hop distance and the chunk that mentions them." |
| Params | `entity: str` (required) — seed name. `relation: str \| None = None` — restrict traversal to one `kind`. `depth: int = 1` — max hops. `direction: str = "any"` — `"any" \| "out" \| "in"`. `limit: int = 20` — max entities returned. |
| Cypher | Seeded expansion from §5.4 (pattern variant per `direction`), then chunk back-reference per returned entity (bounded: back-refs fetched for at most `limit` entities). |
| Complements | **`qdrant-find`** — vector search answers "what text is similar"; this answers "what is structurally connected". Typical chain: `qdrant-find` surfaces an entity name → `qdrant-graph-find` walks its neighborhood. |
| Returns | §7.1 envelope. |
| Errors | Unknown seed → `found: false` envelope (not an exception). Invalid params → FastMCP tool error. |

### 6.2 `qdrant-graph-expand-from-hits`

One-round-trip vector→graph bridge: embed + search Qdrant, harvest entity
names from hit metadata, expand the graph from each.

| Aspect | Specification |
|---|---|
| Description (env-overridable via `TOOL_GRAPH_EXPAND_DESCRIPTION`) | "Search memories in Qdrant and expand the entity graph from the entities those memories mention. One call replaces a qdrant-find followed by several qdrant-graph-find calls when you want everything related to a topic." |
| Params | `query: str` (required). `collection_name: str` (required; hidden when `QDRANT_SETTINGS.collection_name` default-collection mode is on, mirroring upstream). `relation: str \| None = None`. `depth: int = 1`. `limit: int = 20` — max entities **per seed**. `search_limit: int = 5` — how many Qdrant hits to harvest entities from. |
| Behavior | 1. `qdrant_connector.search(query, collection_name, limit=search_limit)` (existing method, no change). 2. Entity harvest rule, per hit, in order: (a) `metadata["entities"]` if it is a `list[str]` → use; (b) else no entities from that hit. **No NER in v1** — hits without explicit entity metadata contribute nothing. 3. Dedupe entities across hits. 4. For each entity (cap: 16 seeds), run the §5.4 expansion + back-refs. 5. Merge into one envelope with per-seed blocks. |
| Cypher | Qdrant `query_points` (not Cypher) followed by the §5.4 expansion per seed. |
| Complements | **`qdrant-find`** — same entry point (query text), adds the structural layer the vector search cannot express, in a single tool call. This is the recon §5.2 "post-filter graph expansion" tool. |
| Returns | §7.2 envelope. |
| Errors | Qdrant failure → raise (vector backend is the primary dependency here). Empty hits or no harvestable entities → `seeds: []` envelope. |

### 6.3 `qdrant-store` — additive `entities` / `relations` parameters

Not a new tool: an opt-in extension of the existing one.

| Aspect | Specification |
|---|---|
| New params (only when graph connector is active) | `entities: list[str] \| None = None` — entity names mentioned by `information`. `relations: list[dict] \| None = None` — each `{"subject": str, "predicate": str, "object": str}`. |
| Signature surgery | Exposed exactly like upstream's `wrap_filters`/`make_partial_function` pattern: when the graph is off (not installed, disabled, or failed to open), the registered signature is byte-identical to upstream (recon §5.4 item 4). When on, the two optional params are appended. |
| Behavior | After the existing Qdrant upsert succeeds (and only then), run §5.3 in one transaction. Point id is the same `uuid4().hex` just upserted. Graph failure after a successful Qdrant write must NOT fail the tool: log to stderr, return the upstream confirmation string with an appended `" (graph sync skipped: <error>)"`. Qdrant remains the source of truth. |
| Cypher | §5.3 MERGE block. |
| Complements | **`qdrant-store`** itself — it extends the write path so memories become traversable, which is what makes the two read tools above useful. |
| Returns | Unchanged upstream string (plus the failure suffix above). |
| Read-only modes | `QDRANT_READ_ONLY=true` hides `qdrant-store` entirely (upstream behavior, untouched). `KUZU_READ_ONLY=true` hides the graph extras (param not exposed) while vector storage still works. |

---

## 7. Return shapes (Decision D8)

All graph tools return plain dicts of str/int/float/bool/list — JSON-
serializable by FastMCP without custom models (keeps the upstream pydantic
pin untouched). `None` is never returned; absence is explicit (`found: false`,
empty lists).

### 7.1 `qdrant-graph-find` envelope

```json
{
  "seed": {"name": "PyTorch", "found": true, "type": "library"},
  "depth_searched": 2,
  "entities": [
    {
      "name": "CUDA",
      "type": "technology",
      "depth": 1,
      "via": "depends_on",
      "direction": "out"
    },
    {"name": "cuDNN", "type": "technology", "depth": 2, "via": "depends_on", "direction": "out"}
  ],
  "chunks": [
    {"entity": "CUDA", "point_id": "a1b2…", "collection": "memories", "snippet": "PyTorch builds on CUDA…"}
  ],
  "count": 2,
  "truncated": false
}
```

Rules: `entities` is depth-then-name ordered, deduped by name (nearest hop
wins); `chunks` carries at most one back-reference per entity; `truncated` is
true when the Cypher `LIMIT` boundary was hit. `via` is the `kind` of the last
edge on the shortest found path; `direction` is `"out"`/`"in"` relative to the
seed (`"any"` collapses to the actual direction of the final edge).

### 7.2 `qdrant-graph-expand-from-hits` envelope

```json
{
  "query": "how do model layers depend on the GPU stack",
  "collection": "memories",
  "hits_used": 3,
  "seeds": [
    {
      "seed": {"name": "PyTorch", "found": true, "type": "library"},
      "entities": [ {"name": "CUDA", "type": "technology", "depth": 1, "via": "depends_on", "direction": "out"} ],
      "count": 1
    }
  ],
  "truncated": false
}
```

`truncated` is true when either the seed cap (16) or any per-seed `limit` was
hit.

### 7.3 Why structured envelopes instead of upstream's `list[str]`

Graph rows are relational (name/type/depth/via); flattening them into prose
strings would force every client to re-parse. The upstream string envelope
exists for free-text memories; structure is the point of a graph tool. The
first key of each envelope still echoes the request, mirroring upstream's
`"Results for the query '<query>'"` convention, so log readers keep their
orientation. (Weight increments from §5.3's `ON MATCH` are intentionally not
reflected in read results in v1 — `weight` is stored but unused downstream;
see risk R3.)

---

## 8. Parameter validation rules

Validation happens in the tool layer **before** any Cypher is built; failures
raise `ValueError` (FastMCP surfaces it as a tool error with the message).
Bounded inputs are the injection defense-in-depth on top of parameter binding.

| Param | Rule |
|---|---|
| `entity` / entity names in `entities` / `subject` / `object` | `str`, stripped, 1–256 chars after strip, no C0 control chars. |
| `entities` | `list[str]`, ≤ 32 items, deduplicated (order preserved), empty list = no-op. |
| `relations` | `list[dict]`, ≤ 32 items; each has exactly `subject`, `predicate`, `object`; subject/object follow the entity rule; `subject != object` (no self-loops in v1); implicit entities (subjects/objects) merge with `entities` for the MERGE pass. |
| `predicate` / `relation` (kind) | matches `^[a-z][a-z0-9_]{0,63}$`. |
| `depth` | `int`, 1–3. Default 1. (Cost control: variable-length expansion is exponential in the worst case; 3 hops over a memory-scale graph is ample.) |
| `limit` | `int`, 1–100. Default 20. |
| `search_limit` | `int`, 1–10. Default 5. |
| `direction` | one of `"any"`, `"out"`, `"in"`. Default `"any"`. |
| `query` / `collection_name` | non-empty `str`; semantics delegated to the existing Qdrant path (identical expectations as upstream tools). |
| `snippet` | server-side truncation of stored `information` to 200 characters. |
| Interpolated values | **only** `min`/`max` hop bounds, interpolated as decimal ints after the `depth` check. Every string reaches Cypher via `$parameters`. |

Type mismatches (e.g. `depth: "2"`) are rejected, not coerced — MCP clients
get the JSON Schema from the signatures, so a mismatch is a caller bug worth
surfacing.

---

## 9. Risks and open questions

Each risk carries a fallback the implementer applies if the optimistic path
fails on kuzu 0.11.3. None blocks starting work.

- **R1 — Undirected variable-length patterns.** `-[rs:RELATES*1..3]-`
  (direction "any") is expected to work, but if 0.11.3 rejects undirected
  variable-length patterns, fall back to running the `out` and `in` queries
  and unioning in Python (dedupe by name, keep nearer hop). Verify in the
  first implementation milestone.
- **R2 — `ALL(r IN rs WHERE r.kind = $kind)` on rel lists.** If the quantifier
  over a variable-length rel list is unsupported, fall back to Python post-
  filtering: drop the `WHERE ALL`, return `rs` kinds per row, filter rows in
  the connector. Always correct, slightly more data over the wire.
- **R3 — Arithmetic `SET` in `ON MATCH`.** `r.weight = r.weight + 1` is
  plausible but unverified; v1 spec already avoids it (`last_seen` plain
  assignment only, weight fixed at 1.0 on create). If arithmetic SET works,
  the implementer MAY add the increment — it is a one-line, read-invisible
  improvement.
- **R4 — Parameters inside MERGE property maps.** `MERGE (e:Entity {name:
  $name})` should bind, but if 0.11.3 requires literals there, restructure to
  `MERGE (e:Entity) WHERE e.name = $name …`-equivalent two-step (MATCH first,
  CREATE on miss) inside the same transaction. Must be verified by a unit
  test before the store path ships.
- **R5 — `size(rs)` / `rs[-1]` in RETURN.** If list built-ins misbehave with
  rel lists, compute depth/via in Python from the returned rel objects.
- **R6 — Archived upstream.** kuzudb/kuzu is archived; 0.11.3 is final.
  Mitigation: core-engine-only usage (no extensions), pinned
  `>=0.11.3,<0.12`, and the wheel set for our platforms is already published.
  A future migration (e.g. LadybugDB) is out of scope but the connector-is-a-
  single-file design keeps that door open.
- **R7 — Scaffold collision.** This doc and `docs/recon.md` live in the
  project root the scaffold task is populating. The scaffold must copy the
  upstream tree in **without wiping `docs/`** (flagged on the board).
- **Open question (non-blocking):** should `qdrant-graph-find` support
  multiple seed entities? Deferred — `qdrant-graph-expand-from-hits` already
  covers the multi-seed case via query harvesting.

---

## 10. Acceptance criteria (for this design and its implementation)

This document satisfies the task's acceptance criterion via the per-tool
table below and §6.1–6.3 ("complements" row = (a); "Cypher" row = (b)):

| Tool / param | (a) Complements upstream Qdrant tool | (b) Cypher pattern executed |
|---|---|---|
| `qdrant-graph-find` | `qdrant-find` (structural relations vs semantic similarity) | Seeded var-length expansion `MATCH (a:Entity {name:$seed})-[rs:RELATES*1..d]-(b:Entity) WHERE ALL(r IN rs WHERE r.kind=$kind) RETURN …` + `MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {name:$n})` back-refs (§5.4) |
| `qdrant-graph-expand-from-hits` | `qdrant-find` (same query entry point, adds graph layer in one call) | Qdrant `query_points` → per-seeded-entity: same expansion + back-refs as `qdrant-graph-find` (§5.4) |
| `qdrant-store` `entities`/`relations` params | `qdrant-store` (extends the write path that feeds the graph) | `MERGE (e:Entity {name:$name}) …` / `MERGE (c:Chunk {chunk_id:$cid}) …` / `MERGE (c)-[:MENTIONS]->(e)` / `MERGE (s)-[r:RELATES {kind:$kind}]->(o)` with `ON CREATE`/`ON MATCH` (§5.3) |

Implementation acceptance (binds task `t_f2f6cae6`; mirrors recon §5.4):

1. Without the `[graph]` extra, `tools/list` returns exactly `qdrant-find`
   and `qdrant-store` with upstream-identical schemas.
2. With the extra and a writable `KUZU_DB_PATH`, `tools/list` additionally
   returns `qdrant-graph-find` and `qdrant-graph-expand-from-hits`, and
   `qdrant-store` exposes `entities`/`relations`.
3. Restarting the server against the same `KUZU_DB_PATH` preserves entities
   stored in a prior run (proves D3 persistence).
4. `KUZU_READ_ONLY=true` hides the graph write path; graph reads still work
   against a pre-existing DB.
5. A corrupted/locked Kuzu path degrades per D7 (vector tools still listed).
6. Every §8 rule has a test demonstrating rejection of invalid input.
7. R1/R2/R4 verified by targeted unit tests; fallbacks applied where needed
   and noted in the task summary.

---

## 11. Citations (live sources consulted for this design, 2026-09-25)

- Kuzu repo (archival notice, features, MIT): https://github.com/kuzudb/kuzu
- kuzu on PyPI (0.11.3, wheel matrix via JSON API): https://pypi.org/project/kuzu/ and https://pypi.org/pypi/kuzu/0.11.3/json
- Kuzu Python API docs (Database/Connection/AsyncConnection/QueryResult): https://kuzudb.github.io/api-docs/python
- Kuzu Python client guide (sync + async examples): https://kuzudb.github.io/docs/client-apis/python
- QueryResult source (iteration, get_column_names, as_dict): https://github.com/kuzudb/kuzu/blob/master/tools/python_api/src_py/query_result.py
- Connection source (execute + parameters, multi-statement): https://github.com/kuzudb/kuzu/blob/master/tools/python_api/src_py/connection.py
- Transactions (autocommit, single-writer, BEGIN/COMMIT/ROLLBACK, CHECKPOINT): https://kuzudb.github.io/docs/cypher/transaction
- DDL (CREATE NODE/REL TABLE, IF NOT EXISTS, PRIMARY KEY): https://kuzudb.github.io/docs/cypher/data-definition/create-table
- MERGE / ON CREATE SET / ON MATCH SET: https://kuzudb.github.io/docs/cypher/data-manipulation-clauses (mirror with worked examples: http://kuzu-docs.mew.im/import/merge)
- Variable-length paths require a named rel type (Kuzu rejects untyped `[*1..3]`): cross-engine migration notes at https://github.com/hivellm/nexus/blob/main/docs/migration/FROM_KUZU.md
- Persistence modes (on-disk WAL/checkpoint vs `:memory:`): https://kuzudb.github.io/docs/get-started/index
- Upstream recon for this project (tool surface, extension points, §5 decision): `docs/recon.md` in this repo
