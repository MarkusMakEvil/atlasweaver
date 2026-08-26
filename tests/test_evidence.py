from __future__ import annotations

from dataclasses import replace
import hashlib
from importlib import resources
import json
from pathlib import Path, PurePosixPath

import pytest

import project_knowledge.evidence as evidence_module
from project_knowledge.adapters import (
    CapturedArtifact,
    NormalizationResult,
    ReasonCount,
    adapter_for,
)
from project_knowledge.compatibility import (
    RenderedCommand,
    render_graphify_argv,
    resolve_graphify_compatibility,
)
from project_knowledge.evidence import (
    GRAPH_EVIDENCE_MAX_BYTES,
    EvidenceError,
    build_extraction_invocation,
    build_graph_evidence,
    parse_graph_evidence,
)


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def contract():
    return resolve_graphify_compatibility("0.9.48")


def rendered_commands(
    *,
    backend: str = "openai",
    model: str = "gpt-4.1-mini",
    track_html: bool = False,
) -> tuple[RenderedCommand, ...]:
    selected = contract()
    binary = Path("/Users/private/bin/graphify")
    source = Path("/Users/private/staged/sk-live-secret")
    output = Path("/Users/private/raw")
    native = output / "graph.json"
    return (
        render_graphify_argv(
            selected,
            "extract",
            binary=binary,
            source=source,
            output=output,
            backend=backend,
            model=model,
        ),
        render_graphify_argv(
            selected,
            "diagnose",
            binary=binary,
            source=source,
            output=output,
            graph=native,
        ),
        render_graphify_argv(
            selected,
            "cluster",
            binary=binary,
            source=Path("/Users/private/cluster-workspace"),
            output=Path("/Users/private/clustered"),
            graph=Path("/Users/private/cluster-input/graph.json"),
            track_html=track_html,
        ),
    )


def artifact(path: str, payload: bytes | None = None) -> CapturedArtifact:
    return CapturedArtifact.from_payload(
        PurePosixPath(path), payload if payload is not None else (path + "\n").encode()
    )


def invocation_artifacts(*, track_html: bool = False) -> tuple[CapturedArtifact, ...]:
    items = (
        artifact("raw/graph.json"),
        artifact("raw/diagnose.json"),
        artifact("cluster-input/graph.json"),
        artifact("clustered/graph.json"),
        artifact("clustered/GRAPH_REPORT.md"),
    )
    if track_html:
        return items + (artifact("clustered/graph.html"),)
    return items


def invocation_factory(**overrides: object):
    arguments: dict[str, object] = {
        "executable_sha256": "a" * 64,
        "capability_smoke_digest": "9" * 64,
        "commands": rendered_commands(),
        "backend": "openai",
        "model": "gpt-4.1-mini",
        "configuration_sha256": "b" * 64,
        "source_digest": "c" * 64,
        "projection_digest": "d" * 64,
        "environment_names": ("HOME", "LANG", "LC_ALL", "OPENAI_API_KEY", "PATH"),
        "artifacts": invocation_artifacts(),
    }
    if "commands" not in overrides and (
        "backend" in overrides or "model" in overrides
    ):
        arguments["commands"] = rendered_commands(
            backend=overrides.get("backend", arguments["backend"]),  # type: ignore[arg-type]
            model=overrides.get("model", arguments["model"]),  # type: ignore[arg-type]
        )
    arguments.update(overrides)
    return build_extraction_invocation(contract(), **arguments)


