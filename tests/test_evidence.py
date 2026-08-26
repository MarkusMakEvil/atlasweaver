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
    CommandEnvironmentBinding,
    EvidenceError,
    ExtractionInvocation,
    build_extraction_invocation,
    build_graph_evidence,
    decide_impact_trust,
    index_final_edge_evidence,
    parse_graph_evidence,
)
from project_knowledge.integrity import GraphIntegrity, canonical_final_edge_id


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def parse_semantic_evidence(payload: bytes):
    """Self-anchor test bytes only when exercising post-anchor semantics."""
    return parse_graph_evidence(
        payload,
        contract(),
        expected_digest=hashlib.sha256(payload).hexdigest(),
    )


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
    native = output / "graphify-out/graph.json"
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
            source=Path("/Users/private/cluster"),
            output=Path("/Users/private/clustered"),
            graph=Path("/Users/private/cluster/graphify-out/graph.json"),
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


def command_environments(
    *,
    extract_names: tuple[str, ...] = (
        "HOME",
        "LANG",
        "LC_ALL",
        "OPENAI_API_KEY",
        "PATH",
    ),
    diagnose_names: tuple[str, ...] = ("HOME", "LANG", "LC_ALL", "PATH"),
    cluster_names: tuple[str, ...] = ("HOME", "LANG", "LC_ALL", "PATH"),
) -> tuple[CommandEnvironmentBinding, ...]:
    return (
        CommandEnvironmentBinding("extract", extract_names),
        CommandEnvironmentBinding("diagnose", diagnose_names),
        CommandEnvironmentBinding("cluster", cluster_names),
    )


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
        "environments": command_environments(),
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
        (
            "environments",
            command_environments(
                extract_names=(
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "OPENAI_API_KEY",
                    "OPENAI_BASE_URL",
                    "PATH",
                )
            ),
        ),
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
        (
            {"environments": command_environments(extract_names=("PATH", "HOME"))},
            "environment names",
        ),
        (
            {
                "environments": command_environments(
                    extract_names=(
                        "HOME",
                        "LANG",
                        "LC_ALL",
                        "NOT_ADMITTED",
                        "PATH",
                    )
                )
            },
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
            environments=command_environments(),
            artifacts=invocation_artifacts(),
        )


def test_invocation_binds_semantic_environment_only_to_extract() -> None:
    invocation = invocation_factory()
    document = json.loads(invocation.payload)

    assert document["environments"] == [
        {
            "operation": "extract",
            "names": ["HOME", "LANG", "LC_ALL", "OPENAI_API_KEY", "PATH"],
        },
        {"operation": "diagnose", "names": ["HOME", "LANG", "LC_ALL", "PATH"]},
        {"operation": "cluster", "names": ["HOME", "LANG", "LC_ALL", "PATH"]},
    ]
    assert "environment_names" not in document


@pytest.mark.parametrize(
    "environments",
    [
        command_environments()[::-1],
        command_environments(
            diagnose_names=("HOME", "LANG", "LC_ALL", "OPENAI_API_KEY", "PATH")
        ),
        command_environments(
            cluster_names=("HOME", "LANG", "LC_ALL", "OPENAI_API_KEY", "PATH")
        ),
        command_environments(
            extract_names=("HOME", "LANG", "LC_ALL", "PATH")
        ),
    ],
)
def test_invocation_rejects_reordered_or_cross_command_environment_names(
    environments: tuple[CommandEnvironmentBinding, ...],
) -> None:
    with pytest.raises(EvidenceError, match="environment"):
        invocation_factory(environments=environments)


def test_invocation_parser_rejects_the_legacy_environment_shape() -> None:
    invocation = invocation_factory()
    document = json.loads(invocation.payload)
    document["environment_names"] = document.pop("environments")[0]["names"]
    payload = canonical_json(document)
    forged = replace(
        invocation,
        payload=payload,
        digest=hashlib.sha256(
            b"atlasweaver-graphify-pipeline-v1\0" + payload
        ).hexdigest(),
    )

    with pytest.raises(EvidenceError, match="extraction invocation"):
        evidence_module._validate_invocation(forged, contract())


