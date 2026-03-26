# bfs-ql

A Python implementation of the BFS-QL graph query protocol for language
models. Exposes any supported knowledge graph as five MCP tools that an
LLM can use to traverse and reason over structured knowledge -- without
writing SPARQL or Cypher.

## What It Does

BFS-QL turns a knowledge graph into an MCP server with five tools:

- **`describe_schema()`** -- returns entity types, predicate vocabulary, and
  a human-readable description of the graph. Call this first against an
  unfamiliar graph.
- **`search_entities(query)`** -- resolves a natural-language name or alias
  to one or more canonical entity IDs.
- **`bfs_query(seeds, max_hops, node_types, predicates)`** -- breadth-first
  traversal from one or more seed entities. Filters control detail level,
  not which nodes appear: non-matching nodes return as lightweight stubs so
  the LLM always sees an accurate picture of the graph's topology.
- **`describe_entity(id)`** -- retrieves full metadata for a single entity
  by canonical ID. Use this to expand a stub.
- **`intersect_subgraphs(seeds, k)`** -- returns the nodes within k
  undirected hops of ALL given seeds. Useful for finding shared context
  between multiple entities (e.g. common co-stars, shared diseases,
  overlapping pathways).

## Installation

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env
# edit .env to set DATABASE_URL (Postgres backend) or SPARQL_ENDPOINT_URL
```

## Quickstart

```bash
# Start the MCP server in SSE mode against a Postgres/pgvector backend
cd ~/bfs-ql
uv run bfs-ql serve --backend postgres --transport sse --description "Biomedical literature graph"

# Or against a SPARQL endpoint (e.g. DBpedia)
uv run bfs-ql serve --backend sparql --transport sse \
  --endpoint https://dbpedia.org/sparql \
  --prefix DBpedia=http://dbpedia.org/resource/ \
  --description "DBpedia knowledge graph"

# Or against a Neo4j instance
uv run bfs-ql serve --backend neo4j --transport sse \
  --description "My property graph"
```

Register with Claude Code (one-time):

```bash
claude mcp add --transport sse --scope user bfs-ql http://127.0.0.1:8000/sse
```

Start a new Claude Code session -- the five tools are immediately available.
For other MCP clients (Cursor, etc.) point them at `http://127.0.0.1:8000/sse`.

A typical session looks like this:

```
# 1. Orient
describe_schema()
→ { entity_types: ["Disease", "Drug", "Gene", ...], predicates: ["TREATS", ...] }

# 2. Resolve a name to a canonical ID
search_entities("Cushing syndrome")
→ [{ id: "MeSH:D003480", entity_type: "Disease", name: "Cushing Syndrome" }]

# 3. Traverse the neighborhood
bfs_query(
  seeds=["MeSH:D003480"],
  max_hops=2,
  node_types=["Drug", "Gene"],
  predicates=["TREATS", "INHIBITS"]
)
→ BfsResult with full Drug/Gene nodes, stub nodes for everything else,
  full TREATS/INHIBITS edges with provenance, stub edges for other predicates

# 4. Drill into a stub
describe_entity("MeSH:D049970")
→ full metadata for that entity

# 5. Find shared context between two entities
intersect_subgraphs(seeds=["MeSH:D003480", "MeSH:D006965"], k=2)
→ nodes within 2 hops of both seeds
```

## Configuration

Database connection is read from the environment. Copy `.env.example` to
`.env` and set the appropriate variables for your backend:

```
# Postgres backend
DATABASE_URL=postgresql://user:password@localhost:5432/mydb

# Neo4j backend
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=secret
NEO4J_DATABASE=neo4j          # optional, defaults to neo4j
NEO4J_ID_PROPERTY=id          # optional; pipe-separated for fallback: title|name
```

The server reads these at startup via `python-dotenv`.

## Architecture

### The Backend ABC

All graph access goes through `GraphDbInterface`, a deliberately primitive
abstract base class:

```python
class GraphDbInterface(ABC):

    @abstractmethod
    def search_entities(self, query: str) -> list[EntityStub]: ...

    @abstractmethod
    def edges_from(self, entity_id: str) -> list[Edge]: ...

    @abstractmethod
    def edges_to(self, entity_id: str) -> list[Edge]: ...

    @abstractmethod
    def get_node(self, entity_id: str) -> Node: ...

    @abstractmethod
    def metadata_for_node(self, entity_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def metadata_for_edge(self, edge: Edge) -> dict[str, Any]: ...

    @abstractmethod
    def entity_types(self) -> list[str]: ...

    @abstractmethod
    def predicates(self) -> list[str]: ...
```

All BFS-QL intelligence -- traversal, stub/full filtering, multi-seed union,
intersection, context-window management -- is implemented once in the server
layer in terms of these eight primitives. Backend implementors answer only
one question: how do I perform basic graph navigation against this particular
store?

### The Caching Layer

`CachedGraphDb` wraps any backend with an LRU cache keyed on
`(method, args)`. Backends don't implement caching themselves. Because
caching operates at the primitive level, all BFS traversal intelligence
benefits automatically -- repeated `edges_from` or `metadata_for_node`
calls within a multi-hop traversal return cached results immediately.

