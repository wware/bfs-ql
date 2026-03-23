"""Integration tests for PostgresBackend against a real Postgres instance.

Requires DATABASE_URL in the environment (or .env file) pointing to a
Postgres instance. A temporary schema is created for each test session
and dropped on teardown.

Run with:
    uv run pytest tests/test_postgres.py -v
"""

import json
import os
import uuid

import asyncpg
import pytest
from dotenv import load_dotenv

from bfsql.backends.postgres import PostgresBackend
from bfsql.models import Edge, EdgeWithMetadata, Node

load_dotenv()


# ---------------------------------------------------------------------------
# Schema and fixtures
# ---------------------------------------------------------------------------

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS entity (
    entity_id       TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    name            TEXT,
    status          TEXT,
    confidence      FLOAT,
    source          TEXT,
    canonical_url   TEXT,
    synonyms        JSON DEFAULT '[]',
    properties      JSON DEFAULT '{}',
    embedding       JSON
);

CREATE TABLE IF NOT EXISTS relationship (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id      TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object_id       TEXT NOT NULL,
    confidence      FLOAT,
    source_documents JSON DEFAULT '[]',
    properties      JSON DEFAULT '{}',
    UNIQUE (subject_id, predicate, object_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id              SERIAL PRIMARY KEY,
    relationship_id UUID NOT NULL REFERENCES relationship(id) ON DELETE CASCADE,
    paper_id        INTEGER,
    evidence_type   TEXT NOT NULL,
    confidence_score FLOAT,
    metadata_       JSONB DEFAULT '{}'
);
"""

DROP_TABLES = """
DROP TABLE IF EXISTS evidence;
DROP TABLE IF EXISTS relationship;
DROP TABLE IF EXISTS entity;
"""

# Small test graph matching the mock backend graph in test_engine.py:
#   Drug:A --TREATS--> Disease:B
#   Drug:A --INHIBITS--> Gene:C
#   Gene:C --ASSOCIATED_WITH--> Disease:B
#   Disease:B --COMORBID_WITH--> Disease:D

TEST_ENTITIES = [
    ("Drug:A",    "Drug",    "DrugA",    None),
    ("Disease:B", "Disease", "DiseaseB", None),
    ("Gene:C",    "Gene",    "GeneC",    None),
    ("Disease:D", "Disease", "DiseaseD", None),
    ("Drug:Z",    "Drug",    "Merged",   "merged"),   # should be invisible
]

TEST_RELATIONSHIPS = [
    ("Drug:A",    "TREATS",          "Disease:B", 0.95),
    ("Drug:A",    "INHIBITS",        "Gene:C",    0.80),
    ("Gene:C",    "ASSOCIATED_WITH", "Disease:B", 0.70),
    ("Disease:B", "COMORBID_WITH",   "Disease:D", 0.60),
]


@pytest.fixture(scope="session")
async def conn():
    """Raw asyncpg connection for schema setup/teardown."""
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    yield c
    await c.close()


@pytest.fixture(scope="session", autouse=True)
async def schema(conn):
    """Create tables, insert test data, drop on teardown."""
    await conn.execute(DROP_TABLES)
    await conn.execute(CREATE_TABLES)

    for entity_id, entity_type, name, status in TEST_ENTITIES:
        await conn.execute(
            """
            INSERT INTO entity (entity_id, entity_type, name, status, properties)
            VALUES ($1, $2, $3, $4, $5)
            """,
            entity_id, entity_type, name, status, json.dumps({"test": True}),
        )

    rel_ids = {}
    for subject_id, predicate, object_id, confidence in TEST_RELATIONSHIPS:
        row = await conn.fetchrow(
            """
            INSERT INTO relationship (subject_id, predicate, object_id, confidence,
                                      source_documents)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            subject_id, predicate, object_id, confidence,
            json.dumps(["PMC001"]),
        )
        rel_ids[(subject_id, predicate, object_id)] = row["id"]

    # Add evidence for the TREATS relationship
    treats_id = rel_ids[("Drug:A", "TREATS", "Disease:B")]
    await conn.execute(
        """
        INSERT INTO evidence (relationship_id, evidence_type, confidence_score, metadata_)
        VALUES ($1, $2, $3, $4)
        """,
        treats_id, "clinical_trial", 0.95,
        json.dumps({"source_doc": "PMC001", "section": "Results"}),
    )

    yield

    await conn.execute(DROP_TABLES)


