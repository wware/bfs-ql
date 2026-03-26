# Eight Methods Are Enough: Building a BFS-QL Backend

The previous articles in this series covered the problem (language models can't
reliably query knowledge graphs through SPARQL or vector retrieval) and the
solution (a four-tool protocol called BFS-QL -- Breadth-First Search Query
Language -- that exposes graphs through orient, resolve, traverse, and expand
operations). This article is about the other side of that interface: what you
have to implement to connect any graph store to BFS-QL, and why the answer is
simpler than you might expect.

---

## The Separation That Makes This Work

Good interface design separates concerns. BFS-QL has two layers, and they have
very different jobs.

The **server layer** is what the language model sees. It implements the four
tools (`describe_schema`, `search_entities`, `bfs_query`, `describe_entity`),
runs multi-seed breadth-first search, decides which nodes get full metadata and
which become lightweight stubs, applies topology mode, and manages an LRU cache.
This is where all the intelligence lives.

The **backend layer** is what a developer implements to connect a graph store.
It is deliberately primitive: eight methods, all basic graph navigation, no
traversal logic, no filtering, no caching. The backend answers simple questions
about the graph; the server layer does everything interesting with the answers.

This separation means the two layers don't interfere with each other. Backends
are easy to write because they don't have to know about BFS. The server is
expressive because it doesn't have to know about Postgres or SPARQL or Neo4j.

## The Eight Methods

The complete backend contract is an abstract base class with eight async methods:

```python
class GraphDbInterface(ABC):

    @abstractmethod
    async def search_entities(self, query: str) -> list[EntityStub]:
        """Resolve a natural-language name or alias to candidate entity stubs."""

    @abstractmethod
    async def edges_from(self, entity_id: str) -> list[Edge]:
        """Return all outgoing edges from the given entity."""

    @abstractmethod
    async def edges_to(self, entity_id: str) -> list[Edge]:
        """Return all incoming edges to the given entity."""

    @abstractmethod
    async def get_node(self, entity_id: str) -> Node:
        """Return the node record for the given entity ID."""

    @abstractmethod
    async def metadata_for_node(self, entity_id: str) -> dict[str, Any]:
        """Return all available metadata for the given entity."""

    @abstractmethod
    async def metadata_for_edge(self, edge: Edge) -> dict[str, Any]:
        """Return full metadata for the given edge, including provenance."""

    @abstractmethod
    async def entity_types(self) -> list[str]:
        """Return the list of valid entity type names in this graph."""

    @abstractmethod
    async def predicates(self) -> list[str]:
        """Return the list of valid predicate names in this graph."""
```

Three pairs and two singletons. `edges_from` / `edges_to` are the traversal
primitives -- directed navigation in both directions. `get_node` /
`metadata_for_node` separate identity from detail: the first returns just the
entity ID and type, the second returns everything else. `metadata_for_edge`
provides full provenance for a given edge. Then the two singletons:
`search_entities` maps natural-language names to canonical IDs, and
`entity_types` / `predicates` expose the graph's own vocabulary.

The separation of `get_node` from `metadata_for_node` is deliberate. During
BFS expansion, the engine calls both concurrently for nodes that need full
records, but calls only `get_node` for nodes that will become stubs. A backend
that fetches metadata lazily -- or from a separate service -- can implement
both cheaply without conflating the two concerns.

There is no `bfs_query` method in the backend. There is no `count_neighbors`
method, no `find_shortest_path`, no filter parameter, no hop limit. All of that
is in the server layer, implemented once, and it works for every backend
automatically.

## Every Method Is Async

Every method in the interface is `async`. This is a performance decision.

BFS expansion calls `edges_from` and `edges_to` for every node in the current
frontier concurrently, using `asyncio.gather`. A 2-hop BFS over a frontier of
40 nodes issues 80 concurrent edge queries. Against a Postgres backend, these
resolve as concurrent connection pool requests. Against a SPARQL endpoint, they
resolve as concurrent HTTP requests. Against a Neo4j backend, they resolve as
concurrent Bolt protocol calls.

A synchronous interface would serialize this work unnecessarily. The async
design is a performance contract: backends that can serve concurrent queries
concurrently will. Backends that cannot -- in-memory dicts, file-backed stores
-- pay no penalty, because `async def` with no `await` inside is just a regular
function in async clothing.

## What the Caching Layer Does for You

