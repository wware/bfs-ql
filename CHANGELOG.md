# Changelog

## 0.1.0 (unreleased)

Initial implementation.

### Added

- `GraphDbInterface` ABC -- eight-method contract for graph backends
- `CachedGraphDb` -- primitive-level LRU cache wrapping any backend
- BFS traversal engine -- multi-seed expansion, stub/full filtering,
  concurrent async calls via `asyncio.gather`
- Pydantic models: `EntityStub`, `Node`, `Edge`, `EdgeWithMetadata`,
  `BfsQuery`, `BfsResult`, `SchemaDescription` -- all frozen
- `PostgresBackend` -- kgraph-schema Postgres/pgvector backend with
  vector similarity search and evidence provenance
- FastMCP server with four tools: `describe_schema`, `search_entities`,
  `bfs_query`, `describe_entity`; dynamic schema injection into tool
  description for small schemas
- CLI entry point: `bfs-ql serve --backend postgres`
- 27 tests: 8 engine (mock), 12 Postgres integration (skip when unavailable),
  7 server (direct tool calls)
