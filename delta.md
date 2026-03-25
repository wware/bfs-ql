# Schema Design Discussion (follow-on from DBpediaProblems.md)

**Will**

One thing I've realized is that BFS-QL's interaction with DBpedia (or whatever source) is not
constrained by the context window. It can always pull a full subgraph, and then filter it when
serializing for the LLM. So name/type info is always available to BFS-QL. Does that change the
calculus?

**Claude**

Yes, significantly. The `schema_summary` idea becomes essentially free and reliable — BFS-QL
already has the full subgraph in memory, so summarizing types and predicates found is just a fold
over data you already hold, with no extra backend calls.

It also means the bootstrapping problem mostly dissolves. The LLM doesn't need to know predicates
before querying — it just needs a seed entity ID (via `search_entities`), fires a 1-hop BFS, and
the response comes back with a `schema_summary` giving it exactly the vocabulary relevant to that
neighborhood.

This shifts the design question away from "how do we give the LLM schema knowledge upfront" toward
"how do we make the query response self-describing." The `describe_schema` tool becomes less
important for heterogeneous backends — still useful for backends with a complete schema, but for
DBpedia-style graphs the real entry point is: search → BFS → read the schema_summary in the result.

The `comprehensive` flag still matters, but its job is narrower: just tell the LLM "don't expect
`describe_schema` to give you a complete picture, rely on what comes back in query results instead."

**Will**

Maybe we can give `describe_schema` an argument flag that says, "this is the first operation, give
me a topology-only subgraph regardless whether comprehensive or not."

**Claude**

Yes — essentially an "orient me" operation. A `seeds` argument: if non-null, run a topology-only
BFS from those seeds (1 hop, or backend-determined) and return that plus a `schema_summary`.
Fan-out cap worth adding to avoid overwhelming results on wide graphs. The pieces fit cleanly:
`seeds` is optional so existing behavior is unchanged, topology-only BFS is already implemented,
`schema_summary` falls naturally out of the result, and `comprehensive` sits alongside
independently.

The main implementation note: `describe_schema` currently lives in `server.py` and delegates to
the backend for schema info. Adding a `seeds` path means it would also need to call into the BFS
engine — not a problem architecturally, but the call path changes for that code path.

**Will**

I think some of this gets simpler if the backend is told "here are a few different prompts for
different sizes or natures of graph data, offer the LLM the prompt that makes sense."

**Claude**

Good inversion — the backend knows its own data best and is in the best position to ship the right
playbook. Concretely: `describe_schema` returns a `recommended_workflow` string (or short list of
steps) that the backend populates. For a comprehensive backend: "call describe_schema, use these
types and predicates." For DBpedia: "call search_entities first, then 1-hop BFS, read the
schema_summary."

This simplifies a lot — the `seeds` idea, the `comprehensive` flag, even the fan-out cap concern
all become backend-local decisions rather than protocol-level machinery. The protocol just needs a
field to carry the workflow hint. `schema_summary` in BFS results is still worth keeping at the
protocol level — it's genuinely useful regardless of backend and costs nothing to produce.
