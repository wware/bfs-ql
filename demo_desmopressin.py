"""
Desmopressin / Bleeding Disorders Demo
=======================================
Demonstrates BFS-QL features against a live DBpedia SPARQL endpoint:

  1. topology_only=True  -- pure structure, no metadata (fast)
  2. node_types filter   -- full metadata only for owl:Thing nodes;
                           category/class nodes appear as lightweight stubs
  3. predicates filter   -- full metadata only for DBpedia-owl:medication edges;
                           all other edges appear as stubs
  4. describe_entity()   -- rich expansion of a single stub node

Server startup (see ./H for full flags):
    uv run bfs-ql serve --backend sparql --transport sse --port 8001 \\
        --bif-contains --max-concurrent 1 --request-delay 0.2 \\
        --restrict-to-prefixes --log-level WARNING \\
        --endpoint https://dbpedia.org/sparql \\
        --prefix DBpedia=http://dbpedia.org/resource/ \\
        --prefix DBpedia-owl=http://dbpedia.org/ontology/ \\
        --exclude-predicate DBpedia-owl:wikiPageWikiLink \\
        --exclude-predicate DBpedia-owl:wikiPageRedirects \\
        --exclude-predicate "http://dbpedia.org/property/wikiPageUsesTemplate" \\
        --description "DBpedia: open encyclopedia knowledge graph derived from Wikipedia"

Usage:
    uv run python demo_desmopressin.py
"""

import asyncio
import json
import textwrap
from typing import Any

from fastmcp import Client


SERVER_URL = "http://127.0.0.1:8001/sse"
SEED = "DBpedia:Desmopressin"

# Node type that gets full metadata; everything else becomes a stub.
FULL_NODE_TYPE = "http://www.w3.org/2002/07/owl#Thing"

# Predicate that gets full metadata; all others become stubs.
FULL_PREDICATE = "DBpedia-owl:medication"

# A bleeding-disorder node to expand at the end.
BLEEDING_DISORDER = "DBpedia:Von_Willebrand_disease"


def _pp(label: str, data: Any) -> None:
    """Pretty-print a labelled JSON result."""
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(json.dumps(data, indent=2, ensure_ascii=False)[:4000])
    if len(json.dumps(data, ensure_ascii=False)) > 4000:
        print("  ... (truncated)")


def _summarise_bfs(result: dict, label: str) -> None:
    """Print a compact summary of a BfsResult."""
    nodes = result.get("nodes", [])
    edges = result.get("edges", [])
    full_nodes  = [n for n in nodes if n.get("metadata")]
    stub_nodes  = [n for n in nodes if not n.get("metadata")]
    full_edges  = [e for e in edges if e.get("metadata") is not None]
    stub_edges  = [e for e in edges if e.get("metadata") is None]

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Nodes : {len(nodes):>4}  ({len(full_nodes)} full, {len(stub_nodes)} stubs)")
    print(f"  Edges : {len(edges):>4}  ({len(full_edges)} full, {len(stub_edges)} stubs)")

    if full_nodes:
        print("\n  -- Full nodes (have metadata) --")
        for n in full_nodes:
            etype = n.get("entity_type", "?")
            # Shorten long URIs for display
            etype_short = etype.split("/")[-1].split("#")[-1]
            print(f"    {n['id']}  [{etype_short}]")

    if stub_nodes:
        print("\n  -- Stub nodes (no metadata) --")
        for n in stub_nodes:
            etype = n.get("entity_type", "?")
            etype_short = etype.split("/")[-1].split("#")[-1]
            print(f"    {n['id']}  [{etype_short}]")

    if full_edges:
        print("\n  -- Full edges (have metadata) --")
        for e in full_edges:
            print(f"    {e['subject']}  --[{e['predicate']}]-->  {e['object']}")

    if stub_edges:
        print("\n  -- Stub edges (no metadata) --")
        for e in stub_edges:
            print(f"    {e['subject']}  --[{e['predicate']}]-->  {e['object']}")