def test_invocation_digest_binds_contract_without_private_paths() -> None:
    invocation = invocation_factory()

    assert len(invocation.digest) == 64
    assert invocation.digest == hashlib.sha256(
        b"atlasweaver-graphify-pipeline-v1\0" + invocation.payload
    ).hexdigest()
    assert b"<staged-root>" in invocation.payload
    assert b"/Users/" not in invocation.payload
    assert b"sk-live-secret" not in invocation.payload
    document = json.loads(invocation.payload)
    assert [item["operation"] for item in document["argv"]] == [
        "extract",
        "diagnose",
        "cluster",
    ]
    assert [item["path"] for item in document["artifacts"]] == [
        "cluster-input/graph.json",
        "clustered/GRAPH_REPORT.md",
        "clustered/graph.json",
        "raw/diagnose.json",
        "raw/graph.json",
    ]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("executable_sha256", "e" * 64),
        ("capability_smoke_digest", "8" * 64),
        ("model", "other-model"),
        ("configuration_sha256", "f" * 64),
        ("source_digest", "1" * 64),
        ("projection_digest", "2" * 64),
        ("environment_names", ("HOME", "PATH")),
    ],
)
def test_invocation_mutation_changes_digest(field: str, replacement: object) -> None:
    original = invocation_factory()
    changed = invocation_factory(**{field: replacement})

    assert changed.digest != original.digest


def test_invocation_optional_html_is_bound_to_command_and_artifact() -> None:
    invocation = invocation_factory(
        commands=rendered_commands(track_html=True),
        artifacts=invocation_artifacts(track_html=True),
    )
    document = json.loads(invocation.payload)

    assert document["artifacts"][2]["path"] == "clustered/graph.html"
    assert "--no-viz" not in document["argv"][2]["items"]


@pytest.mark.parametrize(
    ("override", "match"),
    [
        (
            {"artifacts": invocation_artifacts() + (artifact("raw/graph.json"),)},
            "artifact paths",
        ),
        ({"environment_names": ("PATH", "HOME")}, "environment names"),
        (
            {"environment_names": ("HOME", "LANG", "LC_ALL", "NOT_ADMITTED", "PATH")},
            "environment names",
        ),
        (
            {"commands": rendered_commands()[:1] + rendered_commands()[:1] + rendered_commands()[2:]},
            "command order",
        ),
        ({"commands": tuple(reversed(rendered_commands()))}, "command order"),
        (
            {
                "commands": (
                    replace(
                        rendered_commands()[0],
                        canonical_argv=(
                            "/Users/private/bin/graphify",
                            *rendered_commands()[0].canonical_argv[1:],
                        ),
                    ),
                    *rendered_commands()[1:],
                )
            },
            "canonical command",
        ),
        (
            {
                "commands": (
                    replace(
                        rendered_commands()[0],
                        canonical_argv=rendered_commands()[0].canonical_argv + ("--future-adapter",),
                    ),
                    *rendered_commands()[1:],
                )
            },
            "canonical command",
        ),
    ],
)
def test_invocation_rejects_noncanonical_or_incomplete_inputs(
    override: dict[str, object], match: str
) -> None:
    with pytest.raises(EvidenceError, match=match):
        invocation_factory(**override)


@pytest.mark.parametrize(
    "field",
    [
        "executable_sha256",
        "capability_smoke_digest",
        "configuration_sha256",
        "source_digest",
        "projection_digest",
    ],
)
@pytest.mark.parametrize("invalid", ["0" * 63, "G" * 64, True, None])
def test_invocation_rejects_invalid_digests(field: str, invalid: object) -> None:
    with pytest.raises(EvidenceError, match="digest"):
        invocation_factory(**{field: invalid})


def test_invocation_rejects_artifact_descriptor_mismatch() -> None:
    good = artifact("raw/graph.json")
    forged = object.__new__(CapturedArtifact)
    object.__setattr__(forged, "logical_path", good.logical_path)
    object.__setattr__(forged, "payload", good.payload)
    object.__setattr__(forged, "sha256", "0" * 64)
    object.__setattr__(forged, "byte_length", True)
    items = (forged,) + invocation_artifacts()[1:]

    with pytest.raises(EvidenceError, match="artifact descriptor"):
        invocation_factory(artifacts=items)


def test_invocation_rejects_a_spoofed_compatibility_contract() -> None:
    spoofed = replace(contract(), adapter_id="other-adapter")
    with pytest.raises(EvidenceError, match="registered compatibility contract"):
        build_extraction_invocation(
            spoofed,
            executable_sha256="a" * 64,
            capability_smoke_digest="9" * 64,
            commands=rendered_commands(),
            backend="openai",
            model="gpt-4.1-mini",
            configuration_sha256="b" * 64,
            source_digest="c" * 64,
            projection_digest="d" * 64,
            environment_names=("HOME", "PATH"),
            artifacts=invocation_artifacts(),
        )


