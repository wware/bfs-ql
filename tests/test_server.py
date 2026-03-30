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
    tools = await mcp.get_tools()
    if name not in tools:
        raise KeyError(f"Tool {name!r} not found")
    return tools[name].fn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_describe_schema(server, mock_backend):
    """describe_schema returns entity types, predicates, comprehensive flag, and next_steps."""
    await mock_backend.entity_types()
    await mock_backend.predicates()

    fn = await _get_tool(server, "describe_schema")
    result = await fn()
    assert set(result["entity_types"]) == {"Drug", "Disease", "Gene"}
    assert "TREATS" in result["predicates"]
    assert result["graph_description"] == "Test graph."
    assert isinstance(result["comprehensive"], bool)
    assert isinstance(result["next_steps"], str)
    assert len(result["next_steps"]) > 0


async def test_search_entities(server):
    fn = await _get_tool(server, "search_entities")
    results = await fn(query="DiseaseB")
    ids = {r["id"] for r in results}
    assert "Disease:B" in ids


async def test_bfs_query_basic(server):
    fn = await _get_tool(server, "bfs_query")
    result = await fn(seeds=["Drug:A"], max_hops=1)
    node_ids = {n["id"] for n in result["nodes"]}
    assert "Drug:A" in node_ids
    assert "Disease:B" in node_ids
    assert "Gene:C" in node_ids


async def test_bfs_query_schema_summary(server):
    """bfs_query always includes schema_summary with types and predicates found."""
    fn = await _get_tool(server, "bfs_query")
    result = await fn(seeds=["Drug:A"], max_hops=1)
    summary = result.get("schema_summary")
    assert summary is not None
    assert "entity_types_found" in summary
    assert "predicates_found" in summary
    assert "Drug" in summary["entity_types_found"]
    assert "TREATS" in summary["predicates_found"]


async def test_bfs_query_schema_summary_topology_only(server):
    """schema_summary is present even when topology_only=True."""
    fn = await _get_tool(server, "bfs_query")
    result = await fn(seeds=["Drug:A"], max_hops=1, topology_only=True)
    summary = result.get("schema_summary")
    assert summary is not None
    assert "entity_types_found" in summary
    assert "predicates_found" in summary


async def test_bfs_query_with_filters(server):
    fn = await _get_tool(server, "bfs_query")
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
    fn = await _get_tool(server, "describe_entity")
    result = await fn(id="Disease:B")
    assert result["id"] == "Disease:B"
    assert result["entity_type"] == "Disease"


async def test_describe_entity_missing(server):
    fn = await _get_tool(server, "describe_entity")
    with pytest.raises(KeyError):
        await fn(id="NoSuch:X")


async def test_bfs_query_exclude_node_types(server):
    """exclude_node_types removes nodes and their edges from the result."""
    fn = await _get_tool(server, "bfs_query")
    result = await fn(seeds=["Drug:A"], max_hops=2, exclude_node_types=["Gene"])
    node_ids = {n["id"] for n in result["nodes"]}
    assert "Gene:C" not in node_ids
    assert "Drug:A" in node_ids
    assert "Disease:B" in node_ids
    # INHIBITS touches Gene:C -- should be absent
    predicates = {e["predicate"] for e in result["edges"]}
    assert "INHIBITS" not in predicates
    assert "TREATS" in predicates


async def test_describe_entities_batch(server):
    """describe_entities returns full metadata for multiple IDs."""
    fn = await _get_tool(server, "describe_entities")
    results = await fn(ids=["Drug:A", "Disease:B"])
    assert len(results) == 2
    ids = {r["id"] for r in results}
    assert ids == {"Drug:A", "Disease:B"}
    for r in results:
        assert "entity_type" in r


async def test_describe_entities_skips_missing(server):
    """describe_entities silently omits IDs that don't exist."""
    fn = await _get_tool(server, "describe_entities")
    results = await fn(ids=["Drug:A", "NoSuch:X", "Disease:B"])
    assert len(results) == 2
    ids = {r["id"] for r in results}
    assert ids == {"Drug:A", "Disease:B"}


async def test_describe_entities_empty(server):
    """describe_entities with empty list returns empty list."""
    fn = await _get_tool(server, "describe_entities")
    results = await fn(ids=[])
    assert results == []


async def test_six_tools_registered(server):
    """Exactly six tools are registered."""
    tools = await server.get_tools()
    assert set(tools.keys()) == {
        "describe_schema",
        "search_entities",
        "bfs_query",
        "describe_entity",
        "describe_entities",
        "intersect_subgraphs",
    }