def test_invocation_rejects_actual_nonpath_tokens_that_disagree_with_canonical() -> None:
    commands = rendered_commands()
    extract = commands[0]
    actual = list(extract.argv)
    actual[actual.index("gpt-4.1-mini")] = "unbound-model"
    forged = (replace(extract, argv=tuple(actual)), *commands[1:])

    with pytest.raises(EvidenceError, match="actual command"):
        invocation_factory(commands=forged)


@pytest.mark.parametrize(
    "forged_extract",
    [
        lambda command: replace(command, argv=list(command.argv)),
        lambda command: replace(command, argv=command.argv[:4]),
        lambda command: replace(command, canonical_argv=list(command.canonical_argv)),
        lambda command: replace(
            command,
            canonical_argv=(command.canonical_argv[0], ["extract"], *command.canonical_argv[2:]),
        ),
        lambda command: RenderedCommand(
            {"operation": "extract"}, command.argv, command.canonical_argv
        ),
        lambda command: object.__new__(RenderedCommand),
    ],
)
def test_invocation_rejects_nonexact_rendered_command_shapes(forged_extract) -> None:
    commands = rendered_commands()
    forged = (forged_extract(commands[0]), *commands[1:])

    with pytest.raises(EvidenceError, match="command"):
        invocation_factory(commands=forged)


def mutate_actual_token(
    commands: tuple[RenderedCommand, ...],
    command_index: int,
    token_index: int,
    replacement: str,
) -> tuple[RenderedCommand, ...]:
    changed = list(commands)
    command = changed[command_index]
    argv = list(command.argv)
    argv[token_index] = replacement
    changed[command_index] = replace(command, argv=tuple(argv))
    return tuple(changed)


@pytest.mark.parametrize(
    ("command_index", "token_index", "replacement"),
    [
        (0, 2, "--backend"),
        (0, 2, "relative/source"),
        (0, 4, "/Users/private/raw/../escape"),
        (0, 4, "/Users/private//raw"),
        (1, 4, "/Users/private/raw/./graph.json"),
        (2, 4, "/Users/private/cluster-input/graph.json/"),
        (2, 2, "/Users/private/cluster\nworkspace"),
        (2, 4, "/Users/private/cluster\x00input/graph.json"),
    ],
)
def test_invocation_rejects_unsafe_actual_path_operands(
    command_index: int, token_index: int, replacement: str
) -> None:
    commands = mutate_actual_token(
        rendered_commands(), command_index, token_index, replacement
    )

    with pytest.raises(EvidenceError, match="actual command path") as captured:
        invocation_factory(commands=commands)
    assert replacement not in str(captured.value)


def test_invocation_requires_one_binary_identity_across_all_commands() -> None:
    commands = mutate_actual_token(
        rendered_commands(), 1, 0, "/opt/other/bin/graphify"
    )

    with pytest.raises(EvidenceError, match="binary"):
        invocation_factory(commands=commands)


def test_invocation_binds_diagnosis_to_the_extraction_output_graph() -> None:
    commands = mutate_actual_token(
        rendered_commands(), 1, 4, "/Users/private/other/graph.json"
    )

    with pytest.raises(EvidenceError, match="path relationship"):
        invocation_factory(commands=commands)


def test_invocation_binds_cluster_input_to_the_cluster_workspace() -> None:
    commands = mutate_actual_token(
        rendered_commands(), 2, 4, "/Users/private/other/graph.json"
    )

    with pytest.raises(EvidenceError, match="path relationship"):
        invocation_factory(commands=commands)