`CachedGraphDb` is a wrapper that sits between the server layer and any backend.
It maintains per-method dict caches keyed by entity ID. Every call to
`edges_from(id)` checks a dict before hitting the backend. Every
`metadata_for_node(id)` is cached after the first fetch. `entity_types` and
`predicates` are cached indefinitely -- they are stable for the lifetime of a
session.

```python
class CachedGraphDb(GraphDbInterface):
    async def edges_from(self, entity_id: str) -> list[Edge]:
        if entity_id not in self._edges_from_cache:
            self._edges_from_cache[entity_id] = await self._backend.edges_from(entity_id)
        return self._edges_from_cache[entity_id]
```

The critical property is that caching operates at the level where it pays. BFS
traversal at depth 2 may visit the same node from multiple directions. Without
caching, each visit triggers a backend round-trip. With primitive-level caching,
the second visit is a dict lookup. A multi-hop traversal over a well-connected
graph can reduce backend round-trips by an order of magnitude.

Because `CachedGraphDb` implements `GraphDbInterface`, it is transparent to the
server layer. Backends do not implement caching themselves -- they return fresh
data on every call. The server layer decides caching policy. Backends stay simple.

---

## Three Backends in Practice

### Postgres

The Postgres backend is the natural target for graphs built with an extraction
pipeline that writes entities, relationships, and embeddings into a structured
schema. The schema has three tables: an `entity` table with canonical IDs,
types, names, embeddings, and metadata; a `relationship` table with subject,
predicate, object, confidence, and source documents; and a `bundle_evidence`
table with text spans and provenance.

`search_entities` has two implementations. When an embedding function is
provided, it uses pgvector's cosine distance operator (`<=>`) to find the
nearest entity embeddings. When no embedding function is provided, it falls
back to case-insensitive substring matching (`ILIKE`). The embedding model used
at query time must match the model used at extraction time -- if the pipeline
used `text-embedding-3-small` and the query uses a different model, the
distances are meaningless.

The traversal methods -- `edges_from` and `edges_to` -- are single-table
queries against the relationship table. Both are called concurrently for every
node in the BFS frontier; the connection pool manages concurrent acquisition.

### SPARQL

SPARQL (the standard query language for RDF knowledge graphs) endpoints expose
data as subject-predicate-object triples, accessible over HTTP. DBpedia,
Wikidata, UniProt, ChEMBL, the Gene Ontology -- these are public graphs with
public endpoints, accumulated over decades. A SPARQL backend makes all of them
accessible through the same four-tool BFS-QL interface.

`edges_from` and `edges_to` translate directly to SPARQL property path queries:

```sparql
SELECT ?predicate ?object WHERE {
    <{entity_id}> ?predicate ?object .
    FILTER(!isBlank(?object))
}
LIMIT 500
```

The `FILTER(!isBlank(?object))` clause excludes blank nodes -- anonymous
intermediate nodes in RDF data that have no canonical ID and cannot be
meaningfully referenced in BFS-QL. Blank nodes are a modeling convenience in
RDF; they are a navigational dead end for graph traversal.

SPARQL endpoints represent entities as URIs. BFS-QL canonical IDs are strings.
The backend maps between them using a configurable prefix map: for DBpedia,
`http://dbpedia.org/resource/` maps to `DBpedia:`; for Wikidata,
`http://www.wikidata.org/entity/` maps to `Wikidata:`. Outgoing IDs are
expanded to URIs before insertion into queries; incoming URIs are compressed
using the prefix map.

SPARQL 1.1 is a standard, but implementations differ. Virtuoso (which backs
DBpedia), GraphDB, Stardog, and Amazon Neptune each have quirks around timeouts,
blank node handling, and property path support. The backend handles this through
a small set of configuration knobs -- query timeout, result set size limit, and
a flag for whether `SELECT DISTINCT` over the full graph is safe to issue. The
endpoint variance is confined entirely to the backend; the LLM sees identical
behavior regardless of which SPARQL implementation is underneath.

### Neo4j

Neo4j is a property graph database, not an RDF store. Where RDF graphs represent
everything as triples of URIs and literals, property graphs attach key-value pairs
directly to nodes and relationships. A node in Neo4j has a label (or multiple
labels) and a set of properties. A relationship has a type and a set of properties.

