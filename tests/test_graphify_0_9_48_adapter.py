from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path, PurePosixPath
import re

import pytest

from project_knowledge.adapters import (
    AdapterContractError,
    CapturedArtifact,
    NativeGraph,
    ReasonCount,
    adapter_for,
    capture_native_artifact,
)
from project_knowledge.adapters import _ADAPTER_FACTORIES
from project_knowledge.compatibility import (
    resolve_graphify_compatibility,
    supported_graphify_versions,
)
from project_knowledge.integrity import canonical_final_edge_id


DIAGNOSTIC_FIELDS = {
    "context_variant_groups",
    "dangling_endpoint_edges",
    "effective_directed",
    "exact_duplicate_edges",
    "missing_endpoint_edges",
    "node_count",
    "post_build_edge_count",
    "post_build_graph_type",
    "post_build_node_count",
    "raw_edge_count",
    "relation_variant_groups",
    "same_endpoint_group_count",
    "self_loop_edges",
    "source_file_variant_groups",
    "source_location_variant_groups",
    "undirected_same_endpoint_collapsed_edges",
    "undirected_unique_endpoint_pairs",
}


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def assert_no_absolute_posix_or_windows_strings(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert_no_absolute_posix_or_windows_strings(key)
            assert_no_absolute_posix_or_windows_strings(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_absolute_posix_or_windows_strings(item)
    elif isinstance(value, str):
        assert not value.startswith("/")
        assert re.match(r"^[A-Za-z]:[\\/]|^\\\\", value) is None


def adapter():
    return adapter_for(resolve_graphify_compatibility("0.9.48"))


def native_artifact(
    *,
    nodes: list[dict[str, object]] | None = None,
    edges: list[dict[str, object]] | None = None,
) -> CapturedArtifact:
    document = {
        "nodes": nodes
        if nodes is not None
        else [{"id": "module:a", "label": "a"}, {"id": "function:run", "label": "run"}],
        "edges": edges
        if edges is not None
        else [{"source": "module:a", "target": "function:run", "relation": "calls"}],
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }
    return CapturedArtifact.from_payload(
        PurePosixPath("raw/graph.json"), canonical_json(document)
    )


def native_fixture() -> CapturedArtifact:
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "graphify_0_9_48.json"
    ).read_bytes()
    document = json.loads(payload)
    return CapturedArtifact.from_payload(
        PurePosixPath("raw/graph.json"), canonical_json(document["native_graph"])
    )


def clustered_artifact(document: object) -> CapturedArtifact:
    return CapturedArtifact.from_payload(
        PurePosixPath("clustered/graph.json"), canonical_json(document)
    )


def test_packaged_fixture_is_safe_real_0948_output() -> None:
    payload = resources.files("project_knowledge.compatibility_fixtures").joinpath(
        "graphify_0_9_48.json"
    ).read_bytes()
    document = json.loads(payload)

    assert payload == canonical_json(document)
    assert document["schema_version"] == 1
    assert document["graphify_version"] == "0.9.48"
    assert document["native_graph"]["edges"]
    assert set(document["diagnosis"]) == {"schema_version", "summary"}
    assert set(document["diagnosis"]["summary"]) == DIAGNOSTIC_FIELDS
    assert "producer_suppression" not in document["diagnosis"]
    assert "examples" not in document["diagnosis"]
    assert "input_path" not in json.dumps(document["diagnosis"])
    assert b"/Users/" not in payload and b"/home/" not in payload and b"/tmp/" not in payload
    assert_no_absolute_posix_or_windows_strings(document)
    contract = resolve_graphify_compatibility("0.9.48")
    assert hashlib.sha256(payload).hexdigest() == contract.compatibility_fixture_digest


def test_fixture_source_remains_the_reviewed_code_only_corpus() -> None:
    assert Path("tests/fixtures/graphify/0.9.48/source/fixture.py").read_bytes() == (
        b"from dataclasses import dataclass\n\n\n"
        b"@dataclass(frozen=True)\n"
        b"class Request:\n"
        b"    name: str\n\n\n"
        b"def normalize(request: Request) -> str:\n"
        b"    return request.name.strip().casefold()\n\n\n"
        b"def dispatch(request: Request) -> str:\n"
        b"    return normalize(request)\n"
    )
    assert Path(
        "tests/fixtures/graphify/0.9.48/source/.atlasweaver-fixture-source.json"
    ).read_bytes() == b'{"files":["fixture.py"],"schema_version":1}\n'


def test_adapter_factories_exactly_cover_registry_adapter_ids() -> None:
    expected = {
        resolve_graphify_compatibility(version).adapter_id
        for version in supported_graphify_versions()
    }

    assert set(_ADAPTER_FACTORIES) == expected
    resolved = [
        adapter_for(resolve_graphify_compatibility(version)).contract.adapter_id
        for version in supported_graphify_versions()
    ]
    assert sorted(resolved) == sorted(expected)


def test_adapter_rejects_a_spoofed_contract_with_a_known_adapter_id() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    with pytest.raises(AdapterContractError, match="registered compatibility contract"):
        adapter_for(replace(contract, native_schema_fingerprint="0" * 64))


def test_captured_artifact_from_payload_is_immutable_and_self_describing() -> None:
    artifact = CapturedArtifact.from_payload(PurePosixPath("raw/graph.json"), b"{}\n")

    assert artifact.sha256 == "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"
    assert artifact.byte_length == 3
    with pytest.raises(FrozenInstanceError):
        artifact.payload = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "logical_path",
    [
        PurePosixPath("."),
        PurePosixPath("/raw/graph.json"),
        PurePosixPath("../raw/graph.json"),
        PurePosixPath("raw/../graph.json"),
        PurePosixPath("raw\\graph.json"),
        PurePosixPath("C:/raw/graph.json"),
        PurePosixPath("raw/graph\x00.json"),
    ],
)
def test_captured_artifact_rejects_unconfined_logical_paths(
    logical_path: PurePosixPath,
) -> None:
    with pytest.raises(AdapterContractError, match="logical artifact path"):
        CapturedArtifact.from_payload(logical_path, b"{}\n")


