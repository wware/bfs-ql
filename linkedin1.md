# The Knowledge Is in the Graph. Your LLM Can't Get to It.

Structured knowledge graphs -- DBpedia, Wikidata, UniProt, corporate Neo4j
instances, domain-specific SPARQL endpoints -- contain enormous amounts of
curated, queryable knowledge. Almost none of it is reliably accessible to a
language model in practice. Here is why, and what a better interface would
look like.

---

## The Graph RAG Problem

In 2023, Microsoft Research published a well-executed paper on Graph RAG --
using knowledge graphs to improve LLM reasoning. The results were real.
Developers read it and started building.

What happened next was instructive. The demos worked. The production
deployments were harder. Teams connecting LLMs to real graphs -- existing
SPARQL endpoints, corporate Neo4j instances, Wikidata, domain-specific triple
stores -- ran into the same problems repeatedly. The models wrote queries that
were syntactically plausible but semantically broken. They hallucinated
predicate names. They got URI prefixes wrong. They produced SPARQL that parsed
but returned nothing, or returned the wrong thing, or timed out.

The knowledge was in the graph. The LLM still couldn't reliably get to it.

These failures were not random and they were not going to be fixed by better
prompting. They followed from something more fundamental: the mismatch between
how graph query languages are structured and how language models actually work.

## Why Context Windows Make This Hard

In 2017, the transformer architecture introduced a constraint that underlies
every large language model in use today: self-attention is O(n²) in sequence
length. Double the context and you roughly quadruple the compute. Every token
in the context window imposes a cost on every other token. This is not a
hardware limitation -- it is structural to how transformers work.

The practical consequence: a knowledge graph neighborhood within two or three
hops of a seed node can easily contain hundreds of nodes and thousands of
edges. You cannot just serialize the neighborhood and stuff it into context.
The expense is real, and it compounds in an unexpected way.

In 2023, Stanford researchers published "Lost in the Middle: How Language
Models Use Long Contexts." The finding was stark: LLM performance on
retrieval tasks degrades sharply for information positioned in the middle of
a long context. Models were good at using information near the beginning and
near the end. The middle was a dead zone.

A large, unfiltered graph dump doesn't just waste tokens -- it actively
degrades reasoning. Giving the model more context is not the answer. Giving
it the right context is.

This is a problem computer architects solved sixty years ago. In the 1960s,
RAM was expensive and scarce, just like context window space is today. Peter
Denning's working set theory (1968) formalized the question: what is the
minimum a process needs in fast memory to run efficiently? The answer was
not "everything" and not "nothing" -- it was the working set, the pages
currently active in the computation. Keep those in fast memory, page
everything else out.

The context window is fast memory. The graph is backing storage. The design
question is not "how much of the graph can we fit?" but "what does the model
actually need right now?"

## Why SPARQL Generation Fails

The natural first answer is to let the LLM write the query. SPARQL is
expressive and powerful. Just ask the model to generate it.

A language model generates text token by token, sampling from a probability
distribution conditioned on what came before. It has no symbolic reasoner,
no query planner, no schema validator -- just statistical patterns from its
training corpus. When asked to write a query, it produces text that *looks*
like a query. Most of the time, the surface form is correct. The query
parses.

What the model cannot do is verify. It cannot check that a predicate name it
generated actually exists in the target schema. It cannot confirm that a URI
prefix is valid for this endpoint. It generates plausible text and stops.
Verification is not part of the architecture.

The failure modes are predictable:

**Hallucinated predicates.** The model writes `dbo:treatedBy` when the actual
predicate is `mesh:treats`. The query returns nothing. The model concludes
the relationship doesn't exist in the graph -- a false negative.

**Wrong URI prefixes.** `dbo:` and `dbr:` are different namespaces in
DBpedia. `wd:` and `wdt:` are different in Wikidata. The distinctions are
non-obvious and frequently confused.

**Syntactically valid, semantically empty.** Some failures produce queries
that parse, execute, and return results -- just not the right ones.

These are not bugs that better prompting fixes. They are structural
consequences of asking a model to generate a precise formal language against
a schema it cannot inspect, with no feedback loop.

Cypher has the same problem. Different syntax, identical failure mechanism.

## Why RAG Doesn't Close the Gap

The natural response to query generation failures is to bypass it entirely:
retrieve relevant content from the graph and give it to the model as text.

The insight behind RAG is sound. Giving the model something to reason from
rather than asking it to reason from memory reduces hallucination. For
document retrieval, vector similarity search is a good fit.

Graphs break this in a specific way. Relevance in a graph is structural, not
semantic. The most important node for answering a question might be two hops
away from any node that looks semantically similar to the query.

