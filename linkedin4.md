# I Was Wrong About Four

In the second article in this series, I wrote: *"There is no fifth operation in this list. Any candidate fifth tool -- 'find shortest path,' 'count neighbors,' 'list all entities of type X' -- either reduces to a composition of these four or adds complexity without adding coverage. The surface is complete."*

I meant it when I wrote it. I was wrong.

To be precise: I was right about the surface being minimal. I was right that "find shortest path" and "count neighbors" are the wrong things to add. I was right that adding tools for their own sake adds burden, not capability. All of that holds.

What I missed was a specific, common pattern of reasoning that the four-tool surface handles awkwardly -- not incorrectly, but awkwardly enough that in practice the LLM struggles. Discovering this required actually using the system against real graphs, which is how these things usually go.

---

## The Operation I Missed

The four-tool surface handles the question *"what connects to this entity?"* cleanly. It handles *"what do multiple entities share?"* poorly.

Consider the question: *"What actors appeared in movies with both Tom Hanks and Meg Ryan?"*

With four tools, the LLM's options are:

1. Traverse from Tom Hanks (1 hop), traverse from Meg Ryan (1 hop), hold both result sets in context, manually identify the intersection.
2. Issue a multi-seed traversal from both simultaneously, receive the union, and manually identify which nodes appear in *both* neighborhoods.

Neither is technically wrong. But in both cases, "manually identify" means the LLM is doing set intersection in context -- examining potentially hundreds of nodes, comparing them, filtering down to what appears in all neighborhoods.  That is exactly the kind of structured bookkeeping that language models do poorly. It is also expensive: a 1-hop neighborhood from a well-connected actor node in a movie recommendations graph can contain hundreds of nodes. Two overlapping hundred-node neighborhoods held simultaneously in context is a lot of structural noise for a simple relational question.

The multi-seed `bfs_query` returns the *union* of neighborhoods. That is useful for many questions. It is the wrong operation when the question is about the *intersection*.

---

## What Was Actually Needed

The fifth tool is `intersect_subgraphs`. Given a list of seed entities and a hop count k, it returns the nodes within k undirected hops of *every* seed -- the intersection of neighborhoods, not the union.

```
intersect_subgraphs(
  seeds=["Movie:TomHanks", "Movie:MegRyan"],
  k=1
)
→ nodes within 1 hop of both Tom Hanks AND Meg Ryan
```

For the Tom Hanks / Meg Ryan question, this returns the co-stars who appeared in films with both of them. Not Tom Hanks' full filmography neighborhood. Not Meg Ryan's full filmography neighborhood. Not the union of both. The intersection. Directly.

The operation is useful whenever the question has the form *"what is common to all of these?"* -- which turns out to be a large fraction of interesting relational questions:

- Common co-authors of two researchers
- Shared diseases between two drugs in a clinical graph
- Overlapping pathways between two genes
- Cities visited by all members of a travel group
- Papers citing both of two foundational works

In each case, the answer is structural -- it lives in the graph as a set intersection -- and the LLM shouldn't have to do the set bookkeeping manually.

---

## Why This Is Different from "Find Shortest Path"

In the previous article I argued that candidate fifth tools either reduce to compositions of the existing four or add complexity without coverage. Why doesn't `intersect_subgraphs` fall into the first category?

The reduction argument would go: issue two separate traversals, take the intersection of the ID sets, done. That works -- but it fails the "can the LLM do this reliably?" test. The problem is not whether the operation is *logically* decomposable into the existing primitives. The problem is whether the LLM can *reliably execute that decomposition in context*.

Set intersection over two hundred-node result sets is structured bookkeeping.  Language models are not good at structured bookkeeping. They miss nodes, they get confused by similar IDs, they conflate approximate matches with exact ones.  The right test for whether an operation belongs in the protocol is not "is this logically primitive?" but "does putting it in the protocol produce reliably better outcomes than leaving it out?" For `intersect_subgraphs`, the answer is yes.