def test_captured_artifact_rejects_forged_digest_or_length() -> None:
    with pytest.raises(AdapterContractError, match="descriptor does not match"):
        CapturedArtifact(PurePosixPath("raw/graph.json"), b"{}\n", "0" * 64, 3)
    with pytest.raises(AdapterContractError, match="descriptor does not match"):
        CapturedArtifact(
            PurePosixPath("raw/graph.json"),
            b"{}\n",
            "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
            4,
        )


def test_capture_native_artifact_reads_a_stable_regular_file_at_the_cap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "graph.json"
    path.write_bytes(b"12345678")

    artifact = capture_native_artifact(
        path, PurePosixPath("raw/graph.json"), max_bytes=8
    )

    assert artifact.payload == b"12345678"
    assert artifact.byte_length == 8
    assert artifact.sha256 == hashlib.sha256(b"12345678").hexdigest()


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1.5])
def test_capture_native_artifact_rejects_invalid_caps(
    tmp_path: Path, max_bytes: object
) -> None:
    path = tmp_path / "graph.json"
    path.write_bytes(b"x")
    with pytest.raises(AdapterContractError, match="capture size cap"):
        capture_native_artifact(
            path,
            PurePosixPath("raw/graph.json"),
            max_bytes=max_bytes,  # type: ignore[arg-type]
        )


def test_capture_native_artifact_rejects_oversize_symlink_and_nonregular(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"123456789")
    with pytest.raises(AdapterContractError, match="exceeds capture size cap"):
        capture_native_artifact(
            oversized, PurePosixPath("raw/graph.json"), max_bytes=8
        )

    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(AdapterContractError, match="stable regular file"):
        capture_native_artifact(link, PurePosixPath("raw/graph.json"), max_bytes=8)

    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(AdapterContractError, match="stable regular file"):
        capture_native_artifact(
            directory, PurePosixPath("raw/graph.json"), max_bytes=8
        )


def test_capture_native_artifact_rejects_in_place_mutation_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "graph.json"
    path.write_bytes(b"x" * 70_000)
    original_read = os.read
    mutated = False

    def mutate_after_first_read(descriptor: int, count: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, count)
        if not mutated:
            mutated = True
            with path.open("ab") as stream:
                stream.write(b"y")
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_first_read)
    with pytest.raises(AdapterContractError, match="changed during capture"):
        capture_native_artifact(
            path, PurePosixPath("raw/graph.json"), max_bytes=100_000
        )


