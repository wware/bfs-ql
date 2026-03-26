# Four Tools Are Enough: The BFS-QL Protocol for LLM Graph Reasoning

The previous article in this series made the case that SPARQL generation fails,
RAG fails differently, and the right interface for LLM-driven graph reasoning
is breadth-first traversal with a minimal tool surface. This article is about
what that interface actually looks like -- the four tools, how they compose,
and the design choices behind each one. I call this "Breadth-First Search Query
Language", or "BFS-QL".

---

## The Pattern Language Argument

In 1974, computer scientist Christopher Alexander published *A Pattern Language*,
a catalogue of 253 design patterns for buildings and towns. The book's argument
was not that architects should memorize 253 patterns. It was that good design
recurs -- that the same solutions to the same problems appear across different
scales and contexts, and that naming them makes them easier to recognize, teach,
and apply.

What Alexander discovered, and what software engineers rediscovered twenty years
later when they adapted his framework for code, is that the value of a pattern
library is not in its size. It is in the coverage-to-complexity ratio. A small
set of well-chosen patterns that together cover the full space of common problems
is more useful than a large set that covers the same space redundantly or
inconsistently.

BFS-QL has four tools. The choice is not arbitrary and not conservative -- it is
the result of asking, for every candidate tool, whether it covers something the
others do not, and whether the space it covers is one an LLM actually needs.

## Why Four

The full space of what an LLM needs to do with a knowledge graph decomposes into
four operations, each distinct, together exhaustive:

**Orientation.** The LLM arrives at a graph it has never seen. It does not know
what kinds of entities the graph contains, what relationships are represented, or
how they are named. Before it can navigate, it needs a map. This is
`describe_schema`.

**Resolution.** The LLM has a name -- a drug, a disease, an author. It needs the
canonical ID that the graph uses for that entity. Names are ambiguous; canonical
IDs are not. The operation of mapping a name to an ID is fundamental and cannot
be collapsed into traversal without reintroducing the hallucination problem that
plagues SPARQL generation. This is `search_entities`.

**Traversal.** The LLM has a seed -- one or more canonical IDs. It wants to know
what they connect to. This is the core operation, the one that makes graph
knowledge accessible. Everything else is setup or follow-up. This is `bfs_query`.

**Expansion.** The traversal returns stubs -- lightweight placeholders for nodes
that were present in the topology but did not warrant full metadata. The LLM sees
that something is there and wants to know what it is. This is `describe_entity`.

Orient, resolve, traverse, expand. There is no fifth operation in this list. Any
candidate fifth tool -- "find shortest path," "count neighbors," "list all entities
of type X" -- either reduces to a composition of these four or adds complexity
without adding coverage. The surface is complete.

## What Isn't a Tool (and Why)

The choice of four tools is also a choice of what not to include.

*A "shortest path" tool* is useful for certain graph analyses but not for LLM
reasoning, which doesn't navigate to specific destinations -- it explores
neighborhoods. An LLM that needs to know whether two entities are connected can
issue a multi-hop traversal and inspect the result. Adding a dedicated tool adds
one more surface the LLM must reason about.

*A "list all entities of type X" tool* is a context flood. A biomedical graph
might have hundreds of disease entities. A tool that returns all of them is not
useful to an LLM trying to reason; it saturates the context window with
irrelevant entries. The right operation is traversal from a relevant seed with
a type filter, which returns the disease entities connected to something the
LLM already cares about. Relevance is structural, not taxonomic.

*A "count" tool* answers a query-oriented question rather than a
traversal-oriented one. It gives the LLM a fact rather than a navigational
handle. An LLM that receives "there are 119 disease entities" has not learned
anything it can act on.

## The Session Workflow

The four tools define a natural sequence:

```
1. describe_schema()
   → learn entity types, predicates, graph description

2. search_entities(name)
   → returns a ranked list of {id, entity_type} records
   → inspect entity_type to pick the right match if ambiguous

3. bfs_query(seeds, max_hops, ...)
   → returns {node_count, edge_count, nodes[], edges[]}
   → each node is either a full record (with metadata) or a stub {id, entity_type}
   → each edge is either a full record (with confidence, provenance) or a bare triple
   → start with topology_only=True for large graphs
   → use node_types and predicates to control which nodes/edges get full records

4. describe_entity(id)
   → returns the full metadata dict for a single entity
   → use this to expand any stub that warrants closer inspection
```

Steps 3 and 4 are iterative: the output of one traversal identifies stubs that
motivate expansion calls, which may motivate further traversals seeded at newly
discovered nodes. The workflow is a loop, not a pipeline.

The tools compose by construction. `bfs_query` takes canonical IDs -- which
`search_entities` produces. `describe_entity` takes canonical IDs -- which appear
in `bfs_query` results. Each tool's output is the next tool's input.

## Self-Orienting Graphs

In the early days of the web, connecting to a new API meant reading its
documentation -- a separate artifact, maintained by humans, often out of sync
with the actual API, and unavailable to the software that needed it.

