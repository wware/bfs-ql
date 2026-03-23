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
uv run bfs-ql serve --backend postgres

# Paste the printed MCP URL into your MCP client (Claude, Cursor, etc.)
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

## Implementation Plan

### Phase 1 -- Core abstractions

- [ ] `bfsql/models.py` -- Pydantic models: `EntityStub`, `Node`, `Edge`,
  `BfsQuery`, `BfsResult`, `SchemaDescription`
- [ ] `bfsql/abc.py` -- `GraphDbInterface` ABC
- [ ] `bfsql/cache.py` -- `CachedGraphDb` LRU wrapper
- [ ] `bfsql/engine.py` -- BFS traversal engine: multi-seed expansion,
  stub/full filtering, result assembly
- [ ] Tests for engine logic against a mock backend

### Phase 2 -- Postgres backend

- [ ] `bfsql/backends/postgres.py` -- `PostgresBackend` implementation
  - `search_entities` via pgvector cosine similarity
  - `edges_from` / `edges_to` via SQL joins
  - `get_node` / `metadata_for_node` / `metadata_for_edge` via SQL
  - `entity_types` / `predicates` via `SELECT DISTINCT`
- [ ] `.env.example` with `DATABASE_URL` placeholder
- [ ] Database schema documentation (or migration script if needed)
- [ ] Tests against a real Postgres instance (no mocking)

### Phase 3 -- MCP server

- [ ] `bfsql/server.py` -- FastMCP server wiring the four tools to the
  engine
  - `describe_schema()` tool
  - `search_entities(query)` tool
  - `bfs_query(seeds, max_hops, node_types, predicates)` tool with dynamic
    schema injection into tool description
  - `describe_entity(id)` tool
- [ ] `bfsql/__main__.py` -- CLI entry point (`bfs-ql serve --backend postgres`)
- [ ] End-to-end test: start server, connect a client, run a query

### Phase 4 -- Packaging and docs

- [ ] `pyproject.toml` with `uv`-compatible dependencies
- [ ] `.env.example`
- [ ] Docstrings on all public interfaces
- [ ] Usage examples in this README

## Project Layout

```
bfs-ql/
├── bfsql/
│   ├── __init__.py
│   ├── __main__.py         # CLI entry point
│   ├── abc.py              # GraphDbInterface ABC
│   ├── cache.py            # CachedGraphDb
│   ├── engine.py           # BFS traversal engine
│   ├── models.py           # Pydantic models
│   ├── server.py           # FastMCP server
│   └── backends/
│       ├── __init__.py
│       └── postgres.py     # PostgresBackend
├── tests/
│   ├── test_engine.py
│   ├── test_postgres.py
│   └── test_server.py
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