def test_parse_post_dedup_accepts_the_exact_native_schema() -> None:
    artifact = native_artifact()
    graph = adapter().parse_post_dedup(artifact)

    assert tuple(node["id"] for node in graph.nodes) == ("module:a", "function:run")
    assert tuple(edge["relation"] for edge in graph.edges) == ("calls",)
    assert graph.document["input_tokens"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        b'{"nodes":[],"nodes":[],"edges":[],"hyperedges":[],"input_tokens":0,"output_tokens":0}\n',
        b'{"nodes":[{"id":"a","id":"b"}],"edges":[],"hyperedges":[],"input_tokens":0,"output_tokens":0}\n',
        b'{"nodes":[{"id":"a"}],"edges":[],"hyperedges":[],"input_tokens":NaN,"output_tokens":0}\n',
        b'{"nodes":[{"id":"a"}],"edges":[],"hyperedges":[],"input_tokens":Infinity,"output_tokens":0}\n',
        b'{"nodes":[{"id":"a"}],"edges":[],"hyperedges":[],"input_tokens":1e400,"output_tokens":0}\n',
    ],
)
def test_parse_post_dedup_rejects_duplicate_keys_and_nonfinite_numbers(
    payload: bytes,
) -> None:
    artifact = CapturedArtifact.from_payload(PurePosixPath("raw/graph.json"), payload)
    with pytest.raises(AdapterContractError, match="native graph JSON"):
        adapter().parse_post_dedup(artifact)


@pytest.mark.parametrize(
    "document",
    [
        {"nodes": [], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0},
        {"nodes": ["a"], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0},
        {"nodes": [{"id": "a"}], "links": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0},
        {"nodes": [{"id": "a"}], "edges": [], "hyperedges": [], "input_tokens": 0, "output_tokens": 0, "project_id": "wrapped"},
        {"nodes": [{"id": "a"}], "edges": [{"source": None, "target": "a", "relation": "calls"}], "hyperedges": [], "input_tokens": 0, "output_tokens": 0},
    ],
)
def test_parse_post_dedup_rejects_non_native_or_invalid_schema(
    document: object,
) -> None:
    artifact = CapturedArtifact.from_payload(
        PurePosixPath("raw/graph.json"), canonical_json(document)
    )
    with pytest.raises(AdapterContractError, match="native schema"):
        adapter().parse_post_dedup(artifact)


def test_parse_post_dedup_binds_the_registry_schema_fingerprint() -> None:
    contract = resolve_graphify_compatibility("0.9.48")
    implementation = type(adapter())(
        replace(contract, native_schema_fingerprint="0" * 64)
    )
    with pytest.raises(AdapterContractError, match="native schema fingerprint"):
        implementation.parse_post_dedup(native_artifact())


def test_normalization_quarantines_external_import_endpoints() -> None:
    implementation = adapter()
    graph = implementation.parse_post_dedup(native_fixture())
    original = canonical_json(graph.document)

    result = implementation.normalize_for_cluster(graph)

    normalized = json.loads(result.cluster_input.payload)
    node_ids = {node["id"] for node in normalized["nodes"]}
    assert all(
        edge["source"] in node_ids and edge["target"] in node_ids
        for edge in normalized["edges"]
    )
    assert result.quarantines == (ReasonCount("dangling_endpoint", 1),)
    assert result.observed_integrity.dangling_endpoint_edges == 1
    assert result.observed_integrity.edge_count == 7
    assert len(normalized["edges"]) == 6
    assert canonical_json(graph.document) == original


def test_normalization_repairs_only_a_unique_exact_alias() -> None:
    artifact = native_artifact(
        nodes=[
            {"id": "module:a", "label": "a"},
            {"id": "function:run", "label": "run"},
        ],
        edges=[{"source": "a", "target": "function:run", "relation": "calls"}],
    )

    result = adapter().normalize_for_cluster(adapter().parse_post_dedup(artifact))

    edge = json.loads(result.cluster_input.payload)["edges"][0]
    assert edge["source"] == "module:a"
    assert result.repairs == (ReasonCount("unique_exact_node_alias", 1),)
    assert result.quarantines == ()


def test_ambiguous_alias_is_quarantined_not_guessed() -> None:
    artifact = native_artifact(
        nodes=[{"id": "a:run", "label": "run"}, {"id": "b:run", "label": "run"}],
        edges=[{"source": "run", "target": "a:run", "relation": "calls"}],
    )

    result = adapter().normalize_for_cluster(adapter().parse_post_dedup(artifact))

    assert json.loads(result.cluster_input.payload)["edges"] == []
    assert result.quarantines == (ReasonCount("ambiguous_endpoint_alias", 1),)


