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
    IntersectionQuery,
    IntersectionResult,
    Node,
)

# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------

_NODES: dict[str, Node] = {
    "Drug:A": Node(
        id="Drug:A", entity_type="Drug", metadata={"name": "DrugA", "mw": 342.4}
    ),
    "Disease:B": Node(
        id="Disease:B",
        entity_type="Disease",
        metadata={"name": "DiseaseB", "mesh": "D001"},
    ),
    "Gene:C": Node(
        id="Gene:C", entity_type="Gene", metadata={"name": "GeneC", "hgnc": "123"}
    ),
    "Disease:D": Node(
        id="Disease:D",
        entity_type="Disease",
        metadata={"name": "DiseaseD", "mesh": "D002"},
    ),
}

_EDGES: list[Edge] = [
    Edge(subject="Drug:A", predicate="TREATS", object="Disease:B"),
    Edge(subject="Drug:A", predicate="INHIBITS", object="Gene:C"),
    Edge(subject="Gene:C", predicate="ASSOCIATED_WITH", object="Disease:B"),
    Edge(subject="Disease:B", predicate="COMORBID_WITH", object="Disease:D"),
]

_EDGE_META: dict[Edge, dict[str, Any]] = {
    Edge(subject="Drug:A", predicate="TREATS", object="Disease:B"): {
        "confidence": 0.95,
        "provenance": ["PMC001"],
    },
    Edge(subject="Drug:A", predicate="INHIBITS", object="Gene:C"): {
        "confidence": 0.80,
        "provenance": ["PMC002"],
    },
    Edge(subject="Gene:C", predicate="ASSOCIATED_WITH", object="Disease:B"): {
        "confidence": 0.70,
        "provenance": ["PMC003"],
    },
    Edge(subject="Disease:B", predicate="COMORBID_WITH", object="Disease:D"): {
        "confidence": 0.60,
        "provenance": ["PMC004"],
    },
}


class MockBackend(GraphDbInterface):
    async def search_entities(
        self,
        query: str,
        node_types: list[str] | None = None,
    ) -> list[EntityStub]:
        results = [
            EntityStub(id=nid, entity_type=n.entity_type)
            for nid, n in _NODES.items()
            if query.lower() in n.metadata.get("name", "").lower()
        ]
        if node_types:
            results = [r for r in results if r.entity_type in node_types]
        return results

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
    return [
        n
        for n in result.nodes
        if isinstance(n, EntityStub) or not getattr(n, "metadata", None)
    ]


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
    result = await bfs_query(
        db, BfsQuery(seeds=["Drug:A"], max_hops=1, node_types=["Disease"])
    )
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
    result = await bfs_query(
        db, BfsQuery(seeds=["Drug:A"], max_hops=1, predicates=["TREATS"])
    )
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
        assert isinstance(
            node, Node
        ), f"Expected full Node, got {type(node)} for {node.id}"
        assert node.metadata
    for edge in result.edges:
        assert isinstance(
            edge, EdgeWithMetadata
        ), f"Expected EdgeWithMetadata, got stub for {edge.predicate}"


async def test_multi_seed_union(db):
    """Multi-seed query returns the union of neighborhoods."""
    result = await bfs_query(db, BfsQuery(seeds=["Drug:A", "Disease:D"], max_hops=1))
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
    result = await bfs_query(
        db, BfsQuery(seeds=["Disease:D"], max_hops=1, node_types=["Drug"])
    )
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
#
# Induced edges within {Drug:A, Disease:B, Gene:C} (k=1 from Drug:A+Gene:C):
#   Drug:A --TREATS--> Disease:B
#   Drug:A --INHIBITS--> Gene:C
#   Gene:C --ASSOCIATED_WITH--> Disease:B
# ---------------------------------------------------------------------------


def _iq(seeds, k, **kwargs) -> IntersectionQuery:
    """Convenience constructor for IntersectionQuery."""
    return IntersectionQuery(seeds=seeds, k=k, **kwargs)


async def test_intersection_two_seeds_k1(db):
    """Nodes within 1 hop of both Drug:A and Gene:C.

    1-hop of Drug:A:    {Drug:A, Disease:B, Gene:C}
    1-hop of Gene:C:    {Gene:C, Drug:A, Disease:B}
    intersection:       {Drug:A, Disease:B, Gene:C}
    """
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    assert isinstance(result, IntersectionResult)
    ids = {n.id for n in result.nodes}
    assert ids == {"Drug:A", "Disease:B", "Gene:C"}


