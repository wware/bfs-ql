"""Pytest configuration and shared fixtures."""

import os
import asyncio
import asyncpg
import pytest
from dotenv import load_dotenv

load_dotenv()

# Derive a test-specific database URL so integration tests never touch the
# live database. Appends '_test' to the database name in DATABASE_URL.
# E.g. postgresql://...@localhost/kgserver -> postgresql://...@localhost/kgserver_test
def _test_dsn() -> str | None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    # Replace the last path component (database name) with name + '_test'
    if "/" in dsn:
        base, dbname = dsn.rsplit("/", 1)
        # Strip any query params from dbname for the replacement
        bare = dbname.split("?")[0]
        suffix = dbname[len(bare):]
        return f"{base}/{bare}_test{suffix}"
    return None


# Expose as environment variable so test_postgres.py picks it up via os.environ
_TEST_DSN = _test_dsn()
if _TEST_DSN:
    os.environ["DATABASE_URL"] = _TEST_DSN


_SPARQL_ENDPOINT = os.environ.get("SPARQL_ENDPOINT_URL", "https://dbpedia.org/sparql")


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when the required backend is not reachable."""
    pg_reachable = _check_pg(_TEST_DSN)
    sparql_reachable = _check_sparql(_SPARQL_ENDPOINT)

    skip_pg = pytest.mark.skip(reason="Postgres not reachable -- skipping integration tests")
    skip_sparql = pytest.mark.skip(reason="SPARQL endpoint not reachable -- skipping integration tests")

    for item in items:
        if "test_postgres" in item.nodeid and not pg_reachable:
            item.add_marker(skip_pg)
        if "test_sparql_integration" in item.nodeid and not sparql_reachable:
            item.add_marker(skip_sparql)


def _check_sparql(endpoint: str) -> bool:
    """Return True if the SPARQL endpoint responds to a lightweight ASK query."""
    try:
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({
            "query": "ASK { ?s ?p ?o }",
            "format": "application/sparql-results+json",
        })
        url = f"{endpoint}?{params}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _check_pg(dsn: str | None) -> bool:
    """Return True if dsn is set and the server accepts connections."""
    if not dsn:
        return False
    try:
        async def _try():
            # Connect to the default 'postgres' db to create the test db if needed
            base, dbname = dsn.rsplit("/", 1)
            bare_dbname = dbname.split("?")[0]
            admin_dsn = f"{base}/postgres"
            conn = await asyncpg.connect(admin_dsn, timeout=2)
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", bare_dbname
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{bare_dbname}"')
            await conn.close()
        asyncio.run(_try())
        return True
    except Exception:
        return False