@pytest.mark.parametrize("endpoint", [None, "", 1, ["a"]])
def test_normalization_quarantines_missing_or_nonstring_endpoints(
    endpoint: object,
) -> None:
    graph = NativeGraph(
        document={},
        nodes=({"id": "a"},),
        edges=({"source": endpoint, "target": "a", "relation": "calls"},),
    )

    result = adapter().normalize_for_cluster(graph)

    assert json.loads(result.cluster_input.payload)["edges"] == []
    assert result.quarantines == (ReasonCount("missing_endpoint", 1),)


def test_normalization_exact_ids_precede_aliases_and_do_not_chain() -> None:
    graph = NativeGraph(
        document={},
        nodes=(
            {"id": "a", "label": "b"},
            {"id": "b", "label": "a"},
        ),
        edges=({"source": "a", "target": "b", "relation": "calls"},),
    )

    result = adapter().normalize_for_cluster(graph)

    assert json.loads(result.cluster_input.payload)["edges"] == [
        {"relation": "calls", "source": "a", "target": "b"}
    ]
    assert result.repairs == ()
    assert result.quarantines == ()


def test_normalization_uses_only_nonempty_exact_label_fields() -> None:
    graph = NativeGraph(
        document={},
        nodes=(
            {"id": "a", "label": "", "norm_label": None},
            {"id": "b", "label": 7, "norm_label": "normalized"},
        ),
        edges=({"source": "normalized", "target": "a", "relation": "calls"},),
    )

    result = adapter().normalize_for_cluster(graph)

    assert json.loads(result.cluster_input.payload)["edges"][0]["source"] == "b"
    assert result.repairs == (ReasonCount("unique_exact_node_alias", 1),)


def test_normalization_reason_counts_are_sorted_and_kept_edges_preserve_order() -> None:
    graph = NativeGraph(
        document={"hyperedges": [], "input_tokens": 0, "output_tokens": 0},
        nodes=({"id": "a", "label": "alias"}, {"id": "b"}),
        edges=(
            {"source": "missing", "target": "b", "relation": "first"},
            {"source": "alias", "target": "b", "relation": "second"},
            {"source": None, "target": "b", "relation": "third"},
            {"source": "a", "target": "b", "relation": "fourth"},
        ),
    )

    result = adapter().normalize_for_cluster(graph)
    normalized = json.loads(result.cluster_input.payload)

    assert [edge["relation"] for edge in normalized["edges"]] == ["second", "fourth"]
    assert result.repairs == (ReasonCount("unique_exact_node_alias", 1),)
    assert result.quarantines == (
        ReasonCount("dangling_endpoint", 1),
        ReasonCount("missing_endpoint", 1),
    )


def test_normalization_records_a_repair_even_when_another_endpoint_is_quarantined() -> None:
    graph = NativeGraph(
        document={},
        nodes=({"id": "a", "label": "alias"},),
        edges=(
            {"source": "alias", "target": "missing", "relation": "calls"},
        ),
    )

    result = adapter().normalize_for_cluster(graph)

    assert json.loads(result.cluster_input.payload)["edges"] == []
    assert result.repairs == (ReasonCount("unique_exact_node_alias", 1),)
    assert result.quarantines == (ReasonCount("dangling_endpoint", 1),)


def valid_clustered_document() -> dict[str, object]:
    return {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "module:a",
                "label": "pkg/a.py",
                "norm_label": "pkg/a.py",
                "source_file": "src/pkg/a.py",
                "source_path": "",
                "description": "",
            },
            {"id": "function:run", "label": "run", "source_file": "src/pkg/a.py"},
        ],
        "links": [
            {
                "source": "module:a",
                "target": "function:run",
                "relation": "contains",
                "confidence": "EXTRACTED",
                "source_file": "src/pkg/a.py",
                "source_location": "L1",
                "context": "navigation-only",
                "path": "",
            }
        ],
    }


def test_adapt_clustered_graph_cleans_paths_canonicalizes_aliases_and_adds_ids() -> None:
    document = valid_clustered_document()

    payload = adapter().adapt_clustered_graph(
        clustered_artifact(document),
        staged_files=frozenset({PurePosixPath("src/pkg/a.py")}),
    )

    final = json.loads(payload)
    assert payload == canonical_json(final)
    assert final["nodes"][0]["label"] == "src/pkg/a.py"
    assert final["nodes"][0]["norm_label"] == "src/pkg/a.py"
    assert "source_path" not in final["nodes"][0]
    assert final["nodes"][0]["description"] == ""
    assert "path" not in final["links"][0]
    edge = final["links"][0]
    identity_input = {key: value for key, value in edge.items() if key != "atlasweaver_edge_id"}
    assert edge["atlasweaver_edge_id"] == canonical_final_edge_id(
        identity_input, resolve_graphify_compatibility("0.9.48").semantics
    )


