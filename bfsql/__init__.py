"""BFS-QL: a graph query protocol for language models.

Public API:

    from bfsql import GraphDbInterface, CachedGraphDb, create_server
    from bfsql.models import BfsQuery, BfsResult, EntityStub, Node, Edge
    from bfsql.engine import bfs_query
    from bfsql.backends.postgres import PostgresBackend
"""

from bfsql.abc import GraphDbInterface
from bfsql.cache import CachedGraphDb
from bfsql.server import create_server

__all__ = ["GraphDbInterface", "CachedGraphDb", "create_server"]
__version__ = "0.1.0"
