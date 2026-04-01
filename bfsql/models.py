"""Pydantic models for the BFS-QL protocol."""

from typing import Any
from pydantic import BaseModel, Field

# Usage guidance included in every describe_schema response so that any LLM
# client automatically learns how to use the BFS-QL tools correctly without
# requiring per-client prompt engineering.
TOOL_USAGE_NOTES = """\
## BFS-QL Tool Usage Guide

### Typical workflow
1. `describe_schema()` — always call first to learn entity types, predicates,
   and any backend-specific workflow instructions in `next_steps`.
2. `search_entities(query)` — resolve a named entity to its canonical ID.
   Call separately for each named entity you need.
3. `bfs_query(seeds, max_hops, ...)` — traverse the graph from known IDs.
4. `describe_entity(id)` — expand a stub node that needs more detail.
5. `intersect_subgraphs(queries)` — find nodes reachable from multiple seeds;
   useful for "how are X and Y connected?" questions.

### bfs_query parameters

| Parameter | Default | Purpose |
|---|---|---|
| seeds | required | List of canonical entity IDs to start from |
| max_hops | required | Graph distance (1=direct neighbors, 2=indirect). Start with 1; only go higher if needed |
| node_types | None | Entity types that get **full metadata**; all others become topology-only stubs. **Always set this when the user asks about a specific type** (e.g. `node_types=["gene"]` when asked about genes) |
| exclude_node_types | None | Entity types to **remove entirely** (not even stubs). Use to suppress high-volume noise types like `paper` or `author` |
| predicates | None | Predicate names that get full metadata; others become stubs |
| topology_only | False | Return only IDs and types for all nodes/edges — fast structural survey |
| min_mentions | 1 | Minimum corpus-wide mention count; lower-confidence nodes are omitted |
| limit / offset | None / 0 | Pagination; node_count/edge_count always reflect the full result size |

### Critical rules
- **Use `node_types` filtering** whenever the question targets a specific entity type.
  Without it, results may contain hundreds of nodes and the entities of interest are
  buried as stubs. Example: "what genes are linked to X?" → `node_types=["gene"]`.
- **Do not fabricate results.** If a query returns empty results or no nodes of the
  requested type, say so honestly. Try `node_types` filtering, then increase `max_hops`,
  then try `intersect_subgraphs` — before concluding the data isn't there.
- **Topology is always preserved.** Non-matching nodes/edges become stubs (id + type),
  not omissions. You can see the full graph structure and selectively expand stubs.
- **exclude_node_types is aggressive** — it removes nodes and all their edges entirely,
  unlike node_types which merely downgrades them to stubs.
"""


class EntityStub(BaseModel, frozen=True):
    """Minimal identity record for an entity -- ID and type only.

    Returned by search_entities and as placeholder nodes in BFS results
    when a node does not match the query's node_types filter.
    """

    id: str = Field(description="Canonical entity ID.")
    entity_type: str = Field(description="Entity type name.")
    name: str | None = Field(
        default=None,
        description="Entity name, populated in search_entities results for reranking. "
        "None in BFS stub nodes.",
    )
    score: float | None = Field(
        default=None,
        description="Vector similarity score (0–1, higher=better), populated in "
        "search_entities results when an embedding_fn is configured. None otherwise.",
    )


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
    exclude_node_types: list[str] = Field(
        default_factory=list,
        description=(
            "Entity type names to remove entirely from the result. Excluded nodes and "
            "any edges whose both endpoints are excluded are omitted. Use this to "
            "suppress high-volume types like 'paper' or 'author' that dominate large "
            "traversals without adding conceptual value."
        ),
    )
    predicates: list[str] = Field(
        default_factory=list,
        description=(
            "Predicate names that receive full metadata including provenance. "
            "Non-matching edges appear as stubs. Omit to receive full data on all edges."
        ),
    )
    min_mentions: int = Field(
        default=1,
        ge=1,
        description=(
            "Minimum number of corpus-wide mentions required for a node to appear in "
            "the result. Nodes with fewer mentions (and all edges touching them) are "
            "omitted. Use this to suppress low-confidence provisional entities that "
            "appear in only one or two source documents. Default 1 (no filtering). "
            "Requires the backend to populate a 'total_mentions' field in node "
            "metadata; nodes without this field are always included."
        ),
    )
    topology_only: bool = Field(
        default=False,
        description=(
            "If True, skip all metadata fetches. All nodes and edges are returned "
            "as stubs (IDs and types only). Much faster for structural exploration."
        ),
    )


