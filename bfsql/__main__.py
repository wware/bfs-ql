"""CLI entry point: bfs-ql serve --backend (postgres|sparql)"""

import argparse
import logging

from dotenv import load_dotenv

load_dotenv()


def _parse_prefix(value: str) -> tuple[str, str]:
    """Parse a KEY=VALUE prefix argument into a (name, uri_base) tuple.

    Example: 'DBpedia=http://dbpedia.org/resource/' -> ('DBpedia', 'http://dbpedia.org/resource/')
    """
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"--prefix must be in KEY=VALUE form, got: {value!r}"
        )
    key, uri_base = value.split("=", 1)
    return key, uri_base


def main():
    parser = argparse.ArgumentParser(
        prog="bfs-ql",
        description="BFS-QL MCP server for knowledge graph traversal.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the MCP server.")
    serve.add_argument(
        "--log-level",
        dest="log_level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING). Use DEBUG to see all SPARQL requests.",
    )
    serve.add_argument(
        "--backend",
        choices=["postgres", "sparql"],
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

    # SPARQL-specific arguments
    serve.add_argument(
        "--endpoint",
        default=None,
        help=(
            "SPARQL endpoint URL (required when --backend sparql). "
            "Defaults to SPARQL_ENDPOINT_URL environment variable."
        ),
    )
    serve.add_argument(
        "--prefix",
        dest="prefixes",
        action="append",
        type=_parse_prefix,
        default=[],
        metavar="KEY=VALUE",
        help=(
            "URI prefix mapping for ID compression, e.g. "
            "'DBpedia=http://dbpedia.org/resource/'. "
            "Repeatable; each --prefix adds one mapping."
        ),
    )
    serve.add_argument(
        "--max-concurrent",
        dest="max_concurrent",
        type=int,
        default=5,
        help=(
            "Maximum concurrent SPARQL requests to the endpoint (default: 5). "
            "Lower this if the endpoint rate-limits (HTTP 429)."
        ),
    )
    serve.add_argument(
        "--bif-contains",
        dest="use_bif_contains",
        action="store_true",
        default=False,
        help=(
            "Use Virtuoso bif:contains for search_entities() instead of portable "
            "CONTAINS(LCASE(...)). Required for DBpedia and other large Virtuoso "
            "endpoints where a full-table scan would time out."
        ),
    )
    serve.add_argument(
        "--restrict-to-prefixes",
        dest="restrict_to_prefixes",
        action="store_true",
        default=False,
        help=(
            "Restrict BFS edges to objects/subjects within the declared prefix "
            "namespaces. Prevents fan-out into external namespaces (e.g. "
            "en.wikipedia.org) that inflate the BFS frontier on DBpedia."
        ),
    )
    serve.add_argument(
        "--node-batch-size",
        dest="node_batch_size",
        type=int,
        default=10,
        help=(
            "Number of entities to fetch types for in a single VALUES query (default: 10). "
            "Increase to reduce SPARQL round-trips; decrease if queries time out."
        ),
    )
    serve.add_argument(
        "--request-delay",
        dest="request_delay",
        type=float,
        default=0.0,
        help=(
            "Seconds to sleep before each SPARQL request (default: 0.0). "
            "Use with --max-concurrent 1 to avoid rate limiting on public endpoints."
        ),
    )
    serve.add_argument(
        "--no-safe-distinct",
        dest="safe_distinct",
        action="store_false",
        default=True,
        help=(
            "Disable SELECT DISTINCT scans for entity_types() and predicates(). "
            "Use on endpoints where even sampled scans time out."
        ),
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on for SSE transport (default: 8000).",
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind for SSE transport (default: 127.0.0.1).",
    )

    args = parser.parse_args()

    if args.command == "serve":
        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("docket").setLevel(logging.WARNING)

        from bfsql.server import create_server

        if args.backend == "postgres":
            from bfsql.backends.postgres import PostgresBackend
            factory = PostgresBackend.create

        elif args.backend == "sparql":
            from bfsql.backends.sparql import SparqlBackend
            prefixes = dict(args.prefixes)
            safe_distinct = args.safe_distinct
            use_bif_contains = args.use_bif_contains
            max_concurrent = args.max_concurrent
            restrict_to_prefixes = args.restrict_to_prefixes
            request_delay = args.request_delay
            node_batch_size = args.node_batch_size

            async def factory():
                return await SparqlBackend.create(
                    endpoint=args.endpoint,
                    prefixes=prefixes,
                    safe_distinct=safe_distinct,
                    use_bif_contains=use_bif_contains,
                    max_concurrent=max_concurrent,
                    restrict_to_prefixes=restrict_to_prefixes,
                    request_delay=request_delay,
                    node_batch_size=node_batch_size,
                )

        else:
            raise ValueError(f"Unknown backend: {args.backend}")

        mcp = create_server(factory, graph_description=args.description)
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
