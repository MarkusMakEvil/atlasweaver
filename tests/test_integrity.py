from __future__ import annotations

from dataclasses import replace

import pytest

from project_knowledge.compatibility import GraphSemantics, resolve_graphify_compatibility
from project_knowledge.integrity import (
    GraphIntegrity,
    IntegrityError,
    analyze_graph,
    canonical_final_edge_id,
    validate_final_graph,
)


def semantics(**changes: object) -> GraphSemantics:
    base = resolve_graphify_compatibility("0.9.48").semantics
    return replace(base, **changes)


def nodes(*identifiers: object) -> list[dict[str, object]]:
    values = identifiers or ("a", "b")
    return [{"id": identifier} for identifier in values]


def edge(
    source: object = "a",
    target: object = "b",
    relation: object = "calls",
    **updates: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "source": source,
        "target": target,
        "relation": relation,
        "provenance": "EXTRACTED",
    }
    value.update(updates)
    return value


def test_graph_integrity_schema_2_preserves_unknown_collapse_evidence() -> None:
    result = analyze_graph(nodes(), [edge()], semantics=semantics())

    assert result.to_dict() == {
        "schema_version": 2,
        "node_count": 2,
        "edge_count": 1,
        "missing_endpoint_edges": 0,
        "dangling_endpoint_edges": 0,
        "invalid_self_loop_edges": 0,
        "exact_duplicate_edges": 0,
        "conflicting_relation_edges": 0,
        "collapsed_edges": None,
        "structurally_valid": True,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_count", True),
        ("edge_count", False),
        ("missing_endpoint_edges", True),
        ("dangling_endpoint_edges", False),
        ("invalid_self_loop_edges", True),
        ("exact_duplicate_edges", False),
        ("conflicting_relation_edges", True),
        ("collapsed_edges", False),
    ],
)
def test_graph_integrity_rejects_boolean_counters(field: str, value: bool) -> None:
    values: dict[str, object] = {
        "node_count": 0,
        "edge_count": 0,
        "missing_endpoint_edges": 0,
        "dangling_endpoint_edges": 0,
        "invalid_self_loop_edges": 0,
        "exact_duplicate_edges": 0,
        "conflicting_relation_edges": 0,
        "collapsed_edges": None,
        "structurally_valid": True,
    }
    values[field] = value

    with pytest.raises(IntegrityError, match="counter is invalid"):
        GraphIntegrity(**values)  # type: ignore[arg-type]


def test_graph_integrity_rejects_inconsistent_structural_validity() -> None:
    with pytest.raises(IntegrityError, match="structural validity is inconsistent"):
        GraphIntegrity(
            node_count=1,
            edge_count=1,
            missing_endpoint_edges=1,
            dangling_endpoint_edges=0,
            invalid_self_loop_edges=0,
            exact_duplicate_edges=0,
            conflicting_relation_edges=0,
            collapsed_edges=None,
            structurally_valid=True,
        )


@pytest.mark.parametrize("collapsed", [-1, True, "0"])
def test_analyze_graph_rejects_invalid_collapsed_counter(collapsed: object) -> None:
    with pytest.raises(IntegrityError, match="collapsed edge counter"):
        analyze_graph(
            nodes(), [], semantics=semantics(), collapsed_edges=collapsed  # type: ignore[arg-type]
        )


def test_analyze_graph_rejects_duplicate_or_invalid_node_ids() -> None:
    with pytest.raises(IntegrityError, match="duplicate node id"):
        analyze_graph(nodes("a", "a"), [], semantics=semantics())

    for identifier in (1, "", None):
        with pytest.raises(IntegrityError, match="invalid node id"):
            analyze_graph(nodes(identifier), [], semantics=semantics())


def test_missing_and_dangling_endpoints_are_counted_separately() -> None:
    result = analyze_graph(
        nodes(),
        [edge("missing", "b"), edge(None, "b"), {"source": "a"}],
        semantics=semantics(),
    )

    assert result.dangling_endpoint_edges == 1
    assert result.missing_endpoint_edges == 2
    assert result.structurally_valid is False


def test_allowed_and_invalid_self_loops_follow_adapter_policy() -> None:
    policy = semantics(allowed_self_loop_relations=frozenset({"contains"}))
    result = analyze_graph(
        nodes("a"),
        [edge("a", "a", "contains"), edge("a", "a", "calls")],
        semantics=policy,
    )

    assert result.invalid_self_loop_edges == 1
    assert result.structurally_valid is False


def test_exact_duplicates_use_only_declared_identity_fields() -> None:
    first = edge(context="same", display="one")
    second = edge(context="same", display="two")
    result = analyze_graph(nodes(), [first, second], semantics=semantics())

    assert result.exact_duplicate_edges == 1
    assert result.conflicting_relation_edges == 0
    assert result.structurally_valid is False


