"""Unit tests for SparqlBackend using mocked HTTP (no network required).

Small fixed graph used throughout:

    DBpedia:Desmopressin --DBpedia-owl:drugUsedForTreatment--> DBpedia:Cushings_disease
    DBpedia:Desmopressin --DBpedia-owl:mechanismOfAction-->    DBpedia:AVPR2
    DBpedia:Cushings_disease --DBpedia-owl:symptom-->          DBpedia:Hypertension

Prefix map:
    DBpedia     -> http://dbpedia.org/resource/
    DBpedia-owl -> http://dbpedia.org/ontology/
    rdf         -> http://www.w3.org/1999/02/22-rdf-syntax-ns#
    rdfs        -> http://www.w3.org/2000/01/rdf-schema#
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bfsql.backends.sparql import SparqlBackend
from bfsql.cache import CachedGraphDb
from bfsql.engine import bfs_query
from bfsql.models import BfsQuery, Edge


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

ENDPOINT = "https://dbpedia.org/sparql"

PREFIXES = {
    "DBpedia":     "http://dbpedia.org/resource/",
    "DBpedia-owl": "http://dbpedia.org/ontology/",
    "rdf":         "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs":        "http://www.w3.org/2000/01/rdf-schema#",
}

# Full URIs for test entities
URI_DESMO    = "http://dbpedia.org/resource/Desmopressin"
URI_CUSHINGS = "http://dbpedia.org/resource/Cushings_disease"
URI_AVPR2    = "http://dbpedia.org/resource/AVPR2"
URI_HYPER    = "http://dbpedia.org/resource/Hypertension"

URI_TREATS    = "http://dbpedia.org/ontology/drugUsedForTreatment"
URI_MECHANISM = "http://dbpedia.org/ontology/mechanismOfAction"
URI_SYMPTOM   = "http://dbpedia.org/ontology/symptom"

URI_TYPE      = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
URI_LABEL     = "http://www.w3.org/2000/01/rdf-schema#label"
URI_DRUG      = "http://dbpedia.org/ontology/Drug"
URI_DISEASE   = "http://dbpedia.org/ontology/Disease"
URI_GENE      = "http://dbpedia.org/ontology/Gene"

# Compressed (canonical) IDs
ID_DESMO    = "DBpedia:Desmopressin"
ID_CUSHINGS = "DBpedia:Cushings_disease"
ID_AVPR2    = "DBpedia:AVPR2"
ID_HYPER    = "DBpedia:Hypertension"

PRED_TREATS    = "DBpedia-owl:drugUsedForTreatment"
PRED_MECHANISM = "DBpedia-owl:mechanismOfAction"
PRED_SYMPTOM   = "DBpedia-owl:symptom"

TYPE_DRUG    = "DBpedia-owl:Drug"
TYPE_DISEASE = "DBpedia-owl:Disease"
TYPE_GENE    = "DBpedia-owl:Gene"


# ---------------------------------------------------------------------------
# SPARQL JSON result builders
# ---------------------------------------------------------------------------

def _uri(value: str) -> dict[str, str]:
    return {"type": "uri", "value": value}


def _literal(value: str, lang: str | None = None) -> dict[str, str]:
    result: dict[str, str] = {"type": "literal", "value": value}
    if lang:
        result["xml:lang"] = lang
    return result


def _bnode(value: str) -> dict[str, str]:
    return {"type": "bnode", "value": value}


def _sparql_result(vars: list[str], bindings: list[dict[str, Any]]) -> dict:
    return {"results": {"bindings": bindings}}


# ---------------------------------------------------------------------------
# Mock helper
# ---------------------------------------------------------------------------

def make_backend(**kwargs) -> SparqlBackend:
    """Create a SparqlBackend without opening any HTTP session."""
    return SparqlBackend(endpoint=ENDPOINT, prefixes=PREFIXES, **kwargs)


def patch_query(backend: SparqlBackend, bindings: list[dict]):
    """Patch backend._query to return given bindings without HTTP."""
    backend._query = AsyncMock(return_value=bindings)


# ---------------------------------------------------------------------------
# URI compress / expand tests
# ---------------------------------------------------------------------------

def test_compress_known_prefix():
    b = make_backend()
    assert b._compress(URI_DESMO) == ID_DESMO


def test_compress_unknown_prefix():
    b = make_backend()
    uri = "http://example.org/Thing"
    assert b._compress(uri) == uri


def test_expand_known_prefix():
    b = make_backend()
    assert b._expand(ID_DESMO) == URI_DESMO


def test_expand_unknown_prefix():
    b = make_backend()
    uri = "http://example.org/Thing"
    assert b._expand(uri) == uri


# ---------------------------------------------------------------------------
# edges_from
# ---------------------------------------------------------------------------

async def test_edges_from():
    """edges_from returns outgoing URI-typed edges compressed to canonical IDs."""
    b = make_backend()
    patch_query(b, [
        {"p": _uri(URI_TREATS),    "o": _uri(URI_CUSHINGS)},
        {"p": _uri(URI_MECHANISM), "o": _uri(URI_AVPR2)},
    ])
    edges = await b.edges_from(ID_DESMO)
    assert len(edges) == 2
    assert Edge(subject=ID_DESMO, predicate=PRED_TREATS,    object=ID_CUSHINGS) in edges
    assert Edge(subject=ID_DESMO, predicate=PRED_MECHANISM, object=ID_AVPR2)    in edges


async def test_literal_filtered_from_edges_from():
    """Literal objects must not appear as edges (FILTER !isLiteral applied)."""
    b = make_backend()
    # Simulate backend already filtered -- return only URI bindings.
    # Test that even if a literal slips through the type check, it's excluded.
    patch_query(b, [
        {"p": _uri(URI_LABEL),  "o": _literal("Desmopressin", lang="en")},
        {"p": _uri(URI_TREATS), "o": _uri(URI_CUSHINGS)},
    ])
    edges = await b.edges_from(ID_DESMO)
    # Literal row has type "literal" not "uri" -- must be dropped
    assert len(edges) == 1
    assert edges[0].predicate == PRED_TREATS


async def test_blank_node_filtered_from_edges_from():
    """Blank node objects must not appear as edges."""
    b = make_backend()
    patch_query(b, [
        {"p": _uri(URI_TREATS), "o": _bnode("b0")},
        {"p": _uri(URI_TREATS), "o": _uri(URI_CUSHINGS)},
    ])
    edges = await b.edges_from(ID_DESMO)
    assert len(edges) == 1
    assert edges[0].object == ID_CUSHINGS


# ---------------------------------------------------------------------------
# edges_to
# ---------------------------------------------------------------------------

async def test_edges_to():
    """edges_to returns incoming URI-typed edges compressed to canonical IDs."""
    b = make_backend()
    patch_query(b, [
        {"s": _uri(URI_DESMO),    "p": _uri(URI_TREATS)},
        {"s": _uri(URI_CUSHINGS), "p": _uri(URI_SYMPTOM)},
    ])
    edges = await b.edges_to(ID_CUSHINGS)
    assert len(edges) == 2
    subjects = {e.subject for e in edges}
    assert ID_DESMO    in subjects
    assert ID_CUSHINGS in subjects
    for e in edges:
        assert e.object == ID_CUSHINGS


async def test_blank_node_filtered_from_edges_to():
    """Blank node subjects must not appear as edges."""
    b = make_backend()
    patch_query(b, [
        {"s": _bnode("b0"),      "p": _uri(URI_TREATS)},
        {"s": _uri(URI_DESMO),   "p": _uri(URI_TREATS)},
    ])
    edges = await b.edges_to(ID_CUSHINGS)
    assert len(edges) == 1
    assert edges[0].subject == ID_DESMO


# ---------------------------------------------------------------------------
# get_node
# ---------------------------------------------------------------------------

async def test_get_node():
    """get_node returns a Node with the first rdf:type, compressed."""
    b = make_backend()
    patch_query(b, [{"type": _uri(URI_DRUG)}])
    node = await b.get_node(ID_DESMO)
    assert node.id == ID_DESMO
    assert node.entity_type == TYPE_DRUG


async def test_get_node_returns_owl_thing_on_empty():
    """get_node returns owl:Thing entity_type when the entity has no rdf:type."""
    b = make_backend()
    patch_query(b, [])
    node = await b.get_node(ID_DESMO)
    assert node.id == ID_DESMO
    assert node.entity_type == "owl:Thing"


# ---------------------------------------------------------------------------
# metadata_for_node
# ---------------------------------------------------------------------------

async def test_metadata_for_node():
    """metadata_for_node returns literal properties as a flat dict."""
    b = make_backend()
    patch_query(b, [
        {"p": _uri(URI_LABEL), "o": _literal("Desmopressin")},
        {"p": _uri("http://dbpedia.org/ontology/iupacName"), "o": _literal("1-desamino-8-D-arginine vasopressin")},
    ])
    meta = await b.metadata_for_node(ID_DESMO)
    assert meta["label"] == "Desmopressin"
    assert meta["iupacName"] == "1-desamino-8-D-arginine vasopressin"


async def test_metadata_for_node_multi_value():
    """Multiple values for the same predicate are stored as a list."""
    b = make_backend()
    patch_query(b, [
        {"p": _uri(URI_LABEL), "o": _literal("Desmopressin", lang="en")},
        {"p": _uri(URI_LABEL), "o": _literal("Desmopressine", lang="fr")},
    ])
    meta = await b.metadata_for_node(ID_DESMO)
    assert isinstance(meta["label"], list)
    assert len(meta["label"]) == 2


async def test_metadata_for_node_skips_uris():
    """URI-valued properties are not included (FILTER isLiteral applied)."""
    b = make_backend()
    # Simulate a URI value sneaking through -- type check must exclude it
    patch_query(b, [
        {"p": _uri(URI_TYPE),  "o": _uri(URI_DRUG)},       # URI -- skip
        {"p": _uri(URI_LABEL), "o": _literal("Desmopressin")},
    ])
    meta = await b.metadata_for_node(ID_DESMO)
    # URI binding has type "uri" not "literal" -- must be excluded by the
    # explicit o-type check in the backend (defence-in-depth beyond FILTER)
    assert "label" in meta
    assert "type" not in meta


# ---------------------------------------------------------------------------
# metadata_for_edge
# ---------------------------------------------------------------------------

async def test_metadata_for_edge_returns_empty():
    """metadata_for_edge always returns {} (DBpedia has no provenance)."""
    b = make_backend()
    edge = Edge(subject=ID_DESMO, predicate=PRED_TREATS, object=ID_CUSHINGS)
    result = await b.metadata_for_edge(edge)
    assert result == {}


# ---------------------------------------------------------------------------
# search_entities
# ---------------------------------------------------------------------------

async def test_search_entities():
    """search_entities returns EntityStubs for matching entities."""
    b = make_backend()
    patch_query(b, [
        {"entity": _uri(URI_DESMO),    "type": _uri(URI_DRUG)},
        {"entity": _uri(URI_CUSHINGS), "type": _uri(URI_DISEASE)},
    ])
    results = await b.search_entities("desmo")
    assert len(results) == 2
    ids = {r.id for r in results}
    assert ID_DESMO    in ids
    assert ID_CUSHINGS in ids


async def test_search_entities_filters_blank_nodes():
    """search_entities skips results where entity or type is not a URI."""
    b = make_backend()
    patch_query(b, [
        {"entity": _bnode("b0"),       "type": _uri(URI_DRUG)},    # bnode entity
        {"entity": _uri(URI_DESMO),    "type": _literal("Drug")},  # literal type
        {"entity": _uri(URI_CUSHINGS), "type": _uri(URI_DISEASE)}, # valid
    ])
    results = await b.search_entities("x")
    assert len(results) == 1
    assert results[0].id == ID_CUSHINGS


# ---------------------------------------------------------------------------
# entity_types
# ---------------------------------------------------------------------------

async def test_entity_types():
    """entity_types returns compressed type URIs in frequency order."""
    b = make_backend()
    patch_query(b, [
        {"type": _uri(URI_DISEASE), "count": _literal("1200")},
        {"type": _uri(URI_DRUG),    "count": _literal("800")},
        {"type": _uri(URI_GENE),    "count": _literal("300")},
    ])
    types = await b.entity_types()
    assert types == [TYPE_DISEASE, TYPE_DRUG, TYPE_GENE]


async def test_entity_types_safe_distinct_false():
    """entity_types returns [] when safe_distinct=False."""
    b = make_backend(safe_distinct=False)
    types = await b.entity_types()
    assert types == []


async def test_entity_types_skips_non_uri():
    """entity_types skips bindings where type is not a URI."""
    b = make_backend()
    patch_query(b, [
        {"type": _literal("SomeType"), "count": _literal("500")},
        {"type": _uri(URI_DRUG),       "count": _literal("800")},
    ])
    types = await b.entity_types()
    assert types == [TYPE_DRUG]


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------

async def test_predicates():
    """predicates returns compressed predicate URIs (rdf:type excluded)."""
    b = make_backend()
    patch_query(b, [
        {"pred": _uri(URI_TREATS)},
        {"pred": _uri(URI_MECHANISM)},
        {"pred": _uri(URI_SYMPTOM)},
    ])
    preds = await b.predicates()
    assert PRED_TREATS    in preds
    assert PRED_MECHANISM in preds
    assert PRED_SYMPTOM   in preds
    # rdf:type is excluded by the SPARQL FILTER -- not in mocked results
    rdf_type_compressed = b._compress(URI_TYPE)
    assert rdf_type_compressed not in preds


async def test_predicates_safe_distinct_false():
    """predicates returns [] when safe_distinct=False."""
    b = make_backend(safe_distinct=False)
    preds = await b.predicates()
    assert preds == []


async def test_predicates_skips_non_uri():
    """predicates skips bindings where pred is not a URI."""
    b = make_backend()
    patch_query(b, [
        {"pred": _literal("notAPredicate")},
        {"pred": _uri(URI_TREATS)},
    ])
    preds = await b.predicates()
    assert preds == [PRED_TREATS]


# ---------------------------------------------------------------------------
# Unknown prefix passthrough
# ---------------------------------------------------------------------------

async def test_unknown_prefix_passthrough_compress():
    """URIs with no matching prefix pass through as-is."""
    b = make_backend()
    uri = "http://schema.org/name"
    assert b._compress(uri) == uri


async def test_unknown_prefix_passthrough_in_edges():
    """Unknown-prefix URIs in edges pass through as-is."""
    b = make_backend()
    foreign_uri = "http://schema.org/relatedTo"
    target_uri  = "http://schema.org/Thing"
    patch_query(b, [
        {"p": _uri(foreign_uri), "o": _uri(target_uri)},
    ])
    edges = await b.edges_from(ID_DESMO)
    assert len(edges) == 1
    assert edges[0].predicate == foreign_uri
    assert edges[0].object    == target_uri


# ---------------------------------------------------------------------------
# End-to-end: SparqlBackend -> CachedGraphDb -> BFS engine
# ---------------------------------------------------------------------------

async def test_end_to_end_bfs_via_cached_db():
    """Two-hop BFS through CachedGraphDb uses SparqlBackend correctly.

    Graph wired:
        Desmopressin --drugUsedForTreatment--> Cushings_disease
        Desmopressin --mechanismOfAction-->    AVPR2
        Cushings_disease --symptom-->          Hypertension
    """
    backend = make_backend()

    # Map entity_id -> (type_uri, outgoing edges, incoming edges, metadata)
    _type_map = {
        ID_DESMO:    URI_DRUG,
        ID_CUSHINGS: URI_DISEASE,
        ID_AVPR2:    URI_GENE,
        ID_HYPER:    URI_DISEASE,
    }
    _out_edges = {
        ID_DESMO:    [(URI_TREATS, URI_CUSHINGS), (URI_MECHANISM, URI_AVPR2)],
        ID_CUSHINGS: [(URI_SYMPTOM, URI_HYPER)],
        ID_AVPR2:    [],
        ID_HYPER:    [],
    }
    _in_edges = {
        ID_DESMO:    [],
        ID_CUSHINGS: [(URI_DESMO, URI_TREATS)],
        ID_AVPR2:    [(URI_DESMO, URI_MECHANISM)],
        ID_HYPER:    [(URI_CUSHINGS, URI_SYMPTOM)],
    }

    async def fake_query(sparql: str) -> list[dict]:
        # Route by unique structural signatures in each generated SPARQL string.
        if "COUNT" in sparql and "GROUP BY" in sparql:
            # entity_types: SELECT ?type (COUNT(?s) AS ?count) ...
            return [
                {"type": _uri(URI_DRUG),    "count": _literal("100")},
                {"type": _uri(URI_DISEASE), "count": _literal("50")},
            ]
        if "DISTINCT ?pred" in sparql:
            # predicates: SELECT DISTINCT ?pred ...
            return [
                {"pred": _uri(URI_TREATS)},
                {"pred": _uri(URI_MECHANISM)},
                {"pred": _uri(URI_SYMPTOM)},
            ]
        if "a ?type" in sparql:
            # get_node: SELECT ?type WHERE { <uri> a ?type . }
            for eid, uri in [
                (ID_DESMO, URI_DESMO), (ID_CUSHINGS, URI_CUSHINGS),
                (ID_AVPR2, URI_AVPR2), (ID_HYPER, URI_HYPER),
            ]:
                if f"<{uri}>" in sparql:
                    return [{"type": _uri(_type_map[eid])}]
        if "FILTER(isLiteral(?o))" in sparql:
            # metadata_for_node: ... FILTER(isLiteral(?o)) (no negation)
            for eid, label in [
                (ID_DESMO, "Desmopressin"), (ID_CUSHINGS, "Cushing's disease"),
                (ID_AVPR2, "AVPR2"), (ID_HYPER, "Hypertension"),
            ]:
                uri = backend._expand(eid)
                if f"<{uri}>" in sparql:
                    return [{"p": _uri(URI_LABEL), "o": _literal(label)}]
        if "SELECT ?p ?o" in sparql:
            # edges_from: SELECT ?p ?o WHERE { <uri> ?p ?o . FILTER(!isLiteral...) }
            for eid, uri in [
                (ID_DESMO, URI_DESMO), (ID_CUSHINGS, URI_CUSHINGS),
                (ID_AVPR2, URI_AVPR2), (ID_HYPER, URI_HYPER),
            ]:
                if f"<{uri}>" in sparql:
                    return [
                        {"p": _uri(p_uri), "o": _uri(o_uri)}
                        for p_uri, o_uri in _out_edges.get(eid, [])
                    ]
        if "SELECT ?s ?p" in sparql:
            # edges_to: SELECT ?s ?p WHERE { ?s ?p <uri> . FILTER(!isLiteral...) }
            for eid, uri in [
                (ID_DESMO, URI_DESMO), (ID_CUSHINGS, URI_CUSHINGS),
                (ID_AVPR2, URI_AVPR2), (ID_HYPER, URI_HYPER),
            ]:
                if f"<{uri}>" in sparql:
                    return [
                        {"s": _uri(s_uri), "p": _uri(p_uri)}
                        for s_uri, p_uri in _in_edges.get(eid, [])
                    ]
        return []

    backend._query = fake_query
    db = CachedGraphDb(backend)

    query = BfsQuery(seeds=[ID_DESMO], max_hops=2, topology_only=True)
    result = await bfs_query(db, query)

    node_ids = {n.id for n in result.nodes}
    assert ID_DESMO    in node_ids
    assert ID_CUSHINGS in node_ids
    assert ID_AVPR2    in node_ids
    assert ID_HYPER    in node_ids

    edge_triples = {(e.subject, e.predicate, e.object) for e in result.edges}
    assert (ID_DESMO,    PRED_TREATS,    ID_CUSHINGS) in edge_triples
    assert (ID_DESMO,    PRED_MECHANISM, ID_AVPR2)    in edge_triples
    assert (ID_CUSHINGS, PRED_SYMPTOM,   ID_HYPER)    in edge_triples
