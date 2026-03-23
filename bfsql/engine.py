"""BFS traversal engine: multi-seed expansion, stub/full filtering, result assembly."""

import asyncio
from typing import Any

from bfsql.abc import GraphDbInterface
from bfsql.models import (
    BfsQuery,
    BfsResult,
    Edge,
    EdgeWithMetadata,
    EntityStub,
    Node,
)


async def bfs_query(db: GraphDbInterface, query: BfsQuery) -> BfsResult:
    """Execute a BFS-QL query against the given backend.

    Expands breadth-first from all seeds simultaneously, collecting the
    union of their neighborhoods up to max_hops. Applies stub/full
    filtering: nodes matching node_types (or all nodes when node_types is
    empty) receive full metadata; others appear as EntityStub records.
    Edges matching predicates (or all edges when predicates is empty)
    receive full metadata; others appear as Edge stubs.

    Topology is always complete: non-matching nodes and edges are never
    omitted, only downgraded to stubs.
    """
    node_type_filter: frozenset[str] = frozenset(query.node_types)
    predicate_filter: frozenset[str] = frozenset(query.predicates)

    # visited tracks entity IDs we have already expanded
    visited: set[str] = set()
    # frontier is the current BFS ring
    frontier: set[str] = set(query.seeds)
    # all_edges accumulated across all hops
    all_edges: set[Edge] = set()

    for _ in range(query.max_hops):
        if not frontier:
            break
        visited.update(frontier)

        # Expand all nodes in the current frontier concurrently
        edge_lists = await asyncio.gather(
            *[db.edges_from(entity_id) for entity_id in frontier],
            *[db.edges_to(entity_id) for entity_id in frontier],
        )
        new_edges: set[Edge] = set()
        for edge_list in edge_lists:
            new_edges.update(edge_list)
        all_edges.update(new_edges)

        # Next frontier: neighbors not yet visited
        neighbors: set[str] = set()
        for edge in new_edges:
            neighbors.add(edge.subject)
            neighbors.add(edge.object)
        frontier = neighbors - visited

    # Collect all node IDs referenced in the subgraph
    all_node_ids: set[str] = set(query.seeds)
    for edge in all_edges:
        all_node_ids.add(edge.subject)
        all_node_ids.add(edge.object)

    # Build node results concurrently
    nodes = await asyncio.gather(
        *[_build_node(db, nid, node_type_filter) for nid in all_node_ids]
    )

    # Build edge results concurrently
    edges = await asyncio.gather(
        *[_build_edge(db, edge, predicate_filter) for edge in all_edges]
    )

    return BfsResult(
        seeds=query.seeds,
        max_hops=query.max_hops,
        node_count=len(nodes),
        edge_count=len(edges),
        nodes=list(nodes),
        edges=list(edges),
    )


async def _build_node(
    db: GraphDbInterface,
    entity_id: str,
    node_type_filter: frozenset[str],
) -> Node | EntityStub:
    """Return a full Node or an EntityStub depending on the filter."""
    node = await db.get_node(entity_id)
    if not node_type_filter or node.entity_type in node_type_filter:
        metadata = await db.metadata_for_node(entity_id)
        return Node(id=node.id, entity_type=node.entity_type, metadata=metadata)
    return EntityStub(id=node.id, entity_type=node.entity_type)


async def _build_edge(
    db: GraphDbInterface,
    edge: Edge,
    predicate_filter: frozenset[str],
) -> EdgeWithMetadata | Edge:
    """Return a full EdgeWithMetadata or a bare Edge stub depending on the filter."""
    if not predicate_filter or edge.predicate in predicate_filter:
        metadata = await db.metadata_for_edge(edge)
        return EdgeWithMetadata(
            subject=edge.subject,
            predicate=edge.predicate,
            object=edge.object,
            metadata=metadata,
        )
    return edge