def test_invocation_rejects_actual_nonpath_tokens_that_disagree_with_canonical() -> None:
    commands = rendered_commands()
    extract = commands[0]
    actual = list(extract.argv)
    actual[actual.index("gpt-4.1-mini")] = "unbound-model"
    forged = (replace(extract, argv=tuple(actual)), *commands[1:])

    with pytest.raises(EvidenceError, match="actual command"):
        invocation_factory(commands=forged)


def fixture_document() -> dict[str, object]:
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "graphify_0_9_48.json"
    ).read_bytes()
    return json.loads(payload)


def fixture_pipeline(
    *,
    normalization_quarantines: tuple[ReasonCount, ...] | None = None,
    remove_source_location: bool = False,
    diagnosis_mutation=None,
):
    selected = contract()
    implementation = adapter_for(selected)
    fixture = fixture_document()
    native = artifact("raw/graph.json", canonical_json(fixture["native_graph"]))
    diagnosis_document = json.loads(canonical_json(fixture["diagnosis"]))
    if diagnosis_mutation is not None:
        diagnosis_mutation(diagnosis_document)
    diagnosis_item = artifact(
        "raw/diagnose.json", canonical_json(diagnosis_document)
    )
    normalization = implementation.normalize_for_cluster(
        implementation.parse_post_dedup(native)
    )
    if normalization_quarantines is not None:
        normalization = replace(
            normalization, quarantines=normalization_quarantines
        )
    normalized = json.loads(normalization.cluster_input.payload)
    raw_clustered_document = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": normalized["nodes"],
        "links": normalized["edges"],
    }
    if remove_source_location:
        raw_clustered_document["links"][0].pop("source_location", None)
    raw_clustered = artifact(
        "clustered/graph.json", canonical_json(raw_clustered_document)
    )
    final = artifact(
        "clustered/graph.json",
        implementation.adapt_clustered_graph(
            raw_clustered,
            staged_files=frozenset({PurePosixPath("fixture.py")}),
        ),
    )
    report = artifact("clustered/GRAPH_REPORT.md", b"# Graph report\n")
    invocation = invocation_factory(
        artifacts=(
            native,
            diagnosis_item,
            normalization.cluster_input,
            raw_clustered,
            report,
        )
    )
    return (
        invocation,
        native,
        diagnosis_item,
        normalization,
        raw_clustered,
        final,
    )


def build_fixture_evidence(**pipeline_overrides: object):
    (
        invocation,
        native,
        diagnosis_item,
        normalization,
        raw_clustered,
        final,
    ) = fixture_pipeline(**pipeline_overrides)
    return build_graph_evidence(
        contract(),
        source_digest="c" * 64,
        projection_digest="d" * 64,
        invocation=invocation,
        native_graph=native,
        diagnosis=diagnosis_item,
        normalization=normalization,
        clustered_graph=raw_clustered,
        final_graph=final,
    )


def test_0948_evidence_is_observed_post_dedup_and_never_complete() -> None:
    evidence = build_fixture_evidence()

    assert evidence.observed_post_dedup.node_count > 0
    assert evidence.pre_dedup is None
    assert evidence.evidence_complete is False
    expected = {"pre_dedup_edge_projection_unavailable"}
    if evidence.normalization_quarantines:
        expected.add("raw_endpoint_unresolved")
    if any(
        edge.source_file is None or edge.source_line is None
        for edge in evidence.final_edges
    ):
        expected.add("impact_edge_provenance_incomplete")
    assert set(evidence.limitations) == expected
    document = json.loads(evidence.payload)
    assert document["pre_dedup"] is None
    assert document["observed_post_dedup"]["dangling_endpoint_edges"] >= 1
    assert document["normalization"]["quarantines"]


