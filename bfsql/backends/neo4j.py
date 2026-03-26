"""Neo4jBackend: BFS-QL backend for Neo4j graph databases.

Expects nodes to have an ``id`` property (string) used as the canonical
entity ID throughout BFS-QL. The first label on each node is used as
entity_type. Relationship types become predicates.

Full-text search uses a Neo4j FULLTEXT index named ``entity_name_index``
(on the ``name`` property) when available. Falls back to a CONTAINS scan
when the index does not exist.

Usage:
    backend = await Neo4jBackend.create()   # reads NEO4J_URI etc from env
    backend = await Neo4jBackend.create(
        uri="bolt://localhost:7687",
        username="neo4j",
        password="secret",
    )
"""

import os
from typing import Any

from dotenv import load_dotenv
from neo4j import AsyncGraphDatabase

from bfsql.abc import GraphDbInterface
from bfsql.models import Edge, EntityStub, Node

load_dotenv()

_FULLTEXT_INDEX = "entity_name_index"


class Neo4jBackend(GraphDbInterface):
    """BFS-QL backend for a Neo4j database.

    Uses the official neo4j async Python driver. Node identity is the
    ``id`` string property; the first label is the entity type.

    search_entities() tries a FULLTEXT index query first and falls back
    to a CONTAINS scan if the index does not exist.
    """

    def __init__(
        self,
        driver,
        database: str | None = None,
        id_property: str = "id",
    ) -> None:
        self._driver = driver
        self._database = database
        self._id = id_property
        self._has_fulltext_index: bool | None = None  # None = unchecked

    def _id_expr(self, var: str = "n") -> str:
        """Cypher expression that returns the canonical ID for a node variable.

        When id_property is a pipe-separated list (e.g. "title|name"), returns
        a coalesce expression so the first non-null property wins.
        """
        parts = [p.strip() for p in self._id.split("|")]
        if len(parts) == 1:
            return f"{var}.{parts[0]}"
        return "coalesce(" + ", ".join(f"{var}.{p}" for p in parts) + ")"

    def _id_not_null(self, var: str = "n") -> str:
        """Cypher WHERE fragment asserting the ID expression is not null."""
        parts = [p.strip() for p in self._id.split("|")]
        return " OR ".join(f"{var}.{p} IS NOT NULL" for p in parts)

    def _match_by_id(self, var: str, param: str = "id") -> str:
        """Cypher WHERE clause matching a node by its ID value."""
        parts = [p.strip() for p in self._id.split("|")]
        return " OR ".join(f"{var}.{p} = ${param}" for p in parts)

    @classmethod
    async def create(
        cls,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        id_property: str | None = None,
    ) -> "Neo4jBackend":
        """Create a Neo4jBackend.

        Args:
            uri: Bolt URI, e.g. ``bolt://localhost:7687``.
                 Defaults to ``NEO4J_URI`` env var.
            username: Neo4j username. Defaults to ``NEO4J_USERNAME`` env var
                      (falling back to ``"neo4j"``).
            password: Neo4j password. Defaults to ``NEO4J_PASSWORD`` env var.
            database: Neo4j database name. Defaults to ``NEO4J_DATABASE`` env
                      var, or the server default when absent.
            id_property: Node property to use as the canonical entity ID.
                         Defaults to ``NEO4J_ID_PROPERTY`` env var, then
                         ``"id"``. Set to e.g. ``"name"`` or ``"title"`` for
                         graphs that use a different property as their key.
        """
        uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        username = username or os.environ.get("NEO4J_USERNAME", "neo4j")
        password = password or os.environ["NEO4J_PASSWORD"]
        database = database or os.environ.get("NEO4J_DATABASE") or None
        id_property = id_property or os.environ.get("NEO4J_ID_PROPERTY", "id")
        driver = AsyncGraphDatabase.driver(uri, auth=(username, password))
        return cls(driver, database=database, id_property=id_property)

    async def close(self) -> None:
        """Close the driver and release all connections."""
        await self._driver.close()

    def _session(self):
        if self._database:
            return self._driver.session(database=self._database)
        return self._driver.session()

    # ------------------------------------------------------------------
    # Fulltext index probe
    # ------------------------------------------------------------------

    async def _check_fulltext_index(self) -> bool:
        """Return True if entity_name_index exists and is ONLINE."""
        async with self._session() as session:
            result = await session.run(
                "SHOW INDEXES WHERE name = $name AND state = 'ONLINE'",
                name=_FULLTEXT_INDEX,
            )
            rows = await result.data()
        return len(rows) > 0

    # ------------------------------------------------------------------
    # GraphDbInterface implementation
    # ------------------------------------------------------------------

    async def search_entities(self, query: str) -> list[EntityStub]:
        """Search for entities by name.

        Uses the ``entity_name_index`` FULLTEXT index when available
        (appends ``*`` for prefix matching). Falls back to CONTAINS scan.
        """
        if self._has_fulltext_index is None:
            self._has_fulltext_index = await self._check_fulltext_index()

        if self._has_fulltext_index:
            return await self._search_fulltext(query)
        return await self._search_contains(query)

    async def edges_from(self, entity_id: str) -> list[Edge]:
        """Return all outgoing edges from entity_id."""
        id_expr = self._id_expr("m")
        not_null = self._id_not_null("m")
        match_n = self._match_by_id("n")
        async with self._session() as session:
            result = await session.run(
                f"MATCH (n)-[r]->(m) "
                f"WHERE ({match_n}) AND ({not_null}) "
                f"RETURN type(r) AS pred, {id_expr} AS obj",
                id=entity_id,
            )
            rows = await result.data()
        return [
            Edge(subject=entity_id, predicate=row["pred"], object=row["obj"])
            for row in rows
        ]

    async def edges_to(self, entity_id: str) -> list[Edge]:
        """Return all incoming edges to entity_id."""
        id_expr = self._id_expr("n")
        not_null = self._id_not_null("n")
        match_m = self._match_by_id("m")
        async with self._session() as session:
            result = await session.run(
                f"MATCH (n)-[r]->(m) "
                f"WHERE ({match_m}) AND ({not_null}) "
                f"RETURN {id_expr} AS subj, type(r) AS pred",
                id=entity_id,
            )
            rows = await result.data()
        return [
            Edge(subject=row["subj"], predicate=row["pred"], object=entity_id)
            for row in rows
        ]

    async def get_node(self, entity_id: str) -> Node:
        """Return a Node for entity_id.

        Uses the first label as entity_type. Raises KeyError if not found.
        """
        match_n = self._match_by_id("n")
        async with self._session() as session:
            result = await session.run(
                f"MATCH (n) WHERE {match_n} RETURN labels(n) AS labels LIMIT 1",
                id=entity_id,
            )
            rows = await result.data()
        if not rows:
            raise KeyError(entity_id)
        labels = rows[0]["labels"]
        entity_type = labels[0] if labels else "Unknown"
        return Node(id=entity_id, entity_type=entity_type)

    async def get_nodes_batch(self, entity_ids: list[str]) -> list[Node]:
        """Fetch types for multiple entities in a single query."""
        id_expr = self._id_expr("n")
        parts = [p.strip() for p in self._id.split("|")]
        where = " OR ".join(f"n.{p} IN $ids" for p in parts)
        async with self._session() as session:
            result = await session.run(
                f"MATCH (n) WHERE {where} "
                f"RETURN {id_expr} AS id, labels(n) AS labels",
                ids=entity_ids,
            )
            rows = await result.data()
        type_map = {
            row["id"]: (row["labels"][0] if row["labels"] else "Unknown")
            for row in rows
        }
        return [
            Node(id=eid, entity_type=type_map.get(eid, "Unknown")) for eid in entity_ids
        ]

    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        """Return all properties of the node as metadata."""
        match_n = self._match_by_id("n")
        async with self._session() as session:
            result = await session.run(
                f"MATCH (n) WHERE {match_n} RETURN properties(n) AS props LIMIT 1",
                id=entity_id,
            )
            rows = await result.data()
        if not rows:
            return {}
        props = dict(rows[0]["props"])
        # Strip whichever id properties are present
        for p in self._id.split("|"):
            props.pop(p.strip(), None)
        return props

    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        """Return all properties of the relationship as metadata."""
        match_a = self._match_by_id("a", "s")
        match_b = self._match_by_id("b", "o")
        async with self._session() as session:
            result = await session.run(
                f"MATCH (a)-[r]->(b) "
                f"WHERE ({match_a}) AND ({match_b}) AND type(r) = $p "
                "RETURN properties(r) AS props LIMIT 1",
                s=edge.subject,
                o=edge.object,
                p=edge.predicate,
            )
            rows = await result.data()
        if not rows:
            return {}
        return dict(rows[0]["props"])

    async def entity_types(self) -> list[str]:
        """Return all node labels in the database."""
        async with self._session() as session:
            result = await session.run(
                "CALL db.labels() YIELD label RETURN label ORDER BY label"
            )
            rows = await result.data()
        return [row["label"] for row in rows]

    async def predicates(self) -> list[str]:
        """Return all relationship types in the database."""
        async with self._session() as session:
            result = await session.run(
                "CALL db.relationshipTypes() YIELD relationshipType "
                "RETURN relationshipType ORDER BY relationshipType"
            )
            rows = await result.data()
        return [row["relationshipType"] for row in rows]

    async def comprehensive(self) -> bool:
        """Neo4j graphs have a complete, enumerable schema."""
        return True

    async def next_steps(self) -> str:
        return (
            "Call search_entities() to resolve entity names to canonical IDs, "
            "then bfs_query() starting at max_hops=1. Use the entity_types and "
            "predicates lists from describe_schema as valid filter values."
        )

    # ------------------------------------------------------------------
    # Private search helpers
    # ------------------------------------------------------------------

    async def _search_fulltext(self, query: str, limit: int = 20) -> list[EntityStub]:
        """Search using the entity_name_index FULLTEXT index."""
        id_expr = self._id_expr("node")
        escaped = query.replace("\\", "\\\\").replace('"', '\\"')
        ft_query = f"{escaped}*"
        async with self._session() as session:
            result = await session.run(
                f"CALL db.index.fulltext.queryNodes('{_FULLTEXT_INDEX}', $q) "
                f"YIELD node "
                f"RETURN {id_expr} AS id, labels(node)[0] AS label "
                f"LIMIT {limit}",
                q=ft_query,
            )
            rows = await result.data()
        return [
            EntityStub(id=row["id"], entity_type=row["label"] or "Unknown")
            for row in rows
            if row["id"] is not None
        ]

    async def _search_contains(self, query: str, limit: int = 20) -> list[EntityStub]:
        """Fallback search using CONTAINS across all string properties."""
        id_expr = self._id_expr("n")
        not_null = self._id_not_null("n")
        async with self._session() as session:
            result = await session.run(
                f"MATCH (n) WHERE ({not_null}) AND ("
                f"  (n.name IS NOT NULL AND toLower(n.name) CONTAINS toLower($q)) OR "
                f"  (n.title IS NOT NULL AND toLower(n.title) CONTAINS toLower($q))"
                f") "
                f"RETURN {id_expr} AS id, labels(n)[0] AS label "
                f"LIMIT {limit}",
                q=query,
            )
            rows = await result.data()
        return [
            EntityStub(id=row["id"], entity_type=row["label"] or "Unknown")
            for row in rows
            if row["id"] is not None
        ]