async def test_intersection_two_seeds_k1_disease_d_excluded(db):
    """Disease:D is 2 hops from Drug:A and 2 hops from Gene:C, excluded at k=1."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    ids = {n.id for n in result.nodes}
    assert "Disease:D" not in ids


async def test_intersection_two_seeds_k2_includes_disease_d(db):
    """At k=2, Disease:D is reachable from both Drug:A and Gene:C.

    2-hop of Drug:A:    {Drug:A, Disease:B, Gene:C, Disease:D}
    2-hop of Gene:C:    {Gene:C, Drug:A, Disease:B, Disease:D}
    intersection:       all four nodes
    """
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=2))
    ids = {n.id for n in result.nodes}
    assert "Disease:D" in ids
    assert ids == {"Drug:A", "Disease:B", "Gene:C", "Disease:D"}


async def test_intersection_distant_seeds_k1(db):
    """Drug:A and Disease:D share only Disease:B at k=1.

    1-hop of Drug:A:    {Drug:A, Disease:B, Gene:C}
    1-hop of Disease:D: {Disease:D, Disease:B}
    intersection:       {Disease:B}
    """
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Disease:D"], k=1))
    ids = {n.id for n in result.nodes}
    assert ids == {"Disease:B"}


async def test_intersection_single_seed(db):
    """Single seed returns its own k-hop neighborhood."""
    result = await neighborhood_intersection(db, _iq(["Drug:A"], k=1))
    ids = {n.id for n in result.nodes}
    assert ids == {"Drug:A", "Disease:B", "Gene:C"}


async def test_intersection_empty_seeds(db):
    """Empty seeds list returns an empty IntersectionResult."""
    result = await neighborhood_intersection(db, _iq([], k=2))
    assert isinstance(result, IntersectionResult)
    assert result.node_count == 0
    assert result.edge_count == 0
    assert result.nodes == []
    assert result.edges == []


async def test_intersection_missing_seed(db):
    """A missing seed causes an empty IntersectionResult."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "NoSuch:X"], k=2))
    assert result.node_count == 0
    assert result.nodes == []


async def test_intersection_same_seed_twice(db):
    """Duplicate seeds are redundant but valid."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Drug:A"], k=1))
    ids = {n.id for n in result.nodes}
    assert ids == {"Drug:A", "Disease:B", "Gene:C"}


async def test_intersection_result_counts(db):
    """node_count and edge_count match the lengths of their lists."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    assert result.node_count == len(result.nodes)
    assert result.edge_count == len(result.edges)


# ---------------------------------------------------------------------------
# Induced edges
# ---------------------------------------------------------------------------


async def test_intersection_induced_edges_k1(db):
    """All three edges among {Drug:A, Disease:B, Gene:C} are included.

    Induced edges:
      Drug:A --TREATS--> Disease:B
      Drug:A --INHIBITS--> Gene:C
      Gene:C --ASSOCIATED_WITH--> Disease:B
    """
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    edge_triples = {(e.subject, e.predicate, e.object) for e in result.edges}
    assert edge_triples == {
        ("Drug:A", "TREATS", "Disease:B"),
        ("Drug:A", "INHIBITS", "Gene:C"),
        ("Gene:C", "ASSOCIATED_WITH", "Disease:B"),
    }


async def test_intersection_induced_edges_exclude_boundary(db):
    """COMORBID_WITH is not induced: Disease:D is outside the k=1 intersection."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    predicates = {e.predicate for e in result.edges}
    assert "COMORBID_WITH" not in predicates


async def test_intersection_induced_edges_k2_includes_comorbid(db):
    """At k=2 all four nodes are in the intersection, so all four edges are induced."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=2))
    predicates = {e.predicate for e in result.edges}
    assert "COMORBID_WITH" in predicates
    assert len(result.edges) == 4


# ---------------------------------------------------------------------------
# Stub/full filtering and topology_only
# ---------------------------------------------------------------------------


