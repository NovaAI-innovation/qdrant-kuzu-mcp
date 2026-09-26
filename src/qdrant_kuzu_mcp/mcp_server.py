import json
import logging
import sys
from typing import Annotated, Any, Optional

from fastmcp import Context, FastMCP
from pydantic import Field
from qdrant_client import models

from qdrant_kuzu_mcp.common.filters import make_indexes
from qdrant_kuzu_mcp.common.func_tools import make_partial_function
from qdrant_kuzu_mcp.common.wrap_filters import wrap_filters
from qdrant_kuzu_mcp.embeddings.base import EmbeddingProvider
from qdrant_kuzu_mcp.embeddings.factory import create_embedding_provider
from qdrant_kuzu_mcp.kuzu_connector import (
    MAX_SEEDS,
    KuzuConnector,
    harvest_entities,
    validate_direction,
    validate_entities,
    validate_int,
    validate_kind,
    validate_nonempty_str,
    validate_relations,
)
from qdrant_kuzu_mcp.qdrant import ArbitraryFilter, Entry, Metadata, QdrantConnector
from qdrant_kuzu_mcp.settings import (
    EmbeddingProviderSettings,
    KuzuSettings,
    QdrantSettings,
    ToolSettings,
)

logger = logging.getLogger(__name__)


