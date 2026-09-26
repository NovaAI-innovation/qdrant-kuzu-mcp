"""Kuzu graph connector for the qdrant-kuzu-mcp server.

Implements the tool surface designed in ``docs/kuzu_tool_design.md``:

- ``Entity`` / ``Chunk`` node tables and ``MENTIONS`` / ``RELATES`` rel tables
  (design D4), created idempotently at startup unless read-only.
- One ``AsyncConnection`` for concurrent reads; all writes serialized through
  an ``asyncio.Lock`` and executed as one explicit BEGIN/COMMIT transaction
  on a single pooled connection (design D6).
- Every string value reaches Cypher via a bound ``$parameter``; the only
  interpolated values are range-checked integers (hop bounds and LIMIT).
- If the Kuzu package is missing, disabled, or the database cannot be opened,
  the connector is simply not constructed: the upstream Qdrant tools keep
  working unchanged (design D7).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from qdrant_kuzu_mcp.settings import KuzuSettings

try:  # The [graph] extra is optional; base installs must keep working.
    import kuzu
except ImportError:  # pragma: no cover - exercised only without the extra
    kuzu = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# --- Schema DDL (design section 5.1) ---------------------------------------

_SCHEMA_DDL = (
    """
    CREATE NODE TABLE IF NOT EXISTS Entity (
        name       STRING PRIMARY KEY,
        type       STRING,
        created_at STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Chunk (
        chunk_id        STRING PRIMARY KEY,
        point_id        STRING,
        collection      STRING,
        content_snippet STRING,
        created_at      STRING
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS MENTIONS (
        FROM Chunk TO Entity,
        first_seen STRING
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS RELATES (
        FROM Entity TO Entity,
        kind        STRING,
        weight      DOUBLE,
        created_at  STRING,
        last_seen   STRING
    )
    """,
)

# --- Read queries (design section 5.4) --------------------------------------
#
# The hop bound ``{max_hops}`` and ``LIMIT {limit}`` are the ONLY interpolated
# fragments; both are range-checked integers (see validate_int). Everything
# else is bound via $parameters.

_EXPAND_QUERY_TEMPLATE = """
MATCH (a:Entity {{name: $seed}}){pattern}(b:Entity)
{where_clause}
RETURN b.name AS name, b.type AS type, rs AS rs
ORDER BY name
LIMIT {limit}
"""

_EXPAND_PATTERNS = {
    "out": "-[rs:RELATES*{min}..{max}]->",
    "in": "<-[rs:RELATES*{min}..{max}]-",
}

_KIND_PREDICATE = "WHERE ALL(r IN rs WHERE r.kind = $kind)"

_SEED_QUERY = """
MATCH (e:Entity {name: $name})
RETURN e.name AS name, e.type AS type
"""

_BACKREF_QUERY = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity {{name: $name}})
RETURN c.point_id AS point_id, c.collection AS collection,
       c.content_snippet AS content_snippet
LIMIT {limit}
"""

# --- Write statements (design section 5.3) ----------------------------------

_MERGE_ENTITY = (
    "MERGE (e:Entity {name: $name}) "
    "ON CREATE SET e.type = $type, e.created_at = $ts"
)

_MERGE_CHUNK = (
    "MERGE (c:Chunk {chunk_id: $chunk_id}) "
    "ON CREATE SET c.point_id = $point_id, c.collection = $collection, "
    "c.content_snippet = $snippet, c.created_at = $ts"
)

_MERGE_MENTIONS = (
    "MATCH (c:Chunk {chunk_id: $chunk_id}), (e:Entity {name: $name}) "
    "MERGE (c)-[m:MENTIONS]->(e) "
    "ON CREATE SET m.first_seen = $ts"
)

_MERGE_RELATES = (
    "MERGE (s:Entity {name: $subject}) "
    "MERGE (o:Entity {name: $object}) "
    "MERGE (s)-[r:RELATES {kind: $kind}]->(o) "
    "ON CREATE SET r.weight = 1.0, r.created_at = $ts "
    "ON MATCH SET r.last_seen = $ts"
)

# --- Validation (design section 8) -------------------------------------------

_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

MAX_ENTITIES = 32
MAX_RELATIONS = 32
MAX_SEEDS = 16
SNIPPET_LEN = 200
MIN_NAME_LEN = 1
MAX_NAME_LEN = 256


class ValidationError(ValueError):
    """Raised when a tool argument violates a design-section-8 rule."""


def _reject_control_chars(value: str, label: str) -> None:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValidationError(f"{label} must not contain control characters")


def validate_entity_name(name: Any, label: str = "entity name") -> str:
    if not isinstance(name, str):
        raise ValidationError(f"{label} must be a string, got {type(name).__name__}")
    stripped = name.strip()
    if not (MIN_NAME_LEN <= len(stripped) <= MAX_NAME_LEN):
        raise ValidationError(
            f"{label} must be {MIN_NAME_LEN}-{MAX_NAME_LEN} chars after stripping"
        )
    _reject_control_chars(stripped, label)
    return stripped


def validate_kind(kind: Any, label: str = "relation kind") -> str:
    if not isinstance(kind, str):
        raise ValidationError(f"{label} must be a string, got {type(kind).__name__}")
    if not _KIND_RE.match(kind):
        raise ValidationError(
            f"{label} must match ^[a-z][a-z0-9_]{{0,63}}$: {kind!r}"
        )
    return kind


def validate_int(value: Any, low: int, high: int, label: str) -> int:
    # bool is an int subclass; the design says reject mismatches, not coerce.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label} must be an int, got {type(value).__name__}")
    if not (low <= value <= high):
        raise ValidationError(f"{label} must be between {low} and {high}, got {value}")
    return value


def validate_direction(value: Any) -> str:
    if value not in ("any", "out", "in"):
        raise ValidationError("direction must be one of 'any', 'out', 'in'")
    return value


def validate_entities(entities: Any) -> list[str]:
    if not isinstance(entities, list):
        raise ValidationError("entities must be a list of strings")
    if len(entities) > MAX_ENTITIES:
        raise ValidationError(f"entities accepts at most {MAX_ENTITIES} items")
    result: list[str] = []
    seen: set[str] = set()
    for item in entities:
        name = validate_entity_name(item, "entity name")
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def validate_relations(relations: Any) -> list[dict[str, str]]:
    if not isinstance(relations, list):
        raise ValidationError("relations must be a list of objects")
    if len(relations) > MAX_RELATIONS:
        raise ValidationError(f"relations accepts at most {MAX_RELATIONS} items")
    result: list[dict[str, str]] = []
    for item in relations:
        if not isinstance(item, dict):
            raise ValidationError("each relation must be an object")
        if set(item.keys()) != {"subject", "predicate", "object"}:
            raise ValidationError(
                "each relation must have exactly 'subject', 'predicate', 'object'"
            )
        subject = validate_entity_name(item["subject"], "relation subject")
        object_ = validate_entity_name(item["object"], "relation object")
        predicate = validate_kind(item["predicate"], "relation predicate")
        if subject == object_:
            raise ValidationError(
                f"self-loops are not supported (subject == object: {subject!r})"
            )
        result.append({"subject": subject, "predicate": predicate, "object": object_})
    return result


def validate_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value


def harvest_entities(metadata: Any) -> list[str]:
    """Design 6.2 harvest rule: metadata['entities'] if it is a list[str]."""
    if not isinstance(metadata, dict):
        return []
    entities = metadata.get("entities")
    if not isinstance(entities, list):
        return []
    return [e for e in entities if isinstance(e, str)]


# --- Connector ---------------------------------------------------------------


class KuzuConnector:
    """Owns the embedded Kuzu database and implements the graph tool queries."""

    def __init__(self, settings: KuzuSettings):
        if kuzu is None:
            raise RuntimeError("kuzu package is not installed")
        self.settings = settings
        db_path = settings.normalized_db_path
        db_kwargs: dict[str, Any] = {}
        if not settings.is_memory:
            db_kwargs["read_only"] = settings.read_only
            if settings.buffer_pool_size > 0:
                db_kwargs["buffer_pool_size"] = settings.buffer_pool_size
        self._database = kuzu.Database(db_path, **db_kwargs)
        self._async_conn = kuzu.AsyncConnection(
            self._database, max_concurrent_queries=settings.max_concurrent_queries
        )
        # Kuzu allows one active write transaction at a time; serialize writes.
        self._write_lock = asyncio.Lock()
        # Capability probes (design risks R2): resolved lazily on first use.
        self._all_predicate_supported: Optional[bool] = None
        if not settings.read_only:
            self._ensure_schema()

    # -- Schema ---------------------------------------------------------------

    def _ensure_schema(self) -> None:
        conn = self._async_conn.acquire_connection()
        try:
            for ddl in _SCHEMA_DDL:
                self._sync_execute(conn, ddl.strip(), None)
        finally:
            self._async_conn.release_connection(conn)

    def _sync_execute(self, conn: Any, query: str, parameters: dict[str, Any] | None):
        result = conn.execute(query, parameters)
        if isinstance(result, list):  # multi-statement strings
            for r in result:
                r.close()
            return None
        result.close()
        return None

    # -- Low-level helpers ----------------------------------------------------

    async def _execute(self, query: str, parameters: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Run one read query on the async pool and return rows as dicts."""
        result = await self._async_conn.execute(query, parameters)
        results = result if isinstance(result, list) else [result]
        rows: list[dict[str, Any]] = []
        try:
            for r in results:
                columns = r.get_column_names()
                while r.has_next():
                    row = r.get_next()
                    rows.append(
                        {col: value for col, value in zip(columns, row)}
                    )
        finally:
            for r in results:
                r.close()
        return rows

    @staticmethod
    def _table_missing(exc: Exception) -> bool:
        # Read paths must return empty envelopes, not raise, when the schema
        # tables are absent (read-only mode against a fresh path).
        message = str(exc).lower()
        return "does not exist" in message or "not found" in message

    async def _read(self, query: str, parameters: dict[str, Any] | None) -> list[dict[str, Any]]:
        try:
            return await self._execute(query, parameters)
        except Exception as exc:
            if self._table_missing(exc):
                return []
            raise

    # -- Reads (design 5.4 / 6.1) ----------------------------------------------

    async def get_seed(self, name: str) -> Optional[dict[str, Any]]:
        rows = await self._read(_SEED_QUERY, {"name": name})
        if not rows:
            return None
        return {"name": rows[0]["name"], "found": True, "type": rows[0]["type"]}

    async def _expand_pattern(
        self,
        seed: str,
        direction: str,
        depth: int,
        kind: Optional[str],
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        pattern = _EXPAND_PATTERNS[direction].format(min=1, max=depth)
        where_clause = ""
        params: dict[str, Any] = {"seed": seed}
        if kind:
            if self._all_predicate_supported is not False:
                where_clause = _KIND_PREDICATE
                params["kind"] = kind
        query = _EXPAND_QUERY_TEMPLATE.format(
            pattern=pattern, where_clause=where_clause, limit=limit
        )
        try:
            rows = await self._read(query, params)
        except Exception as exc:
            if kind and where_clause and self._all_predicate_supported is None:
                # R2 fallback: quantifier over rel lists unsupported ->
                # drop the predicate and filter in Python on the last edge.
                logger.warning(
                    "ALL() predicate over rel list rejected (%s); "
                    "falling back to Python-side kind filtering",
                    exc,
                )
                self._all_predicate_supported = False
                return await self._expand_pattern(seed, direction, depth, kind, limit)
            raise
        if kind and where_clause:
            self._all_predicate_supported = True
        # R5 fallback applied: Kuzu 0.11.3 rejects `rs[-1].kind` in RETURN,
        # so depth/via are computed in Python from the returned rel list.
        # Kuzu returns the var-length rel list as a path dict
        # {"_nodes": [...], "_rels": [...]}.
        entities = []
        for row in rows:
            raw = row["rs"]
            rels = raw["_rels"] if isinstance(raw, dict) else raw
            entities.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "depth": len(rels),
                    "via": rels[-1].get("kind") if rels else None,
                    "direction": direction,
                }
            )
        if kind and not where_clause:
            entities = [e for e in entities if e["via"] == kind]
        truncated = len(rows) == limit
        return entities, truncated

    async def expand(
        self,
        seed: str,
        *,
        relation: Optional[str] = None,
        depth: int = 1,
        direction: str = "any",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Implement ``qdrant-graph-find`` (design 6.1). All args pre-validated."""
        seed_info = await self.get_seed(seed)
        # "any" is served by unioning the two directed queries (design R1
        # fallback applied unconditionally: it also yields the true direction
        # label for every row, which an undirected pattern cannot).
        directions = ["out", "in"] if direction == "any" else [direction]
        collected: dict[str, dict[str, Any]] = {}
        truncated = False
        for d in directions:
            entities, trunc = await self._expand_pattern(
                seed, d, depth, relation, limit
            )
            truncated = truncated or trunc
            for entity in entities:
                existing = collected.get(entity["name"])
                if existing is None or entity["depth"] < existing["depth"]:
                    collected[entity["name"]] = entity
        ordered = sorted(collected.values(), key=lambda e: (e["depth"], e["name"]))
        ordered = ordered[:limit]
        if len(collected.values()) > limit:
            truncated = True

        chunks: list[dict[str, Any]] = []
        for entity in ordered:
            backrefs = await self._read(
                _BACKREF_QUERY.format(limit=1), {"name": entity["name"]}
            )
            if backrefs:
                row = backrefs[0]
                chunks.append(
                    {
                        "entity": entity["name"],
                        "point_id": row["point_id"],
                        "collection": row["collection"],
                        "snippet": row["content_snippet"],
                    }
                )

        return {
            "seed": seed_info or {"name": seed, "found": False, "type": None},
            "depth_searched": depth,
            "entities": ordered,
            "chunks": chunks,
            "count": len(ordered),
            "truncated": truncated,
        }

    # -- Writes (design 5.3 / 6.3) ----------------------------------------------

    @staticmethod
    def _build_write_statements(
        *,
        point_id: str,
        collection: str,
        information: str,
        entities: Optional[list[str]] = None,
        relations: Optional[list[dict[str, str]]] = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        ts = datetime.now(timezone.utc).isoformat()
        chunk_id = f"{collection}:{point_id}"
        snippet = information[:SNIPPET_LEN]
        statements: list[tuple[str, dict[str, Any]]] = []
        names: list[str] = list(entities or [])

        for rel in relations or []:
            statements.append(
                (
                    _MERGE_ENTITY,
                    {"name": rel["subject"], "type": None, "ts": ts},
                )
            )
            statements.append(
                (
                    _MERGE_ENTITY,
                    {"name": rel["object"], "type": None, "ts": ts},
                )
            )
            statements.append(
                (
                    _MERGE_RELATES,
                    {
                        "subject": rel["subject"],
                        "kind": rel["predicate"],
                        "object": rel["object"],
                        "ts": ts,
                    },
                )
            )

        statements.append(
            (
                _MERGE_CHUNK,
                {
                    "chunk_id": chunk_id,
                    "point_id": point_id,
                    "collection": collection,
                    "snippet": snippet,
                    "ts": ts,
                },
            )
        )

        for name in names:
            statements.append(
                (_MERGE_MENTIONS, {"chunk_id": chunk_id, "name": name, "ts": ts})
            )

        return statements

    def _run_transaction(self, conn: Any, statements: list[tuple[str, dict[str, Any]]]) -> None:
        self._sync_execute(conn, "BEGIN TRANSACTION", None)
        try:
            for query, parameters in statements:
                self._sync_execute(conn, query, parameters)
            self._sync_execute(conn, "COMMIT", None)
        except Exception:
            try:
                self._sync_execute(conn, "ROLLBACK", None)
            except Exception:  # pragma: no cover - best-effort rollback
                logger.exception("rollback after failed graph transaction failed")
            raise

    async def store_memory(
        self,
        *,
        point_id: str,
        collection: str,
        information: str,
        entities: Optional[list[str]] = None,
        relations: Optional[list[dict[str, str]]] = None,
    ) -> None:
        """Persist a stored Qdrant memory into the graph (design 5.3)."""
        statements = self._build_write_statements(
            point_id=point_id,
            collection=collection,
            information=information,
            entities=entities,
            relations=relations,
        )
        async with self._write_lock:
            conn = self._async_conn.acquire_connection()
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, self._run_transaction, conn, statements
                )
            finally:
                self._async_conn.release_connection(conn)

    # -- Lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        try:
            self._async_conn.close()
        except Exception:  # pragma: no cover
            logger.exception("closing kuzu async connection failed")
        try:
            # Releasing the file lock lets another process open the same
            # KUZU_DB_PATH (e.g. after a restart of this server).
            self._database.close()
        except Exception:  # pragma: no cover
            logger.exception("closing kuzu database failed")
