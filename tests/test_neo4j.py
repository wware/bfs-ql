"""Integration tests for Neo4jBackend against a live Neo4j instance.

Requires a running Neo4j 5 server accessible at NEO4J_URI (default:
bolt://localhost:7687) with credentials from NEO4J_USERNAME / NEO4J_PASSWORD
(default: neo4j / testpassword for the standard test container).

Start the test container:
    docker run -d --name neo4j-test \\
      -e NEO4J_AUTH=neo4j/testpassword \\
      -p 7687:7687 neo4j:5

Run with:
    uv run pytest tests/test_neo4j.py -v

All tests are skipped automatically if the Neo4j server is unreachable.

Test graph -- a small pharmacogenomics network:

    Drug:Metformin   --TREATS-->          Disease:T2Diabetes
    Drug:Metformin   --INHIBITS-->        Gene:AMPK
    Drug:Metformin   --INTERACTS_WITH-->  Drug:Warfarin
    Drug:Warfarin    --TREATS-->          Disease:AFib
    Gene:AMPK        --REGULATES-->       Gene:mTOR
    Gene:AMPK        --ASSOCIATED_WITH--> Disease:T2Diabetes
    Gene:mTOR        --ASSOCIATED_WITH--> Disease:Cancer
    Disease:T2Diabetes --COMORBID_WITH--> Disease:Hypertension
    Protein:AKT1     --BINDS-->           Gene:AMPK
    Pathway:Glycolysis --INVOLVES-->      Gene:AMPK
"""

import os

import pytest
from dotenv import load_dotenv
from neo4j import AsyncGraphDatabase

from bfsql.backends.neo4j import Neo4jBackend
from bfsql.models import Edge, EdgeWithMetadata, Node

load_dotenv()

_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("NEO4J_USERNAME", "neo4j")
_PASS = os.environ.get("NEO4J_PASSWORD", "testpassword")

# ---------------------------------------------------------------------------
# Reachability skip
# ---------------------------------------------------------------------------

async def _neo4j_reachable() -> bool:
    try:
        driver = AsyncGraphDatabase.driver(_URI, auth=(_USER, _PASS))
        async with driver.session() as session:
            await session.run("RETURN 1")
        await driver.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
async def neo4j_available():
    return await _neo4j_reachable()


# ---------------------------------------------------------------------------
# Schema setup / teardown
# ---------------------------------------------------------------------------

TEST_NODES = [
    # (id, label, name, extra_props)
    ("Drug:Metformin",       "Drug",     "Metformin",   {"mw": 129.16, "approved": True}),
    ("Drug:Warfarin",        "Drug",     "Warfarin",    {"mw": 308.33, "approved": True}),
    ("Disease:T2Diabetes",   "Disease",  "Type 2 Diabetes", {"icd10": "E11"}),
    ("Disease:AFib",         "Disease",  "Atrial Fibrillation", {"icd10": "I48"}),
    ("Disease:Hypertension", "Disease",  "Hypertension", {"icd10": "I10"}),
    ("Disease:Cancer",       "Disease",  "Cancer",      {"icd10": "C80"}),
    ("Gene:AMPK",            "Gene",     "AMPK",        {"chromosome": "1p36"}),
    ("Gene:mTOR",            "Gene",     "mTOR",        {"chromosome": "1p36.2"}),
    ("Protein:AKT1",         "Protein",  "AKT1",        {"uniprot": "P31749"}),
    ("Pathway:Glycolysis",   "Pathway",  "Glycolysis",  {"database": "KEGG"}),
]

TEST_EDGES = [
    # (subject_id, predicate, object_id, props)
    ("Drug:Metformin",     "TREATS",          "Disease:T2Diabetes",   {"confidence": 0.97}),
    ("Drug:Metformin",     "INHIBITS",        "Gene:AMPK",            {"confidence": 0.85}),
    ("Drug:Metformin",     "INTERACTS_WITH",  "Drug:Warfarin",        {"confidence": 0.72, "severity": "moderate"}),
    ("Drug:Warfarin",      "TREATS",          "Disease:AFib",         {"confidence": 0.95}),
    ("Gene:AMPK",          "REGULATES",       "Gene:mTOR",            {"confidence": 0.88}),
    ("Gene:AMPK",          "ASSOCIATED_WITH", "Disease:T2Diabetes",   {"confidence": 0.80}),
    ("Gene:mTOR",          "ASSOCIATED_WITH", "Disease:Cancer",       {"confidence": 0.75}),
    ("Disease:T2Diabetes", "COMORBID_WITH",   "Disease:Hypertension", {"confidence": 0.65}),
    ("Protein:AKT1",       "BINDS",           "Gene:AMPK",            {"confidence": 0.90}),
    ("Pathway:Glycolysis", "INVOLVES",        "Gene:AMPK",            {"confidence": 1.0}),
]