For BFS-QL, the mapping is direct: node labels become `entity_type`, relationship
types become predicates, node properties become `metadata_for_node` output. The
one configuration decision is which node property holds the canonical ID -- in
a graph derived from an extraction pipeline it would be `entity_id`; in a general
Neo4j graph it might be `id`, `uri`, or something domain-specific.

`edges_from` and `edges_to` are natural Cypher traversals:

```cypher
MATCH (n {entity_id: $id})-[r]->(m)
RETURN n.entity_id AS subject,
       type(r) AS predicate,
       m.entity_id AS object
```

`search_entities` requires a full-text index, which must be created at graph
construction time. Unlike Postgres (which can fall back to `ILIKE`) or SPARQL
(which can use `CONTAINS` on labels), Neo4j has no built-in substring search on
node properties. The backend checks for index existence at initialization and
raises a clear error if it is missing, rather than failing silently at query time.

`entity_types` and `predicates` use Neo4j's `db.labels()` and
`db.relationshipTypes()` built-in procedures, which return the complete
vocabulary without scanning the graph. Fast, stable, no `SELECT DISTINCT`
required.

---

## Writing Your Own: The Contract

The eight-method interface is a complete specification. If you can answer each
of the eight questions for a given graph store, you can write a BFS-QL backend
for it, and everything above that layer -- traversal, filtering, caching, the
four-tool MCP interface -- comes for free.

What "correct" means for each method:

**`search_entities`**: Return a ranked list of entity stubs whose names or
aliases match the query string. Most-likely matches first. Return at most 10-20
candidates. Return an empty list, not an error, if nothing matches.

**`edges_from` / `edges_to`**: Return all outgoing or incoming edges. "All"
means all -- do not apply relevance filters. BFS traversal needs complete
topology; filtering happens at the server layer. Raise `KeyError` if the entity
does not exist; return `[]` if it exists but has no edges.

**`get_node`**: Return a node record with the entity's ID and type. Fast.
Raise `KeyError` if the entity does not exist.

**`metadata_for_node`**: Return a dict of all available metadata. Include
everything -- names, synonyms, descriptions, external links, confidence scores.
The server layer passes this dict to the LLM as-is; the LLM decides what is
relevant. Do not omit fields to save space; topology mode handles that at the
server layer.

**`metadata_for_edge`**: Return a dict of all available edge metadata. Include
provenance: text spans, source documents, extraction confidence. The server
strips verbose provenance fields from BFS results but the backend should return
everything and let the server decide what to expose.

**`entity_types` / `predicates`**: Return complete, stable lists. The server
caches these indefinitely; they must not change during a session.

To make the contract concrete, consider a JSON-LD REST API as a backend --
an API that exposes entities at `/entities/{id}` and their relationships at
`/entities/{id}/relations`. The full implementation of all eight methods takes
approximately 60 lines of Python. Once those eight methods work correctly, the
backend can be passed to `create_server()` and immediately served through the
full BFS-QL interface: four tools, stub/full filtering, multi-seed BFS, topology
mode, LRU caching. None of that logic lives in the backend. All of it comes
for free.

## The Payoff

The eight-method interface is deliberately small. Its purpose is not to
constrain what backends can do -- they can expose arbitrary metadata, use any
storage technology, call any external service. Its purpose is to define the
minimum surface that the BFS-QL server needs to function.

A backend that correctly implements all eight methods gets, automatically:

- BFS traversal to any depth with concurrency across the frontier
- Stub/full node and edge filtering based on the caller's type and predicate filters
- Topology mode: pure structural skeleton with no metadata
- Multi-seed union: BFS from multiple seeds simultaneously
- LRU caching at the primitive level -- no repeated round-trips for the same entity within a session
- The full four-tool MCP interface: `describe_schema`, `search_entities`, `bfs_query`, `describe_entity`
- Schema injection: valid node types and predicates injected into the tool description when the schema is small enough

The cost is eight method implementations. The payoff is a fully functional LLM
graph interface against any data store you can navigate.

---

The interface design principle here is the same one that made USB successful
as a hardware standard and HTTP successful as a web substrate: agree on a
minimal contract, implement it independently, and let the interoperability
emerge. The graph a hospital runs against its clinical knowledge base is, from
the language model's perspective, indistinguishable from the graph a
pharmaceutical company runs against its compound library. Same four tools.
Same query format. Same session workflow. The eight-method backend contract
is how that uniformity is delivered -- not by forcing every graph store to
become the same, but by requiring every backend to answer the same eight
questions.
