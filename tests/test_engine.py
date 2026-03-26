"""Tests for the BFS traversal engine against a mock backend.

The mock backend represents a small graph:

    Drug:A --TREATS--> Disease:B
    Drug:A --INHIBITS--> Gene:C
    Gene:C --ASSOCIATED_WITH--> Disease:B
    Disease:B --COMORBID_WITH--> Disease:D

Entity types: Drug, Disease, Gene
Predicates: TREATS, INHIBITS, ASSOCIATED_WITH, COMORBID_WITH
"""

from typing import Any

import pytest

from bfsql.abc import GraphDbInterface
from bfsql.engine import bfs_query, neighborhood_intersection
from bfsql.models import (
    BfsQuery,
    BfsResult,
    Edge,
    EdgeWithMetadata,
    EntityStub,
    Node,
)


# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------

_NODES: dict[str, Node] = {
    "Drug:A":    Node(id="Drug:A",    entity_type="Drug",    metadata={"name": "DrugA",    "mw": 342.4}),
    "Disease:B": Node(id="Disease:B", entity_type="Disease", metadata={"name": "DiseaseB", "mesh": "D001"}),
    "Gene:C":    Node(id="Gene:C",    entity_type="Gene",    metadata={"name": "GeneC",    "hgnc": "123"}),
    "Disease:D": Node(id="Disease:D", entity_type="Disease", metadata={"name": "DiseaseD", "mesh": "D002"}),
}

_EDGES: list[Edge] = [
    Edge(subject="Drug:A",    predicate="TREATS",           object="Disease:B"),
    Edge(subject="Drug:A",    predicate="INHIBITS",         object="Gene:C"),
    Edge(subject="Gene:C",    predicate="ASSOCIATED_WITH",  object="Disease:B"),
    Edge(subject="Disease:B", predicate="COMORBID_WITH",    object="Disease:D"),
]

_EDGE_META: dict[Edge, dict[str, Any]] = {
    Edge(subject="Drug:A",    predicate="TREATS",           object="Disease:B"):  {"confidence": 0.95, "provenance": ["PMC001"]},
    Edge(subject="Drug:A",    predicate="INHIBITS",         object="Gene:C"):     {"confidence": 0.80, "provenance": ["PMC002"]},
    Edge(subject="Gene:C",    predicate="ASSOCIATED_WITH",  object="Disease:B"):  {"confidence": 0.70, "provenance": ["PMC003"]},
    Edge(subject="Disease:B", predicate="COMORBID_WITH",    object="Disease:D"):  {"confidence": 0.60, "provenance": ["PMC004"]},
}


