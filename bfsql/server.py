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


def create_server(backend_or_factory, graph_description: str = "") -> FastMCP:
    """Build and return a FastMCP server wired to the given backend.

    Wraps the backend in CachedGraphDb, fetches entity types and predicates
    at startup, and injects them into the bfs_query tool description when
    the schema is small enough.

    Args:
        backend_or_factory: A GraphDbInterface instance, or an async callable
            that returns one (created inside the server's event loop).
        graph_description: Human-readable description of the graph, included
            in describe_schema() responses.
    """
    # Detect whether we got a factory (async callable) or a live backend instance.
    _is_factory = callable(backend_or_factory) and not hasattr(backend_or_factory, "search_entities")

    # These are populated during lifespan startup.
    _state: dict[str, Any] = {
        "entity_types": [],
        "predicates": [],
        "graph_description": graph_description,
        # For backend instances (tests), wire up immediately; factories wait for lifespan.
        "db": None if _is_factory else CachedGraphDb(backend_or_factory),
    }

    @asynccontextmanager
    async def lifespan(app):
        if _is_factory:
            backend = await backend_or_factory()
            _state["db"] = CachedGraphDb(backend)
        else:
            backend = backend_or_factory
        db = _state["db"]
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

    def _db() -> CachedGraphDb:
        return _state["db"]

    # ------------------------------------------------------------------
    # Tool: describe_schema
    # ------------------------------------------------------------------

    @mcp.tool(description="Return the entity types, predicate vocabulary, and "
              "description of this graph. Call this first against an unfamiliar graph. "
              "The returned entity_types list is the complete set of valid node_types "
              "values for bfs_query. Entity counts per type are not exposed directly -- "
              "use bfs_query with a well-connected seed to explore coverage.")
    async def describe_schema() -> dict:
        """Return schema information for this graph."""
        return SchemaDescription(
            graph_description=_state["graph_description"],
            entity_types=await _db().entity_types(),
            predicates=await _db().predicates(),
        ).model_dump()

    # ------------------------------------------------------------------
    # Tool: search_entities
    # ------------------------------------------------------------------

    @mcp.tool(description="Search for entities by name or alias and return their "
              "canonical IDs. Searches the entity name field -- use a specific name "
              "like 'desmopressin' or 'Cushing disease', NOT an entity type like "
              "'drug' or 'paper'. Always call this before bfs_query when you have a "
              "name but not yet a canonical ID. Results may be ambiguous; inspect "
              "entity_type to pick the right one.")
    async def search_entities(query: str) -> list[dict]:
        """Find entities by name or alias.

        Args:
            query: A specific entity name or partial name (e.g. 'desmopressin',
                'Cushing disease'). Do NOT pass an entity type here.

        Returns:
            List of EntityStub records with id and entity_type.
        """
        results = await _db().search_entities(query)
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
        topology_only: bool = False,
    ) -> dict:
        """Traverse the graph breadth-first from one or more seed entities.

        Args:
            seeds: One or more canonical entity IDs to expand from.
            max_hops: Maximum graph distance from any seed (1-5).
            node_types: Entity types that receive full metadata. Others appear
                as stubs. Omit for full data on all nodes.
            predicates: Predicate names that receive full metadata. Others
                appear as stubs. Omit for full data on all edges.
            topology_only: If True, return only IDs and types for all nodes
                and edges -- no metadata at all. Use this first on large or
                unfamiliar graphs to see structure before fetching details.

        Returns:
            BfsResult with nodes and edges. Edge provenance (text spans) is
            omitted to keep size manageable -- use describe_entity() for full
            provenance on a specific node.
        """
        result: BfsResult = await _bfs_query(_db(), BfsQuery(
            seeds=seeds,
            max_hops=max_hops,
            node_types=node_types or [],
            predicates=predicates or [],
            topology_only=topology_only,
        ))
        return _slim_result(result, topology_only=topology_only)

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
        node = await _db().get_node(id)
        metadata = await _db().metadata_for_node(id)
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


_EDGE_META_STRIP = {"provenance", "strongest_evidence_quote", "evidence_confidence_avg", "created_at"}


def _slim_result(result: BfsResult, topology_only: bool = False) -> dict:
    """Serialize a BfsResult, stripping verbose fields to keep response size manageable.

    When topology_only=True, all metadata is removed and every node/edge is
    reduced to IDs and types only -- useful for an initial structural survey
    of a large or unfamiliar graph.

    Otherwise, verbose edge fields (provenance text, quotes, timestamps) are
    stripped while confidence and source_documents are kept. Full provenance
    is available via describe_entity().
    """
    data = result.model_dump()
    if topology_only:
        data["nodes"] = [
            {"id": n["id"], "entity_type": n["entity_type"]}
            for n in data.get("nodes", [])
        ]
        data["edges"] = [
            {"subject": e["subject"], "predicate": e["predicate"], "object": e["object"]}
            for e in data.get("edges", [])
        ]
    else:
        for edge in data.get("edges", []):
            meta = edge.get("metadata")
            if isinstance(meta, dict):
                for key in _EDGE_META_STRIP:
                    meta.pop(key, None)
    return data


def _server_instructions() -> str:
    return (
        "This MCP server exposes a knowledge graph via BFS-QL. "
        "Recommended workflow: "
        "1) Call describe_schema() to learn entity types, predicates, and graph description. "
        "2) Call search_entities(name) with a specific entity name (not a type) to resolve "
        "it to a canonical ID. Inspect entity_type in results to pick the right match. "
        "3) Call bfs_query(seeds, max_hops, node_types, predicates) to traverse the graph. "
        "Start with max_hops=1 and expand if needed. Use node_types and predicates to focus "
        "on the relevant parts of the graph -- non-matching nodes still appear as stubs so "
        "topology is always complete. "
        "4) Call describe_entity(id) on any stub node that warrants closer inspection. "
        "Important: search_entities searches entity names, not entity types. To find all "
        "entities of a given type, use bfs_query from a relevant seed with node_types filter. "
        "Entity IDs beginning with 'prov:' are provisional -- the pipeline that built this "
        "graph could not resolve them to a canonical authority. They are structurally present "
        "but carry no external meaning. Skip them or treat them as anonymous placeholders."
    )
