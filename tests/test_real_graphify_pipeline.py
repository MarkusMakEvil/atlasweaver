from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from uuid import UUID

import pytest

from project_knowledge.adapter import adapt_candidate
from project_knowledge.adapters import (
    CapturedArtifact,
    adapter_for,
    capture_native_artifact,
)
from project_knowledge.artifacts import (
    promote_graph,
    validate_candidate,
    validate_owned_graph,
)
from project_knowledge.compatibility import (
    RenderedCommand,
    production_graphify_compatibility,
    render_graphify_argv,
)
from project_knowledge.evidence import (
    CommandEnvironmentBinding,
    build_extraction_invocation,
    build_graph_evidence,
)
from project_knowledge.graphify import (
    SubprocessCommandRunner,
    _sanitize_diagnosis,
    minimal_environment,
    probe_graphify,
    resolve_graphify_executable,
    run_graphify_operation,
)
from project_knowledge.locking import (
    capture_lifecycle_repository,
    repository_lifecycle_lock,
)
from project_knowledge.manifest import render_manifest_v2
from project_knowledge.staging import stage_input
from tests.support import manifest_v2


_NATIVE_GRAPH_CAP = 256 * 1024 * 1024
_DIAGNOSIS_CAP = 4 * 1024 * 1024
_ARTIFACT_CAP = 64 * 1024 * 1024
_EDGE_ID = re.compile(r"edge-[0-9a-f]{64}\Z")


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _require_production_graphify() -> Path:
    contract = production_graphify_compatibility()
    located = shutil.which("graphify")
    if located is None:
        pytest.skip("production Graphify executable is unavailable")
    result = subprocess.run(
        [located, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != f"graphify {contract.version}":
        pytest.skip("exact production Graphify executable is unavailable")
    return Path(located)


def _run(
    runner: SubprocessCommandRunner,
    executable: object,
    command: RenderedCommand,
) -> subprocess.CompletedProcess[str]:
    return run_graphify_operation(
        runner,
        executable,  # type: ignore[arg-type]
        command.argv,
        env=minimal_environment(),
        timeout=60.0,
        operation=command.operation,
    )


def _configuration_digest(project_uid: UUID) -> str:
    payload = json.dumps(
        {
            "backend": None,
            "code_only": True,
            "deep": False,
            "graphify_version": production_graphify_compatibility().version,
            "manifest_schema_version": 2,
            "model": None,
            "project_uid": str(project_uid),
            "track_html": False,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_real_graphify_pipeline_builds_navigation_evidence_and_owned_graph(
    tmp_path: Path,
) -> None:
    binary = _require_production_graphify()
    contract = production_graphify_compatibility()
    executable = resolve_graphify_executable(test_override=binary)
    runner = SubprocessCommandRunner()
    capabilities = probe_graphify(executable, contract, runner)

    repo = tmp_path / "repo"
    source = (
        b"def helper(value: int) -> int:\n"
        b"    return value + 1\n\n"
        b"def run() -> int:\n"
        b"    return helper(1)\n"
    )
    _write(repo / "src/app.py", source)
    selected = manifest_v2(
        project_id="integration-demo",
        project_uid=UUID("973a04ea-47aa-4ad7-90ad-d7c0d14f55e0"),
        display_name="Integration Demo",
        include_roots=(PurePosixPath("src"),),
        obsidian_namespace=PurePosixPath(
            "Projects/integration-demo/Generated"
        ),
        track_html=False,
    )
    _write(repo / ".graphify-project.yaml", render_manifest_v2(selected))

    staged = stage_input(repo, selected, tmp_path / "private-stage")
    assert staged.projection_digest is not None
    raw_workspace = tmp_path / "raw-workspace"
    native_path = raw_workspace / "graphify-out/graph.json"
    extract = render_graphify_argv(
        contract,
        "extract",
        binary=executable.path,
        source=staged.root,
        output=raw_workspace,
        code_only=True,
    )
    _run(runner, executable, extract)
    native = capture_native_artifact(
        native_path,
        PurePosixPath("raw/graph.json"),
        max_bytes=_NATIVE_GRAPH_CAP,
    )

    diagnose = render_graphify_argv(
        contract,
        "diagnose",
        binary=executable.path,
        source=staged.root,
        output=raw_workspace,
        graph=native_path,
    )
    diagnosis_result = _run(runner, executable, diagnose)
    diagnosis_document = _sanitize_diagnosis(json.loads(diagnosis_result.stdout))
    diagnosis_path = _write(
        tmp_path / "private-diagnosis.json",
        json.dumps(
            diagnosis_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    diagnosis = capture_native_artifact(
        diagnosis_path,
        PurePosixPath("raw/diagnose.json"),
        max_bytes=_DIAGNOSIS_CAP,
    )

    implementation = adapter_for(contract)
    parsed_native = implementation.parse_post_dedup(native)
    normalization = implementation.normalize_for_cluster(parsed_native)
    cluster_workspace = tmp_path / "cluster-workspace"
    cluster_input_path = _write(
        cluster_workspace / "graphify-out/graph.json",
        normalization.cluster_input.payload,
    )
    assert native_path.read_bytes() == native.payload

    cluster = render_graphify_argv(
        contract,
        "cluster",
        binary=executable.path,
        source=cluster_workspace,
        output=cluster_workspace,
        graph=cluster_input_path,
        track_html=False,
    )
    _run(runner, executable, cluster)
    assert native_path.read_bytes() == native.payload
    clustered = capture_native_artifact(
        cluster_workspace / "graphify-out/graph.json",
        PurePosixPath("clustered/graph.json"),
        max_bytes=_ARTIFACT_CAP,
    )
    report = capture_native_artifact(
        cluster_workspace / "graphify-out/GRAPH_REPORT.md",
        PurePosixPath("clustered/GRAPH_REPORT.md"),
        max_bytes=_ARTIFACT_CAP,
    )
    final_graph = CapturedArtifact.from_payload(
        PurePosixPath("adapted/graph.json"),
        implementation.adapt_clustered_graph(
            clustered,
            staged_files=frozenset(staged.files),
        ),
    )

    environment_names = tuple(sorted(minimal_environment()))
    invocation = build_extraction_invocation(
        contract,
        executable_sha256=executable.launcher_sha256,
        capability_smoke_digest=capabilities.capability_probe.digest,
        commands=(extract, diagnose, cluster),
        backend=None,
        model=None,
        configuration_sha256=_configuration_digest(selected.project_uid),
        source_digest=staged.source_digest,
        projection_digest=staged.projection_digest,
        environments=(
            CommandEnvironmentBinding("extract", environment_names),
            CommandEnvironmentBinding("diagnose", environment_names),
            CommandEnvironmentBinding("cluster", environment_names),
        ),
        artifacts=(
            native,
            diagnosis,
            normalization.cluster_input,
            clustered,
            report,
        ),
    )
    evidence = build_graph_evidence(
        contract,
        source_digest=staged.source_digest,
        projection_digest=staged.projection_digest,
        invocation=invocation,
        native_graph=native,
        diagnosis=diagnosis,
        normalization=normalization,
        clustered_graph=clustered,
        final_graph=final_graph,
        staged_files=frozenset(staged.files),
    )

    candidate = tmp_path / "candidate"
    adapted = adapt_candidate(
        cluster_workspace / "graphify-out",
        candidate,
        staged,
        selected,
        evidence=evidence,
    )
    validated = validate_candidate(
        adapted.root,
        staged,
        selected,
        expected_projection_digest=staged.projection_digest,
        expected_evidence_digest=evidence.digest,
        build_epoch=1,
    )

    assert validated.artifact_schema_version == 2
    assert validated.impact_trust == "navigation"
    assert "pre_dedup_edge_projection_unavailable" in validated.impact_limitations
    if evidence.normalization_quarantines:
        assert "raw_endpoint_unresolved" in validated.impact_limitations
    assert validated.build_epoch == 1
    assert (candidate / "GRAPH_EVIDENCE.json").is_file()
    assert all(_EDGE_ID.fullmatch(item.final_edge_id) for item in evidence.final_edges)
    unresolved = (
        normalization.observed_integrity.missing_endpoint_edges
        + normalization.observed_integrity.dangling_endpoint_edges
    )
    assert sum(item.count for item in evidence.normalization_quarantines) == unresolved
    adapted_report = (candidate / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "Native observed edges:" in adapted_report
    assert "Normalization quarantines:" in adapted_report
    assert "Final edges:" in adapted_report

    with repository_lifecycle_lock(repo), capture_lifecycle_repository(
        repo
    ) as repository:
        promote_graph(validated, repo, repository_access=repository)
        owned = validate_owned_graph(
            repo / "graphify-out",
            selected,
            expected_source_digest=staged.source_digest,
            expected_projection_digest=staged.projection_digest,
            repository_access=repository,
        )
    assert owned.evidence_digest == validated.evidence_digest
    assert owned.generation_digest == validated.generation_digest
