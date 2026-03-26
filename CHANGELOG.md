# Changelog

## Unreleased (2026-03-26)

### Added

- `intersect_subgraphs` MCP tool: k-hop neighborhood intersection across
  multiple seeds (undirected traversal); returns nodes within k hops of
  ALL given seeds. Implemented purely in the engine layer against the ABC,
  so it works with all backends.
- `IntersectionResult` Pydantic model for intersection query results.
- `Neo4jBackend` -- async Neo4j 5 backend with configurable `id_property`
  supporting pipe-separated fallback (e.g. `"title|name"` → Cypher
  `coalesce(n.title, n.name)`) for graphs with heterogeneous key properties.
  Full-text search via `entity_name_index` with CONTAINS fallback.
- `bfs-ql serve --backend neo4j` CLI option.
- `SchemaSummary` field on `BfsResult` -- entity types and predicates
  actually present in each BFS subgraph, always populated regardless of
  filters. Enables vocabulary discovery when `describe_schema` returns
  `comprehensive=False`.
- `comprehensive` and `next_steps` fields on `SchemaDescription` -- backends
  can signal whether their schema listing is exhaustive and provide
  workflow guidance for the LLM.
- GitHub Actions CI workflow (`.github/workflows/test.yml`).
- `lint.sh`: ruff, mypy, black, and pytest in one script.
- ruff, black, mypy added as dev dependencies; `[tool.black]
  target-version = ["py312"]` for consistent formatting across Python
  versions; mypy configured to ignore test files.

### Changed

- `bfs_query` engine: `topology_only=True` now skips all metadata fetches
  in the engine itself (not just stripped in the server layer), cutting
  response size from ~110K to ~14K chars on a 2-hop traversal.
- Batched node-type resolution: `get_nodes_batch()` added to
  `GraphDbInterface` ABC with a sequential fallback default.
  `SparqlBackend` overrides it with a single `VALUES` query per batch
  (default 10 entities), reducing ~130 round-trips to ~13 per 1-hop query.
  `CachedGraphDb` overrides it to send only uncached IDs to the backend.
- `bfs_query` response slimmed: verbose edge fields (provenance text,
  quotes, timestamps) stripped; full provenance available via
  `describe_entity()`.
- Server instructions updated: LLM advised to treat `prov:` IDs as
  anonymous provisional placeholders.
- MCP server event loop fix: backend created inside FastMCP lifespan
  handler to avoid asyncpg "Future attached to a different loop" errors.
- Integration tests isolated to `_test`-suffixed database.

### Added (SparqlBackend -- 2026-03-24)

- `SparqlBackend` -- async SPARQL 1.1 backend via aiohttp; works with any
  SPARQL endpoint (tested against DBpedia).
- URI prefix compression/expansion for compact canonical IDs.
- `--bif-contains` flag for Virtuoso full-text search.
- `--max-concurrent` and `--request-delay` for polite/rate-limited endpoints.
- `--restrict-to-prefixes` to filter traversal to known namespaces.
- `--exclude-predicate` to suppress high-fan-out noisy predicates from BFS.
- `--node-batch-size` to tune `VALUES`-batch size for type resolution.
- `bfs-ql serve --backend sparql` CLI option.
- SPARQL unit tests (mocked HTTP, no network) and DBpedia integration tests
  (skipped when endpoint unreachable).

## 0.1.0 (2026-03-23)

Initial implementation.

### Added

- `GraphDbInterface` ABC -- eight-method contract for graph backends.
- `CachedGraphDb` -- primitive-level LRU cache wrapping any backend.
- BFS traversal engine -- multi-seed expansion, stub/full filtering,
  concurrent async calls via `asyncio.gather`.
- Pydantic models: `EntityStub`, `Node`, `Edge`, `EdgeWithMetadata`,
  `BfsQuery`, `BfsResult`, `SchemaDescription` -- all frozen.
- `PostgresBackend` -- kgraph-schema Postgres/pgvector backend with
  vector similarity search and evidence provenance.
- FastMCP server with four tools: `describe_schema`, `search_entities`,
  `bfs_query`, `describe_entity`; dynamic schema injection into tool
  description for small schemas.
- CLI entry point: `bfs-ql serve --backend postgres`.
- Medlit demo dataset documentation (`DEMO_DATA.md`).
- 27 tests: 8 engine (mock), 12 Postgres integration (skip when
  unavailable), 7 server (direct tool calls).