_CREATE_GRAPH = """
UNWIND $nodes AS n
CALL apoc.merge.node([n.label], {id: n.id}, n.props) YIELD node
RETURN count(node)
"""


@pytest.fixture(scope="session")
async def raw_driver(neo4j_available):
    if not neo4j_available:
        pytest.skip("Neo4j not reachable")
    driver = AsyncGraphDatabase.driver(_URI, auth=(_USER, _PASS))
    yield driver
    await driver.close()


@pytest.fixture(scope="session", autouse=True)
async def graph(raw_driver):
    """Create test graph, yield, then clean up."""
    async with raw_driver.session() as session:
        # Wipe existing data
        await session.run("MATCH (n) DETACH DELETE n")

        # Create nodes
        for node_id, label, name, extra in TEST_NODES:
            props = {"name": name, **extra}
            await session.run(
                f"CREATE (n:{label} {{id: $id, name: $name}}) SET n += $props",
                id=node_id, name=name, props=props,
            )

        # Create edges
        for subj, pred, obj, props in TEST_EDGES:
            await session.run(
                f"MATCH (a {{id: $s}}), (b {{id: $o}}) "
                f"CREATE (a)-[r:{pred}]->(b) SET r += $props",
                s=subj, o=obj, props=props,
            )

        # Create fulltext index for name search
        await session.run(
            "CREATE FULLTEXT INDEX entity_name_index IF NOT EXISTS "
            "FOR (n:Drug|Disease|Gene|Protein|Pathway) ON EACH [n.name]"
        )

    yield

    async with raw_driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
        await session.run(
            "DROP INDEX entity_name_index IF EXISTS"
        )


@pytest.fixture(scope="session")
async def backend(neo4j_available):
    if not neo4j_available:
        pytest.skip("Neo4j not reachable")
    b = await Neo4jBackend.create(uri=_URI, username=_USER, password=_PASS)
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_entity_types(backend, graph):
    types = await backend.entity_types()
    assert set(types) >= {"Drug", "Disease", "Gene", "Protein", "Pathway"}


async def test_predicates(backend, graph):
    preds = await backend.predicates()
    assert set(preds) >= {
        "TREATS", "INHIBITS", "INTERACTS_WITH", "REGULATES",
        "ASSOCIATED_WITH", "COMORBID_WITH", "BINDS", "INVOLVES",
    }


async def test_search_fulltext(backend, graph):
    """Fulltext index search for 'Diabetes' returns the right node."""
    results = await backend.search_entities("Diabetes")
    ids = {r.id for r in results}
    assert "Disease:T2Diabetes" in ids


async def test_search_fulltext_partial(backend, graph):
    """Prefix match: 'Metfor' finds Metformin."""
    results = await backend.search_entities("Metfor")
    ids = {r.id for r in results}
    assert "Drug:Metformin" in ids


async def test_search_contains_fallback(backend, graph):
    """CONTAINS fallback search works when fulltext index is bypassed."""
    # Temporarily disable index so _search_contains is exercised
    backend._has_fulltext_index = False
    results = await backend.search_entities("Warfarin")
    backend._has_fulltext_index = None  # reset so future tests recheck
    ids = {r.id for r in results}
    assert "Drug:Warfarin" in ids


async def test_edges_from(backend, graph):
    edges = await backend.edges_from("Drug:Metformin")
    preds = {e.predicate for e in edges}
    assert "TREATS" in preds
    assert "INHIBITS" in preds
    assert "INTERACTS_WITH" in preds


async def test_edges_to(backend, graph):
    edges = await backend.edges_to("Gene:AMPK")
    subjects = {e.subject for e in edges}
    assert "Drug:Metformin" in subjects
    assert "Protein:AKT1" in subjects
    assert "Pathway:Glycolysis" in subjects


async def test_get_node(backend, graph):
    node = await backend.get_node("Gene:AMPK")
    assert node.id == "Gene:AMPK"
    assert node.entity_type == "Gene"


async def test_get_node_missing_raises_key_error(backend, graph):
    with pytest.raises(KeyError):
        await backend.get_node("NoSuch:Entity")


async def test_get_nodes_batch(backend, graph):
    nodes = await backend.get_nodes_batch(
        ["Drug:Metformin", "Disease:T2Diabetes", "Gene:AMPK"]
    )
    assert len(nodes) == 3
    by_id = {n.id: n for n in nodes}
    assert by_id["Drug:Metformin"].entity_type == "Drug"
    assert by_id["Disease:T2Diabetes"].entity_type == "Disease"
    assert by_id["Gene:AMPK"].entity_type == "Gene"


