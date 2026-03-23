"""CLI entry point: bfs-ql serve --backend postgres"""

import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        prog="bfs-ql",
        description="BFS-QL MCP server for knowledge graph traversal.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the MCP server.")
    serve.add_argument(
        "--backend",
        choices=["postgres"],
        default="postgres",
        help="Backend to connect to (default: postgres).",
    )
    serve.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio).",
    )
    serve.add_argument(
        "--description",
        default="",
        help="Human-readable description of this graph, included in describe_schema().",
    )

    args = parser.parse_args()

    if args.command == "serve":
        from bfsql.server import create_server

        if args.backend == "postgres":
            from bfsql.backends.postgres import PostgresBackend
            factory = PostgresBackend.create
        else:
            raise ValueError(f"Unknown backend: {args.backend}")

        mcp = create_server(factory, graph_description=args.description)
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
