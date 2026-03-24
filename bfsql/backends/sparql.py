"""SparqlBackend: BFS-QL backend for any SPARQL 1.1 endpoint.

Uses aiohttp for async HTTP. URIs are compressed to canonical IDs using a
caller-supplied prefix map (e.g. {"DBpedia": "http://dbpedia.org/resource/"}).

Usage:
    backend = await SparqlBackend.create()           # reads SPARQL_ENDPOINT_URL
    backend = await SparqlBackend.create(endpoint="https://dbpedia.org/sparql",
                                         prefixes={"DBpedia": "http://dbpedia.org/resource/"})
"""

import asyncio
import logging
import os
from typing import Any

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

from bfsql.abc import GraphDbInterface
from bfsql.models import Edge, EntityStub, Node

load_dotenv()

# SPARQL JSON result bindings use these type strings
_URI = "uri"
_BLANK = "bnode"
_LITERAL = "literal"


class SparqlBackend(GraphDbInterface):
    """BFS-QL backend for a SPARQL 1.1 endpoint.

    URIs returned by the endpoint are compressed to short canonical IDs
    using the prefix map. IDs passed in are expanded back to full URIs
    before being inserted into queries.

    entity_types() uses COUNT+GROUP BY ordered by frequency (most common
    types first). predicates() uses a fast index-scan SELECT DISTINCT which
    is not frequency-ordered but avoids timeouts on large endpoints.
    """

    def __init__(
        self,
        endpoint: str,
        prefixes: dict[str, str] | None = None,
        timeout: int = 30,
        edge_limit: int = 500,
        entity_type_limit: int = 30,
        predicate_limit: int = 100,
        safe_distinct: bool = True,
        use_bif_contains: bool = False,
        max_concurrent: int = 5,
        restrict_to_prefixes: bool = False,
        request_delay: float = 0.0,
    ) -> None:
        self._endpoint = endpoint
        self._prefixes = prefixes or {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._edge_limit = edge_limit
        self._entity_type_limit = entity_type_limit
        self._predicate_limit = predicate_limit
        self._safe_distinct = safe_distinct
        self._use_bif_contains = use_bif_contains
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._restrict_to_prefixes = restrict_to_prefixes
        self._request_delay = request_delay
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    async def create(
        cls,
        endpoint: str | None = None,
        prefixes: dict[str, str] | None = None,
        **kwargs,
    ) -> "SparqlBackend":
        """Create a SparqlBackend.

        Args:
            endpoint: SPARQL endpoint URL. Defaults to SPARQL_ENDPOINT_URL env var.
            prefixes: maps short prefix name → URI base, used for ID compression.
            **kwargs: passed through to __init__ (timeout, edge_limit, etc.)
        """
        endpoint = endpoint or os.environ["SPARQL_ENDPOINT_URL"]
        return cls(endpoint=endpoint, prefixes=prefixes or {}, **kwargs)

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # URI ↔ canonical ID helpers
    # ------------------------------------------------------------------

    def _compress(self, uri: str) -> str:
        """Compress a full URI to a canonical ID using the prefix map.

        Returns the URI unchanged if no prefix matches.
        """
        for prefix, base in self._prefixes.items():
            if uri.startswith(base):
                return f"{prefix}:{uri[len(base):]}"
        return uri

    def _expand(self, entity_id: str) -> str:
        """Expand a canonical ID to a full URI using the prefix map.

        Returns the ID unchanged if no prefix matches (treated as bare URI).
        """
        if ":" in entity_id:
            prefix, local = entity_id.split(":", 1)
            if prefix in self._prefixes:
                return self._prefixes[prefix] + local
        return entity_id

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _query(self, sparql: str) -> list[dict[str, Any]]:
        """Execute a SPARQL SELECT query and return the bindings list.

        Requests are throttled by a semaphore (max_concurrent) to avoid
        overwhelming public endpoints with the concurrent BFS fan-out.
        """
        session = self._get_session()
        async with self._semaphore:
            await asyncio.sleep(self._request_delay)
            logger.debug("SPARQL request:\n%s", sparql.strip())
            async with session.get(
                self._endpoint,
                params={
                    "query": sparql,
                    "format": "application/sparql-results+json",
                },
            ) as resp:
                logger.debug("SPARQL response: HTTP %s", resp.status)
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        bindings = data["results"]["bindings"]
        logger.debug("SPARQL result: %d bindings", len(bindings))
        return bindings

    @staticmethod
    def _escape_sparql_string(value: str) -> str:
        """Escape a string for safe interpolation into a SPARQL string literal."""
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _namespace_filter(self, var: str = "?o") -> str:
        """Return a SPARQL FILTER clause fragment restricting var to known prefix bases.

        Returns an empty string when restrict_to_prefixes=False (no filtering).
        When enabled, generates: && (STRSTARTS(STR(?o), "base1") || ...)
        """
        if not self._restrict_to_prefixes or not self._prefixes:
            return ""
        clauses = " || ".join(
            f'STRSTARTS(STR({var}), "{base}")'
            for base in self._prefixes.values()
        )
        return f" && ({clauses})"

    # ------------------------------------------------------------------
    # GraphDbInterface implementation
    # ------------------------------------------------------------------

    async def search_entities(self, query: str) -> list[EntityStub]:
        """Search by rdfs:label.

        Uses Virtuoso bif:contains for fast full-text index lookup when
        self._use_bif_contains is True (default). Falls back to portable
        CONTAINS(LCASE(...)) for non-Virtuoso endpoints, but that variant
        is a full-table scan and will time out on large graphs like DBpedia.
        """
        escaped = self._escape_sparql_string(query)
        if self._use_bif_contains:
            sparql = f"""
SELECT DISTINCT ?entity ?type WHERE {{
    ?entity <http://www.w3.org/2000/01/rdf-schema#label> ?label .
    ?entity a ?type .
    FILTER(bif:contains(?label, "{escaped}"))
}}
LIMIT 20
"""
        else:
            sparql = f"""
SELECT DISTINCT ?entity ?type WHERE {{
    ?entity <http://www.w3.org/2000/01/rdf-schema#label> ?label ;
            a ?type .
    FILTER(CONTAINS(LCASE(STR(?label)), LCASE("{escaped}")))
}}
LIMIT 20
"""
        bindings = await self._query(sparql)
        results = []
        for b in bindings:
            if b.get("entity", {}).get("type") != _URI:
                continue
            if b.get("type", {}).get("type") != _URI:
                continue
            entity_id = self._compress(b["entity"]["value"])
            entity_type = self._compress(b["type"]["value"])
            results.append(EntityStub(id=entity_id, entity_type=entity_type))
        return results

    async def edges_from(self, entity_id: str) -> list[Edge]:
        """Return all outgoing edges from entity_id, excluding literals and blank nodes.

        When namespace_filter is set, only objects whose URI starts with one of
        the known prefix bases are returned, avoiding fan-out into external
        namespaces (e.g. en.wikipedia.org) that inflate the BFS frontier.
        """
        uri = self._expand(entity_id)
        ns_filter = self._namespace_filter()
        sparql = f"""
SELECT ?p ?o WHERE {{
    <{uri}> ?p ?o .
    FILTER(!isLiteral(?o) && !isBlank(?o){ns_filter})
}}
LIMIT {self._edge_limit}
"""
        bindings = await self._query(sparql)
        edges = []
        for b in bindings:
            if b.get("p", {}).get("type") != _URI:
                continue
            if b.get("o", {}).get("type") != _URI:
                continue
            predicate = self._compress(b["p"]["value"])
            obj = self._compress(b["o"]["value"])
            edges.append(Edge(subject=entity_id, predicate=predicate, object=obj))
        return edges

    async def edges_to(self, entity_id: str) -> list[Edge]:
        """Return all incoming edges to entity_id, excluding literals and blank nodes.

        When namespace_filter is set, only subjects in the known prefix namespaces
        are returned.
        """
        uri = self._expand(entity_id)
        ns_filter = self._namespace_filter(var="?s")
        sparql = f"""
SELECT ?s ?p WHERE {{
    ?s ?p <{uri}> .
    FILTER(!isLiteral(?s) && !isBlank(?s){ns_filter})
}}
LIMIT {self._edge_limit}
"""
        bindings = await self._query(sparql)
        edges = []
        for b in bindings:
            if b.get("s", {}).get("type") != _URI:
                continue
            if b.get("p", {}).get("type") != _URI:
                continue
            subject = self._compress(b["s"]["value"])
            predicate = self._compress(b["p"]["value"])
            edges.append(Edge(subject=subject, predicate=predicate, object=entity_id))
        return edges

    async def get_node(self, entity_id: str) -> Node:
        """Return a Node with entity_type from the first rdf:type triple.

        Raises KeyError if the entity has no rdf:type in this endpoint.
        """
        uri = self._expand(entity_id)
        sparql = f"""
SELECT ?type WHERE {{
    <{uri}> a ?type .
}}
LIMIT 1
"""
        bindings = await self._query(sparql)
        if not bindings:
            return Node(id=entity_id, entity_type="owl:Thing")
        entity_type = self._compress(bindings[0]["type"]["value"])
        return Node(id=entity_id, entity_type=entity_type)

    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        """Return all literal-valued properties as a flat dict.

        Keys are the local name of the predicate URI (e.g. rdfs:label -> label).
        Multiple values for the same predicate are stored as a list.
        """
        uri = self._expand(entity_id)
        sparql = f"""
SELECT ?p ?o WHERE {{
    <{uri}> ?p ?o .
    FILTER(isLiteral(?o))
}}
LIMIT {self._edge_limit}
"""
        bindings = await self._query(sparql)
        meta: dict[str, Any] = {}
        for b in bindings:
            if b.get("p", {}).get("type") != _URI:
                continue
            if b.get("o", {}).get("type") != _LITERAL:
                continue
            pred_uri = b["p"]["value"]
            # Use local name: everything after the last # or /
            local = pred_uri.split("#")[-1].split("/")[-1]
            value = b["o"]["value"]
            if local in meta:
                existing = meta[local]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    meta[local] = [existing, value]
            else:
                meta[local] = value
        return meta

    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        """DBpedia carries no per-edge provenance. Returns empty dict."""
        return {}

    async def entity_types(self) -> list[str]:
        """Return entity types ordered by frequency (most common first).

        Returns [] if safe_distinct=False.
        """
        if not self._safe_distinct:
            return []
        sparql = f"""
SELECT ?type (COUNT(?s) AS ?count) WHERE {{
    ?s a ?type .
}}
GROUP BY ?type
ORDER BY DESC(?count)
LIMIT {self._entity_type_limit}
"""
        bindings = await self._query(sparql)
        return [
            self._compress(b["type"]["value"])
            for b in bindings
            if b.get("type", {}).get("type") == _URI
        ]

    async def predicates(self) -> list[str]:
        """Return a sample of predicates via fast index-scan SELECT DISTINCT.

        Not frequency-ordered. Returns [] if safe_distinct=False.
        """
        if not self._safe_distinct:
            return []
        sparql = f"""
SELECT DISTINCT ?pred WHERE {{
    ?s ?pred ?o .
    FILTER(?pred != <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>)
}}
LIMIT {self._predicate_limit}
"""
        bindings = await self._query(sparql)
        return [
            self._compress(b["pred"]["value"])
            for b in bindings
            if b.get("pred", {}).get("type") == _URI
        ]