def test_invocation_rejects_an_option_in_the_cluster_graph_path_role() -> None:
    commands = mutate_actual_token(rendered_commands(), 2, 4, "--graph")

    with pytest.raises(EvidenceError, match="actual command path"):
        invocation_factory(commands=commands)


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4.1-mini",
        "llama3.2:latest",
        "public-org/model-v2",
        "m" * 65,
    ],
)
def test_invocation_accepts_bounded_public_model_identifiers(model: str) -> None:
    invocation = invocation_factory(model=model)

    assert json.loads(invocation.payload)["model"] == model


@pytest.mark.parametrize(
    "model",
    [
        "/Users/private/model",
        r"private\model",
        "../private/model",
        "sk-live-proof-token",
        "Bearer proof-token",
        "OPENAI_API_KEY=proof-token",
        "model token",
        "model\nname",
        "secret:model",
        "gh" + "p_" + "a" * 36,
        "gh" + "o_" + "a" * 36,
        "gh" + "u_" + "a" * 36,
        "gh" + "s_" + "a" * 36,
        "gh" + "r_" + "a" * 36,
        "AK" + "IA" + "A" * 16,
        "AI" + "za" + "A" * 35,
        "sk" + "_live_" + "a" * 24,
        "rk" + "_live_" + "a" * 24,
        "xox" + "b-" + "1" * 12 + "-" + "a" * 24,
        "eyJ" + "a" * 12 + "." + "b" * 16 + "." + "c" * 16,
        "m" * 129,
    ],
)
def test_invocation_rejects_private_or_secret_shaped_models_without_echo(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized: list[str] = []
    canonicalize = evidence_module._canonical_json

    def record_serialization(value: object) -> bytes:
        serialized.append(repr(value))
        return canonicalize(value)

    monkeypatch.setattr(evidence_module, "_canonical_json", record_serialization)
    with pytest.raises(EvidenceError, match="public model identifier") as captured:
        invocation_factory(model=model, commands=rendered_commands())
    assert model not in str(captured.value)
    assert all(model not in value for value in serialized)


@pytest.mark.parametrize(
    "model",
    [
        "org_" + "gh" + "p_" + "a" * 36,
        "org_" + "github" + "_pat_" + "a" * 82,
        "org_" + "AK" + "IA" + "A" * 16,
        "org_" + "AS" + "IA" + "A" * 16,
        "org_" + "AI" + "za" + "A" * 35,
        "org_" + "xox" + "b-" + "1" * 12 + "-" + "a" * 24,
        "org_" + "eyJ" + "a" * 12 + "." + "b" * 16 + "." + "c" * 16,
    ],
)
def test_invocation_rejects_namespaced_secret_models_without_echo(
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized: list[str] = []
    canonicalize = evidence_module._canonical_json

    def record_serialization(value: object) -> bytes:
        serialized.append(repr(value))
        return canonicalize(value)

    monkeypatch.setattr(evidence_module, "_canonical_json", record_serialization)
    with pytest.raises(EvidenceError, match="public model identifier") as captured:
        invocation_factory(model=model, commands=rendered_commands())
    assert captured.value.__cause__ is None
    assert model not in str(captured.value)
    assert all(model not in value for value in serialized)


def fixture_document() -> dict[str, object]:
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "graphify_0_9_48.json"
    ).read_bytes()
    return json.loads(payload)


def fixture_pipeline(
    *,
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
        "adapted/graph.json",
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
        staged_files=frozenset({PurePosixPath("fixture.py")}),
    )


def rebind_pipeline_invocation(
    native: CapturedArtifact,
    diagnosis_item: CapturedArtifact,
    normalization: NormalizationResult,
    raw_clustered: CapturedArtifact,
):
    return invocation_factory(
        artifacts=(
            native,
            diagnosis_item,
            normalization.cluster_input,
            raw_clustered,
            artifact("clustered/GRAPH_REPORT.md", b"# Graph report\n"),
        )
    )


def build_from_pipeline(
    pipeline,
    *,
    invocation=None,
    normalization=None,
    raw_clustered=None,
    final=None,
    staged_files=frozenset({PurePosixPath("fixture.py")}),
):
    (
        original_invocation,
        native,
        diagnosis_item,
        original_normalization,
        original_clustered,
        original_final,
    ) = pipeline
    return build_graph_evidence(
        contract(),
        source_digest="c" * 64,
        projection_digest="d" * 64,
        invocation=invocation or original_invocation,
        native_graph=native,
        diagnosis=diagnosis_item,
        normalization=normalization or original_normalization,
        clustered_graph=raw_clustered or original_clustered,
        final_graph=final or original_final,
        staged_files=staged_files,
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
    parsed = parse_graph_evidence(
        built.payload,
        contract(),
        expected_digest=built.digest,
    )

    assert parsed == built
    assert parsed.payload is built.payload or parsed.payload == built.payload
    assert parsed.digest == hashlib.sha256(parsed.payload).hexdigest()
    assert parsed.invocation.payload == built.invocation.payload
    assert parsed.invocation.digest == built.extraction_invocation_digest


def test_evidence_parser_requires_a_keyword_only_external_digest() -> None:
    built = build_fixture_evidence()

    with pytest.raises(TypeError):
        parse_graph_evidence(built.payload, contract())
    parsed = parse_graph_evidence(
        built.payload,
        contract(),
        expected_digest=built.digest,
    )
    assert parsed == built


@pytest.mark.parametrize("expected_digest", [None, True, "A" * 64, "0" * 63])
def test_evidence_parser_rejects_invalid_external_digest_before_json(
    expected_digest: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_parse(*args, **kwargs):
        raise AssertionError("JSON parser must not run")

    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(EvidenceError, match="digest") as captured:
        parse_graph_evidence(
            b"{}\n",
            contract(),
            expected_digest=expected_digest,
        )
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document["final_edges"][0].update(relation="forged"),
        lambda document: document["final_edges"][0].update(
            source_file="other.py"
        ),
    ],
)
def test_evidence_parser_rejects_canonical_payload_mutation_against_trusted_digest(
    mutation,
) -> None:
    built = build_fixture_evidence()
    document = json.loads(built.payload)
    mutation(document)
    mutated = canonical_json(document)

    with pytest.raises(EvidenceError, match="digest") as captured:
        parse_graph_evidence(
            mutated,
            contract(),
            expected_digest=built.digest,
        )
    assert captured.value.__cause__ is None
    assert built.digest not in str(captured.value)


def test_builder_rejects_a_valid_but_unrelated_final_graph() -> None:
    pipeline = fixture_pipeline()
    document = json.loads(pipeline[-1].payload)
    edge = document["links"][0]
    edge["relation"] = "unrelated_relation"
    identity_input = {
        key: value for key, value in edge.items() if key != "atlasweaver_edge_id"
    }
    edge["atlasweaver_edge_id"] = canonical_final_edge_id(
        identity_input, contract().semantics
    )
    unrelated = artifact("adapted/graph.json", canonical_json(document))

    with pytest.raises(EvidenceError, match="derived final graph"):
        build_from_pipeline(pipeline, final=unrelated)


def test_builder_rejects_unstaged_private_provenance_in_clustered_bytes() -> None:
    pipeline = fixture_pipeline()
    _, native, diagnosis_item, normalization, raw_clustered, final = pipeline
    document = json.loads(raw_clustered.payload)
    document["links"][0]["source_file"] = "private/.env"
    private_clustered = artifact("clustered/graph.json", canonical_json(document))
    invocation = rebind_pipeline_invocation(
        native, diagnosis_item, normalization, private_clustered
    )

    with pytest.raises(EvidenceError, match="derived final graph") as captured:
        build_from_pipeline(
            pipeline,
            invocation=invocation,
            raw_clustered=private_clustered,
            final=final,
        )
    assert "private/.env" not in str(captured.value)


def test_builder_requires_the_fixed_final_graph_logical_path() -> None:
    pipeline = fixture_pipeline()
    wrong_path = artifact("clustered/graph.json", pipeline[-1].payload)

    with pytest.raises(EvidenceError, match="derived final graph"):
        build_from_pipeline(pipeline, final=wrong_path)


@pytest.mark.parametrize(
    "staged_files",
    [
        {PurePosixPath("fixture.py")},
        frozenset({"fixture.py"}),
        frozenset({Path("fixture.py")}),
        frozenset({PurePosixPath("/private/fixture.py")}),
        frozenset({PurePosixPath(".")}),
    ],
)
def test_builder_requires_exact_confined_staged_files(staged_files: object) -> None:
    with pytest.raises(EvidenceError, match="staged files"):
        build_from_pipeline(fixture_pipeline(), staged_files=staged_files)


@pytest.mark.parametrize(
    "denied",
    [
        ".env",
        "private/.env",
        "config/auth-token.yaml",
        "src/database-creds.json",
        "workspace/runtime/state.json",
    ],
)
def test_builder_reapplies_immutable_privacy_denies_to_staged_files(
    denied: str,
) -> None:
    staged_files = frozenset(
        {PurePosixPath("fixture.py"), PurePosixPath(denied)}
    )

    with pytest.raises(EvidenceError, match="staged files") as captured:
        build_from_pipeline(fixture_pipeline(), staged_files=staged_files)
    assert denied not in str(captured.value)


def test_builder_accepts_scanned_sensitive_source_name() -> None:
    evidence = build_from_pipeline(
        fixture_pipeline(),
        staged_files=frozenset(
            {PurePosixPath("fixture.py"), PurePosixPath("src/credentials.py")}
        ),
    )

    assert evidence.source_digest == "c" * 64


@pytest.mark.parametrize(
    "denied",
    [
        "tokens/config.py",
        "a/token-store/file.py",
        "credentials/config.py",
        "creds/config.py",
        "secrets/config.py",
        "foo/secret-cache/a.py",
        "TOKENS/config.py",
        "a/Token-Store/file.py",
        "CREDENTIALS/config.py",
        "CREDS/config.py",
        "Secrets/config.py",
        "foo/Secret-Cache/a.py",
    ],
)
def test_builder_reapplies_immutable_privacy_denies_to_directory_descendants(
    denied: str,
) -> None:
    staged_files = frozenset(
        {PurePosixPath("fixture.py"), PurePosixPath(denied)}
    )

    with pytest.raises(EvidenceError, match="staged files") as captured:
        build_from_pipeline(fixture_pipeline(), staged_files=staged_files)
    assert captured.value.__cause__ is None
    assert denied not in str(captured.value)


def test_builder_rejects_oversized_final_before_any_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = fixture_pipeline()
    oversized = artifact(
        "clustered/graph.json", b" " * (GRAPH_EVIDENCE_MAX_BYTES + 1)
    )

    def must_not_parse(*args, **kwargs):
        raise AssertionError("JSON parser must not run")

    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(EvidenceError, match="final graph size cap"):
        build_from_pipeline(pipeline, final=oversized)


def test_builder_rejects_oversized_clustered_before_any_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = fixture_pipeline()
    _, native, diagnosis_item, normalization, _, final = pipeline
    oversized = artifact(
        "clustered/graph.json", b" " * (GRAPH_EVIDENCE_MAX_BYTES + 1)
    )
    invocation = rebind_pipeline_invocation(
        native, diagnosis_item, normalization, oversized
    )

    def must_not_parse(*args, **kwargs):
        raise AssertionError("JSON parser must not run")

    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(EvidenceError, match="clustered graph size cap"):
        build_from_pipeline(
            pipeline,
            invocation=invocation,
            raw_clustered=oversized,
            final=final,
        )


def mutate_normalization_cluster_input(
    normalization: NormalizationResult,
) -> NormalizationResult:
    document = json.loads(normalization.cluster_input.payload)
    document["input_tokens"] = 1
    return replace(
        normalization,
        cluster_input=artifact("cluster-input/graph.json", canonical_json(document)),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: replace(
            value,
            observed_integrity=replace(
                value.observed_integrity,
                node_count=value.observed_integrity.node_count + 1,
            ),
        ),
        mutate_normalization_cluster_input,
        lambda value: replace(
            value, repairs=(ReasonCount("unique_exact_node_alias", 1),)
        ),
        lambda value: replace(value, quarantines=()),
    ],
)
def test_builder_rejects_every_caller_mutation_of_normalization(mutation) -> None:
    pipeline = fixture_pipeline()
    _, native, diagnosis_item, normalization, raw_clustered, _ = pipeline
    mutated = mutation(normalization)
    invocation = rebind_pipeline_invocation(
        native, diagnosis_item, mutated, raw_clustered
    )

    with pytest.raises(EvidenceError, match="normalization result"):
        build_from_pipeline(
            pipeline,
            invocation=invocation,
            normalization=mutated,
        )


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