Roy Fielding's REST dissertation (2000) argued for hypermedia as a first-class
constraint: a well-designed API should carry, in its responses, the information
a client needs to navigate it. Links, not documentation. The API tells you what
it can do. This principle -- that interfaces should be self-describing -- became
standard practice. OpenAPI specifications, GraphQL introspection, FastAPI's
`/docs` endpoint are all expressions of the same idea.

`describe_schema` is BFS-QL's implementation of this principle for knowledge
graphs. One call returns three things:

- **`graph_description`**: A human-readable string describing the graph and its
  domain -- what the data represents, where it came from, what kinds of questions
  it is meant to answer. A biomedical graph might say "36 PubMed papers on Cushing
  disease and related endocrinology." That sentence tells the LLM the corpus is
  small, the domain is focused, and the data source is biomedical literature. A
  well-written description tells the LLM whether this is the right graph for its
  current question.

- **`entity_types`**: A list of strings -- the complete set of valid entity type
  names. Not approximate names, not documentation -- the actual strings the query
  engine understands and the LLM can pass as `node_types` filters.

- **`predicates`**: A list of strings -- the complete set of valid predicate names,
  exactly the values valid as `predicates` filters.

A `describe_schema` response for a biomedical graph looks like this:

```json
{
  "graph_description": "36 PubMed papers on Cushing disease and related endocrinology",
  "entity_types": ["disease", "drug", "gene", "paper", "procedure", "protein", "symptom"],
  "predicates": ["AUTHORED", "CAUSES", "CITES", "INHIBITS", "TREATS", "USED_IN"]
}
```

After one call, the LLM knows what it is looking at before it starts navigating.
It knows that `drug`, `disease`, and `procedure` are valid node types -- and that
`protein` and `enzyme` are also present, which tells it something about the level
of mechanistic detail in the graph. It knows that `TREATS`, `CAUSES`, and
`INHIBITS` are valid predicates -- and that `CITES` and `AUTHORED` are also
present, which tells it that the graph includes bibliographic structure alongside
clinical knowledge.

There is also a zero-cost shortcut for small schemas. If the graph has fewer than
20 entity types and 30 predicates, the BFS-QL server injects the valid values
directly into the `bfs_query` tool description. The LLM reads the tool description
before it calls the tool, so it arrives at `bfs_query` already knowing what filter
values are valid -- no explicit `describe_schema` call required.

## The Query Model

The core of BFS-QL is a single query structure with five parameters:

**`seeds`** -- a list of canonical entity IDs to start from. Multiple seeds are
supported because many useful questions are inherently relational: not "what
connects to this entity?" but "what do these two entities have in common?"
A multi-seed query issues a single traversal from all seeds simultaneously and
returns their combined neighborhood, deduplicated. No manual merging required.

**`max_hops`** -- traversal depth. A value of 1 returns immediate neighbors; 2
returns neighbors of neighbors. The practical guidance is to start at 1 and
expand only if the result doesn't contain what you need. A 2-hop traversal from
a well-connected node in a 36-paper biomedical graph returns 84 nodes and 99
edges. A 3-hop traversal from the same node returns most of the graph. Depth is
a context budget decision, not a correctness decision.

**`node_types`** -- optional filter. Nodes whose type matches receive full
metadata. Nodes whose type does not match are returned as stubs: present with
their ID and type, no metadata. Omitting this parameter gives full metadata
for all nodes.

**`predicates`** -- optional filter. Edges whose predicate matches receive full
metadata including confidence scores, source documents, and provenance. Edges
whose predicate does not match are returned as bare subject-predicate-object
triples. Same topology, less detail.

**`topology_only`** -- when true, suppresses all metadata. Every node is a bare
ID and type; every edge a bare triple. Pure structural skeleton, minimum token
cost.

The response contains `node_count`, `edge_count`, a `nodes` list, and an `edges`
list. Each node is either a full record or a stub depending on whether its type
matched `node_types`; each edge is either a full record or a bare triple depending
on whether its predicate matched `predicates`.

Full node vs. stub:

```json
// full node (type matched node_types, or node_types omitted)
{
  "id": "RxNorm:3251",
  "entity_type": "drug",
  "metadata": {
    "name": "desmopressin",
    "confidence": 0.97,
    "canonical_url": "https://rxnav.nlm.nih.gov/REST/rxcui/3251"
  }
}

// stub (type did not match node_types)
{
  "id": "MeSH:D003480",
  "entity_type": "disease"
}
```

Full edge vs. stub:

```json
// full edge (predicate matched predicates, or predicates omitted)
{
  "subject": "RxNorm:3251",
  "predicate": "TREATS",
  "object": "MeSH:D003480",
  "metadata": {
    "confidence": 0.91,
    "source_documents": ["PMC11128938"]
  }
}

// stub (predicate did not match predicates)
{
  "subject": "RxNorm:3251",
  "predicate": "CITES",
  "object": "PMC9876543"
}
```

