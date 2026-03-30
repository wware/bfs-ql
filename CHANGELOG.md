# Changelog

## 0.1.4 (2026-03-28)

### Added

- `node_types` filter parameter on `search_entities` -- restricts results to
  entities of the given types before ranking. Useful for disambiguating common
  terms that match both concept entities and papers (e.g. pass
  `node_types=["disease"]` to suppress paper results).
- `limit` and `offset` pagination parameters on `bfs_query` -- cap the number
  of nodes returned and allow paging through large neighborhoods.
  `node_count` and `edge_count` always reflect the full traversal regardless
  of pagination. Edges are filtered to those whose both endpoints appear in
  the returned node window. `schema_summary` always reflects the full
  traversal, not the current page.
- `schema_summary` is now a deliberate, documented feature of the BFS-QL
  protocol (previously present but not emphasized). It reports the entity
  types and predicates actually found in the result subgraph, independent of
  the `node_types`/`predicates` filters applied. Essential for discovering
  valid filter values from live results, especially in large or open-world
  graphs where `describe_schema` returns `comprehensive=False`.

## 0.1.3 (2026-03-30)

### Fixed

- `search_entities` now returns relevant concept entities (diseases, genes,
  drugs, etc.) rather than papers when both match the query. A three-stage
  pipeline replaces the previous single-pass vector/name search:
  1. **Exact match short-circuit** -- `WHERE lower(name) = lower(query)` runs
     first; matching entities are pinned to the top of results regardless of
     vector score or type.
  2. **Oversized candidate pool** -- 30 candidates fetched instead of 10 to
     give the reranker room to work.
  3. **Composite reranker** -- candidates scored as
     `0.5 * vec_sim + 0.5 * (token_coverage × type_weight)`.
     `token_coverage` penalises long names that merely contain the query
     (a paper title scores far lower than a two-word disease name).
     `type_weight` de-prioritises papers (0.3) relative to biological
     concept entities (0.8–1.0).
- `EntityStub` gains optional `name` and `score` fields, populated by
  `search_entities` for use by the reranker. BFS stub nodes leave both `None`.
- `describe_schema` no longer returns empty `entity_types` and `predicates`
  when the server starts against an empty database and data is loaded later.
  `CachedGraphDb` now re-queries if the cached list is empty (treating empty
  as a cache miss), and `describe_schema` refreshes `_state` from the live
  result.

## 0.1.1 (2026-03-29)

### Fixed

- `describe_schema` returned empty `entity_types` and `predicates` arrays
  (`comprehensive=true` but lists empty) when the MCP server started before
  the database was populated. `CachedGraphDb.entity_types()` and
  `.predicates()` now treat an empty cached list as a cache miss so a
  subsequent call after data is loaded returns the correct schema.

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
