# PLAN.md: DBpedia SPARQL Backend

This document describes the plan for adding a DBpedia/SPARQL backend to BFS-QL,
writing pytests against it, and running a live chatbot test in Claude Code.

---

## Overview

The goal is a `SparqlBackend` class that implements `GraphDbInterface` against
any SPARQL 1.1 endpoint, with DBpedia as a first test target. The target will
be selected by the `SPARQL_ENDPOINT_URL` environment variable, which can come
from a `.env` file or be set with `export` in a bash shell.  Once the backend
exists, we can:

1. Run unit tests against a mock SPARQL server (no network required)
2. Run integration tests against the live DBpedia endpoint (skipped if unreachable)
3. Repeat the chatbot demo -- connect `bfs-ql serve` against DBpedia and run a
   live session in Claude Code, as we did with the medlit/Postgres graph

---

## Phase 1: `SparqlBackend` Implementation

**File**: `bfsql/backends/sparql.py`

### Constructor

```python
class SparqlBackend(GraphDbInterface):
    def __init__(
        self,
        endpoint: str,
        prefixes: dict[str, str] | None = None,
        timeout: int = 30,
        edge_limit: int = 500,
        entity_type_limit: int = 30,
        predicate_limit: int = 100,
        safe_distinct: bool = True,
    ) -> None:
```

- `endpoint`: SPARQL endpoint URL (e.g. `https://dbpedia.org/sparql`)
- `prefixes`: maps short prefix → URI base (e.g. `{"DBpedia": "http://dbpedia.org/resource/"}`)
  Used to compress incoming URIs to canonical IDs and expand outgoing IDs to full URIs.
- `timeout`: HTTP request timeout in seconds
- `edge_limit`: LIMIT clause for `edges_from`/`edges_to` queries
- `entity_type_limit`: LIMIT for `entity_types()` -- types returned in decreasing frequency order
- `predicate_limit`: LIMIT for `predicates()` -- arbitrary sample, not frequency-ordered
- `safe_distinct`: if False, `entity_types()` and `predicates()` return `[]`
  (useful for endpoints where even sampled scans are too slow)

Uses `aiohttp.ClientSession` for async HTTP. Session is created lazily on first
use and closed via an explicit `close()` method (matches the Postgres pattern).

A `create()` classmethod handles async initialization and env var fallback:

```python
@classmethod
async def create(
    cls,
    endpoint: str | None = None,
    prefixes: dict[str, str] | None = None,
    **kwargs,
) -> "SparqlBackend":
    endpoint = endpoint or os.environ["SPARQL_ENDPOINT_URL"]
    return cls(endpoint=endpoint, prefixes=prefixes or {}, **kwargs)
```

This mirrors `PostgresBackend.create()` reading `DATABASE_URL`. The factory
`SparqlBackend.create` is what gets passed to `create_server()`, and can be
called with an explicit URL or left to pick it up from the environment.

### URI ↔ canonical ID mapping

- **Compress** (URI → ID): match the URI against known prefixes, replace the
  matching base with `Prefix:`. If no prefix matches, use the URI as-is.
- **Expand** (ID → URI): reverse lookup. If the ID contains `:` and the prefix
  before `:` is in the map, expand. Otherwise treat as a bare URI.

### Method implementations

**`edges_from(entity_id)`** / **`edges_to(entity_id)`**:

```sparql
SELECT ?p ?o WHERE {     # edges_from
    <{uri}> ?p ?o .
    FILTER(!isLiteral(?o) && !isBlank(?o))
}
LIMIT {limit}
```

```sparql
SELECT ?s ?p WHERE {     # edges_to
    ?s ?p <{uri}> .
    FILTER(!isLiteral(?s) && !isBlank(?s))
}
LIMIT {limit}
```

Filter out literal objects (strings, numbers) -- BFS-QL edges connect entities,
not literals. Filter out blank nodes. Compress result URIs to canonical IDs.

