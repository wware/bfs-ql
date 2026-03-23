"""Pytest configuration and shared fixtures."""

import os
import asyncio
import asyncpg
import pytest
from dotenv import load_dotenv

load_dotenv()


def pytest_collection_modifyitems(config, items):
    """Skip Postgres tests if the database is not reachable."""
    pg_reachable = _check_pg()
    skip = pytest.mark.skip(reason="Postgres not reachable -- skipping integration tests")
    for item in items:
        if "test_postgres" in item.nodeid and not pg_reachable:
            item.add_marker(skip)


def _check_pg() -> bool:
    """Return True if DATABASE_URL is set and the server accepts connections."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return False
    try:
        async def _try():
            conn = await asyncpg.connect(dsn, timeout=2)
            await conn.close()
        asyncio.run(_try())
        return True
    except Exception:
        return False
