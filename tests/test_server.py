"""End-to-end tests for the FastMCP server using the mock backend.

Tests call the tool functions directly (bypassing MCP transport) to verify
that the server correctly wires the four tools to the engine and cache.
"""

import pytest

from bfsql.server import create_server
from tests.test_engine import MockBackend


@pytest.fixture
def mock_backend():
    return MockBackend()


@pytest.fixture
async def server(mock_backend):
    """Create a server with the mock backend.

    We bypass the lifespan startup (which calls entity_types/predicates)
    by calling create_server and then manually priming the state via the
    describe_schema tool.
    """
    return create_server(mock_backend, graph_description="Test graph.")


# ---------------------------------------------------------------------------
# Helpers: call tools directly from the registered functions
# ---------------------------------------------------------------------------

async def _get_tool(mcp, name):
    """Retrieve a registered tool's underlying function by name."""
    for tool in await mcp.list_tools():
        if tool.name == name:
            return tool.fn
    raise KeyError(f"Tool {name!r} not found")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_describe_schema(server, mock_backend):
    """describe_schema returns entity types and predicates from the backend."""
    # Prime the cache by calling entity_types/predicates directly
    await mock_backend.entity_types()
    await mock_backend.predicates()

    fn = await _get_tool(server,"describe_schema")
    result = await fn()
    assert set(result["entity_types"]) == {"Drug", "Disease", "Gene"}
    assert "TREATS" in result["predicates"]
    assert result["graph_description"] == "Test graph."


async def test_search_entities(server):
    fn = await _get_tool(server,"search_entities")
    results = await fn(query="DiseaseB")
    ids = {r["id"] for r in results}
    assert "Disease:B" in ids


async def test_bfs_query_basic(server):
    fn = await _get_tool(server,"bfs_query")
    result = await fn(seeds=["Drug:A"], max_hops=1)
    node_ids = {n["id"] for n in result["nodes"]}
    assert "Drug:A" in node_ids
    assert "Disease:B" in node_ids
    assert "Gene:C" in node_ids


async def test_bfs_query_with_filters(server):
    fn = await _get_tool(server,"bfs_query")
    result = await fn(
        seeds=["Drug:A"],
        max_hops=1,
        node_types=["Disease"],
        predicates=["TREATS"],
    )
    node_ids = {n["id"] for n in result["nodes"]}
    # All nodes present (stubs for non-matching)
    assert "Drug:A" in node_ids
    assert "Disease:B" in node_ids
    assert "Gene:C" in node_ids

    # Disease:B is full (has metadata)
    disease_b = next(n for n in result["nodes"] if n["id"] == "Disease:B")
    assert disease_b.get("metadata") or disease_b.get("name")

    # TREATS edge is full, INHIBITS is stub
    treats = next((e for e in result["edges"] if e["predicate"] == "TREATS"), None)
    assert treats is not None
    inhibits = next((e for e in result["edges"] if e["predicate"] == "INHIBITS"), None)
    assert inhibits is not None


async def test_describe_entity(server):
    fn = await _get_tool(server,"describe_entity")
    result = await fn(id="Disease:B")
    assert result["id"] == "Disease:B"
    assert result["entity_type"] == "Disease"


async def test_describe_entity_missing(server):
    fn = await _get_tool(server,"describe_entity")
    with pytest.raises(KeyError):
        await fn(id="NoSuch:X")


async def test_four_tools_registered(server):
    """Exactly four tools are registered."""
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {"describe_schema", "search_entities", "bfs_query", "describe_entity"}