def test_evidence_limitations_are_independently_derived_sorted_and_unique() -> None:
    evidence = build_fixture_evidence()
    complete_edges = evidence.final_edges
    missing_provenance = (
        replace(complete_edges[0], source_line=None),
        *complete_edges[1:],
    )

    assert evidence_module._derive_evidence_limitations(
        contract(), (), complete_edges, pre_dedup=None
    ) == ("pre_dedup_edge_projection_unavailable",)
    assert evidence_module._derive_evidence_limitations(
        contract(),
        (ReasonCount("dangling_endpoint", 1),),
        complete_edges,
        pre_dedup=None,
    ) == (
        "pre_dedup_edge_projection_unavailable",
        "raw_endpoint_unresolved",
    )
    assert evidence_module._derive_evidence_limitations(
        contract(), (), missing_provenance, pre_dedup=None
    ) == (
        "impact_edge_provenance_incomplete",
        "pre_dedup_edge_projection_unavailable",
    )


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
        parse_semantic_evidence(payload)


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
        parse_semantic_evidence(mutate_evidence(mutator))


def test_evidence_parser_rejects_alternate_json_encoding() -> None:
    document = json.loads(build_fixture_evidence().payload)
    payload = json.dumps(document, ensure_ascii=False, indent=2).encode() + b"\n"

    with pytest.raises(EvidenceError, match="canonical"):
        parse_semantic_evidence(payload)


