from __future__ import annotations

import pytest

from project_knowledge.integrity import IntegrityError, analyze_graph


def nodes() -> list[dict[str, object]]:
    return [{"id": "a"}, {"id": "b"}]


def edge(source: object = "a", target: object = "b", relation: str = "calls") -> dict[str, object]:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "provenance": "EXTRACTED",
    }


def test_unknown_collapsed_edge_evidence_keeps_impact_analysis_untrusted() -> None:
    result = analyze_graph(nodes(), [edge()])

    assert result.to_dict() == {
        "schema_version": 1,
        "dangling_edges": 0,
        "missing_endpoints": 0,
        "self_loops": 0,
        "duplicate_edges": 0,
        "collapsed_edges": None,
        "impact_analysis_trusted": False,
    }


def test_complete_clean_diagnostics_can_trust_impact_analysis() -> None:
    result = analyze_graph(nodes(), [edge()], collapsed_edges=0)

    assert result.impact_analysis_trusted is True


def test_integrity_counts_dangling_and_missing_endpoints() -> None:
    result = analyze_graph(
        nodes(),
        [edge("missing", "b"), edge(None, "b"), {"source": "a"}],
    )

    assert result.dangling_edges == 1
    assert result.missing_endpoints == 2
    assert result.impact_analysis_trusted is False


def test_integrity_counts_self_loops_and_duplicate_edges() -> None:
    duplicate = edge()
    result = analyze_graph(
        nodes(),
        [edge("a", "a"), duplicate, dict(duplicate)],
        collapsed_edges=0,
    )

    assert result.self_loops == 1
    assert result.duplicate_edges == 1
    assert result.impact_analysis_trusted is False


def test_integrity_rejects_duplicate_or_invalid_node_ids() -> None:
    with pytest.raises(IntegrityError, match="duplicate node id"):
        analyze_graph([{"id": "a"}, {"id": "a"}], [])

    with pytest.raises(IntegrityError, match="invalid node id"):
        analyze_graph([{"id": 1}], [])


@pytest.mark.parametrize("collapsed", [-1, True, "0"])
def test_integrity_rejects_invalid_collapsed_counter(collapsed: object) -> None:
    with pytest.raises(IntegrityError, match="collapsed edge counter"):
        analyze_graph(nodes(), [], collapsed_edges=collapsed)  # type: ignore[arg-type]
