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

    async def search_entities(
        self,
        query: str,
        node_types: list[str] | None = None,
    ) -> list[EntityStub]:
        """Resolve a name to candidate entity stubs.

        Search strategy (in order):
        1. Exact name match (case-insensitive) — pinned to top of results.
        2. Vector similarity search (if embedding_fn configured) or ILIKE
           fallback — fetches an oversized candidate pool.
        3. Rerank candidates by a composite score combining vector similarity,
           token coverage ratio (penalises long names that merely contain the
           query), and a type weight (papers are de-prioritised).
        Excludes merged entities.

        Args:
            query: A specific entity name or partial name.
            node_types: If provided, restrict results to these entity types.
        """
        exact = await self._search_exact(query, node_types=node_types)
        exact_ids = {e.id for e in exact}

        if self._embedding_fn is not None:
            candidates = await self._search_by_vector(query, limit=30, node_types=node_types)
        else:
            candidates = await self._search_by_name(query, limit=30, node_types=node_types)

        # Rerank candidates, excluding anything already in exact results.
        reranked = _rerank(query, [c for c in candidates if c.id not in exact_ids])
        combined = exact + reranked
        return combined[:10]

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
        return [
            Edge(
                subject=r["subject_id"], predicate=r["predicate"], object=r["object_id"]
            )
            for r in rows
        ]

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
        return [
            Edge(
                subject=r["subject_id"], predicate=r["predicate"], object=r["object_id"]
            )
            for r in rows
        ]

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
            rows = await conn.fetch("""
                SELECT DISTINCT entity_type
                FROM entity
                WHERE status IS NULL OR status != 'merged'
                ORDER BY entity_type
                """)
        return [r["entity_type"] for r in rows]

    async def predicates(self) -> list[str]:
        """Return all distinct predicate names present in the graph."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT predicate
                FROM relationship
                ORDER BY predicate
                """)
        return [r["predicate"] for r in rows]

    async def comprehensive(self) -> bool:
        """Postgres graphs have a complete, enumerable schema."""
        return True

    async def next_steps(self) -> str:
        return (
            "Call search_entities() to resolve entity names to canonical IDs, "
            "then bfs_query() starting at max_hops=1. Use the entity_types and "
            "predicates lists from describe_schema as valid filter values."
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _search_exact(
        self, query: str, node_types: list[str] | None = None
    ) -> list[EntityStub]:
        """Return entities whose name is an exact case-insensitive match."""
        type_filter = "AND entity_type = ANY($2::text[])" if node_types else ""
        sql = f"""
            SELECT entity_id, entity_type, name
            FROM entity
            WHERE lower(name) = lower($1)
              AND (status IS NULL OR status != 'merged')
              {type_filter}
            ORDER BY entity_type
            """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, query, *([ node_types] if node_types else []))
        return [
            EntityStub(id=r["entity_id"], entity_type=r["entity_type"], name=r["name"])
            for r in rows
        ]

    async def _search_by_vector(
        self, query: str, limit: int = 10, node_types: list[str] | None = None
    ) -> list[EntityStub]:
        """Search by cosine similarity on the embedding column."""
        embedding: list[float] = await self._embedding_fn(query)
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
        type_filter = "AND entity_type = ANY($3::text[])" if node_types else ""
        sql = f"""
            SELECT entity_id, entity_type, name,
                   1.0 - (embedding::vector <=> $1::vector) AS similarity
            FROM entity
            WHERE embedding IS NOT NULL
              AND (status IS NULL OR status != 'merged')
              {type_filter}
            ORDER BY embedding::vector <=> $1::vector
            LIMIT $2
            """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                sql, embedding_str, limit, *([ node_types] if node_types else [])
            )
        return [
            EntityStub(
                id=r["entity_id"],
                entity_type=r["entity_type"],
                name=r["name"],
                score=float(r["similarity"]),
            )
            for r in rows
        ]

    async def _search_by_name(
        self, query: str, limit: int = 10, node_types: list[str] | None = None
    ) -> list[EntityStub]:
        """Fallback name search using ILIKE on name."""
        pattern = f"%{query}%"
        type_filter = "AND entity_type = ANY($3::text[])" if node_types else ""
        sql = f"""
            SELECT entity_id, entity_type, name
            FROM entity
            WHERE name ILIKE $1
              AND (status IS NULL OR status != 'merged')
              {type_filter}
            ORDER BY name
            LIMIT $2
            """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                sql, pattern, limit, *([ node_types] if node_types else [])
            )
        return [
            EntityStub(id=r["entity_id"], entity_type=r["entity_type"], name=r["name"])
            for r in rows
        ]


# Lower weight = deprioritised in search results. Papers are noisy for
# concept searches so they receive the lowest weight.
_TYPE_WEIGHT: dict[str, float] = {
    "disease": 1.0,
    "gene": 1.0,
    "protein": 1.0,
    "drug": 1.0,
    "pathway": 1.0,
    "biologicalprocess": 0.9,
    "mutation": 0.9,
    "biomarker": 0.9,
    "symptom": 0.9,
    "procedure": 0.8,
    "anatomicalstructure": 0.8,
    "hormone": 0.8,
    "enzyme": 0.8,
    "paper": 0.3,
}
_DEFAULT_TYPE_WEIGHT = 0.7


def _token_coverage(query: str, name: str) -> float:
    """Fraction of name tokens covered by query tokens.

    Returns 1.0 for an exact token match, approaching 0 as name grows
    relative to query. Penalises long names that merely contain the query.
    """
    if not name:
        return 0.0
    q_tokens = set(query.lower().split())
    n_tokens = set(name.lower().split())
    if not n_tokens:
        return 0.0
    return len(q_tokens & n_tokens) / len(n_tokens)


_ALPHA = 0.5  # weight for vector similarity
_BETA = 0.5   # weight for coverage × type_weight


def _rerank(query: str, candidates: list["EntityStub"]) -> list["EntityStub"]:
    """Rerank candidates by a composite of vector similarity, token coverage,
    and entity type weight.

    score = alpha * vec_sim + beta * (coverage * type_weight)

    When vec_sim is unavailable (name-only search), falls back to pure
    coverage × type_weight (equivalent to alpha=0, beta=1).
    """

    def score(stub: "EntityStub") -> float:
        coverage = _token_coverage(query, stub.name or "")
        type_weight = _TYPE_WEIGHT.get(stub.entity_type, _DEFAULT_TYPE_WEIGHT)
        lexical = coverage * type_weight
        if stub.score is not None:
            return _ALPHA * stub.score + _BETA * lexical
        return lexical

    return sorted(candidates, key=score, reverse=True)


async def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the current schema without triggering a postgres error."""
    row = await conn.fetchrow(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_name = $1
        """,
        table_name,
    )
    return row is not None


async def _fetch_evidence(conn, rel_id, rel_key: str) -> list[dict[str, Any]]:
    """Return provenance rows from whichever evidence table exists in this schema.

    Supports two schemas:
    - Test schema: ``evidence`` table with relationship_id UUID FK and
      evidence_type / confidence_score / metadata_ columns.
    - kgserver schema: ``bundle_evidence`` table keyed by relationship_key string
      with text_span / confidence / document_id columns.
    """
    if await _table_exists(conn, "evidence"):
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