@pytest.mark.parametrize(
    "source_file",
    ["dir//x.py", "dir/./x.py", "fixture.py/"],
)
def test_evidence_parser_rejects_noncanonical_final_source_paths(
    source_file: str,
) -> None:
    payload = mutate_evidence(
        lambda value: value["final_edges"][0].update(source_file=source_file)
    )

    with pytest.raises(EvidenceError, match="path"):
        parse_semantic_evidence(payload)


@pytest.mark.parametrize(
    ("canonical", "alias"),
    [
        ("cluster-input/graph.json", "cluster-input//graph.json"),
        ("cluster-input/graph.json", "cluster-input/./graph.json"),
        ("cluster-input/graph.json", "cluster-input/graph.json/"),
        ("clustered/GRAPH_REPORT.md", "clustered//GRAPH_REPORT.md"),
        ("clustered/GRAPH_REPORT.md", "clustered/./GRAPH_REPORT.md"),
        ("clustered/GRAPH_REPORT.md", "clustered/GRAPH_REPORT.md/"),
        ("clustered/graph.json", "clustered//graph.json"),
        ("clustered/graph.json", "clustered/./graph.json"),
        ("clustered/graph.json", "clustered/graph.json/"),
        ("raw/diagnose.json", "raw//diagnose.json"),
        ("raw/diagnose.json", "raw/./diagnose.json"),
        ("raw/diagnose.json", "raw/diagnose.json/"),
        ("raw/graph.json", "raw//graph.json"),
        ("raw/graph.json", "raw/./graph.json"),
        ("raw/graph.json", "raw/graph.json/"),
    ],
)
def test_evidence_parser_rejects_noncanonical_invocation_artifact_paths(
    canonical: str, alias: str
) -> None:
    document = json.loads(build_fixture_evidence().payload)
    bindings = document["extraction_invocation"]["artifacts"]
    selected = next(item for item in bindings if item["path"] == canonical)
    selected["path"] = alias
    invocation_payload = canonical_json(document["extraction_invocation"])
    document["extraction_invocation_digest"] = hashlib.sha256(
        b"atlasweaver-graphify-pipeline-v1\0" + invocation_payload
    ).hexdigest()
    payload = canonical_json(document)

    with pytest.raises(EvidenceError, match="path"):
        parse_semantic_evidence(payload)


