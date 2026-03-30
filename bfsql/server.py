"""BFS-QL MCP server -- five tools over a GraphDbInterface backend."""

from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from bfsql.cache import CachedGraphDb
from bfsql.engine import bfs_query as _bfs_query
from bfsql.engine import neighborhood_intersection as _neighborhood_intersection
from bfsql.models import (
    BfsQuery,
    BfsResult,
    IntersectionQuery,
    SchemaSummary,
    SchemaDescription,
)

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
    _is_factory = callable(backend_or_factory) and not hasattr(
        backend_or_factory, "search_entities"
    )

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

    @mcp.tool(
        description="Return schema information for this graph. Always call this "
        "first. Follow the next_steps field for graph-specific workflow guidance. "
        "When comprehensive=False, entity_types and predicates are a sample only; "
        "use schema_summary in bfs_query results to discover the local vocabulary."
    )
    async def describe_schema() -> dict:
        """Return schema information for this graph."""
        db = _db()
        entity_types = await db.entity_types()
        predicates = await db.predicates()
        # Refresh _state if it was populated from an empty DB at startup.
        if entity_types:
            _state["entity_types"] = entity_types
        if predicates:
            _state["predicates"] = predicates
        return SchemaDescription(
            graph_description=_state["graph_description"],
            comprehensive=await db.comprehensive(),
            entity_types=entity_types,
            predicates=predicates,
            next_steps=await db.next_steps(),
        ).model_dump()

    # ------------------------------------------------------------------
    # Tool: search_entities
    # ------------------------------------------------------------------

    @mcp.tool(
        description="Search for entities by name or alias and return their "
        "canonical IDs. Searches the entity name field -- use a specific name "
        "like 'desmopressin' or 'Cushing disease', NOT an entity type like "
        "'drug' or 'paper'. Always call this before bfs_query when you have a "
        "name but not yet a canonical ID. Results may be ambiguous; inspect "
        "entity_type to pick the right one. Use node_types to restrict results "
        "to specific entity types (e.g. ['disease'] to avoid paper results)."
    )
    async def search_entities(
        query: str,
        node_types: list[str] | None = None,
    ) -> list[dict]:
        """Find entities by name or alias.

        Args:
            query: A specific entity name or partial name (e.g. 'desmopressin',
                'Cushing disease'). Do NOT pass an entity type here.
            node_types: Optional list of entity types to restrict results to
                (e.g. ['disease', 'gene']). Useful when common terms like
                'breast cancer' match both concept entities and papers.

        Returns:
            List of EntityStub records with id, entity_type, and name.
        """
        results = await _db().search_entities(query, node_types=node_types)
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
        exclude_node_types: list[str] | None = None,
        predicates: list[str] | None = None,
        min_mentions: int = 1,
        topology_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        """Traverse the graph breadth-first from one or more seed entities.

        Args:
            seeds: One or more canonical entity IDs to expand from.
            max_hops: Maximum graph distance from any seed (1-5).
            node_types: Entity types that receive full metadata. Others appear
                as stubs. Omit for full data on all nodes.
            exclude_node_types: Entity types to remove entirely from the result.
                Excluded nodes and all edges touching them are omitted. Use
                this to suppress high-volume types like 'paper' or 'author'
                that dominate large traversals.
            predicates: Predicate names that receive full metadata. Others
                appear as stubs. Omit for full data on all edges.
            min_mentions: Minimum corpus-wide mention count for a node to
                appear. Nodes with fewer mentions (and edges touching them)
                are omitted. Default 1 (no filtering). Use 2 or 3 to suppress
                low-confidence provisional entities from single documents.
            topology_only: If True, return only IDs and types for all nodes
                and edges -- no metadata at all. Use this first on large or
                unfamiliar graphs to see structure before fetching details.
            limit: Maximum number of nodes to return. node_count and edge_count
                always reflect the full result size. Use with offset to page
                through large subgraphs. Edges are filtered to those whose
                both endpoints appear in the returned node window.
            offset: Number of nodes to skip before returning results (default 0).

        Returns:
            BfsResult with nodes, edges, and schema_summary. node_count and
            edge_count reflect the full traversal; nodes/edges may be a page.
            Edge provenance (text spans) is omitted to keep size manageable
            -- use describe_entity() for full provenance on a specific node.
        """
        result: BfsResult = await _bfs_query(
            _db(),
            BfsQuery(
                seeds=seeds,
                max_hops=max_hops,
                node_types=node_types or [],
                exclude_node_types=exclude_node_types or [],
                predicates=predicates or [],
                min_mentions=min_mentions,
                topology_only=topology_only,
            ),
        )
        return _slim_result(result, topology_only=topology_only, limit=limit, offset=offset)

    # ------------------------------------------------------------------
    # Tool: intersect_subgraphs
    # ------------------------------------------------------------------

    @mcp.tool(
        description=(
            "Return nodes within k hops of ALL given seeds (intersection of "
            "k-hop neighborhoods) and the induced subgraph edges between them. "
            "Edges are treated as undirected for traversal. Use this to answer "
            "questions like 'what actors appeared in movies with both Tom Hanks "
            "and Meg Ryan?' (seeds=[Tom Hanks, Meg Ryan], k=2) or 'what concepts "
            "are near all of these entities?'. Supports the same node_types, "
            "exclude_node_types, predicates, and topology_only filters as bfs_query."
        )
    )
    async def intersect_subgraphs(
        seeds: list[str],
        k: int,
        node_types: list[str] | None = None,
        exclude_node_types: list[str] | None = None,
        predicates: list[str] | None = None,
        min_mentions: int = 1,
        topology_only: bool = False,
    ) -> dict:
        """Find nodes within k undirected hops of every seed and induced edges.

        Args:
            seeds: Two or more canonical entity IDs.
            k: Hop radius (1-5). All seeds must reach the result nodes
               within this many hops treating edges as undirected.
            node_types: Entity types that receive full metadata. Others appear
                as stubs. Omit for full data on all nodes.
            exclude_node_types: Entity types to remove entirely from the result.
                Excluded nodes and all edges touching them are omitted.
            predicates: Predicate names that receive full metadata. Others
                appear as stubs. Omit for full data on all edges.
            min_mentions: Minimum corpus-wide mention count for a node to
                appear. Default 1 (no filtering).
            topology_only: If True, return only IDs and types -- no metadata.

        Returns:
            IntersectionResult with nodes and induced subgraph edges.
        """
        result = await _neighborhood_intersection(
            _db(),
            IntersectionQuery(
                seeds=seeds,
                k=k,
                node_types=node_types or [],
                exclude_node_types=exclude_node_types or [],
                predicates=predicates or [],
                min_mentions=min_mentions,
                topology_only=topology_only,
            ),
        )
        return result.model_dump()

    # ------------------------------------------------------------------
    # Tool: describe_entity
    # ------------------------------------------------------------------

    @mcp.tool(
        description="Retrieve full metadata for a single entity by canonical ID. "
        "Use this to expand a stub node returned by bfs_query."
    )
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

    # ------------------------------------------------------------------
    # Tool: describe_entities (batch)
    # ------------------------------------------------------------------

    @mcp.tool(
        description="Retrieve full metadata for multiple entities by canonical ID "
        "in a single call. Use this instead of multiple describe_entity calls when "
        "you have several stub nodes to expand. Returns results in the same order "
        "as the input ids list; missing or invalid IDs are omitted from results."
    )
    async def describe_entities(ids: list[str]) -> list[dict]:
        """Get full metadata for multiple entities.

        Args:
            ids: List of canonical entity IDs to expand.

        Returns:
            List of full node metadata dicts, one per valid ID, in input order.
            IDs that do not exist in the graph are silently omitted.
        """
        db = _db()
        results = []
        for entity_id in ids:
            try:
                node = await db.get_node(entity_id)
                metadata = await db.metadata_for_node(entity_id)
                results.append({"id": node.id, "entity_type": node.entity_type, **metadata})
            except KeyError:
                pass
        return results

    return mcp


