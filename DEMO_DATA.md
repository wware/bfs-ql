# Loading the medlit Demo Dataset

This document explains how to populate the local Postgres instance with the
`medlit_bundle` dataset from the kgraph repo, so that BFS-QL can be exercised
against the same data used in kggraph demos.

## Background

The kgraph project produces knowledge-graph bundles from medical literature.
The `medlit_bundle` at `~/kgraph/medlit_bundle/` contains 1,900 entities and
2,184 relationships extracted from 36 PubMed Central papers. The kgserver
loads this data via `BUNDLE_PATH` at startup.

BFS-QL's `PostgresBackend` queries the same `entity` and `relationship` tables
that kgserver writes. Loading the demo data into your local Postgres therefore
gives you a realistic graph to explore through the four BFS-QL MCP tools.

## Schema

The schema is owned by kgserver and created via SQLModel's `create_all`. The
tables BFS-QL reads are:

| Table          | BFS-QL usage                                  |
|----------------|-----------------------------------------------|
| `entity`       | search, get_node, metadata_for_node           |
| `relationship` | edges_from, edges_to, metadata_for_edge       |
| `evidence`     | provenance rows in metadata_for_edge          |

kgserver also creates `bundle`, `papers`, `bundle_mention`, and
`bundle_evidence` tables that BFS-QL does not use.

## Prerequisites

- Local Postgres running on port 5432 with database `kgserver`
  (credentials: `postgres` / `postgres`)
- kgraph repo cloned at `~/kgraph/`
- `medlit_bundle` present at `~/kgraph/medlit_bundle/`
- `uv` available in the kgraph virtualenv

Verify connectivity:

```bash
psql postgresql://postgres:postgres@localhost:5432/kgserver -c "SELECT 1;"
```

## Loading the Data

Run kgserver's bundle loader directly, pointing it at the local Postgres and
the medlit bundle. Run from the kgraph repo root (not the kgserver subdirectory)
so that `uv` picks up the correct virtualenv:

```bash
cd ~/kgraph

DATABASE_URL=postgresql://postgres:postgres@localhost:5432/kgserver \
BUNDLE_PATH=~/kgraph/medlit_bundle \
uv run python -c "
import sys
sys.path.insert(0, 'kgserver')

from sqlmodel import SQLModel
from query.storage_factory import get_engine
from query.bundle_loader import load_bundle_at_startup

engine, db_url = get_engine()
SQLModel.metadata.create_all(engine)
load_bundle_at_startup(engine, db_url)
print('Done.')
"
```

This is idempotent -- if the bundle is already loaded (same `bundle_id` in the
manifest), it prints a skip message and exits cleanly.

To force a reload (e.g. after schema changes), drop and recreate the tables first:

```bash
psql postgresql://postgres:postgres@localhost:5432/kgserver -c "
DROP TABLE IF EXISTS evidence, bundle_evidence, bundle_mention, relationship, entity, papers, bundle CASCADE;
"
```

Then re-run the load command above.

## Verifying the Load

```bash
psql postgresql://postgres:postgres@localhost:5432/kgserver -c "
SELECT entity_type, COUNT(*) FROM entity GROUP BY entity_type ORDER BY COUNT(*) DESC;
"

psql postgresql://postgres:postgres@localhost:5432/kgserver -c "
SELECT predicate, COUNT(*) FROM relationship GROUP BY predicate ORDER BY COUNT(*) DESC LIMIT 10;
"
```

## Running BFS-QL Against the Demo Data

Once the data is loaded, point BFS-QL at the same Postgres instance. Make sure
`~/bfs-ql/.env` contains:

```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/kgserver
```

Then run the MCP server:

```bash
cd ~/bfs-ql
uv run bfs-ql serve --backend postgres --transport sse \
  --description "Medical literature knowledge graph (medlit bundle, 36 PubMed papers)"
```

Or start an interactive session to call the tools directly:

```bash
cd ~/bfs-ql
uv run python -c "
import asyncio, os
from dotenv import load_dotenv
from bfsql.backends.postgres import PostgresBackend
from bfsql.cache import CachedGraphDb
from bfsql.engine import bfs_query
from bfsql.models import BfsQuery

load_dotenv()

async def demo():
    backend = await PostgresBackend.create()
    db = CachedGraphDb(backend)

    print('Entity types:', await db.entity_types())
    print('Predicates:', await db.predicates())

    hits = await db.search_entities('desmopressin')
    print('Search hits:', hits)

    if hits:
        result = await bfs_query(db, BfsQuery(seeds=[hits[0].id], max_hops=2))
        print(f'BFS result: {len(result.nodes)} nodes, {len(result.edges)} edges')

    await backend.close()

asyncio.run(demo())
"
```

## Schema Compatibility Note

The kgserver stores `entity.embedding` as a JSON array of floats. BFS-QL's
`PostgresBackend` uses this column for pgvector similarity search when an
`embedding_fn` is provided. Without an embedding function (the default),
`search_entities` falls back to a case-insensitive `ILIKE` name match, which
works fine for exploration.

The `entity.properties` column is `JSON` type (not `JSONB`), so asyncpg returns
it as a raw string. BFS-QL handles this with `json.loads()` before merging into
the metadata dict.