def forged_artifact_descriptor(**changes: object) -> CapturedArtifact:
    baseline = artifact("raw/graph.json", b"proof\n")
    forged = object.__new__(CapturedArtifact)
    values = {
        "logical_path": baseline.logical_path,
        "payload": baseline.payload,
        "sha256": baseline.sha256,
        "byte_length": baseline.byte_length,
    }
    values.update(changes)
    for name, value in values.items():
        object.__setattr__(forged, name, value)
    return forged


def uninitialized_pure_posix_path() -> PurePosixPath:
    return object.__new__(PurePosixPath)


def staged_set_with_forged_pure_posix_path() -> frozenset[PurePosixPath]:
    path = object.__new__(PurePosixPath)
    object.__setattr__(path, "_hash", 1)
    return frozenset({path})


@pytest.mark.parametrize(
    "forged",
    [
        forged_artifact_descriptor(logical_path="private-proof/graph.json"),
        forged_artifact_descriptor(logical_path=Path("raw/graph.json")),
        forged_artifact_descriptor(logical_path=PurePosixPath("/private-proof/graph.json")),
        forged_artifact_descriptor(payload=bytearray(b"proof\n")),
        forged_artifact_descriptor(sha256="A" * 64),
        forged_artifact_descriptor(sha256="0" * 64),
        forged_artifact_descriptor(byte_length=True),
        forged_artifact_descriptor(byte_length=999),
    ],
)
def test_artifact_descriptor_boundary_rejects_forgery_without_details(
    forged: CapturedArtifact,
) -> None:
    with pytest.raises(EvidenceError, match="artifact binding") as captured:
        evidence_module._require_artifact_descriptor(forged)
    assert captured.value.__cause__ is None
    assert "private-proof" not in str(captured.value)