class MockBackend(GraphDbInterface):
    async def search_entities(self, query: str) -> list[EntityStub]:
        return [EntityStub(id=nid, entity_type=n.entity_type)
                for nid, n in _NODES.items() if query.lower() in n.metadata.get("name", "").lower()]

    async def edges_from(self, entity_id: str) -> list[Edge]:
        return [e for e in _EDGES if e.subject == entity_id]

    async def edges_to(self, entity_id: str) -> list[Edge]:
        return [e for e in _EDGES if e.object == entity_id]

    async def get_node(self, entity_id: str) -> Node:
        if entity_id not in _NODES:
            raise KeyError(entity_id)
        return _NODES[entity_id]

    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        return _NODES[entity_id].metadata

    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        return _EDGE_META[edge]

    async def entity_types(self) -> list[str]:
        return ["Drug", "Disease", "Gene"]

    async def predicates(self) -> list[str]:
        return ["TREATS", "INHIBITS", "ASSOCIATED_WITH", "COMORBID_WITH"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def node_ids(result: BfsResult) -> set[str]:
    return {n.id for n in result.nodes}

def edge_tuples(result: BfsResult) -> set[tuple[str, str, str]]:
    return {(e.subject, e.predicate, e.object) for e in result.edges}

def full_nodes(result: BfsResult) -> list[Node]:
    return [n for n in result.nodes if isinstance(n, Node) and n.metadata]

def stub_nodes(result: BfsResult) -> list[EntityStub]:
    return [n for n in result.nodes if isinstance(n, EntityStub) or not getattr(n, "metadata", None)]

def full_edges(result: BfsResult) -> list[EdgeWithMetadata]:
    return [e for e in result.edges if isinstance(e, EdgeWithMetadata)]

def stub_edges(result: BfsResult) -> list[Edge]:
    return [e for e in result.edges if not isinstance(e, EdgeWithMetadata)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def db() -> MockBackend:
    return MockBackend()


async def test_one_hop_from_drug(db):
    """One hop from Drug:A reaches Disease:B and Gene:C."""
    result = await bfs_query(db, BfsQuery(seeds=["Drug:A"], max_hops=1))
    assert "Drug:A" in node_ids(result)
    assert "Disease:B" in node_ids(result)
    assert "Gene:C" in node_ids(result)
    assert "Disease:D" not in node_ids(result)


async def test_two_hops_reaches_disease_d(db):
    """Two hops from Drug:A reaches Disease:D via Disease:B."""
    result = await bfs_query(db, BfsQuery(seeds=["Drug:A"], max_hops=2))
    assert "Disease:D" in node_ids(result)


async def test_topology_complete_with_node_type_filter(db):
    """Non-matching nodes appear as stubs, not omitted."""
    result = await bfs_query(db, BfsQuery(
        seeds=["Drug:A"], max_hops=1, node_types=["Disease"]
    ))
    ids = node_ids(result)
    # Disease:B matches -- should be present
    assert "Disease:B" in ids
    # Drug:A and Gene:C don't match -- should still be present as stubs
    assert "Drug:A" in ids
    assert "Gene:C" in ids

    # Disease:B should be a full node
    disease_nodes = [n for n in result.nodes if getattr(n, "id", None) == "Disease:B"]
    assert len(disease_nodes) == 1
    assert isinstance(disease_nodes[0], Node)
    assert disease_nodes[0].metadata

    # Drug:A should be a stub (no metadata)
    drug_nodes = [n for n in result.nodes if getattr(n, "id", None) == "Drug:A"]
    assert len(drug_nodes) == 1
    assert isinstance(drug_nodes[0], EntityStub) or not drug_nodes[0].metadata


async def test_topology_complete_with_predicate_filter(db):
    """Non-matching edges appear as stubs, not omitted."""
    result = await bfs_query(db, BfsQuery(
        seeds=["Drug:A"], max_hops=1, predicates=["TREATS"]
    ))
    tuples = edge_tuples(result)
    # TREATS edge should be present
    assert ("Drug:A", "TREATS", "Disease:B") in tuples
    # INHIBITS edge should also be present (as stub)
    assert ("Drug:A", "INHIBITS", "Gene:C") in tuples

    treats_edges = [e for e in result.edges if e.predicate == "TREATS"]
    assert all(isinstance(e, EdgeWithMetadata) for e in treats_edges)

    inhibits_edges = [e for e in result.edges if e.predicate == "INHIBITS"]
    assert all(not isinstance(e, EdgeWithMetadata) for e in inhibits_edges)


async def test_no_filter_returns_all_full(db):
    """With no filters, all nodes and edges are full records."""
    result = await bfs_query(db, BfsQuery(seeds=["Drug:A"], max_hops=2))
    for node in result.nodes:
        assert isinstance(node, Node), f"Expected full Node, got {type(node)} for {node.id}"
        assert node.metadata
    for edge in result.edges:
        assert isinstance(edge, EdgeWithMetadata), f"Expected EdgeWithMetadata, got stub for {edge.predicate}"


async def test_multi_seed_union(db):
    """Multi-seed query returns the union of neighborhoods."""
    result = await bfs_query(db, BfsQuery(
        seeds=["Drug:A", "Disease:D"], max_hops=1
    ))
    ids = node_ids(result)
    # From Drug:A at 1 hop
    assert "Disease:B" in ids
    assert "Gene:C" in ids
    # From Disease:D at 1 hop (incoming: Disease:B via COMORBID_WITH)
    assert "Disease:B" in ids
    # Disease:B appears once even though reachable from both seeds
    disease_b_count = sum(1 for n in result.nodes if n.id == "Disease:B")
    assert disease_b_count == 1


async def test_node_count_and_edge_count(db):
    """node_count and edge_count match the actual lists."""
    result = await bfs_query(db, BfsQuery(seeds=["Drug:A"], max_hops=2))
    assert result.node_count == len(result.nodes)
    assert result.edge_count == len(result.edges)


async def test_seeds_always_present(db):
    """Seed nodes are always in the result even with restrictive filters."""
    result = await bfs_query(db, BfsQuery(
        seeds=["Disease:D"], max_hops=1, node_types=["Drug"]
    ))
    assert "Disease:D" in node_ids(result)


# ---------------------------------------------------------------------------
# neighborhood_intersection tests
#
# Graph recap (undirected traversal):
#   Drug:A -- Disease:B  (via TREATS)
#   Drug:A -- Gene:C     (via INHIBITS)
#   Gene:C -- Disease:B  (via ASSOCIATED_WITH)
#   Disease:B -- Disease:D (via COMORBID_WITH)
#
# Undirected adjacency:
#   Drug:A:    {Disease:B, Gene:C}
#   Disease:B: {Drug:A, Gene:C, Disease:D}
#   Gene:C:    {Drug:A, Disease:B}
#   Disease:D: {Disease:B}
# ---------------------------------------------------------------------------

async def test_intersection_two_seeds_k1(db):
    """Nodes within 1 hop of both Drug:A and Gene:C.

    1-hop of Drug:A:    {Drug:A, Disease:B, Gene:C}
    1-hop of Gene:C:    {Gene:C, Drug:A, Disease:B}
    intersection:       {Drug:A, Disease:B, Gene:C}
    """
    result = await neighborhood_intersection(db, ["Drug:A", "Gene:C"], k=1)
    ids = {n.id for n in result}
    assert ids == {"Drug:A", "Disease:B", "Gene:C"}


async def test_intersection_two_seeds_k1_disease_d_excluded(db):
    """Disease:D is 2 hops from Drug:A and 2 hops from Gene:C, excluded at k=1."""
    result = await neighborhood_intersection(db, ["Drug:A", "Gene:C"], k=1)
    ids = {n.id for n in result}
    assert "Disease:D" not in ids


async def test_intersection_two_seeds_k2_includes_disease_d(db):
    """At k=2, Disease:D is reachable from both Drug:A and Gene:C.

    2-hop of Drug:A:    {Drug:A, Disease:B, Gene:C, Disease:D}
    2-hop of Gene:C:    {Gene:C, Drug:A, Disease:B, Disease:D}
    intersection:       all four nodes
    """
    result = await neighborhood_intersection(db, ["Drug:A", "Gene:C"], k=2)
    ids = {n.id for n in result}
    assert "Disease:D" in ids
    assert ids == {"Drug:A", "Disease:B", "Gene:C", "Disease:D"}


async def test_intersection_distant_seeds_k1_empty(db):
    """Drug:A and Disease:D are 2 hops apart; no common 1-hop neighbors.

    1-hop of Drug:A:    {Drug:A, Disease:B, Gene:C}
    1-hop of Disease:D: {Disease:D, Disease:B}
    intersection:       {Disease:B}
    """
    result = await neighborhood_intersection(db, ["Drug:A", "Disease:D"], k=1)
    ids = {n.id for n in result}
    assert ids == {"Disease:B"}


async def test_intersection_single_seed(db):
    """Single seed returns its own k-hop neighborhood."""
    result = await neighborhood_intersection(db, ["Drug:A"], k=1)
    ids = {n.id for n in result}
    assert ids == {"Drug:A", "Disease:B", "Gene:C"}


async def test_intersection_empty_seeds(db):
    """Empty seeds list returns empty."""
    result = await neighborhood_intersection(db, [], k=2)
    assert result == []


async def test_intersection_missing_seed(db):
    """A missing seed causes empty result."""
    result = await neighborhood_intersection(db, ["Drug:A", "NoSuch:X"], k=2)
    assert result == []


async def test_intersection_same_seed_twice(db):
    """Duplicate seeds are redundant but valid."""
    result = await neighborhood_intersection(db, ["Drug:A", "Drug:A"], k=1)
    ids = {n.id for n in result}
    assert ids == {"Drug:A", "Disease:B", "Gene:C"}


async def test_intersection_returns_entity_stubs(db):
    """Result nodes are EntityStub records (no metadata fetched)."""
    result = await neighborhood_intersection(db, ["Drug:A", "Gene:C"], k=1)
    for node in result:
        assert isinstance(node, EntityStub)
        assert node.entity_type  # type is populated
