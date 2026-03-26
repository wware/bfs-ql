"""BFS traversal engine: multi-seed expansion, stub/full filtering, result assembly."""

import asyncio
from typing import cast

from bfsql.abc import GraphDbInterface
from bfsql.models import (
    BfsQuery,
    BfsResult,
    Edge,
    EdgeWithMetadata,
    EntityStub,
    Node,
    SchemaSummary,
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
        for elist in edge_lists:
            new_edges.update(elist)
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

    # Fetch all node types in one batched call, then build node records.
    # topology_only skips all metadata fetches entirely.
    node_id_list = list(all_node_ids)
    raw_nodes = await db.get_nodes_batch(node_id_list)
    node_list: list[Node | EntityStub]
    if query.topology_only:
        node_list = [EntityStub(id=n.id, entity_type=n.entity_type) for n in raw_nodes]
    else:
        node_list = list(
            await asyncio.gather(
                *[_apply_node_filter(db, n, node_type_filter) for n in raw_nodes]
            )
        )

    # Build edge results concurrently (topology_only uses bare Edge stubs).
    edge_list: list[EdgeWithMetadata | Edge]
    if query.topology_only:
        edge_list = cast(list[EdgeWithMetadata | Edge], list(all_edges))
    else:
        edge_list = list(
            await asyncio.gather(
                *[_build_edge(db, edge, predicate_filter) for edge in all_edges]
            )
        )
    schema_summary = SchemaSummary(
        entity_types_found=sorted({n.entity_type for n in node_list}),
        predicates_found=sorted({e.predicate for e in edge_list}),
    )
    return BfsResult(
        seeds=query.seeds,
        max_hops=query.max_hops,
        node_count=len(node_list),
        edge_count=len(edge_list),
        nodes=node_list,
        edges=edge_list,
        schema_summary=schema_summary,
    )


async def neighborhood_intersection(
    db: GraphDbInterface,
    seeds: list[str],
    k: int,
) -> list[EntityStub]:
    """Return nodes within k hops of ALL seeds (undirected traversal).

    Computes the k-hop neighborhood for each seed independently and
    returns the intersection. Edges are treated as undirected: both
    outgoing and incoming edges are followed at each hop.

    Seeds are included in their own neighborhood, so a seed node appears
    in the result only if it is within k hops of every other seed.

    Returns an empty list if any seed does not exist in the graph.
    """
    if not seeds:
        return []
    neighborhoods = await asyncio.gather(
        *[_k_hop_neighborhood(db, seed, k) for seed in seeds]
    )
    for neighborhood in neighborhoods:
        if not neighborhood:  # missing seed → empty set
            return []
    common_ids = set.intersection(*neighborhoods)
    nodes = await db.get_nodes_batch(list(common_ids))
    return [EntityStub(id=n.id, entity_type=n.entity_type) for n in nodes]


async def _k_hop_neighborhood(
    db: GraphDbInterface,
    seed: str,
    k: int,
) -> set[str]:
    """Return all node IDs within k undirected hops of seed.

    Uses batched edge queries: one edges_from + one edges_to call per hop
    covering the entire frontier, not per-node calls.

    Returns an empty set if the seed does not exist.
    """
    try:
        await db.get_node(seed)
    except KeyError:
        return set()

    visited: set[str] = {seed}
    frontier: set[str] = {seed}

    for _ in range(k):
        if not frontier:
            break
        # Fetch all outgoing and incoming edges for the entire frontier
        # concurrently, then union neighbors.
        edge_lists = await asyncio.gather(
            *[db.edges_from(node_id) for node_id in frontier],
            *[db.edges_to(node_id) for node_id in frontier],
        )
        neighbors: set[str] = set()
        for edge_list in edge_lists:
            for edge in edge_list:
                neighbors.add(edge.subject)
                neighbors.add(edge.object)
        frontier = neighbors - visited
        visited |= frontier

    return visited


async def _apply_node_filter(
    db: GraphDbInterface,
    node: Node | EntityStub,
    node_type_filter: frozenset[str],
) -> Node | EntityStub:
    """Return a full Node or an EntityStub depending on the filter.

    The node's type is already known; this only fetches metadata when needed.
    """
    if not node_type_filter or node.entity_type in node_type_filter:
        metadata = await db.metadata_for_node(node.id)
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