def test_artifact_descriptor_boundary_sanitizes_missing_fields() -> None:
    forged = object.__new__(CapturedArtifact)

    with pytest.raises(EvidenceError, match="artifact binding") as captured:
        evidence_module._require_artifact_descriptor(forged)
    assert captured.value.__cause__ is None


def test_artifact_descriptor_sanitizes_uninitialized_pure_posix_path() -> None:
    forged = forged_artifact_descriptor(
        logical_path=uninitialized_pure_posix_path()
    )

    with pytest.raises(EvidenceError, match="artifact binding") as captured:
        evidence_module._require_artifact_descriptor(forged)
    assert captured.value.__cause__ is None


def test_staged_files_sanitize_forged_exact_pure_posix_path() -> None:
    staged_files = staged_set_with_forged_pure_posix_path()

    with pytest.raises(EvidenceError, match="staged files") as captured:
        build_from_pipeline(fixture_pipeline(), staged_files=staged_files)
    assert captured.value.__cause__ is None


def test_builder_sanitizes_uninitialized_extraction_invocation() -> None:
    forged = object.__new__(ExtractionInvocation)

    with pytest.raises(EvidenceError, match="invocation") as captured:
        build_from_pipeline(fixture_pipeline(), invocation=forged)
    assert captured.value.__cause__ is None


def test_builder_sanitizes_uninitialized_normalization_result() -> None:
    forged = object.__new__(NormalizationResult)

    with pytest.raises(EvidenceError, match="normalization") as captured:
        build_from_pipeline(fixture_pipeline(), normalization=forged)
    assert captured.value.__cause__ is None