def _bfs_query_description(entity_types: list[str], predicates: list[str]) -> str:
    """Build the bfs_query tool description, injecting schema when small enough."""
    base = (
        "Perform a breadth-first traversal from one or more seed entities. "
        "Filters control detail level, not which nodes appear: non-matching "
        "nodes and edges appear as lightweight stubs preserving full topology."
    )
    if (
        entity_types
        and len(entity_types) <= _MAX_INJECT_TYPES
        and predicates
        and len(predicates) <= _MAX_INJECT_PREDICATES
    ):
        types_str = ", ".join(entity_types)
        preds_str = ", ".join(predicates)
        return (
            f"{base} "
            f"Valid node_types: {types_str}. "
            f"Valid predicates: {preds_str}."
        )
    return base


_EDGE_META_STRIP = {
    "provenance",
    "strongest_evidence_quote",
    "evidence_confidence_avg",
    "created_at",
}


def _slim_result(
    result: BfsResult,
    topology_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> dict:
    """Serialize a BfsResult, stripping verbose fields to keep response size manageable.

    When topology_only=True, all metadata is removed and every node/edge is
    reduced to IDs and types only -- useful for an initial structural survey
    of a large or unfamiliar graph.

    Otherwise, verbose edge fields (provenance text, quotes, timestamps) are
    stripped while confidence and source_documents are kept. Full provenance
    is available via describe_entity().

    schema_summary is always included and reflects the FULL traversal result
    regardless of pagination, so callers can discover the complete vocabulary
    even when viewing only a page of nodes.

    Pagination: limit/offset apply to nodes. Edges are then filtered to only
    those whose both endpoints are present in the returned node window.
    node_count and edge_count always reflect the full result before pagination.
    """
    data = result.model_dump()

    # Apply pagination to nodes first, before any further processing.
    all_nodes = data.get("nodes", [])
    if offset or limit is not None:
        end = (offset + limit) if limit is not None else None
        paged_nodes = all_nodes[offset:end]
        paged_node_ids = {n["id"] for n in paged_nodes}
        paged_edges = [
            e for e in data.get("edges", [])
            if e["subject"] in paged_node_ids and e["object"] in paged_node_ids
        ]
        data["nodes"] = paged_nodes
        data["edges"] = paged_edges
    # node_count / edge_count always reflect the full traversal.

    if topology_only:
        data["nodes"] = [
            {"id": n["id"], "entity_type": n["entity_type"]}
            for n in data.get("nodes", [])
        ]
        data["edges"] = [
            {
                "subject": e["subject"],
                "predicate": e["predicate"],
                "object": e["object"],
            }
            for e in data.get("edges", [])
        ]
    else:
        for edge in data.get("edges", []):
            meta = edge.get("metadata")
            if isinstance(meta, dict):
                for key in _EDGE_META_STRIP:
                    meta.pop(key, None)

    # schema_summary always reflects the full traversal, not the page.
    data["schema_summary"] = _build_schema_summary(result).model_dump()
    return data


def _build_schema_summary(result: BfsResult) -> SchemaSummary:
    """Derive a SchemaSummary from the nodes and edges in a BfsResult."""
    entity_types = sorted({n.entity_type for n in result.nodes})
    predicates = sorted({e.predicate for e in result.edges})
    return SchemaSummary(entity_types_found=entity_types, predicates_found=predicates)


def _server_instructions() -> str:
    return (
        "This MCP server exposes a knowledge graph via BFS-QL. "
        "Recommended workflow: 1) Call describe_schema() to learn entity types, predicates, and graph description. "
        "Read the next_steps field -- it contains backend-specific instructions for how to proceed; follow those "
        "in preference to any generic guidance. "
        "2) Call search_entities(name, node_types=[...]) with a specific entity name (not a type) to resolve "
        "it to a canonical ID. Pass node_types to restrict results to the entity type you want (e.g. "
        "['disease']) and avoid paper or author matches. Inspect entity_type in results to pick the right match. "
        "3) Call bfs_query(seeds, max_hops, ...) to traverse the graph. "
        "Start with max_hops=1. On literature-derived graphs, use exclude_node_types=['paper','author'] and "
        "min_mentions=2 to get a compact concept-only result in a single call. "
        "Use node_types and predicates to control metadata detail level -- non-matching nodes still appear "
        "as stubs so topology is preserved. "
        "Each bfs_query result includes a schema_summary listing the entity types and predicates "
        "actually found in that subgraph -- use these as filter values in follow-up queries, "
        "especially when describe_schema returns comprehensive=False. "
        "4) Call describe_entities([id, ...]) to expand multiple stub nodes in a single call. "
        "Use describe_entity(id) for single-node lookups. "
        "Important: search_entities searches entity names, not entity types. To find all "
        "entities of a given type, use bfs_query from a relevant seed with node_types filter. "
        "Entity IDs beginning with 'prov:' are provisional -- the pipeline that built this "
        "graph could not resolve them to a canonical authority. They are structurally present "
        "but carry no external meaning. Skip them or treat them as anonymous placeholders."
    )
