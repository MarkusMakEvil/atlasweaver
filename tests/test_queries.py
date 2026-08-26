from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_knowledge.compatibility import production_graphify_compatibility
from project_knowledge.queries import (
    QueryError,
    _build_snapshot,
    _parse_graph,
    _synthetic_validated,
    affected_nodes,
    explain_node,
    query_nodes,
    shortest_path,
)


def graph_document(*, duplicate_label: bool = False, injected_label: str | None = None):
    return {
        "nodes": [
            {
                "id": "source",
                "label": injected_label or "Source",
                "nested": {"items": ["one", "two"]},
            },
            {"id": "dispatch", "label": "Dispatch"},
            {"id": "sink", "label": "Sink"},
            {"id": "handler:one", "label": "Handler"},
            {
                "id": "handler:two",
                "label": "Handler" if duplicate_label else "Second Handler",
            },
        ],
        "links": [
            {
                "atlasweaver_edge_id": "edge-source-dispatch",
                "source": "source",
                "target": "dispatch",
                "relation": "calls",
            },
            {
                "atlasweaver_edge_id": "edge-dispatch-sink",
                "source": "dispatch",
                "target": "sink",
                "relation": "calls",
            },
        ],
    }


def snapshot(**changes: object):
    document = graph_document(**changes)
    payload = (json.dumps(document, sort_keys=True) + "\n").encode()
    contract = production_graphify_compatibility()
    nodes, edges, frozen, evidence = _parse_graph(
        payload, contract, evidence_payload=None, evidence_digest=None
    )
    validated = _synthetic_validated(
        "navigation", ("pre_dedup_edge_projection_unavailable",)
    )
    return _build_snapshot(
        Path("/private-query"),
        validated,
        frozen,
        contract,
        evidence,
        None,
        nodes,
        edges,
    )


def test_exact_id_wins_and_duplicate_labels_require_explicit_id() -> None:
    selected = snapshot(duplicate_label=True)

    assert explain_node(selected, "handler:one").result["node"]["id"] == "handler:one"
    with pytest.raises(QueryError) as raised:
        explain_node(selected, "Handler")
    assert raised.value.code == "ambiguous_node"
    assert raised.value.candidates == ("handler:one", "handler:two")


def test_query_path_and_reverse_affected_are_deterministic() -> None:
    selected = snapshot()

    query = query_nodes(selected, "dispatch")
    assert query.result["nodes"][0]["id"] == "dispatch"
    path = shortest_path(selected, "source", "sink")
    assert [node["id"] for node in path.result["nodes"]] == [
        "source",
        "dispatch",
        "sink",
    ]
    affected = affected_nodes(selected, "sink", depth=2, relations=("calls",))
    assert [node["id"] for node in affected.result["nodes"]] == [
        "dispatch",
        "source",
    ]
    assert affected.trust == "navigation"
    assert "source_verification_required" in affected.limitations
    assert "unaffected" not in json.dumps(affected.to_dict()).casefold()


def test_no_path_is_not_proof_of_no_impact() -> None:
    result = shortest_path(snapshot(), "source", "handler:one")

    assert result.result == {"nodes": (), "edges": ()}
    assert "no_path_found_not_proof_of_no_impact" in result.limitations


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (lambda item: query_nodes(item, "x" * 4097), "query_input_too_large"),
        (
            lambda item: shortest_path(item, "source", "sink", max_depth=33),
            "query_depth_exceeded",
        ),
        (lambda item: affected_nodes(item, "sink", depth=9), "query_depth_exceeded"),
        (
            lambda item: affected_nodes(
                item, "sink", relations=tuple(str(index) for index in range(17))
            ),
            "query_relation_cap_exceeded",
        ),
        (lambda item: query_nodes(item, "source", limit=0), "query_result_cap_invalid"),
    ],
)
def test_query_caps_fail_without_partial_result(operation, expected: str) -> None:
    with pytest.raises(QueryError) as raised:
        operation(snapshot())
    assert raised.value.code == expected


def test_snapshot_is_deeply_immutable_and_every_string_is_redacted() -> None:
    secret = "ghp_" + "a" * 32
    selected = snapshot(injected_label=secret)

    with pytest.raises(TypeError):
        selected.nodes["source"].attributes["nested"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        selected.nodes["source"].attributes["nested"]["items"][0] = "changed"  # type: ignore[index]

    document = explain_node(selected, "source").to_dict()
    serialized = json.dumps(document)
    assert secret not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize(
    "payload",
    [
        b'{"nodes":[],"nodes":[],"links":[]}',
        b'{"nodes":[],"links":[],"score":NaN}',
        b'{"nodes":[],"links":[],"edges":[]}',
        b'{"nodes":{},"links":[]}',
    ],
)
def test_graph_parser_rejects_ambiguous_or_non_strict_json(payload: bytes) -> None:
    with pytest.raises(QueryError) as raised:
        _parse_graph(
            payload,
            production_graphify_compatibility(),
            evidence_payload=None,
            evidence_digest=None,
        )
    assert raised.value.code == "query_graph_invalid"
