"""PostgresBackend: BFS-QL backend for kgraph-derived Postgres/pgvector databases.

Expects the kgraph schema:
  - entity table: entity_id, entity_type, name, embedding (JSON float list),
    properties (JSON), status, confidence, synonyms (JSON), source, canonical_url
  - relationship table: subject_id, predicate, object_id, confidence,
    source_documents (JSON), properties (JSON)
  - evidence table: relationship_id (FK), paper_id (FK), evidence_type,
    confidence_score, metadata_ (JSONB)

Entities with status='merged' are excluded from all queries.
"""

import json
import os
from typing import Any

import asyncpg
from dotenv import load_dotenv

from bfsql.abc import GraphDbInterface
from bfsql.models import Edge, EntityStub, Node

load_dotenv()


class PostgresBackend(GraphDbInterface):
    """BFS-QL backend for a kgraph Postgres database.

    Uses asyncpg for async I/O. search_entities uses pgvector cosine
    similarity on the entity embedding column. All other operations are
    straightforward SQL against the entity and relationship tables.

    Usage:
        backend = await PostgresBackend.create()
        # or
        backend = await PostgresBackend.create(dsn="postgresql://...")
    """

    def __init__(self, pool: asyncpg.Pool, embedding_fn) -> None:
        self._pool = pool
        self._embedding_fn = embedding_fn

    @classmethod
    async def create(
        cls,
        dsn: str | None = None,
        embedding_fn=None,
        min_size: int = 2,
        max_size: int = 10,
    ) -> "PostgresBackend":
        """Create a PostgresBackend with a connection pool.

        Args:
            dsn: Postgres connection string. Defaults to DATABASE_URL env var.
            embedding_fn: Async callable (str) -> list[float] for embedding
                queries in search_entities. If None, search falls back to
                trigram/ILIKE name matching.
            min_size: Minimum pool connections.
            max_size: Maximum pool connections.
        """
        dsn = dsn or os.environ["DATABASE_URL"]

        async def _init_conn(conn):
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.set_type_codec(
                "jsonb",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )

        pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            init=_init_conn,
        )
        return cls(pool, embedding_fn)

    async def close(self) -> None:
        """Close the connection pool."""
        await self._pool.close()

    # ------------------------------------------------------------------
    # GraphDbInterface implementation
    # ------------------------------------------------------------------

    async def search_entities(self, query: str) -> list[EntityStub]:
        """Resolve a name to candidate entity stubs.

        If an embedding_fn is configured, uses pgvector cosine similarity
        ranked search. Otherwise falls back to ILIKE name/synonym matching.
        Excludes merged entities.
        """
        if self._embedding_fn is not None:
            return await self._search_by_vector(query)
        return await self._search_by_name(query)

    async def edges_from(self, entity_id: str) -> list[Edge]:
        """Return all outgoing edges from entity_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject_id, predicate, object_id
                FROM relationship
                WHERE subject_id = $1
                """,
                entity_id,
            )
        return [Edge(subject=r["subject_id"], predicate=r["predicate"], object=r["object_id"])
                for r in rows]

    async def edges_to(self, entity_id: str) -> list[Edge]:
        """Return all incoming edges to entity_id."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT subject_id, predicate, object_id
                FROM relationship
                WHERE object_id = $1
                """,
                entity_id,
            )
        return [Edge(subject=r["subject_id"], predicate=r["predicate"], object=r["object_id"])
                for r in rows]

    async def get_node(self, entity_id: str) -> Node:
        """Return the node record for entity_id.

        Raises KeyError if the entity does not exist or is merged.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT entity_id, entity_type, name, status,
                       confidence, source, canonical_url, properties
                FROM entity
                WHERE entity_id = $1
                  AND (status IS NULL OR status != 'merged')
                """,
                entity_id,
            )
        if row is None:
            raise KeyError(entity_id)
        return Node(
            id=row["entity_id"],
            entity_type=row["entity_type"],
            metadata=_node_metadata(row),
        )

    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        """Return full metadata for entity_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT entity_id, entity_type, name, status,
                       confidence, source, canonical_url, synonyms, properties
                FROM entity
                WHERE entity_id = $1
                  AND (status IS NULL OR status != 'merged')
                """,
                entity_id,
            )
        if row is None:
            raise KeyError(entity_id)
        meta = _node_metadata(row)
        if row["synonyms"]:
            meta["synonyms"] = row["synonyms"]
        return meta

    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        """Return full metadata for an edge, including evidence provenance."""
        async with self._pool.acquire() as conn:
            rel_row = await conn.fetchrow(
                """
                SELECT id, confidence, source_documents, properties
                FROM relationship
                WHERE subject_id = $1
                  AND predicate = $2
                  AND object_id = $3
                """,
                edge.subject,
                edge.predicate,
                edge.object,
            )
            if rel_row is None:
                return {}

            # Try evidence table (test schema), then bundle_evidence (kgserver schema)
            rel_key = f"{edge.subject}:{edge.predicate}:{edge.object}"
            evidence_rows = await _fetch_evidence(conn, rel_row["id"], rel_key)

        meta: dict[str, Any] = {}
        if rel_row["confidence"] is not None:
            meta["confidence"] = rel_row["confidence"]
        if rel_row["source_documents"]:
            src = rel_row["source_documents"]
            meta["source_documents"] = json.loads(src) if isinstance(src, str) else src
        if rel_row["properties"]:
            props = rel_row["properties"]
            if isinstance(props, str):
                props = json.loads(props)
            meta.update(props)
        if evidence_rows:
            meta["provenance"] = evidence_rows
        return meta

    async def entity_types(self) -> list[str]:
        """Return all distinct entity types present in the graph."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT entity_type
                FROM entity
                WHERE status IS NULL OR status != 'merged'
                ORDER BY entity_type
                """
            )
        return [r["entity_type"] for r in rows]

    async def predicates(self) -> list[str]:
        """Return all distinct predicate names present in the graph."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT predicate
                FROM relationship
                ORDER BY predicate
                """
            )
        return [r["predicate"] for r in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _search_by_vector(self, query: str, limit: int = 10) -> list[EntityStub]:
        """Search by cosine similarity on the embedding column."""
        embedding: list[float] = await self._embedding_fn(query)
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entity_id, entity_type
                FROM entity
                WHERE embedding IS NOT NULL
                  AND (status IS NULL OR status != 'merged')
                ORDER BY embedding::vector <=> $1::vector
                LIMIT $2
                """,
                embedding_str,
                limit,
            )
        return [EntityStub(id=r["entity_id"], entity_type=r["entity_type"]) for r in rows]

    async def _search_by_name(self, query: str, limit: int = 10) -> list[EntityStub]:
        """Fallback name search using ILIKE on name and synonyms."""
        pattern = f"%{query}%"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entity_id, entity_type
                FROM entity
                WHERE (name ILIKE $1)
                  AND (status IS NULL OR status != 'merged')
                ORDER BY name
                LIMIT $2
                """,
                pattern,
                limit,
            )
        return [EntityStub(id=r["entity_id"], entity_type=r["entity_type"]) for r in rows]