def test_adapt_clustered_graph_canonicalizes_source_aliases_only_for_staged_files() -> None:
    with pytest.raises(AdapterContractError, match="staged input"):
        adapter().adapt_clustered_graph(
            clustered_artifact(valid_clustered_document()),
            staged_files=frozenset({PurePosixPath("other.py")}),
        )


@pytest.mark.parametrize(
    "path",
    [PurePosixPath("."), PurePosixPath("C:/src/a.py"), PurePosixPath("src/a\x00.py")],
)
def test_adapt_clustered_graph_rejects_unconfined_staged_paths(
    path: PurePosixPath,
) -> None:
    with pytest.raises(AdapterContractError, match="staged files contain an invalid path"):
        adapter().adapt_clustered_graph(
            clustered_artifact(valid_clustered_document()),
            staged_files=frozenset({path}),
        )


@pytest.mark.parametrize(
    "denied",
    [
        ".env",
        "private/.env",
        "config/auth-token.yaml",
        "workspace/runtime/state.json",
    ],
)
def test_adapt_clustered_graph_reapplies_immutable_privacy_denies(
    denied: str,
) -> None:
    staged_files = frozenset(
        {PurePosixPath("src/pkg/a.py"), PurePosixPath(denied)}
    )

    with pytest.raises(AdapterContractError, match="staged files") as captured:
        adapter().adapt_clustered_graph(
            clustered_artifact(valid_clustered_document()),
            staged_files=staged_files,
        )
    assert denied not in str(captured.value)


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
def test_adapt_clustered_graph_reapplies_immutable_privacy_denies_to_directory_descendants(
    denied: str,
) -> None:
    staged_files = frozenset(
        {PurePosixPath("src/pkg/a.py"), PurePosixPath(denied)}
    )

    with pytest.raises(AdapterContractError, match="staged files") as captured:
        adapter().adapt_clustered_graph(
            clustered_artifact(valid_clustered_document()),
            staged_files=staged_files,
        )
    assert captured.value.__cause__ is None
    assert denied not in str(captured.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(edges=value.pop("links")),
        lambda value: value.update(directed=True),
        lambda value: value.update(multigraph=True),
        lambda value: value["nodes"].append({"id": "module:a"}),
        lambda value: value["links"].append(
            {"source": "missing", "target": "function:run", "relation": "calls"}
        ),
        lambda value: value["links"][0].update(atlasweaver_edge_id="edge-" + "0" * 64),
    ],
)
def test_adapt_clustered_graph_fails_closed_on_invalid_structure(mutation) -> None:
    document = valid_clustered_document()
    mutation(document)

    with pytest.raises(AdapterContractError):
        adapter().adapt_clustered_graph(
            clustered_artifact(document),
            staged_files=frozenset({PurePosixPath("src/pkg/a.py")}),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"directed":false,"multigraph":false,"nodes":[],"nodes":[],"links":[]}\n',
        b'{"directed":false,"multigraph":false,"nodes":[],"links":[],"graph":{"weight":NaN}}\n',
        b'{"directed":false,"multigraph":false,"nodes":[],"links":[],"graph":{"weight":Infinity}}\n',
        b'{"directed":false,"multigraph":false,"nodes":[],"links":[],"graph":{"weight":1e400}}\n',
    ],
)
def test_adapt_clustered_graph_rejects_noncanonical_json_values(payload: bytes) -> None:
    artifact = CapturedArtifact.from_payload(PurePosixPath("clustered/graph.json"), payload)
    with pytest.raises(AdapterContractError, match="clustered graph JSON"):
        adapter().adapt_clustered_graph(
            artifact,
            staged_files=frozenset({PurePosixPath("src/pkg/a.py")}),
        )


def test_adapt_clustered_graph_rejects_nonfinite_values_created_in_memory() -> None:
    document = valid_clustered_document()
    document["graph"] = {"weight": math.inf}
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    artifact = CapturedArtifact.from_payload(PurePosixPath("clustered/graph.json"), payload)
    with pytest.raises(AdapterContractError, match="clustered graph JSON"):
        adapter().adapt_clustered_graph(
            artifact,
            staged_files=frozenset({PurePosixPath("src/pkg/a.py")}),
        )
