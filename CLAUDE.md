# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

BFS-QL is a graph query protocol that exposes knowledge graphs to language models
via a four-tool MCP interface. The library provides:

- A `GraphDbInterface` ABC that any graph store backend must implement (8 methods)
- A BFS traversal engine with stub/full filtering and multi-seed support
- A `CachedGraphDb` wrapper providing session-scoped LRU caching for any backend
- A FastMCP server (`create_server()`) exposing four tools: `describe_schema`,
  `search_entities`, `bfs_query`, `describe_entity`
- A `bfs-ql serve` CLI command for local deployment
- A Postgres/pgvector backend for kgraph-derived graphs

The companion book is at `~/bfs-ql-book`. The companion kgraph pipeline is at
`~/kgraph`.

## Package Structure

```
bfsql/
  abc.py          -- GraphDbInterface ABC (8 abstract methods)
  models.py       -- Pydantic models: EntityStub, Node, Edge, EdgeWithMetadata,
                     BfsQuery, BfsResult, SchemaDescription
  engine.py       -- BFS traversal engine (bfs_query function)
  cache.py        -- CachedGraphDb wrapper
  server.py       -- FastMCP server (create_server, _slim_result, tool definitions)
  __main__.py     -- CLI entrypoint (bfs-ql serve)
  backends/
    postgres.py   -- PostgresBackend (asyncpg + pgvector)

tests/
  conftest.py     -- Rewrites DATABASE_URL to kgserver_test; creates DB if absent
  test_engine.py  -- Unit tests for BFS traversal logic
  test_server.py  -- Unit tests for MCP server tools
  test_postgres.py -- Integration tests against live Postgres (skipped if DB unreachable)
```

## Build & Test Commands

```bash
# Install dependencies
uv pip install -e ".[dev]"

# Run all tests (Postgres integration tests skipped if DB not reachable)
uv run pytest

# Run a specific test file
uv run pytest tests/test_server.py -v

# Start the MCP server (SSE transport, Postgres backend)
uv run bfs-ql serve --backend postgres --transport sse \
  --description "My knowledge graph"
```

Requires `DATABASE_URL` env var for Postgres tests and server. Set it in `.env`
or the environment. Integration tests automatically use a `_test`-suffixed database
(e.g., `kgserver` → `kgserver_test`) to avoid touching live data.

## Key Design Decisions

- **All traversal intelligence is in the server layer**, not the backends. Backends
  answer eight primitive questions; BFS, filtering, caching, and tool logic are
  in `engine.py`, `cache.py`, and `server.py`.
- **Factory pattern for async initialization**: `PostgresBackend.create` is passed
  as an async callable to `create_server()`. The pool is created inside the FastMCP
  lifespan handler, ensuring it runs in the correct event loop (avoids asyncpg
  "Future attached to a different loop" errors).
- **Stubs, not omissions**: Non-matching nodes and edges are returned as lightweight
  stubs preserving full topology. Filters control detail level, not presence.
- **`topology_only=True`**: Suppresses all metadata, returning pure structural
  skeleton. Use as first move on large or unfamiliar graphs (~14K chars vs ~110K
  for full metadata on a 2-hop medlit traversal).
- **`prov:` provisional IDs**: Pipeline artifacts with no canonical meaning. The
  server instructs the LLM to treat them as anonymous placeholders.

## Python coding plans

- **Plan filenames** -- plans while in development will have filenames of the form
  `PLAN[1-9][0-9]*\.md`.
- **Readiness** -- a plan is **ready** if it is *clear*, *specific*, *actionable*,
  and in a state where it can be executed with little or no supervision.

## Python and Testing Conventions

- Use `uv run pytest` to run tests, `uv run bfs-ql` to run the CLI.
- Use `uv run python` for any Python scripting tasks -- not `python` or `python3`.
- Pydantic models with `frozen=True` throughout (`models.py`).
- All `GraphDbInterface` methods are `async`; the engine uses `asyncio.gather` for
  concurrent frontier expansion.
- pytest-asyncio in `auto` mode with session-scoped event loops (set in
  `pyproject.toml`) -- required for asyncpg pool sharing across tests.

## Demo Data

See `DEMO_DATA.md` for instructions on loading the medlit demo dataset (36 PubMed
papers on Cushing disease, ~1,900 entities, ~2,184 relationships) into local
Postgres and starting the MCP server against it.