def test_builder_sanitizes_nested_uninitialized_integrity() -> None:
    pipeline = fixture_pipeline()
    normalization = pipeline[3]
    forged = replace(
        normalization,
        observed_integrity=object.__new__(GraphIntegrity),
    )

    with pytest.raises(EvidenceError, match="normalization") as captured:
        build_from_pipeline(pipeline, normalization=forged)
    assert captured.value.__cause__ is None


def test_parser_sanitizes_nested_forged_registered_contract() -> None:
    built = build_fixture_evidence()
    forged = object.__new__(type(contract()))
    object.__setattr__(forged, "version", "0.9.48")

    with pytest.raises(EvidenceError, match="registered") as captured:
        parse_graph_evidence(
            built.payload,
            forged,
            expected_digest=built.digest,
        )
    assert captured.value.__cause__ is None


def test_graph_evidence_size_boundary_is_checked_before_json_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    at_cap = b" " * GRAPH_EVIDENCE_MAX_BYTES
    assert evidence_module._bounded_evidence_payload(at_cap) is at_cap

    def must_not_parse(*args, **kwargs):
        raise AssertionError("JSON allocation must not occur")

    monkeypatch.setattr(json, "loads", must_not_parse)
    with pytest.raises(EvidenceError, match="size cap"):
        parse_graph_evidence(
            at_cap + b"x",
            contract(),
            expected_digest="0" * 64,
        )


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
            staged_files=frozenset({PurePosixPath("fixture.py")}),
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
            staged_files=frozenset({PurePosixPath("fixture.py")}),
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
            staged_files=frozenset({PurePosixPath("fixture.py")}),
        )


def test_final_edge_evidence_index_is_read_only_and_joinable() -> None:
    evidence = build_fixture_evidence()

    index = index_final_edge_evidence(evidence)

    assert tuple(index) == tuple(item.final_edge_id for item in evidence.final_edges)
    graph = json.loads(fixture_pipeline()[5].payload)
    assert all(edge["atlasweaver_edge_id"] in index for edge in graph["links"])
    with pytest.raises(TypeError):
        index["forged"] = evidence.final_edges[0]  # type: ignore[index]


def test_final_edge_evidence_index_rejects_duplicate_ids() -> None:
    evidence = build_fixture_evidence()
    duplicated = replace(
        evidence, final_edges=(*evidence.final_edges, evidence.final_edges[0])
    )

    with pytest.raises(EvidenceError, match="duplicated"):
        index_final_edge_evidence(duplicated)


@pytest.mark.parametrize(
    ("flags", "limitation"),
    [
        ({"source_current": False}, "source_digest_stale"),
        ({"source_current": None}, "source_digest_unverified"),
        ({"projection_current": False}, "projection_digest_stale"),
        ({"projection_current": None}, "projection_digest_unverified"),
        ({"coverage_complete": False}, "scope_coverage_incomplete"),
        ({"artifacts_bound": False}, "artifact_binding_invalid"),
    ],
)
def test_external_trust_predicates_have_distinct_stable_limitations(
    flags: dict[str, object], limitation: str
) -> None:
    evidence = build_fixture_evidence()
    arguments = {
        "source_current": True,
        "projection_current": True,
        "coverage_complete": True,
        "artifacts_bound": True,
    }
    arguments.update(flags)

    trust = decide_impact_trust(
        evidence, evidence.final_integrity, **arguments  # type: ignore[arg-type]
    )

    assert trust.level == "navigation"
    assert limitation in trust.limitations
    assert trust.limitations == tuple(sorted(set(trust.limitations)))


def test_graphify_0948_remains_navigation_only_with_clean_external_flags() -> None:
    evidence = build_fixture_evidence()

    trust = decide_impact_trust(
        evidence,
        evidence.final_integrity,
        source_current=True,
        projection_current=True,
        coverage_complete=True,
        artifacts_bound=True,
    )

    assert trust.level == "navigation"
    assert "pre_dedup_edge_projection_unavailable" in trust.limitations