Shortest path does not pass that test. An LLM that wants to know whether two entities are connected can issue a multi-hop traversal and inspect the resulting topology. The traversal result is a navigational handle the LLM can reason about. A shortest path is a fact, not a handle -- it tells the LLM something, but it doesn't give it anything to navigate next. It also assumes a specific structure (directedness, path semantics) that is not uniform across knowledge graphs. `intersect_subgraphs` has neither of these problems.

---

## The Implementation

The tool lives entirely in the server layer. No backend changes were required.  `Neo4jBackend`, `SparqlBackend`, and `PostgresBackend` all support it automatically.

The implementation uses the eight-method ABC: for each seed, perform a k-hop undirected BFS using `edges_from` and `edges_to`, collect the set of reachable node IDs, intersect the sets, fetch the resulting nodes. The traversal is batched per hop -- one `edges_from` and one `edges_to` call per frontier node, all concurrent via `asyncio.gather`. Seeds are traversed concurrently as well.

```python
async def neighborhood_intersection(
    db: GraphDbInterface,
    seeds: list[str],
    k: int,
) -> list[EntityStub]:
    if not seeds:
        return []
    neighborhoods = await asyncio.gather(
        *[_k_hop_neighborhood(db, seed, k) for seed in seeds]
    )
    for neighborhood in neighborhoods:
        if not neighborhood:   # missing seed → empty intersection
            return []
    common_ids = set.intersection(*neighborhoods)
    nodes = await db.get_nodes_batch(list(common_ids))
    return [EntityStub(id=n.id, entity_type=n.entity_type) for n in nodes]
```

The result is a list of `EntityStub` records -- IDs and entity types. The caller can then pass any interesting IDs to `describe_entity` for full metadata, or to `bfs_query` to explore their neighborhoods.

---

## A Concrete Example

This came up during testing of the Neo4j backend using the Recommendations dataset on sandbox.neo4j.com -- a movie and ratings graph with 28,000+ nodes and 166,000+ edges, substantially larger than the 36-paper biomedical corpus used in earlier articles or the 171-node Movies sandbox used for initial Neo4j integration tests. At that scale, the weakness of the four-tool surface for intersection questions became impossible to ignore.

Asking *"What actors appeared in movies with both Tom Hanks and Meg Ryan?"* via `intersect_subgraphs(seeds=["Tom Hanks", "Meg Ryan"], k=2)` returns a focused set of co-stars -- people who appeared alongside both actors in the same films. Same tool. Same call. Same reliable result, regardless of graph size or backend.

Before this tool existed, the LLM would typically issue two separate traversals, receive ~200 nodes across both result sets, and then attempt to manually identify the overlap. On a good day it got the right answer. On a bad day it missed nodes or hallucinated connections. The operation was doable. It was not reliable.

Reliability is the standard. Not logical possibility.

---

## What Actually Stays the Same

The previous articles' core arguments are unchanged:

The four original tools are still the right four. Orient, resolve, traverse, expand: these cover the fundamental workflow and nothing in that workflow has changed. The fifth tool is additive, not corrective.

The eight-method backend contract is still the right abstraction. Adding `intersect_subgraphs` to the server layer required zero changes to any backend.  The separation between traversal intelligence and graph navigation primitives held exactly as designed.

The minimal surface argument still holds -- it just has a revised minimum. The test is not "can this be decomposed into existing operations?" but "does adding this produce reliably better outcomes?" Four passed that test in 2024. Five passes it now.

---

The lesson I take from this is not that the original analysis was careless.  It is that "complete" is a claim that requires empirical validation, not just logical argument. The four-tool surface was complete against the use cases I had considered. `intersect_subgraphs` emerged from actual use -- a session where the LLM was visibly struggling with a question it should have been able to answer -- and that is the right way for additions to emerge. Not from theorizing about what might be useful, but from observing where the interface fails under real conditions.

The protocol is now at five tools. I expect it will stay there for a while. I am less confident than I was about the word "complete."