@pytest.fixture(scope="session")
async def backend():
    b = await PostgresBackend.create()
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_entity_types(backend):
    types = await backend.entity_types()
    assert set(types) == {"Drug", "Disease", "Gene"}


async def test_predicates(backend):
    preds = await backend.predicates()
    assert set(preds) == {"TREATS", "INHIBITS", "ASSOCIATED_WITH", "COMORBID_WITH"}


async def test_search_by_name(backend):
    results = await backend.search_entities("DiseaseB")
    ids = {r.id for r in results}
    assert "Disease:B" in ids


async def test_search_excludes_merged(backend):
    results = await backend.search_entities("Merged")
    ids = {r.id for r in results}
    assert "Drug:Z" not in ids


async def test_edges_from(backend):
    edges = await backend.edges_from("Drug:A")
    predicates = {e.predicate for e in edges}
    assert "TREATS" in predicates
    assert "INHIBITS" in predicates


async def test_edges_to(backend):
    edges = await backend.edges_to("Disease:B")
    subjects = {e.subject for e in edges}
    assert "Drug:A" in subjects
    assert "Gene:C" in subjects


async def test_get_node(backend):
    node = await backend.get_node("Disease:B")
    assert node.id == "Disease:B"
    assert node.entity_type == "Disease"


async def test_get_node_excludes_merged(backend):
    with pytest.raises(KeyError):
        await backend.get_node("Drug:Z")


async def test_metadata_for_node(backend):
    meta = await backend.metadata_for_node("Drug:A")
    assert meta.get("name") == "DrugA"
    assert meta.get("test") is True  # from properties JSON


async def test_metadata_for_edge_includes_provenance(backend):
    edge = Edge(subject="Drug:A", predicate="TREATS", object="Disease:B")
    meta = await backend.metadata_for_edge(edge)
    assert meta.get("confidence") == pytest.approx(0.95)
    assert "provenance" in meta
    assert len(meta["provenance"]) >= 1
    assert meta["provenance"][0]["evidence_type"] == "clinical_trial"


async def test_metadata_for_edge_no_evidence(backend):
    edge = Edge(subject="Drug:A", predicate="INHIBITS", object="Gene:C")
    meta = await backend.metadata_for_edge(edge)
    assert meta.get("confidence") == pytest.approx(0.80)
    # No evidence rows for this edge -- provenance key absent or empty
    assert not meta.get("provenance")


async def test_full_bfs_query_via_engine(backend):
    """Smoke test: run a real BFS query end-to-end through the engine."""
    from bfsql.cache import CachedGraphDb
    from bfsql.engine import bfs_query
    from bfsql.models import BfsQuery

    cached = CachedGraphDb(backend)
    result = await bfs_query(cached, BfsQuery(
        seeds=["Drug:A"],
        max_hops=2,
        node_types=["Disease"],
        predicates=["TREATS"],
    ))

    node_ids = {n.id for n in result.nodes}
    assert "Drug:A" in node_ids
    assert "Disease:B" in node_ids
    assert "Disease:D" in node_ids

    # Disease:B should be a full node
    disease_b = next(n for n in result.nodes if n.id == "Disease:B")
    assert isinstance(disease_b, Node)
    assert disease_b.metadata

    # TREATS edge should be full with provenance
    treats = next(
        (e for e in result.edges if e.predicate == "TREATS"),
        None,
    )
    assert treats is not None
    assert isinstance(treats, EdgeWithMetadata)
    assert "provenance" in treats.metadata