Consider a question about drug interactions for a specific patient profile.
The relevant nodes include the drug, its metabolic targets, the enzymes those
targets share with other drugs the patient is taking, and the clinical
outcomes associated with those shared pathways. None of those intermediate
nodes -- the enzymes, the pathways -- are semantically similar to "drug
interactions for this patient." They are structurally connected to the answer.
Vector similarity retrieval will not find them.

Vector retrieval asks: what is *near* this query in embedding space? Graph
traversal asks: what is *connected* to what I already know? For multi-hop
relational reasoning, pathway analysis, and provenance tracing, the second
question is the right one. A retrieval system built for the first question
answers the second question poorly -- not because the implementation is bad
but because the operation is wrong.

## What a Better Interface Looks Like

In 1980, David Patterson at UC Berkeley and John Hennessy at Stanford were
separately arriving at an uncomfortable conclusion about computer architecture.
The prevailing wisdom was that more was better: more instructions, more
addressing modes, more hardware support for complex operations. The VAX-11/780
was the apotheosis of this philosophy -- hundreds of instructions, some
extraordinarily powerful. Compiler writers loved it.

Patterson and Hennessy thought it was a mistake. Complex instructions were
expensive in ways that weren't obvious and beneficial in ways that were
overstated. The hardware complexity required to implement the full instruction
set made the processor harder to pipeline, harder to verify, and harder to
push to higher clock speeds. Simplicity wasn't a limitation. It was an
advantage.

The resulting architecture -- RISC, Reduced Instruction Set Computing -- was
controversial. It contradicted decades of conventional wisdom. The market
settled the argument. RISC architectures came to dominate embedded computing,
then mobile computing, then high-performance desktop computing. Fewer, simpler
operations, composable by the compiler, outperformed the rich surface area
that had seemed like a gift to programmers.

The lesson applies directly to LLM tool design. A large interface surface
area is not a feature. It is a burden on any automated system that must
generate calls into it reliably.

An approach we could take: expose the graph through a small number of
composable operations designed for traversal, not querying.

- **Orient** -- one call that returns the graph's entity types and predicates, so the model knows what exists before it tries to navigate.
- **Resolve** -- one call that maps a natural-language name to a canonical ID. The model should never need to guess an identifier.
- **Traverse** -- one call that expands a breadth-first search neighborhood from one or more seed entities, returning full data for the node types and predicates the model cares about and lightweight stubs for everything else.
- **Drill down** -- one call to retrieve full detail for any stub that warrants closer inspection.

Four operations. Orient, resolve, traverse, drill down. That is the minimum
complete set. There is no fifth operation that adds capability without adding
complexity the model must now reason about.

The stub design deserves attention, because it solves the context problem
directly. When a query filters for specific node types, the naive approach
is to discard non-matching nodes. But that produces a misleading picture --
the model sees a Disease connected to ten drugs and doesn't know the Disease
is also connected to genes and publications. It can't ask follow-up questions
about connections it doesn't know exist.

The right answer separates topology from presentation. Non-matching nodes
appear as stubs: present in the result, carrying only their ID and type,
consuming almost no context. The model knows the topology is complete. It
can follow up on any stub it wants to explore. The context cost is paid only
where the model has declared it matters.

This is Denning's working set answer applied to graph data. Not "give me
everything" and not "give me only what matches" -- give me the working set:
full data where I need it, topology everywhere else.

**One more thing about the seeds.** In this approach, the model navigates
by canonical ID -- the stable identifiers maintained by ontological
authorities like MeSH for diseases, HGNC for genes, RxNorm for drugs,
UniProt for proteins. This is not an implementation detail. A canonical ID
is not just a unique key. When you assign a MeSH term to a disease entity,
you are connecting it to the accumulated judgment of a community of experts:
its definition, its place in a carefully considered taxonomy, its
distinctions from related concepts, its history of revision. The identifier
is a pointer into a shared epistemic commons -- the entity is not merely
named, it is *placed* in the structure of human knowledge as that community
understands it.

That placement is what makes the graph worth querying. A node identified
by a canonical ID inherits the epistemic authority of the ontology it
anchors to. Relationships between canonically identified entities are claims
that can be evaluated against that shared structure. A graph built on
canonical IDs is not just organized data -- it is data that participates in
something larger: the accumulated, maintained, trusted infrastructure of
human knowledge about a domain.

---

The knowledge is in the graph. The interface is the missing piece -- not
because the problem is unsolved, but because the natural first answers
(write SPARQL, use RAG) don't fit how language models actually work.
Traversal fits. Minimal surface fits. Working-set-aware responses fit.
The pieces are all available.