def test_evidence_excludes_context_and_retains_only_safe_provenance() -> None:
    evidence = build_fixture_evidence()
    document = json.loads(evidence.payload)

    assert evidence.payload == canonical_json(document)
    assert all(
        set(edge)
        == {
            "confidence",
            "final_edge_id",
            "relation",
            "source",
            "source_column",
            "source_file",
            "source_line",
            "target",
        }
        for edge in document["final_edges"]
    )
    assert "context" not in json.dumps(document["final_edges"])
    assert "navigation-only" not in json.dumps(document["final_edges"])
    assert {edge["source_file"] for edge in document["final_edges"]} == {
        "fixture.py"
    }
    assert {edge["source_line"] for edge in document["final_edges"]} == {
        5,
        9,
        13,
        14,
    }


def test_evidence_parser_round_trips_identical_canonical_bytes() -> None:
    built = build_fixture_evidence()
    parsed = parse_graph_evidence(built.payload, contract())

    assert parsed == built
    assert parsed.payload is built.payload or parsed.payload == built.payload
    assert parsed.digest == hashlib.sha256(parsed.payload).hexdigest()
    assert parsed.invocation.payload == built.invocation.payload
    assert parsed.invocation.digest == built.extraction_invocation_digest


@pytest.mark.parametrize(
    "field",
    [
        "node_count",
        "raw_edge_count",
        "missing_endpoint_edges",
        "dangling_endpoint_edges",
        "self_loop_edges",
        "exact_duplicate_edges",
    ],
)
def test_diagnosis_must_match_adapter_recomputation(field: str) -> None:
    def mutation(document: dict[str, object]) -> None:
        summary = document["summary"]
        assert isinstance(summary, dict)
        summary[field] += 1

    with pytest.raises(
        EvidenceError,
        match="Graphify diagnosis does not match captured native graph",
    ):
        build_fixture_evidence(diagnosis_mutation=mutation)


def test_diagnosis_requires_false_effective_directed() -> None:
    def mutation(document: dict[str, object]) -> None:
        summary = document["summary"]
        assert isinstance(summary, dict)
        summary["effective_directed"] = True

    with pytest.raises(EvidenceError, match="diagnosis"):
        build_fixture_evidence(diagnosis_mutation=mutation)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"normalization_quarantines": ()},
            ("pre_dedup_edge_projection_unavailable",),
        ),
        (
            {
                "normalization_quarantines": (
                    ReasonCount("dangling_endpoint", 1),
                )
            },
            (
                "pre_dedup_edge_projection_unavailable",
                "raw_endpoint_unresolved",
            ),
        ),
        (
            {
                "normalization_quarantines": (),
                "remove_source_location": True,
            },
            (
                "impact_edge_provenance_incomplete",
                "pre_dedup_edge_projection_unavailable",
            ),
        ),
    ],
)
def test_evidence_limitations_are_independently_derived_sorted_and_unique(
    overrides: dict[str, object], expected: tuple[str, ...]
) -> None:
    assert build_fixture_evidence(**overrides).limitations == expected


def mutate_evidence(mutator) -> bytes:
    document = json.loads(build_fixture_evidence().payload)
    mutator(document)
    return canonical_json(document)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":1,"schema_version":1}\n',
        b'{"outer":{"value":1,"value":1}}\n',
        b'{"outer":[{"value":1,"value":1}]}\n',
        b'{"schema_version":NaN}\n',
        b'{"schema_version":Infinity}\n',
        b'{"schema_version":1e400}\n',
    ],
)
def test_evidence_parser_rejects_duplicate_keys_and_nonfinite_numbers(
    payload: bytes,
) -> None:
    with pytest.raises(EvidenceError, match="duplicate|non-finite"):
        parse_graph_evidence(payload, contract())


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda value: value.update(unknown=True), "schema"),
        (lambda value: value.update(schema_version=2), "schema"),
        (lambda value: value.update(adapter_id="other"), "adapter"),
        (lambda value: value.update(graphify_version="0.9.49"), "version"),
        (lambda value: value.update(source_digest="0" * 64), "source digest"),
        (
            lambda value: value.update(extraction_invocation_digest="0" * 64),
            "invocation digest",
        ),
        (
            lambda value: value["final_integrity"].update(edge_count=999),
            "counter",
        ),
        (
            lambda value: value["final_edges"][1].update(
                final_edge_id=value["final_edges"][0]["final_edge_id"]
            ),
            "edge IDs",
        ),
        (
            lambda value: value["final_edges"][0].update(source_file="/etc/passwd"),
            "path",
        ),
        (
            lambda value: value["final_edges"][0].update(source_file="../escape.py"),
            "path",
        ),
        (
            lambda value: value["final_edges"][0].update(source_file="src\\escape.py"),
            "path",
        ),
        (lambda value: value["final_edges"][0].update(source_line=True), "line"),
        (lambda value: value["final_edges"][0].update(source_column=0), "column"),
        (
            lambda value: value["final_edges"][0].update(confidence="CERTAIN"),
            "confidence",
        ),
        (
            lambda value: value["final_edges"][0].update(context="source fragment"),
            "schema",
        ),
        (
            lambda value: value["normalization"]["quarantines"].append(
                value["normalization"]["quarantines"][0]
            ),
            "reason counts",
        ),
        (lambda value: value.update(evidence_complete=True), "complete"),
    ],
)
def test_evidence_parser_rejects_closed_schema_and_consistency_mutations(
    mutator, match: str
) -> None:
    with pytest.raises(EvidenceError, match=match):
        parse_graph_evidence(mutate_evidence(mutator), contract())