async def _fetch_evidence(conn, rel_id, rel_key: str) -> list[dict[str, Any]]:
    """Return provenance rows from whichever evidence table exists in this schema.

    Supports two schemas:
    - Test schema: ``evidence`` table with relationship_id UUID FK and
      evidence_type / confidence_score / metadata_ columns.
    - kgserver schema: ``bundle_evidence`` table keyed by relationship_key string
      with text_span / confidence / document_id columns.
    """
    try:
        rows = await conn.fetch(
            """
            SELECT evidence_type, confidence_score, metadata_
            FROM evidence
            WHERE relationship_id = $1
            ORDER BY confidence_score DESC NULLS LAST
            """,
            rel_id,
        )
        return [
            {
                "evidence_type": r["evidence_type"],
                "confidence_score": r["confidence_score"],
                **(r["metadata_"] or {}),
            }
            for r in rows
        ]
    except Exception:
        pass

    try:
        rows = await conn.fetch(
            """
            SELECT text_span, confidence, document_id, section
            FROM bundle_evidence
            WHERE relationship_key = $1
            ORDER BY confidence DESC NULLS LAST
            """,
            rel_key,
        )
        return [
            {
                "evidence_type": "text_span",
                "confidence_score": r["confidence"],
                "text": r["text_span"],
                "document_id": r["document_id"],
                "section": r["section"],
            }
            for r in rows
        ]
    except Exception:
        return []


def _node_metadata(row) -> dict[str, Any]:
    """Build a metadata dict from a row, excluding None values."""
    meta: dict[str, Any] = {}
    for key in ("name", "status", "confidence", "source", "canonical_url"):
        val = row[key]
        if val is not None:
            meta[key] = val
    if row["properties"]:
        props = row["properties"]
        if isinstance(props, str):
            props = json.loads(props)
        meta.update(props)
    return meta