async def test_get_nodes_batch_missing_returns_unknown(backend, graph):
    """Batch lookup for a nonexistent ID returns entity_type='Unknown'."""
    nodes = await backend.get_nodes_batch(["Drug:Metformin", "NoSuch:Entity"])
    by_id = {n.id: n for n in nodes}
    assert by_id["Drug:Metformin"].entity_type == "Drug"
    assert by_id["NoSuch:Entity"].entity_type == "Unknown"


async def test_metadata_for_node(backend, graph):
    meta = await backend.metadata_for_node("Drug:Metformin")
    assert meta.get("name") == "Metformin"
    assert meta.get("approved") is True
    assert "mw" in meta
    assert "id" not in meta  # id should be stripped from metadata


async def test_metadata_for_edge(backend, graph):
    edge = Edge(subject="Drug:Metformin", predicate="TREATS", object="Disease:T2Diabetes")
    meta = await backend.metadata_for_edge(edge)
    assert meta.get("confidence") == pytest.approx(0.97)


async def test_metadata_for_edge_with_extra_props(backend, graph):
    edge = Edge(subject="Drug:Metformin", predicate="INTERACTS_WITH", object="Drug:Warfarin")
    meta = await backend.metadata_for_edge(edge)
    assert meta.get("confidence") == pytest.approx(0.72)
    assert meta.get("severity") == "moderate"


async def test_metadata_for_edge_missing_returns_empty(backend, graph):
    edge = Edge(subject="Drug:Metformin", predicate="NONEXISTENT", object="Gene:AMPK")
    meta = await backend.metadata_for_edge(edge)
    assert meta == {}


async def test_comprehensive(backend, graph):
    assert await backend.comprehensive() is True


async def test_full_bfs_via_engine(backend, graph):
    """End-to-end BFS through the engine layer."""
    from bfsql.cache import CachedGraphDb
    from bfsql.engine import bfs_query
    from bfsql.models import BfsQuery

    cached = CachedGraphDb(backend)
    result = await bfs_query(cached, BfsQuery(
        seeds=["Drug:Metformin"],
        max_hops=2,
        node_types=["Disease"],
        predicates=["TREATS"],
    ))

    node_ids = {n.id for n in result.nodes}
    # Hop 1: Metformin -TREATS-> T2Diabetes
    assert "Disease:T2Diabetes" in node_ids
    # Hop 2: AMPK -ASSOCIATED_WITH-> T2Diabetes (already there); T2Diabetes -COMORBID_WITH-> Hypertension
    assert "Disease:Hypertension" in node_ids

    # Disease:T2Diabetes should be a full Node (matched node_types)
    t2d = next(n for n in result.nodes if n.id == "Disease:T2Diabetes")
    assert isinstance(t2d, Node)
    assert t2d.metadata.get("name") == "Type 2 Diabetes"

    # Metformin -TREATS-> T2Diabetes edge should be full EdgeWithMetadata
    treats = next(
        (e for e in result.edges
         if e.predicate == "TREATS" and e.subject == "Drug:Metformin"),
        None,
    )
    assert treats is not None
    assert isinstance(treats, EdgeWithMetadata)
    assert treats.metadata.get("confidence") == pytest.approx(0.97)


async def test_bfs_topology_only(backend, graph):
    """topology_only=True returns stubs only with no metadata fetches."""
    from bfsql.cache import CachedGraphDb
    from bfsql.engine import bfs_query
    from bfsql.models import BfsQuery, EntityStub

    cached = CachedGraphDb(backend)
    result = await bfs_query(cached, BfsQuery(
        seeds=["Gene:AMPK"],
        max_hops=1,
        topology_only=True,
    ))

    # All nodes are stubs in topology_only mode
    for node in result.nodes:
        assert isinstance(node, EntityStub)
        assert not isinstance(node, Node) or node.metadata == {}

    node_ids = {n.id for n in result.nodes}
    assert "Gene:AMPK" in node_ids
    assert "Gene:mTOR" in node_ids
    assert "Disease:T2Diabetes" in node_ids


async def test_schema_summary_populated(backend, graph):
    """schema_summary in BfsResult reflects types and predicates found."""
    from bfsql.cache import CachedGraphDb
    from bfsql.engine import bfs_query
    from bfsql.models import BfsQuery

    cached = CachedGraphDb(backend)
    result = await bfs_query(cached, BfsQuery(
        seeds=["Drug:Metformin"],
        max_hops=1,
        topology_only=True,
    ))

    assert "Drug" in result.schema_summary.entity_types_found
    assert "TREATS" in result.schema_summary.predicates_found
