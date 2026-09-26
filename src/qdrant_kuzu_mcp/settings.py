from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings

from qdrant_kuzu_mcp.embeddings.types import EmbeddingProviderType

DEFAULT_TOOL_STORE_DESCRIPTION = (
    "Keep the memory for later use, when you are asked to remember something."
)
DEFAULT_TOOL_FIND_DESCRIPTION = (
    "Look up memories in Qdrant. Use this tool when you need to: \n"
    " - Find memories by their content \n"
    " - Access memories for further analysis \n"
    " - Get some personal information about the user"
)

DEFAULT_TOOL_GRAPH_FIND_DESCRIPTION = (
    "Traverse the entity graph derived from stored memories. Use after qdrant-find "
    "when the question is about relationships between entities (dependencies, "
    "ownership, co-occurrence) rather than similar text. Provide a seed entity "
    "name; returns related entities with hop distance and the chunk that "
    "mentions them."
)
DEFAULT_TOOL_GRAPH_EXPAND_DESCRIPTION = (
    "Search memories in Qdrant and expand the entity graph from the entities "
    "those memories mention. One call replaces a qdrant-find followed by "
    "several qdrant-graph-find calls when you want everything related to a "
    "topic."
)

METADATA_PATH = "metadata"


class ToolSettings(BaseSettings):
    """
    Configuration for all the tools.
    """

    tool_store_description: str = Field(
        default=DEFAULT_TOOL_STORE_DESCRIPTION,
        validation_alias="TOOL_STORE_DESCRIPTION",
    )
    tool_find_description: str = Field(
        default=DEFAULT_TOOL_FIND_DESCRIPTION,
        validation_alias="TOOL_FIND_DESCRIPTION",
    )
    tool_graph_find_description: str = Field(
        default=DEFAULT_TOOL_GRAPH_FIND_DESCRIPTION,
        validation_alias="TOOL_GRAPH_FIND_DESCRIPTION",
    )
    tool_graph_expand_description: str = Field(
        default=DEFAULT_TOOL_GRAPH_EXPAND_DESCRIPTION,
        validation_alias="TOOL_GRAPH_EXPAND_DESCRIPTION",
    )


class KuzuSettings(BaseSettings):
    """
    Configuration for the Kuzu graph connector.

    ``enabled`` allows operators to force the graph layer off without
    uninstalling the ``[graph]`` extra.
    """

    enabled: bool = Field(default=True, validation_alias="KUZU_ENABLED")
    db_path: str = Field(default="./kuzu_db", validation_alias="KUZU_DB_PATH")
    read_only: bool = Field(default=False, validation_alias="KUZU_READ_ONLY")
    buffer_pool_size: int = Field(
        default=512 * 1024 * 1024,
        validation_alias="KUZU_BUFFER_POOL_SIZE",
    )
    max_concurrent_queries: int = Field(
        default=4,
        validation_alias="KUZU_MAX_CONCURRENT_QUERIES",
    )

    @property
    def is_memory(self) -> bool:
        """True when the DB path selects the in-memory (per-process) mode."""
        return self.db_path in {":memory:", ""}

    @property
    def normalized_db_path(self) -> str:
        # ``""`` also selects in-memory mode in Kuzu; normalize so the
        # connector can branch on one spelling.
        if self.is_memory:
            return ":memory:"
        return self.db_path


class EmbeddingProviderSettings(BaseSettings):
    """
    Configuration for the embedding provider.
    """

    provider_type: EmbeddingProviderType = Field(
        default=EmbeddingProviderType.FASTEMBED,
        validation_alias="EMBEDDING_PROVIDER",
    )
    model_name: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        validation_alias="EMBEDDING_MODEL",
    )


class FilterableField(BaseModel):
    name: str = Field(description="The name of the field payload field to filter on")
    description: str = Field(
        description="A description for the field used in the tool description"
    )
    field_type: Literal["keyword", "integer", "float", "boolean"] = Field(
        description="The type of the field"
    )
    condition: Literal["==", "!=", ">", ">=", "<", "<=", "any", "except"] | None = (
        Field(
            default=None,
            description=(
                "The condition to use for the filter. If not provided, the field will be indexed, but no "
                "filter argument will be exposed to MCP tool."
            ),
        )
    )
    required: bool = Field(
        default=False,
        description="Whether the field is required for the filter.",
    )


class QdrantSettings(BaseSettings):
    """
    Configuration for the Qdrant connector.
    """

    location: str | None = Field(default=None, validation_alias="QDRANT_URL")
    api_key: str | None = Field(default=None, validation_alias="QDRANT_API_KEY")
    collection_name: str | None = Field(
        default=None, validation_alias="COLLECTION_NAME"
    )
    local_path: str | None = Field(default=None, validation_alias="QDRANT_LOCAL_PATH")
    search_limit: int = Field(default=10, validation_alias="QDRANT_SEARCH_LIMIT")
    read_only: bool = Field(default=False, validation_alias="QDRANT_READ_ONLY")

    filterable_fields: list[FilterableField] | None = Field(default=None)

    allow_arbitrary_filter: bool = Field(
        default=False, validation_alias="QDRANT_ALLOW_ARBITRARY_FILTER"
    )

    def filterable_fields_dict(self) -> dict[str, FilterableField]:
        if self.filterable_fields is None:
            return {}
        return {field.name: field for field in self.filterable_fields}

    def filterable_fields_dict_with_conditions(self) -> dict[str, FilterableField]:
        if self.filterable_fields is None:
            return {}
        return {
            field.name: field
            for field in self.filterable_fields
            if field.condition is not None
        }

    @model_validator(mode="after")
    def check_local_path_conflict(self) -> "QdrantSettings":
        if self.local_path:
            if self.location is not None or self.api_key is not None:
                raise ValueError(
                    "If 'local_path' is set, 'location' and 'api_key' must be None."
                )
        return self