# FastMCP is an alternative interface for declaring the capabilities
# of the server. Its API is based on FastAPI.
class QdrantMCPServer(FastMCP):
    """
    A MCP server for Qdrant.
    """

    def __init__(
        self,
        tool_settings: ToolSettings,
        qdrant_settings: QdrantSettings,
        embedding_provider_settings: Optional[EmbeddingProviderSettings] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        kuzu_settings: Optional[KuzuSettings] = None,
        name: str = "qdrant-kuzu-mcp",
        instructions: str | None = None,
        **settings: Any,
    ):
        self.tool_settings = tool_settings
        self.qdrant_settings = qdrant_settings

        if embedding_provider_settings and embedding_provider:
            raise ValueError(
                "Cannot provide both embedding_provider_settings and embedding_provider"
            )

        if not embedding_provider_settings and not embedding_provider:
            raise ValueError(
                "Must provide either embedding_provider_settings or embedding_provider"
            )

        self.embedding_provider_settings: Optional[EmbeddingProviderSettings] = None
        self.embedding_provider: Optional[EmbeddingProvider] = None

        if embedding_provider_settings:
            self.embedding_provider_settings = embedding_provider_settings
            self.embedding_provider = create_embedding_provider(
                embedding_provider_settings
            )
        else:
            self.embedding_provider_settings = None
            self.embedding_provider = embedding_provider

        assert self.embedding_provider is not None, "Embedding provider is required"

        self.qdrant_connector = QdrantConnector(
            qdrant_settings.location,
            qdrant_settings.api_key,
            qdrant_settings.collection_name,
            self.embedding_provider,
            qdrant_settings.local_path,
            make_indexes(qdrant_settings.filterable_fields_dict()),
        )

        # Graph layer (design D7): optional, degrade to vector-only on any
        # startup failure. Never break the upstream Qdrant tools.
        self.kuzu_settings = kuzu_settings
        self.kuzu_connector: Optional[KuzuConnector] = None
        if kuzu_settings is not None and kuzu_settings.enabled:
            try:
                self.kuzu_connector = KuzuConnector(kuzu_settings)
            except Exception as exc:
                print(f"qdrant-kuzu-mcp: graph layer disabled ({exc})", file=sys.stderr)
                logger.warning("Kuzu graph layer disabled: %s", exc)
                self.kuzu_connector = None

        super().__init__(name=name, instructions=instructions, **settings)

        self.setup_tools()

    def format_entry(self, entry: Entry) -> str:
        """
        Feel free to override this method in your subclass to customize the format of the entry.
        """
        entry_metadata = json.dumps(entry.metadata) if entry.metadata else ""
        return f"<entry><content>{entry.content}</content><metadata>{entry_metadata}</metadata></entry>"

    def setup_tools(self):
        """
        Register the tools in the server.
        """

        async def store(
            ctx: Context,
            information: Annotated[str, Field(description="Text to store")],
            collection_name: Annotated[
                str, Field(description="The collection to store the information in")
            ],
            # The `metadata` parameter is defined as non-optional, but it can be None.
            # If we set it to be optional, some of the MCP clients, like Cursor, cannot
            # handle the optional parameter correctly.
            metadata: Annotated[
                Metadata | None,
                Field(
                    description="Extra metadata stored along with memorised information. Any json is accepted."
                ),
            ] = None,
            entities: Annotated[
                list[str] | None,
                Field(
                    description=(
                        "Optional entity names mentioned by the information. "
                        "When provided, they are added to the Kuzu entity graph "
                        "and linked to this memory."
                    )
                ),
            ] = None,
            relations: Annotated[
                list[dict[str, str]] | None,
                Field(
                    description=(
                        "Optional relations between entities, each an object with "
                        "'subject', 'predicate' (snake_case, e.g. depends_on), and "
                        "'object'. Entities mentioned here are created implicitly."
                    )
                ),
            ] = None,
        ) -> str:
            """
            Store some information in Qdrant.
            :param ctx: The context for the request.
            :param information: The information to store.
            :param metadata: JSON metadata to store with the information, optional.
            :param entities: Entity names to link to this memory in the entity graph, optional.
            :param relations: Entity relations to record in the entity graph, optional.
            :param collection_name: The name of the collection to store the information in, optional. If not provided,
                                    the default collection is used.
            :return: A message indicating that the information was stored.
            """
            await ctx.debug(f"Storing information {information} in Qdrant")

            # Validate graph inputs BEFORE the Qdrant write so a caller error
            # surfaces as a tool error rather than a half-written memory.
            clean_entities: list[str] = []
            clean_relations: list[dict[str, str]] = []
            if self.kuzu_connector is not None and (entities or relations):
                clean_entities = validate_entities(entities or [])
                clean_relations = validate_relations(relations or [])

            entry = Entry(content=information, metadata=metadata)

            point_id = await self.qdrant_connector.store(
                entry, collection_name=collection_name
            )
            if collection_name:
                confirmation = f"Remembered: {information} in collection {collection_name}"
            else:
                confirmation = f"Remembered: {information}"

            if self.kuzu_connector is not None and (clean_entities or clean_relations):
                try:
                    await self.kuzu_connector.store_memory(
                        point_id=point_id,
                        collection=collection_name,
                        information=information,
                        entities=clean_entities,
                        relations=clean_relations,
                    )
                except Exception as exc:
                    # Qdrant is the source of truth; a graph sync failure must
                    # not fail an already-successful vector write (design 6.3).
                    logger.warning("Kuzu graph sync skipped: %s", exc)
                    confirmation += f" (graph sync skipped: {exc})"

            return confirmation

        async def find(
            ctx: Context,
            query: Annotated[str, Field(description="What to search for")],
            collection_name: Annotated[
                str, Field(description="The collection to search in")
            ],
            query_filter: ArbitraryFilter | None = None,
        ) -> list[str] | None:
            """
            Find memories in Qdrant.
            :param ctx: The context for the request.
            :param query: The query to use for the search.
            :param collection_name: The name of the collection to search in, optional. If not provided,
                                    the default collection is used.
            :param query_filter: The filter to apply to the query.
            :return: A list of entries found or None.
            """

            # Log query_filter
            await ctx.debug(f"Query filter: {query_filter}")

            query_filter = models.Filter(**query_filter) if query_filter else None

            await ctx.debug(f"Finding results for query {query}")

            entries = await self.qdrant_connector.search(
                query,
                collection_name=collection_name,
                limit=self.qdrant_settings.search_limit,
                query_filter=query_filter,
            )
            if not entries:
                return None
            content = [
                f"Results for the query '{query}'",
            ]
            for entry in entries:
                content.append(self.format_entry(entry))
            return content

        async def graph_find(
            ctx: Context,
            entity: Annotated[
                str, Field(description="Seed entity name to expand the graph from")
            ],
            relation: Annotated[
                str | None,
                Field(
                    description=(
                        "Restrict traversal to one relation kind "
                        "(snake_case, e.g. depends_on). Optional."
                    )
                ),
            ] = None,
            depth: Annotated[
                int, Field(description="Maximum hops from the seed (1-3)", ge=1, le=3)
            ] = 1,
            direction: Annotated[
                str,
                Field(description="Traversal direction: 'any', 'out', or 'in'"),
            ] = "any",
            limit: Annotated[
                int, Field(description="Maximum entities returned (1-100)", ge=1, le=100)
            ] = 20,
        ) -> dict:
            """
            Traverse the entity graph derived from stored memories.
            :param ctx: The context for the request.
            :param entity: Seed entity name.
            :param relation: Optional relation kind filter.
            :param depth: Maximum hops (1-3).
            :param direction: 'any', 'out', or 'in'.
            :param limit: Maximum entities returned (1-100).
            :return: Structured envelope with seed info, related entities, and
                     mentioning chunks (see docs/kuzu_tool_design.md section 7.1).
            """
            await ctx.debug(f"Expanding entity graph from {entity!r}")

            if self.kuzu_connector is None:  # pragma: no cover - guarded at registration
                raise RuntimeError("Graph layer is not available")

            seed = validate_nonempty_str(entity, "entity").strip()
            kind = validate_kind(relation, "relation") if relation else None
            hops = validate_int(depth, 1, 3, "depth")
            dir_ = validate_direction(direction)
            max_results = validate_int(limit, 1, 100, "limit")

            return await self.kuzu_connector.expand(
                seed, relation=kind, depth=hops, direction=dir_, limit=max_results
            )

        async def graph_expand_from_hits(
            ctx: Context,
            query: Annotated[str, Field(description="What to search for")],
            collection_name: Annotated[
                str, Field(description="The collection to search in")
            ],
            relation: Annotated[
                str | None,
                Field(
                    description=(
                        "Restrict traversal to one relation kind "
                        "(snake_case, e.g. depends_on). Optional."
                    )
                ),
            ] = None,
            depth: Annotated[
                int, Field(description="Maximum hops from each seed (1-3)", ge=1, le=3)
            ] = 1,
            limit: Annotated[
                int,
                Field(
                    description="Maximum entities returned per seed (1-100)", ge=1, le=100
                ),
            ] = 20,
            search_limit: Annotated[
                int,
                Field(
                    description="How many Qdrant hits to harvest entities from (1-10)",
                    ge=1,
                    le=10,
                ),
            ] = 5,
        ) -> dict:
            """
            Search memories in Qdrant and expand the entity graph from the
            entities those memories mention.
            :param ctx: The context for the request.
            :param query: The query to use for the vector search.
            :param collection_name: The collection to search in.
            :param relation: Optional relation kind filter.
            :param depth: Maximum hops per seed (1-3).
            :param limit: Maximum entities per seed (1-100).
            :param search_limit: How many Qdrant hits to harvest entities from (1-10).
            :return: Structured envelope with per-seed graph expansions
                     (see docs/kuzu_tool_design.md section 7.2).
            """
            await ctx.debug(f"Graph-expanding hits for query {query!r}")

            if self.kuzu_connector is None:  # pragma: no cover - guarded at registration
                raise RuntimeError("Graph layer is not available")

            clean_query = validate_nonempty_str(query, "query")
            clean_collection = validate_nonempty_str(
                collection_name, "collection_name"
            )
            kind = validate_kind(relation, "relation") if relation else None
            hops = validate_int(depth, 1, 3, "depth")
            max_results = validate_int(limit, 1, 100, "limit")
            max_hits = validate_int(search_limit, 1, 10, "search_limit")

            # Step 1: vector search (failure here raises — Qdrant is the
            # primary dependency of this tool, design 6.2).
            entries = await self.qdrant_connector.search(
                clean_query,
                collection_name=clean_collection,
                limit=max_hits,
            )

            # Step 2: harvest explicit entity metadata (no NER in v1).
            seed_names: list[str] = []
            seen: set[str] = set()
            for entry in entries:
                for name in harvest_entities(entry.metadata):
                    if name not in seen:
                        seen.add(name)
                        seed_names.append(name)

            truncated = len(seed_names) > MAX_SEEDS
            seed_names = seed_names[:MAX_SEEDS]

            seed_blocks: list[dict] = []
            for name in seed_names:
                block = await self.kuzu_connector.expand(
                    name,
                    relation=kind,
                    depth=hops,
                    direction="any",
                    limit=max_results,
                )
                seed_blocks.append(
                    {
                        "seed": block["seed"],
                        "entities": block["entities"],
                        "count": block["count"],
                        "truncated": block["truncated"],
                    }
                )
                truncated = truncated or block["truncated"]

            return {
                "query": clean_query,
                "collection": clean_collection,
                "hits_used": len(entries),
                "seeds": seed_blocks,
                "truncated": truncated,
            }

        find_foo = find
        store_foo = store

        filterable_conditions = (
            self.qdrant_settings.filterable_fields_dict_with_conditions()
        )

        if len(filterable_conditions) > 0:
            find_foo = wrap_filters(find_foo, filterable_conditions)
        elif not self.qdrant_settings.allow_arbitrary_filter:
            find_foo = make_partial_function(find_foo, {"query_filter": None})

        if self.qdrant_settings.collection_name:
            find_foo = make_partial_function(
                find_foo, {"collection_name": self.qdrant_settings.collection_name}
            )
            store_foo = make_partial_function(
                store_foo, {"collection_name": self.qdrant_settings.collection_name}
            )
            graph_expand_foo = make_partial_function(
                graph_expand_from_hits,
                {"collection_name": self.qdrant_settings.collection_name},
            )
        else:
            graph_expand_foo = graph_expand_from_hits

        if not (
            self.kuzu_connector is not None and not self.kuzu_settings.read_only
        ):
            # Graph off (not installed, disabled, failed to open, or Kuzu
            # read-only): hide the graph parameters entirely so the
            # registered qdrant-store signature matches upstream.
            store_foo = make_partial_function(
                store_foo, {"entities": None, "relations": None}
            )

        self.tool(
            find_foo,
            name="qdrant-find",
            description=self.tool_settings.tool_find_description,
        )

        if not self.qdrant_settings.read_only:
            # Those methods can modify the database
            self.tool(
                store_foo,
                name="qdrant-store",
                description=self.tool_settings.tool_store_description,
            )

        if self.kuzu_connector is not None:
            self.tool(
                graph_find,
                name="qdrant-graph-find",
                description=self.tool_settings.tool_graph_find_description,
            )
            self.tool(
                graph_expand_foo,
                name="qdrant-graph-expand-from-hits",
                description=self.tool_settings.tool_graph_expand_description,
            )