def test_evidence_parser_rejects_alternate_json_encoding() -> None:
    document = json.loads(build_fixture_evidence().payload)
    payload = json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"

    with pytest.raises(EvidenceError, match="canonical"):
        parse_graph_evidence(payload, contract())


def test_graph_evidence_size_boundary_is_checked_before_json_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at_cap = b" " * GRAPH_EVIDENCE_MAX_BYTES
    assert evidence_module._bounded_evidence_payload(at_cap) is at_cap

    def must_not_parse(*args, **kwargs):
        raise AssertionError("JSON allocation must not occur")

    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(EvidenceError, match="size cap"):
        parse_graph_evidence(at_cap + b"x", contract())


def test_graph_evidence_cap_has_one_production_assignment() -> None:
    assert GRAPH_EVIDENCE_MAX_BYTES == 67_108_864
    assignments = []
    for path in Path("src/project_knowledge").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("GRAPH_EVIDENCE_MAX_BYTES ="):
                assignments.append(path.as_posix())
    assert assignments == ["src/project_knowledge/evidence.py"]


def test_diagnosis_is_rejected_above_its_cap_before_parsing() -> None:
    (
        invocation,
        native,
        _,
        normalization,
        raw_clustered,
        final,
    ) = fixture_pipeline()
    oversized = artifact("raw/diagnose.json", b" " * 4_194_305)
    report = artifact("clustered/GRAPH_REPORT.md", b"# Graph report\n")
    invocation = invocation_factory(
        artifacts=(
            native,
            oversized,
            normalization.cluster_input,
            raw_clustered,
            report,
        )
    )

    with pytest.raises(EvidenceError, match="diagnosis size cap"):
        build_graph_evidence(
            contract(),
            source_digest="c" * 64,
            projection_digest="d" * 64,
            invocation=invocation,
            native_graph=native,
            diagnosis=oversized,
            normalization=normalization,
            clustered_graph=raw_clustered,
            final_graph=final,
        )


def test_builder_rejects_unbound_source_projection_and_artifacts() -> None:
    (
        invocation,
        native,
        diagnosis_item,
        normalization,
        raw_clustered,
        final,
    ) = fixture_pipeline()

    with pytest.raises(EvidenceError, match="source digest"):
        build_graph_evidence(
            contract(),
            source_digest="0" * 64,
            projection_digest="d" * 64,
            invocation=invocation,
            native_graph=native,
            diagnosis=diagnosis_item,
            normalization=normalization,
            clustered_graph=raw_clustered,
            final_graph=final,
        )
    with pytest.raises(EvidenceError, match="artifact binding"):
        build_graph_evidence(
            contract(),
            source_digest="c" * 64,
            projection_digest="d" * 64,
            invocation=invocation,
            native_graph=native,
            diagnosis=diagnosis_item,
            normalization=normalization,
            clustered_graph=artifact("clustered/graph.json", b"different\n"),
            final_graph=final,
        )