async def test_intersection_default_returns_full_nodes(db):
    """Without node_types filter, all nodes are full Node records."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    for node in result.nodes:
        assert isinstance(node, Node)
        assert node.metadata  # metadata was fetched


async def test_intersection_node_type_filter(db):
    """node_types filter: Drug nodes get full records, others get stubs."""
    result = await neighborhood_intersection(
        db, _iq(["Drug:A", "Gene:C"], k=1, node_types=["Drug"])
    )
    for node in result.nodes:
        if node.entity_type == "Drug":
            assert isinstance(node, Node)
        else:
            assert isinstance(node, EntityStub)


async def test_intersection_predicate_filter(db):
    """predicates filter: TREATS edges get full records, others get stubs."""
    result = await neighborhood_intersection(
        db, _iq(["Drug:A", "Gene:C"], k=1, predicates=["TREATS"])
    )
    for edge in result.edges:
        if edge.predicate == "TREATS":
            assert isinstance(edge, EdgeWithMetadata)
        else:
            assert isinstance(edge, Edge)


async def test_intersection_topology_only(db):
    """topology_only: all nodes are EntityStub, all edges are bare Edge."""
    result = await neighborhood_intersection(
        db, _iq(["Drug:A", "Gene:C"], k=1, topology_only=True)
    )
    for node in result.nodes:
        assert isinstance(node, EntityStub)
    for edge in result.edges:
        assert isinstance(edge, Edge)
        assert not isinstance(edge, EdgeWithMetadata)


# ---------------------------------------------------------------------------
# schema_summary
# ---------------------------------------------------------------------------


async def test_intersection_schema_summary(db):
    """schema_summary reflects actual entity types and predicates in the result."""
    result = await neighborhood_intersection(db, _iq(["Drug:A", "Gene:C"], k=1))
    assert set(result.schema_summary.entity_types_found) == {"Drug", "Disease", "Gene"}
    assert set(result.schema_summary.predicates_found) == {
        "TREATS",
        "INHIBITS",
        "ASSOCIATED_WITH",
    }


async def test_intersection_schema_summary_populated_with_topology_only(db):
    """schema_summary is populated even when topology_only=True."""
    result = await neighborhood_intersection(
        db, _iq(["Drug:A", "Gene:C"], k=1, topology_only=True)
    )
    assert result.schema_summary.entity_types_found
    assert result.schema_summary.predicates_found


# ---------------------------------------------------------------------------
# exclude_node_types tests (bfs_query)
# ---------------------------------------------------------------------------


async def test_exclude_node_types_removes_nodes(db):
    """Excluded entity types are absent from the result entirely."""
    result = await bfs_query(
        db, BfsQuery(seeds=["Drug:A"], max_hops=2, exclude_node_types=["Gene"])
    )
    ids = node_ids(result)
    assert "Gene:C" not in ids
    # Non-excluded nodes still present
    assert "Drug:A" in ids
    assert "Disease:B" in ids
    assert "Disease:D" in ids


async def test_exclude_node_types_removes_incident_edges(db):
    """Edges touching an excluded node are also removed."""
    result = await bfs_query(
        db, BfsQuery(seeds=["Drug:A"], max_hops=2, exclude_node_types=["Gene"])
    )
    tuples = edge_tuples(result)
    # INHIBITS connects Drug:A to Gene:C -- should be gone
    assert ("Drug:A", "INHIBITS", "Gene:C") not in tuples
    # ASSOCIATED_WITH connects Gene:C to Disease:B -- should be gone
    assert ("Gene:C", "ASSOCIATED_WITH", "Disease:B") not in tuples
    # TREATS connects Drug:A to Disease:B -- should remain
    assert ("Drug:A", "TREATS", "Disease:B") in tuples


async def test_exclude_node_types_empty_list_is_noop(db):
    """An empty exclude list leaves the result unchanged."""
    result_no_exclude = await bfs_query(db, BfsQuery(seeds=["Drug:A"], max_hops=2))
    result_empty_exclude = await bfs_query(
        db, BfsQuery(seeds=["Drug:A"], max_hops=2, exclude_node_types=[])
    )
    assert node_ids(result_no_exclude) == node_ids(result_empty_exclude)


async def test_exclude_node_types_schema_summary_reflects_exclusion(db):
    """schema_summary does not include excluded entity types."""
    result = await bfs_query(
        db, BfsQuery(seeds=["Drug:A"], max_hops=2, exclude_node_types=["Gene"])
    )
    assert "Gene" not in result.schema_summary.entity_types_found


# ---------------------------------------------------------------------------
# exclude_node_types tests (neighborhood_intersection)
# ---------------------------------------------------------------------------


async def test_intersection_exclude_node_types(db):
    """Excluded types are absent from intersection results."""
    result = await neighborhood_intersection(
        db, _iq(["Drug:A", "Gene:C"], k=1, exclude_node_types=["Disease"])
    )
    ids = {n.id for n in result.nodes}
    assert "Disease:B" not in ids
    # Drug:A and Gene:C remain
    assert "Drug:A" in ids
    assert "Gene:C" in ids


async def test_intersection_exclude_removes_incident_edges(db):
    """Edges touching excluded nodes are removed from intersection results."""
    result = await neighborhood_intersection(
        db, _iq(["Drug:A", "Gene:C"], k=1, exclude_node_types=["Disease"])
    )
    predicates = {e.predicate for e in result.edges}
    # TREATS (Drug:A->Disease:B) and ASSOCIATED_WITH (Gene:C->Disease:B) removed
    assert "TREATS" not in predicates
    assert "ASSOCIATED_WITH" not in predicates
    # INHIBITS (Drug:A->Gene:C) remains
    assert "INHIBITS" in predicates
