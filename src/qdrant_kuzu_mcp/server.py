from qdrant_kuzu_mcp.mcp_server import QdrantMCPServer
from qdrant_kuzu_mcp.settings import (
    EmbeddingProviderSettings,
    KuzuSettings,
    QdrantSettings,
    ToolSettings,
)

mcp = QdrantMCPServer(
    tool_settings=ToolSettings(),
    qdrant_settings=QdrantSettings(),
    embedding_provider_settings=EmbeddingProviderSettings(),
    kuzu_settings=KuzuSettings(),
)