Stubs are never omitted: the topology is always complete, and the LLM can follow
up on any stub it wants to expand.

The five parameters are passed as a flat JSON object. JSON is the right choice
here not for aesthetic reasons but because it is what LLMs are trained on most
heavily -- the format they generate most reliably and with the fewest structural
errors. No nesting, no sub-query structure, no boolean expression language.
Every level of nesting in a query format is an opportunity for a language model
to make a structural error -- a misplaced bracket, a filter applied at the wrong
scope. A flat format has no levels. The model either provides the parameter or
it doesn't.

## Managing the Context Budget

The context window constraint that motivated this entire design doesn't disappear
once you have the right interface -- it has to be actively managed at query time.
The query parameters are the mechanism.

The recommended progression:

**First: topology survey.** Call `bfs_query` with `topology_only=True` and
`max_hops=2`. This returns the complete structural skeleton of the neighborhood
at minimum token cost. For a 2-hop traversal over 84 nodes and 99 edges, this
is roughly 14,000 characters. The same traversal with full metadata is roughly
110,000 characters. The LLM reads the topology, identifies what matters, and
decides where to spend context budget -- without having already spent it.

**Second: selective expansion.** Call `describe_entity` on the specific nodes
the topology survey identified as significant. Each call retrieves full metadata
for one node. The LLM pays for exactly the information it has decided it needs.

**Third: targeted re-query.** If a follow-up traversal is needed, issue a new
`bfs_query` with `node_types` and `predicates` filters focused on what matters.
The third query is more expensive than the first but more targeted.

This is Denning's working set principle applied to graph queries. The topology
survey establishes what's in the neighborhood cheaply. The expansion and
re-query fill in detail selectively.

## Multi-Seed Queries

The multi-seed case deserves attention because it is the natural form for a large
class of interesting questions.

"What do these two drugs have in common?" "What connects this disease to this gene?"
These are questions about the intersection of two neighborhoods. In a single-seed
model, the LLM issues two queries, holds both results in context, and reasons about
the overlap manually. With multi-seed support, it issues one query with two seeds
and receives the union, deduplicated, in a single response.

A concrete example: a 1-hop multi-seed query from a vasopressin analog drug and
Cushing syndrome returns 35 nodes and 37 edges. Of those, exactly two nodes are
in the direct neighborhood of both seeds -- the paper that co-describes both
entities, and the specific disease subtype the drug treats. Those two nodes are
the structural answer to "what connects these two entities?" Found in a single
query, without manual intersection. The LLM does not need to know it is looking
for an intersection. It issues the query, receives the result, and the answer
is present in the topology.

## MCP as the Delivery Mechanism

BFS-QL is implemented as a server using the Model Context Protocol (MCP),
a standard introduced by Anthropic in 2024 for connecting language models to
external tools and data sources. The value of MCP is standardization: a tool
implemented to the MCP specification can be connected to any MCP-compatible
client without modification. The tool vendor doesn't need to know which model
will call it. The model vendor doesn't need to know which tools will be
connected. This is the same insight that made HTTP successful as a web substrate
and USB successful as a hardware interface standard: an agreed protocol,
implemented independently by many parties, creates a market of interoperable
components.

Connecting a BFS-QL graph to an MCP-compatible LLM client takes three steps:

```bash
# Start the server
uv run bfs-ql serve --backend postgres --transport sse \
  --description "My knowledge graph"

# Register with the client (one time)
claude mcp add --transport sse --scope user my-graph \
  http://127.0.0.1:8000/sse
```

Then start a new session. The four BFS-QL tools are available immediately. No
schema configuration, no query templates, no prompt engineering. The graph
self-describes through `describe_schema`; the server instructions carry
graph-specific guidance; the tools define their own parameters and return types.

MCP is a transport and discovery protocol. It specifies how tools are described
(JSON Schema), how they are called (JSON-RPC), and how results are returned.
It does not specify what the tools do or how the LLM should use them. That is
determined by BFS-QL.

This is the right division of labor. MCP handles the plumbing. BFS-QL handles
the graph interface semantics: stubs versus full nodes, topology completeness,
the working set model, canonical IDs as seeds. The model handles the reasoning:
what to query, what the results mean, what to do with them.

None of these three components knows more than it needs to about the others.
The model doesn't know whether the backend is Postgres or a SPARQL endpoint or
Neo4j -- it only sees four tools. BFS-QL doesn't know what question the model
is trying to answer -- it only executes queries. The backend doesn't know
anything about either -- it only answers eight primitive navigation operations.
Each layer is replaceable independently of the others.

The graph a hospital runs against its clinical knowledge base is, from the
model's perspective, indistinguishable from the graph a pharmaceutical company
runs against its compound library, or the graph a university runs against its
research literature. Same four tools. Same query format. Same session workflow.
The protocol is how that uniformity is delivered.

---

The next article covers what's on the other side of that interface: the backend
abstraction that lets any graph store -- Postgres, SPARQL, Neo4j, or a custom
REST API -- speak BFS-QL with eight method implementations.