def test_undirected_identity_is_endpoint_order_independent() -> None:
    forward = edge("a", "b", relation="calls")
    reverse = edge("b", "a", relation="calls")

    assert canonical_final_edge_id(forward, semantics()) == canonical_final_edge_id(
        reverse, semantics()
    )


def test_directed_identity_preserves_endpoint_order() -> None:
    directed = semantics(directed=True)

    assert canonical_final_edge_id(edge("a", "b"), directed) != canonical_final_edge_id(
        edge("b", "a"), directed
    )


def test_final_edge_identity_is_domain_separated_and_stable() -> None:
    assert canonical_final_edge_id(edge(), semantics()) == (
        "edge-74b6c22f8e04a6835afef9870aaf6816334d573421c24751967edcb28339b6b0"
    )


def test_parallel_relation_conflict_uses_adapter_policy() -> None:
    result = analyze_graph(
        nodes(),
        [edge("a", "b", "calls"), edge("a", "b", "imports")],
        semantics=semantics(),
    )

    assert result.exact_duplicate_edges == 0
    assert result.conflicting_relation_edges == 1
    assert result.structurally_valid is False


def test_parallel_policy_supports_distinct_relations_and_allow() -> None:
    edges = [edge("a", "b", "calls"), edge("a", "b", "imports")]

    distinct = analyze_graph(
        nodes(), edges, semantics=semantics(parallel_policy="distinct-relation")
    )
    allowed = analyze_graph(nodes(), edges, semantics=semantics(parallel_policy="allow"))

    assert distinct.conflicting_relation_edges == 0
    assert distinct.structurally_valid is True
    assert allowed.conflicting_relation_edges == 0
    assert allowed.structurally_valid is True


def test_distinct_relation_policy_rejects_same_relation_parallel_identities() -> None:
    result = analyze_graph(
        nodes(),
        [edge(source_file="a.py"), edge(source_file="b.py")],
        semantics=semantics(parallel_policy="distinct-relation"),
    )

    assert result.conflicting_relation_edges == 1
    assert result.structurally_valid is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", 1),
        ("target", ["b"]),
        ("relation", 3),
        ("source_file", {"path": "a.py"}),
        ("source_location", 10),
        ("context", ["call"]),
    ],
)
def test_non_string_identity_fields_are_rejected(field: str, value: object) -> None:
    value_edge = edge()
    value_edge[field] = value

    with pytest.raises(IntegrityError, match=f"edge identity field {field} is invalid"):
        canonical_final_edge_id(value_edge, semantics())


def test_pre_forged_final_edge_identity_is_rejected() -> None:
    with pytest.raises(IntegrityError, match="reserved AtlasWeaver identity"):
        canonical_final_edge_id(
            edge(atlasweaver_edge_id="edge-" + "0" * 64), semantics()
        )


def final_document(*edges: dict[str, object]) -> dict[str, object]:
    final_edges = list(edges) or [edge()]
    for value in final_edges:
        value["atlasweaver_edge_id"] = canonical_final_edge_id(value, semantics())
    return {"nodes": nodes(), "links": final_edges}


def test_validate_final_graph_accepts_one_edge_member_and_matching_ids() -> None:
    result = validate_final_graph(final_document(), semantics())

    assert result.structurally_valid is True
    assert result.edge_count == 1


@pytest.mark.parametrize(
    "document",
    [
        {"nodes": [], "links": [], "edges": []},
        {"nodes": []},
        {"nodes": {}, "links": []},
        {"nodes": [], "links": {}},
        {"nodes": ["a"], "links": []},
        {"nodes": [], "links": ["a"]},
    ],
)
def test_validate_final_graph_rejects_invalid_shape(document: dict[str, object]) -> None:
    with pytest.raises(IntegrityError, match="final graph"):
        validate_final_graph(document, semantics())


def test_validate_final_graph_rejects_missing_forged_or_duplicate_ids() -> None:
    missing = {"nodes": nodes(), "links": [edge()]}
    forged = final_document()
    forged_links = forged["links"]
    assert isinstance(forged_links, list)
    forged_links[0]["atlasweaver_edge_id"] = "edge-" + "0" * 64
    duplicate = final_document(edge(), edge())

    with pytest.raises(IntegrityError, match="missing AtlasWeaver edge identity"):
        validate_final_graph(missing, semantics())
    with pytest.raises(IntegrityError, match="AtlasWeaver edge identity is invalid"):
        validate_final_graph(forged, semantics())
    with pytest.raises(IntegrityError, match="duplicate AtlasWeaver edge identity"):
        validate_final_graph(duplicate, semantics())
