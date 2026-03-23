"""Pydantic models for the BFS-QL protocol."""

from typing import Any
from pydantic import BaseModel, Field


class EntityStub(BaseModel, frozen=True):
    """Minimal identity record for an entity -- ID and type only.

    Returned by search_entities and as placeholder nodes in BFS results
    when a node does not match the query's node_types filter.
    """

    id: str = Field(description="Canonical entity ID.")
    entity_type: str = Field(description="Entity type name.")


class Node(BaseModel, frozen=True):
    """Full node record including all available metadata."""

    id: str = Field(description="Canonical entity ID.")
    entity_type: str = Field(description="Entity type name.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="All available metadata for this node.",
    )


class Edge(BaseModel, frozen=True):
    """A directed relationship between two entities."""

    subject: str = Field(description="Canonical ID of the subject entity.")
    predicate: str = Field(description="Predicate name.")
    object: str = Field(description="Canonical ID of the object entity.")


class EdgeWithMetadata(BaseModel, frozen=True):
    """An edge with full provenance and metadata."""

    subject: str = Field(description="Canonical ID of the subject entity.")
    predicate: str = Field(description="Predicate name.")
    object: str = Field(description="Canonical ID of the object entity.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Full edge metadata including provenance.",
    )


class BfsQuery(BaseModel, frozen=True):
    """A BFS-QL query."""

    seeds: list[str] = Field(
        description="One or more canonical entity IDs to expand from."
    )
    max_hops: int = Field(
        description="Maximum graph distance from any seed. Values of 1-3 are typical.",
        ge=1,
        le=5,
    )
    node_types: list[str] = Field(
        default_factory=list,
        description=(
            "Entity type names that receive full metadata. Non-matching nodes appear "
            "as stubs. Omit to receive full data on all nodes."
        ),
    )
    predicates: list[str] = Field(
        default_factory=list,
        description=(
            "Predicate names that receive full metadata including provenance. "
            "Non-matching edges appear as stubs. Omit to receive full data on all edges."
        ),
    )


class BfsResult(BaseModel, frozen=True):
    """The result of a BFS-QL query."""

    seeds: list[str] = Field(description="The seed IDs used in this query.")
    max_hops: int = Field(description="The hop depth used in this query.")
    node_count: int = Field(description="Total number of nodes in the result.")
    edge_count: int = Field(description="Total number of edges in the result.")
    nodes: list[Node | EntityStub] = Field(
        description=(
            "All nodes in the subgraph. Nodes matching node_types (or all nodes when "
            "node_types is empty) are full Node records. Others are EntityStub records."
        )
    )
    edges: list[EdgeWithMetadata | Edge] = Field(
        description=(
            "All edges in the subgraph. Edges matching predicates (or all edges when "
            "predicates is empty) are EdgeWithMetadata records. Others are Edge stubs."
        )
    )


class SchemaDescription(BaseModel, frozen=True):
    """The schema of a graph, returned by describe_schema."""

    graph_description: str = Field(
        description="Human-readable description of the graph and its domain."
    )
    entity_types: list[str] = Field(
        description="All valid entity type names in this graph."
    )
    predicates: list[str] = Field(
        description="All valid predicate names in this graph."
    )
