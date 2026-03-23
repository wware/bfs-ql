# bfs-ql

A Python implementation of the BFS-QL graph query protocol for language
models. Exposes any supported knowledge graph as four MCP tools that an
LLM can use to traverse and reason over structured knowledge -- without
writing SPARQL or Cypher.

## What It Does

BFS-QL turns a knowledge graph into an MCP server with four tools:

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

## Installation

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env
# edit .env to set DATABASE_URL
```

## Quickstart

```bash
# Start the MCP server against a Postgres/pgvector backend
uv run bfs-ql serve --backend postgres --description "Biomedical literature graph"

# Paste the printed MCP URL into your MCP client (Claude, Cursor, etc.)
```

Once connected, the LLM has four tools. A typical session looks like this:

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
```

## Configuration

Database connection is read from the environment. Copy `.env.example` to
`.env` and set:

```
DATABASE_URL=postgresql://user:password@localhost:5432/mydb
```

The server reads this at startup via `python-dotenv`.

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
context-window management -- is implemented once in the server layer in terms
of these eight primitives. Backend implementors answer only one question: how
do I perform basic graph navigation against this particular store?

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

### Future Backends

- `SparqlBackend` -- any SPARQL 1.1 endpoint (DBpedia, Wikidata, Fuseki,
  Virtuoso, GraphDB, Neptune)
- `Neo4jBackend` -- property graphs via the official Neo4j Python driver

## Implementation Status

### Phase 1 -- Core abstractions ✓

- [x] `bfsql/models.py` -- Pydantic models: `EntityStub`, `Node`, `Edge`,
  `BfsQuery`, `BfsResult`, `SchemaDescription`
- [x] `bfsql/abc.py` -- `GraphDbInterface` ABC
- [x] `bfsql/cache.py` -- `CachedGraphDb` wrapper
- [x] `bfsql/engine.py` -- BFS traversal engine: multi-seed expansion,
  stub/full filtering, result assembly
- [x] 8 engine tests against a mock backend

### Phase 2 -- Postgres backend ✓

- [x] `bfsql/backends/postgres.py` -- `PostgresBackend` implementation
- [x] `.env.example` with `DATABASE_URL` placeholder
- [x] 12 integration tests (skip cleanly when Postgres unavailable)

### Phase 3 -- MCP server ✓

- [x] `bfsql/server.py` -- FastMCP server with four tools and schema injection
- [x] `bfsql/__main__.py` -- CLI entry point
- [x] 7 server tests via direct tool function calls

### Phase 4 -- Packaging and docs ✓

- [x] `pyproject.toml` with metadata, classifiers, URLs
- [x] `.env.example`
- [x] Docstrings on all public interfaces
- [x] Usage examples in this README

### Roadmap

- [ ] `SparqlBackend` -- any SPARQL 1.1 endpoint
- [ ] `Neo4jBackend` -- property graphs via Neo4j Python driver
- [ ] Migration script / schema docs for the Postgres backend
- [ ] `bfs-ql serve --transport sse` with a printed MCP URL

## Project Layout

```
bfs-ql/
├── bfsql/
│   ├── __init__.py
│   ├── __main__.py         # CLI entry point: bfs-ql serve
│   ├── abc.py              # GraphDbInterface ABC (eight methods)
│   ├── cache.py            # CachedGraphDb -- primitive-level LRU cache
│   ├── engine.py           # BFS traversal engine
│   ├── models.py           # Pydantic models (frozen)
│   ├── server.py           # FastMCP server -- four tools
│   └── backends/
│       ├── __init__.py
│       └── postgres.py     # PostgresBackend (kgraph schema)
├── tests/
│   ├── conftest.py         # Postgres connectivity check, skip logic
│   ├── test_engine.py      # 8 tests, mock backend
│   ├── test_postgres.py    # 12 integration tests, real Postgres
│   └── test_server.py      # 7 tests, direct tool calls
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

**Primitive-level caching.** The cache wraps the backend, not the query.
This means any two BFS traversals that touch the same node share cached
primitive results, even if they were issued with different filters or seeds.

**Async by default.** Backend methods are async. The BFS engine issues
`edges_from` and `metadata_for_node` calls concurrently during expansion,
which matters for I/O-bound backends (Postgres, SPARQL endpoints).

## Relationship to the Book

This library is the implementation described in *BFS-QL: A Graph Query
Protocol for Language Models* (Graphwright Publications). The
`PostgresBackend` is the coupling point with the companion library
[kgraph](https://github.com/wware/kgraph): kgraph writes entity embeddings
and relationship records; bfs-ql reads them through the eight-method
interface.