**`get_node(entity_id)`**:

```sparql
SELECT ?type WHERE {
    <{uri}> a ?type .
}
LIMIT 1
```

Return the first `rdf:type` as `entity_type`, compressed. Raise `KeyError` if
no results.

**`metadata_for_node(entity_id)`**:

```sparql
SELECT ?p ?o WHERE {
    <{uri}> ?p ?o .
    FILTER(isLiteral(?o))
}
LIMIT {limit}
```

Collect all literal-valued properties as a flat dict. Use the local name of `?p`
as the key (e.g. `rdfs:label` → `label`).

**`metadata_for_edge(edge)`**:

DBpedia does not carry per-edge provenance. Return `{}` for now. This is
correct -- the ABC contract allows an empty dict.

**`search_entities(query)`**:

```sparql
SELECT DISTINCT ?entity ?type WHERE {
    ?entity rdfs:label ?label ;
            a ?type .
    FILTER(CONTAINS(LCASE(STR(?label)), LCASE("{query}")))
}
LIMIT 20
```

Portable across endpoints. For DBpedia specifically, a Virtuoso-specific
`bif:contains` variant can be added later as an optimization.

Note: the query string must be escaped before interpolation (replace `"` with
`\\"`, `\` with `\\`) to prevent SPARQL injection. SPARQL does not support
parameterized queries in the way SQL does, so escaping at the application layer
is the correct approach.

**`entity_types()`**:

```sparql
SELECT ?type (COUNT(?s) AS ?count) WHERE {
    ?s a ?type .
}
GROUP BY ?type
ORDER BY DESC(?count)
LIMIT {limit}
```

Returns types ordered by entity count -- most-populated types first. This is
more useful to an LLM than alphabetical order, and the query is fast enough on
DBpedia (~5 seconds for LIMIT 30). The constructor takes an `entity_type_limit`
parameter (default: 30) to keep the result manageable. Compress all result URIs.

If `safe_distinct=False`, return `[]`.

**`predicates()`**:

`COUNT(*) GROUP BY` over all predicates times out on DBpedia. The approach
instead is to collect predicates from a sample of edges in the graph -- a
`SELECT DISTINCT` scoped to a random or fixed sample of subjects:

```sparql
SELECT DISTINCT ?pred WHERE {
    ?s ?pred ?o .
    FILTER(?pred != rdf:type)
}
LIMIT {limit}
```

This returns whatever predicates Virtuoso happens to encounter first in its
index scan -- not ordered by frequency, but fast. The constructor takes a
`predicate_limit` parameter (default: 100). For endpoints where even this is
too slow, `safe_distinct=False` returns `[]`.

A future improvement: sample predicates from the neighborhoods of a few
well-known seed entities (provided at construction time) to get a more
representative and domain-relevant predicate list.

---

## Phase 2: Unit Tests (no network)

**File**: `tests/test_sparql.py`

Use `aioresponses` (or `unittest.mock.patch`) to intercept `aiohttp` calls and
return canned SPARQL JSON responses. Test structure mirrors `test_engine.py`:

- Small fixed graph with 3-4 entities and 3-4 relationships
- Mock all HTTP calls; assert correct SPARQL was sent and correct results returned

### Test cases

| Test | What it covers |
|------|---------------|
| `test_edges_from` | Correct SPARQL generated, URIs compressed to IDs |
| `test_edges_to` | Incoming edge direction |
| `test_get_node` | rdf:type extraction, KeyError on empty result |
| `test_metadata_for_node` | Literal property collection |
| `test_search_entities` | CONTAINS filter, result ranking |
| `test_entity_types` | DISTINCT type query, compression |
| `test_predicates` | DISTINCT predicate query, rdf:type filtered out |
| `test_blank_node_filtered` | Blank nodes don't appear as edges |
| `test_literal_filtered` | Literal objects don't appear as edges |
| `test_unknown_prefix_passthrough` | URIs with no matching prefix pass through as-is |

### End-to-end via mock server

Wire `SparqlBackend` (with mocked HTTP) through `CachedGraphDb` and the BFS
engine. Run a 2-hop traversal, verify stub/full filtering and topology
completeness. This re-uses the engine test patterns from `test_engine.py`.

---

## Phase 3: Integration Tests (live DBpedia)

**File**: `tests/test_sparql_integration.py`

Skip automatically if DBpedia is unreachable (same pattern as Postgres tests
in `conftest.py`). Use a well-known stable seed -- `DBpedia:Desmopressin` or
`DBpedia:Cushing%27s_disease` -- so results are predictable.

### Test cases

| Test | What it covers |
|------|---------------|
| `test_search_desmopressin` | `search_entities("desmopressin")` returns a result with `DBpedia:Desmopressin` |
| `test_edges_from_desmopressin` | `edges_from` returns non-empty list for known entity |
| `test_get_node_type` | `get_node` returns a known rdf:type |
| `test_bfs_1hop` | 1-hop BFS via engine returns nodes and edges |
| `test_entity_types_nonempty` | `entity_types()` returns non-empty list |
| `test_predicates_nonempty` | `predicates()` returns non-empty list, rdf:type absent |
| `test_server_describe_schema` | Full server tool: `describe_schema()` works end-to-end |
| `test_server_bfs_query` | Full server tool: `bfs_query` with `topology_only=True` |

`conftest.py` addition: a `_check_sparql(url)` helper that does a lightweight
`ASK { ?s ?p ?o }` query with a short timeout; marks tests as skipped if it fails.

---

## Phase 4: Chatbot Demo

Once the backend and tests pass, run a live session:

```bash
# Start bfs-ql against DBpedia
cd ~/bfs-ql
uv run bfs-ql serve --backend sparql --transport sse \
  --endpoint https://dbpedia.org/sparql \
  --prefix DBpedia=http://dbpedia.org/resource/ \
  --prefix DBpedia-owl=http://dbpedia.org/ontology/ \
  --description "DBpedia: open encyclopedia knowledge graph derived from Wikipedia"
```

Register with Claude Code:
```bash
claude mcp add --transport sse --scope user dbpedia http://127.0.0.1:8000/sse
```

Then open a new Claude Code session and run the same five manual tests used for
the medlit demo:

1. `describe_schema` -- verify entity types and predicates come back
2. `search_entities("desmopressin")` -- verify canonical ID resolves
3. `bfs_query` with `topology_only=True` -- verify topology survey works
4. `describe_entity` on a stub node -- verify expansion works
5. Multi-seed query -- e.g. desmopressin + Cushing's disease

Document results in `DEMO_DATA.md` under a new "DBpedia Demo" section.

---

## CLI Changes Needed

`__main__.py` needs new arguments for the `serve` subcommand:

- `--backend sparql` (add to `choices`)
- `--endpoint URL` (SPARQL endpoint URL, required when backend=sparql)
- `--prefix KEY=VALUE` (repeatable; builds the prefix map; KEY is the short
  prefix name, VALUE is the full URI base, separated by `=`,
  e.g. `--prefix DBpedia=http://dbpedia.org/resource/`)
- `--no-safe-distinct` (flag to disable DISTINCT scans on large endpoints)

---

## Dependencies to Add

```toml
# pyproject.toml [project.dependencies]
"aiohttp>=3.9",

# [project.optional-dependencies] dev
"aioresponses>=0.7",
```

---

## Order of Work

1. Add `aiohttp` dependency, create `bfsql/backends/sparql.py` skeleton
2. Write unit tests in `test_sparql.py` with mocked HTTP (TDD: tests first)
3. Implement `SparqlBackend` methods until unit tests pass
4. Add `conftest.py` DBpedia reachability check
5. Write integration tests in `test_sparql_integration.py`
6. Extend CLI (`__main__.py`) with `--backend sparql` and related args
7. Run chatbot demo, document results