async def run_demo() -> None:
    async with Client(SERVER_URL) as client:

        # ------------------------------------------------------------------
        # 0. Schema
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print("# STEP 0: describe_schema()")
        print("#"*70)
        schema = await client.call_tool("describe_schema", {})
        schema_data = json.loads(schema.content[0].text)
        print(f"  Graph description: {schema_data.get('graph_description', '(none)')}")
        print(f"  Entity types returned: {len(schema_data.get('entity_types', []))}")
        print(f"  Predicates returned  : {len(schema_data.get('predicates', []))}")

        # ------------------------------------------------------------------
        # 1. Topology-only 1-hop
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print("# STEP 1: bfs_query(topology_only=True, max_hops=1)")
        print("#  Pure structure -- zero metadata fetched.  Very fast.")
        print("#"*70)
        r = await client.call_tool("bfs_query", {
            "seeds": [SEED],
            "max_hops": 1,
            "topology_only": True,
        })
        topo1 = json.loads(r.content[0].text)
        nodes = topo1.get("nodes", [])
        edges = topo1.get("edges", [])
        print(f"\n  1-hop topology: {len(nodes)} nodes, {len(edges)} edges")
        print("  Node IDs:")
        for n in nodes:
            print(f"    {n['id']}  [{n.get('entity_type','?').split('/')[-1].split('#')[-1]}]")

        # ------------------------------------------------------------------
        # 2. Topology-only 2-hop
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print("# STEP 2: bfs_query(topology_only=True, max_hops=2)")
        print("#  Expands to the 2-hop neighbourhood -- still no metadata.")
        print("#"*70)
        r = await client.call_tool("bfs_query", {
            "seeds": [SEED],
            "max_hops": 2,
            "topology_only": True,
        })
        topo2 = json.loads(r.content[0].text)
        nodes2 = topo2.get("nodes", [])
        edges2 = topo2.get("edges", [])
        print(f"\n  2-hop topology: {len(nodes2)} nodes, {len(edges2)} edges")

        # ------------------------------------------------------------------
        # 3. Stubs vs full nodes
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print(f"# STEP 3: bfs_query with node_types=['{FULL_NODE_TYPE}']")
        print("#  owl:Thing nodes get full metadata.")
        print("#  skos:Concept categories and owl:Class nodes become stubs.")
        print("#"*70)
        r = await client.call_tool("bfs_query", {
            "seeds": [SEED],
            "max_hops": 1,
            "node_types": [FULL_NODE_TYPE],
        })
        result3 = json.loads(r.content[0].text)
        _summarise_bfs(result3, f"1-hop, node_types=['{FULL_NODE_TYPE}']")

        # ------------------------------------------------------------------
        # 4. Stubs vs full edges
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print(f"# STEP 4: bfs_query with predicates=['{FULL_PREDICATE}']")
        print(f"#  Only '{FULL_PREDICATE}' edges carry metadata.")
        print("#  All other edge types become stubs (structure preserved).")
        print("#"*70)
        r = await client.call_tool("bfs_query", {
            "seeds": [SEED],
            "max_hops": 1,
            "node_types": [FULL_NODE_TYPE],
            "predicates": [FULL_PREDICATE],
        })
        result4 = json.loads(r.content[0].text)
        _summarise_bfs(result4, f"1-hop, node_types + predicates=['{FULL_PREDICATE}']")

        # ------------------------------------------------------------------
        # 5. describe_entity on a bleeding-disorder stub
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print(f"# STEP 5: describe_entity('{BLEEDING_DISORDER}')")
        print("#  Expands a stub node into full metadata.")
        print("#  Tells the mechanistic story: Desmopressin treats vWD.")
        print("#"*70)
        r = await client.call_tool("describe_entity", {"id": BLEEDING_DISORDER})
        entity = json.loads(r.content[0].text)

        interesting_keys = ["label", "description", "synonyms", "symptoms",
                            "treatment", "icd10", "meshId", "omim"]
        print(f"\n  Entity: {entity.get('id')}")
        print(f"  Type  : {entity.get('entity_type')}")
        for k in interesting_keys:
            v = entity.get(k)
            if v is None:
                continue
            # Truncate long multi-language label lists
            if isinstance(v, list) and len(v) > 3:
                v = v[:3] + [f"... ({len(v)-3} more)"]
            text = json.dumps(v, ensure_ascii=False)
            print(f"  {k:12s}: {textwrap.shorten(text, 120)}")

        # ------------------------------------------------------------------
        # 6. Narrative summary
        # ------------------------------------------------------------------
        print("\n" + "#"*70)
        print("# NARRATIVE SUMMARY")
        print("#"*70)
        print(textwrap.dedent("""
          Desmopressin is a synthetic analogue of arginine-vasopressin (AVP),
          classified as both a Drug and a ChemicalSubstance in DBpedia.

          In the 1-hop neighbourhood:
            - DBpedia-owl:medication edges connect Desmopressin to two disease
              nodes: Central_diabetes_insipidus and Diabetes_insipidus.
            - The drug is also listed as an agonist of Vasopressin receptor nodes.

          BFS-QL filtering mechanics demonstrated here:
            node_types  -- owl:Thing nodes carry full metadata (labels,
                           descriptions, ICD codes, synonyms, treatment info);
                           skos:Concept category nodes and owl:Class nodes
                           are returned as lightweight stubs that preserve the
                           full graph topology without the payload.

            predicates  -- DBpedia-owl:medication edges carry full metadata;
                           dc:subject, rdf:type, and property/* edges appear as
                           stubs.  The graph structure is never omitted -- only
                           the detail level changes.

            topology_only -- skips ALL metadata fetches entirely; useful as a
                           first move to survey an unfamiliar graph before
                           deciding which nodes/edges warrant detail.

          Von Willebrand disease (1-hop from Desmopressin via Vasopressin):
            Desmopressin boosts plasma levels of von Willebrand factor by
            stimulating endothelial V2 receptors -- the same mechanism that
            makes it effective in Central diabetes insipidus.  The describe_entity
            call reveals the ICD-10 code, OMIM entry, and multilingual labels
            that confirm we are looking at the right node.
        """))

    print("\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(run_demo())