class SchemaSummary(BaseModel, frozen=True):
    """A summary of entity types and predicates found in a BFS result subgraph."""

    entity_types_found: list[str] = Field(
        description="Entity types present in this subgraph."
    )
    predicates_found: list[str] = Field(
        description="Predicates present in this subgraph."
    )


class BfsResult(BaseModel, frozen=True):
    """The result of a BFS-QL query."""

    seeds: list[str] = Field(description="The seed IDs used in this query.")
    max_hops: int = Field(description="The hop depth used in this query.")
    node_count: int = Field(
        description="Total number of nodes in the full result before pagination."
    )
    edge_count: int = Field(
        description="Total number of edges in the full result before pagination."
    )
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
    schema_summary: SchemaSummary = Field(
        description=(
            "Entity types and predicates actually present in this subgraph. "
            "Always populated regardless of filters. Use this to discover valid "
            "node_types and predicates values for follow-up queries, especially "
            "when describe_schema returns comprehensive=False."
        )
    )


class IntersectionQuery(BaseModel, frozen=True):
    """Parameters for a subgraph intersection query."""

    seeds: list[str] = Field(
        description="Two or more canonical entity IDs to intersect from."
    )
    k: int = Field(
        description="Hop radius. Nodes within this many undirected hops of every seed are included.",
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
    exclude_node_types: list[str] = Field(
        default_factory=list,
        description=(
            "Entity type names to remove entirely from the result. Excluded nodes and "
            "any edges whose both endpoints are excluded are omitted."
        ),
    )
    predicates: list[str] = Field(
        default_factory=list,
        description=(
            "Predicate names that receive full metadata including provenance. "
            "Non-matching edges appear as stubs. Omit to receive full data on all edges."
        ),
    )
    min_mentions: int = Field(
        default=1,
        ge=1,
        description=(
            "Minimum number of corpus-wide mentions required for a node to appear in "
            "the result. Nodes with fewer mentions (and all edges touching them) are "
            "omitted. Nodes without a 'total_mentions' field are always included."
        ),
    )
    topology_only: bool = Field(
        default=False,
        description=(
            "If True, skip all metadata fetches. All nodes and edges are returned "
            "as stubs (IDs and types only). Much faster for structural exploration."
        ),
    )


class IntersectionResult(BaseModel, frozen=True):
    """Result of a subgraph intersection query."""

    seeds: list[str] = Field(description="The seed IDs used in this query.")
    k: int = Field(description="The hop radius used.")
    node_count: int = Field(description="Number of nodes in the intersection.")
    edge_count: int = Field(
        description="Number of edges in the induced subgraph on the intersection nodes."
    )
    nodes: list[Node | EntityStub] = Field(
        description=(
            "Nodes within k undirected hops of every seed. "
            "Nodes matching node_types (or all nodes when node_types is empty) are full "
            "Node records. Others are EntityStub records. "
            "Includes the seeds themselves if they are mutually reachable."
        )
    )
    edges: list[EdgeWithMetadata | Edge] = Field(
        description=(
            "Induced subgraph edges: edges whose both endpoints are in the intersection. "
            "Edges matching predicates (or all edges when predicates is empty) are "
            "EdgeWithMetadata records. Others are Edge stubs."
        )
    )
    schema_summary: SchemaSummary = Field(
        description=(
            "Entity types and predicates actually present in this intersection subgraph. "
            "Always populated regardless of filters."
        )
    )


class SchemaDescription(BaseModel, frozen=True):
    """The schema of a graph, returned by describe_schema."""

    graph_description: str = Field(
        description="Human-readable description of the graph and its domain."
    )
    comprehensive: bool = Field(
        description=(
            "True if entity_types and predicates are complete and exhaustive. "
            "False if the graph is too large or open-world to enumerate fully -- "
            "treat the lists as a sample only and rely on schema_summary in "
            "bfs_query results to discover types and predicates."
        )
    )
    entity_types: list[str] = Field(
        description="Valid entity type names for use in bfs_query node_types filter."
    )
    predicates: list[str] = Field(
        description="Valid predicate names for use in bfs_query predicates filter."
    )
    next_steps: str = Field(
        description=(
            "Backend-authored instructions for how to proceed after describe_schema. "
            "Follow these in preference to any generic default workflow."
        )
    )
    tool_usage_notes: str = Field(
        default=TOOL_USAGE_NOTES,
        description=(
            "Reference guide for all BFS-QL tools: parameter meanings, filtering "
            "rules, and critical usage patterns. Read this before making tool calls."
        ),
    )
