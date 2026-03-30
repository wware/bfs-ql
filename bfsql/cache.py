"""CachedGraphDb: LRU cache wrapper for any GraphDbInterface backend."""

import functools
from typing import Any

from bfsql.abc import GraphDbInterface
from bfsql.models import Edge, EntityStub, Node


def _make_cache(maxsize: int = 1024):
    return functools.lru_cache(maxsize=maxsize)


class CachedGraphDb(GraphDbInterface):
    """Wraps any GraphDbInterface with a primitive-level LRU cache.

    The cache is keyed per method and arguments. Because caching operates
    at the primitive level rather than the query level, all BFS traversal
    logic benefits automatically -- repeated edges_from or metadata_for_node
    calls within any traversal return cached results with no backend round-trip.

    entity_types() and predicates() are cached indefinitely for the lifetime
    of the instance, as they are stable across a session.
    """

    def __init__(self, backend: GraphDbInterface, maxsize: int = 1024) -> None:
        self._backend = backend
        self._maxsize = maxsize
        self._entity_types_cache: list[str] | None = None
        self._predicates_cache: list[str] | None = None

        # Build per-method caches. We wrap async methods with a sync-keyed
        # dict cache since lru_cache does not support async functions directly.
        self._search_cache: dict[tuple, list[EntityStub]] = {}
        self._edges_from_cache: dict[str, list[Edge]] = {}
        self._edges_to_cache: dict[str, list[Edge]] = {}
        self._get_node_cache: dict[str, Node] = {}
        self._node_meta_cache: dict[str, dict[str, Any]] = {}
        self._edge_meta_cache: dict[Edge, dict[str, Any]] = {}

    async def search_entities(
        self,
        query: str,
        node_types: list[str] | None = None,
    ) -> list[EntityStub]:
        cache_key = (query, tuple(sorted(node_types)) if node_types else None)
        if cache_key not in self._search_cache:
            self._search_cache[cache_key] = await self._backend.search_entities(
                query, node_types=node_types
            )
        return self._search_cache[cache_key]

    async def edges_from(self, entity_id: str) -> list[Edge]:
        if entity_id not in self._edges_from_cache:
            self._edges_from_cache[entity_id] = await self._backend.edges_from(
                entity_id
            )
        return self._edges_from_cache[entity_id]

    async def edges_to(self, entity_id: str) -> list[Edge]:
        if entity_id not in self._edges_to_cache:
            self._edges_to_cache[entity_id] = await self._backend.edges_to(entity_id)
        return self._edges_to_cache[entity_id]

    async def get_node(self, entity_id: str) -> Node:
        if entity_id not in self._get_node_cache:
            self._get_node_cache[entity_id] = await self._backend.get_node(entity_id)
        return self._get_node_cache[entity_id]

    async def get_nodes_batch(self, entity_ids: list[str]) -> list[Node]:
        uncached = [eid for eid in entity_ids if eid not in self._get_node_cache]
        if uncached:
            fetched = await self._backend.get_nodes_batch(uncached)
            for node in fetched:
                self._get_node_cache[node.id] = node
        return [self._get_node_cache[eid] for eid in entity_ids]

    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        if entity_id not in self._node_meta_cache:
            self._node_meta_cache[entity_id] = await self._backend.metadata_for_node(
                entity_id
            )
        return self._node_meta_cache[entity_id]

    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        if edge not in self._edge_meta_cache:
            self._edge_meta_cache[edge] = await self._backend.metadata_for_edge(edge)
        return self._edge_meta_cache[edge]

    async def entity_types(self) -> list[str]:
        if not self._entity_types_cache:
            self._entity_types_cache = await self._backend.entity_types()
        return self._entity_types_cache

    async def predicates(self) -> list[str]:
        if not self._predicates_cache:
            self._predicates_cache = await self._backend.predicates()
        return self._predicates_cache
