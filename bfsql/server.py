"""BFS-QL MCP server -- four tools over a GraphDbInterface backend."""

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from bfsql.cache import CachedGraphDb
from bfsql.engine import bfs_query as _bfs_query
from bfsql.models import BfsQuery, BfsResult, SchemaDescription


# Schema injection threshold: if the graph has more entity types or
# predicates than these limits, skip injection and rely on describe_schema().
_MAX_INJECT_TYPES = 20
_MAX_INJECT_PREDICATES = 30


def create_server(backend, graph_description: str = "") -> FastMCP:
    """Build and return a FastMCP server wired to the given backend.

    Wraps the backend in CachedGraphDb, fetches entity types and predicates
    at startup, and injects them into the bfs_query tool description when
    the schema is small enough.

    Args:
        backend: Any GraphDbInterface implementation.
        graph_description: Human-readable description of the graph, included
            in describe_schema() responses.
    """
    db = CachedGraphDb(backend)

    # These are populated during lifespan startup.
    _state: dict[str, Any] = {
        "entity_types": [],
        "predicates": [],
        "graph_description": graph_description,
    }

    @asynccontextmanager
    async def lifespan(app):
        entity_types = await db.entity_types()
        predicates = await db.predicates()
        _state["entity_types"] = entity_types
        _state["predicates"] = predicates
        yield
        if hasattr(backend, "close"):
            await backend.close()

    mcp = FastMCP(
        name="bfs-ql",
        instructions=_server_instructions(),
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Tool: describe_schema
    # ------------------------------------------------------------------

    @mcp.tool(description="Return the entity types, predicate vocabulary, and "
              "description of this graph. Call this first against an unfamiliar graph.")
    async def describe_schema() -> dict:
        """Return schema information for this graph."""
        return SchemaDescription(
            graph_description=_state["graph_description"],
            entity_types=await db.entity_types(),
            predicates=await db.predicates(),
        ).model_dump()

    # ------------------------------------------------------------------
    # Tool: search_entities
    # ------------------------------------------------------------------

    @mcp.tool(description="Resolve a natural-language name or alias to one or more "
              "canonical entity IDs. Always call this before bfs_query if you do not "
              "already have a canonical ID.")
    async def search_entities(query: str) -> list[dict]:
        """Find entities by name or alias.

        Args:
            query: Name, alias, or partial name to look up.

        Returns:
            List of EntityStub records with id and entity_type.
        """
        results = await db.search_entities(query)
        return [r.model_dump() for r in results]

    # ------------------------------------------------------------------
    # Tool: bfs_query
    # ------------------------------------------------------------------

    # Build the bfs_query description dynamically, injecting valid filter
    # values when the schema is small enough.
    bfs_description = _bfs_query_description(
        _state["entity_types"],
        _state["predicates"],
    )

    @mcp.tool(description=bfs_description)
    async def bfs_query(
        seeds: list[str],
        max_hops: int,
        node_types: list[str] | None = None,
        predicates: list[str] | None = None,
    ) -> dict:
        """Traverse the graph breadth-first from one or more seed entities.

        Args:
            seeds: One or more canonical entity IDs to expand from.
            max_hops: Maximum graph distance from any seed (1-5).
            node_types: Entity types that receive full metadata. Others appear
                as stubs. Omit for full data on all nodes.
            predicates: Predicate names that receive full metadata. Others
                appear as stubs. Omit for full data on all edges.

        Returns:
            BfsResult with nodes and edges.
        """
        result: BfsResult = await _bfs_query(db, BfsQuery(
            seeds=seeds,
            max_hops=max_hops,
            node_types=node_types or [],
            predicates=predicates or [],
        ))
        return result.model_dump()

    # ------------------------------------------------------------------
    # Tool: describe_entity
    # ------------------------------------------------------------------

    @mcp.tool(description="Retrieve full metadata for a single entity by canonical ID. "
              "Use this to expand a stub node returned by bfs_query.")
    async def describe_entity(id: str) -> dict:
        """Get full metadata for one entity.

        Args:
            id: Canonical entity ID.

        Returns:
            Full node metadata as a flat dict.
        """
        node = await db.get_node(id)
        metadata = await db.metadata_for_node(id)
        return {"id": node.id, "entity_type": node.entity_type, **metadata}

    return mcp


def _bfs_query_description(entity_types: list[str], predicates: list[str]) -> str:
    """Build the bfs_query tool description, injecting schema when small enough."""
    base = (
        "Perform a breadth-first traversal from one or more seed entities. "
        "Filters control detail level, not which nodes appear: non-matching "
        "nodes and edges appear as lightweight stubs preserving full topology."
    )
    if (entity_types and len(entity_types) <= _MAX_INJECT_TYPES and
            predicates and len(predicates) <= _MAX_INJECT_PREDICATES):
        types_str = ", ".join(entity_types)
        preds_str = ", ".join(predicates)
        return (
            f"{base} "
            f"Valid node_types: {types_str}. "
            f"Valid predicates: {preds_str}."
        )
    return base


def _server_instructions() -> str:
    return (
        "This MCP server exposes a knowledge graph via BFS-QL. "
        "Recommended workflow: "
        "1) Call describe_schema() to learn entity types and predicates. "
        "2) Call search_entities(name) to resolve names to canonical IDs -- "
        "inspect results carefully, common names are often ambiguous. "
        "3) Call bfs_query(seeds, max_hops, node_types, predicates) starting "
        "with max_hops=1 and expand if needed. "
        "4) Call describe_entity(id) on any stub node that warrants closer inspection."
    )
