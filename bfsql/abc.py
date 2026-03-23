"""GraphDbInterface: the abstract base class all backends must implement."""

from abc import ABC, abstractmethod
from typing import Any

from bfsql.models import Edge, EntityStub, Node


class GraphDbInterface(ABC):
    """Primitive graph navigation interface.

    All BFS-QL intelligence -- traversal, stub/full filtering, multi-seed
    union, caching -- is implemented in the server layer in terms of these
    eight methods. Backend implementors answer only one question: how do I
    perform basic graph navigation against this particular store?

    All methods are async. I/O-bound backends (Postgres, SPARQL, Neo4j)
    benefit from concurrent calls during BFS expansion.
    """

    @abstractmethod
    async def search_entities(self, query: str) -> list[EntityStub]:
        """Resolve a natural-language name or alias to candidate entity stubs.

        Results should be ranked by relevance -- cosine similarity for
        vector backends, text match score for full-text index backends.
        The caller inspects results and chooses seeds; ambiguous names
        commonly return multiple candidates.
        """

    @abstractmethod
    async def edges_from(self, entity_id: str) -> list[Edge]:
        """Return all outgoing edges from the given entity."""

    @abstractmethod
    async def edges_to(self, entity_id: str) -> list[Edge]:
        """Return all incoming edges to the given entity."""

    @abstractmethod
    async def get_node(self, entity_id: str) -> Node:
        """Return the node record for the given entity ID.

        Raises KeyError if the entity does not exist.
        """

    @abstractmethod
    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        """Return all available metadata for the given entity.

        The returned dict is merged into the Node record when constructing
        full (non-stub) node results. Keys and value types are
        backend-defined and graph-specific.
        """

    @abstractmethod
    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        """Return full metadata for the given edge, including provenance.

        The returned dict is merged into the EdgeWithMetadata record when
        constructing full (non-stub) edge results. Should include at minimum
        a confidence score and provenance records where available.
        """

    @abstractmethod
    async def entity_types(self) -> list[str]:
        """Return the list of valid entity type names in this graph.

        Results are stable for the lifetime of a session. The server caches
        this call; backends need not cache it themselves.
        """

    @abstractmethod
    async def predicates(self) -> list[str]:
        """Return the list of valid predicate names in this graph.

        Results are stable for the lifetime of a session. The server caches
        this call; backends need not cache it themselves.
        """