### The MCP Server

Built on FastMCP. At startup the server calls `db.entity_types()` and
`db.predicates()` and injects the results into the `bfs_query` tool
description dynamically, so the LLM knows valid filter values without
calling `describe_schema()` explicitly. For large schemas this injection
is omitted and `describe_schema()` is the discovery path.

## Supported Backends

### `PostgresBackend` (Postgres + pgvector)

The natural backend for kgraph-derived graphs. `search_entities` uses
vector similarity search (`ORDER BY embedding <=> $1 LIMIT k`).
`entity_types` and `predicates` are cheap `SELECT DISTINCT` queries.
`edges_from` and `edges_to` are straightforward foreign-key joins.

Requires a Postgres database with pgvector and a schema compatible with
kgraph's entity/relationship tables. Set `DATABASE_URL` in `.env`.

### `SparqlBackend` (any SPARQL 1.1 endpoint)

Works against DBpedia, Wikidata, Fuseki, Virtuoso, GraphDB, Neptune, or
any other SPARQL 1.1-compliant endpoint. URIs are compressed to compact
canonical IDs via a caller-supplied prefix map.

Key CLI flags:
- `--endpoint URL` -- SPARQL endpoint URL
- `--prefix NAME=URI` -- register a URI prefix for ID compression (repeatable)
- `--bif-contains` -- use Virtuoso `bif:contains` for full-text search
- `--max-concurrent N` -- limit parallel requests (default unlimited)
- `--request-delay SECS` -- sleep between requests for polite endpoints
- `--exclude-predicate P` -- drop high-fan-out predicates from BFS traversal
- `--node-batch-size N` -- entities per VALUES batch for type resolution (default 10)
- `--restrict-to-prefixes` -- skip entities outside registered prefixes

### `Neo4jBackend` (Neo4j 5)

Async Neo4j backend via the official Python driver. Full-text search uses
a `entity_name_index` FULLTEXT index when present, with a CONTAINS scan
fallback.

The `id_property` setting (env `NEO4J_ID_PROPERTY`) accepts a
pipe-separated fallback list for graphs with heterogeneous key properties:
`"title|name"` generates `coalesce(n.title, n.name)` in Cypher, allowing
a single backend instance to handle graphs like the Neo4j Movies dataset
where movies use `title` and persons use `name`.

## Project Layout

```
bfs-ql/
├── bfsql/
│   ├── __init__.py
│   ├── __main__.py         # CLI entry point: bfs-ql serve
│   ├── abc.py              # GraphDbInterface ABC (eight methods)
│   ├── cache.py            # CachedGraphDb -- primitive-level LRU cache
│   ├── engine.py           # BFS traversal engine + neighborhood_intersection
│   ├── models.py           # Pydantic models (frozen)
│   ├── server.py           # FastMCP server -- five tools
│   └── backends/
│       ├── __init__.py
│       ├── postgres.py     # PostgresBackend (kgraph schema)
│       ├── sparql.py       # SparqlBackend (aiohttp; any SPARQL 1.1 endpoint)
│       └── neo4j.py        # Neo4jBackend (neo4j async driver)
├── tests/
│   ├── conftest.py         # Connectivity checks and skip logic
│   ├── test_engine.py      # Unit tests, mock backend
│   ├── test_server.py      # Unit tests, direct tool calls
│   ├── test_sparql.py      # SparqlBackend unit tests (mocked HTTP)
│   ├── test_sparql_integration.py  # DBpedia integration (disabled)
│   ├── test_postgres.py    # Postgres integration (skip if unavailable)
│   └── test_neo4j.py       # Neo4j integration (skip if unavailable)
├── .env.example
├── pyproject.toml
└── README.md
```

## Design Notes

**Topology is always complete.** Filters on `node_types` and `predicates`
control the detail level of nodes and edges, not which ones appear. A stub
node is not a missing node -- it is a navigational handle the LLM can follow
up on with `describe_entity` or a new `bfs_query`.

**Stubs are cheap.** A stub node carries only `id` and `entity_type`. A stub
edge carries only `subject`, `predicate`, and `object`. The LLM sees the
full topology of the subgraph without paying the context cost of full
metadata everywhere.

**`topology_only=True` is cheap.** Skips all metadata fetches in the engine
entirely -- not just stripped at the server layer. Use as a first move on
large or unfamiliar graphs (~14K chars vs ~110K for full metadata on a
2-hop traversal).

**Primitive-level caching.** The cache wraps the backend, not the query.
This means any two BFS traversals that touch the same node share cached
primitive results, even if they were issued with different filters or seeds.

**Async by default.** Backend methods are async. The BFS engine issues
`edges_from` and `metadata_for_node` calls concurrently during expansion,
which matters for I/O-bound backends (Postgres, SPARQL endpoints, Neo4j).

**Intersection is backend-agnostic.** `intersect_subgraphs` is implemented
purely in the engine layer against the eight-method ABC. It works with all
backends with no backend changes required.
