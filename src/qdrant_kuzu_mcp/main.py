import argparse


def main():
    """
    Main entry point for the qdrant-kuzu-mcp script defined
    in pyproject.toml. It runs the MCP server with a specific transport
    protocol.
    """

    # Parse the command-line arguments to determine the transport protocol.
    parser = argparse.ArgumentParser(description="qdrant-kuzu-mcp")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
    )
    args = parser.parse_args()

    # Import is done here to make sure environment variables are loaded
    # only after we make the changes.
    from qdrant_kuzu_mcp.server import mcp

    mcp.run(transport=args.transport)
