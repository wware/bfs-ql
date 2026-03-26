"""Integration tests against the live DBpedia SPARQL endpoint.

These tests are skipped automatically if the endpoint is not reachable
(conftest.py checks reachability before the session starts).

Seed entity: DBpedia:Desmopressin -- a synthetic analogue of ADH/vasopressin
used to treat central diabetes insipidus and enuresis. Well-documented in
DBpedia with stable rdf:type and outgoing edges.
"""

import pytest

from bfsql.backends.sparql import SparqlBackend
from bfsql.cache import CachedGraphDb
from bfsql.engine import bfs_query
from bfsql.models import BfsQuery

pytest.skip(
    "DBpedia integration tests disabled -- requires live external endpoint",
    allow_module_level=True,
)

ENDPOINT = "https://dbpedia.org/sparql"

PREFIXES = {
    "DBpedia": "http://dbpedia.org/resource/",
    "DBpedia-owl": "http://dbpedia.org/ontology/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
}

SEED = "DBpedia:Desmopressin"
SEED2 = "DBpedia:Cushing%27s_disease"


@pytest.fixture(scope="module")
async def backend():
    b = await SparqlBackend.create(endpoint=ENDPOINT, prefixes=PREFIXES)
    yield b
    await b.close()


@pytest.fixture(scope="module")
def db(backend):
    return CachedGraphDb(backend)


# ---------------------------------------------------------------------------
# search_entities
# ---------------------------------------------------------------------------


async def test_search_desmopressin(backend):
    """search_entities('desmopressin') returns at least one result containing
    the canonical ID DBpedia:Desmopressin."""
    results = await backend.search_entities("desmopressin")
    assert len(results) > 0
    ids = {r.id for r in results}
    assert SEED in ids, f"Expected {SEED!r} in results, got: {ids}"


# ---------------------------------------------------------------------------
# edges_from
# ---------------------------------------------------------------------------


async def test_edges_from_desmopressin(backend):
    """edges_from returns a non-empty list for Desmopressin."""
    edges = await backend.edges_from(SEED)
    assert len(edges) > 0
    for e in edges:
        assert e.subject == SEED
        assert ":" in e.predicate  # compressed to canonical ID or full URI
        assert e.object  # non-empty


# ---------------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------------


async def test_get_node_type(backend):
    """get_node returns a Node with a non-empty entity_type for Desmopressin."""
    node = await backend.get_node(SEED)
    assert node.id == SEED
    assert node.entity_type  # non-empty compressed type


# ---------------------------------------------------------------------------
# entity_types and predicates
# ---------------------------------------------------------------------------


async def test_entity_types_nonempty(backend):
    """entity_types() returns a non-empty list from the live endpoint."""
    types = await backend.entity_types()
    assert len(types) > 0


async def test_predicates_nonempty(backend):
    """predicates() returns a non-empty list and rdf:type is absent."""
    preds = await backend.predicates()
    assert len(preds) > 0
    rdf_type = backend._compress("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
    assert rdf_type not in preds, "rdf:type should be filtered out by the SPARQL query"


# ---------------------------------------------------------------------------
# 1-hop BFS via engine
# ---------------------------------------------------------------------------


async def test_bfs_1hop(db):
    """1-hop BFS from Desmopressin returns the seed and at least one neighbour."""
    result = await bfs_query(db, BfsQuery(seeds=[SEED], max_hops=1, topology_only=True))
    node_ids = {n.id for n in result.nodes}
    assert SEED in node_ids
    assert result.node_count > 1, "Expected at least one neighbour node"
    assert result.edge_count > 0


# ---------------------------------------------------------------------------
# Server tools end-to-end
# ---------------------------------------------------------------------------


async def test_server_describe_schema(backend):
    """entity_types() and predicates() together produce a valid schema dict."""
    entity_types = await backend.entity_types()
    predicates = await backend.predicates()
    schema = {
        "graph_description": "DBpedia integration test",
        "entity_types": entity_types,
        "predicates": predicates,
    }
    assert schema["graph_description"] == "DBpedia integration test"
    assert isinstance(schema["entity_types"], list)
    assert isinstance(schema["predicates"], list)
    # At least one of each should be populated for a live endpoint
    assert len(schema["entity_types"]) > 0
    assert len(schema["predicates"]) > 0


async def test_server_bfs_query_topology(db):
    """bfs_query with topology_only=True returns a compact structural result."""
    result = await bfs_query(db, BfsQuery(seeds=[SEED], max_hops=2, topology_only=True))
    assert result.node_count >= 1
    # topology_only: all nodes should be stubs (no metadata field populated)
    for node in result.nodes:
        assert not getattr(
            node, "metadata", None
        ), f"topology_only=True but node {node.id!r} has metadata"
