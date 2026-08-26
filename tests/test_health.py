from __future__ import annotations

from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from project_knowledge.compatibility import production_graphify_compatibility
from project_knowledge.integrity import analyze_graph
from project_knowledge.health import (
    FeatureHealth,
    KnowledgeState,
    assess_health,
    inspect_project_state,
    safe_input_snapshot,
)
from project_knowledge.locking import RepositoryAccess
from project_knowledge.models import ProjectManifest
from project_knowledge.staging import inspect_projection
from tests.support import manifest_v2
from tests.test_manifest import write_manifest as write_v1_manifest_file


def healthy_state(**changes: object) -> KnowledgeState:
    values: dict[str, object] = {
        "project_id": "demo",
        "manifest_schema_version": 2,
        "artifact_schema_version": 2,
        "graph_exists": True,
        "graph_valid": True,
        "graph_version": "0.9.48",
        "current_source_digest": "a" * 64,
        "graph_source_digest": "a" * 64,
        "current_projection_digest": "b" * 64,
        "graph_projection_digest": "b" * 64,
        "features": {
            "atlas": FeatureHealth("disabled"),
            "registry": FeatureHealth("disabled"),
            "artifacts": FeatureHealth("disabled"),
        },
        "coverage_skips": 0,
        "unapproved_skips": 0,
        "impact_trust": "navigation",
        "impact_limitations": ("pre_dedup_edge_projection_unavailable",),
        "errors": (),
    }
    values.update(changes)
    return KnowledgeState(**values)  # type: ignore[arg-type]


def test_disabled_optional_features_do_not_lower_core_health() -> None:
    health = assess_health(healthy_state())

    assert health.schema_version == 2
    assert health.core_status == health.status == "healthy"
    assert health.features["atlas"].status == "disabled"
    assert health.issues == ()
    assert "impact_evidence_incomplete" in health.warnings
    assert health.impact_analysis_trusted is False


def test_enabled_but_unavailable_feature_warns_without_lowering_core() -> None:
    health = assess_health(
        healthy_state(
            features={
                "atlas": FeatureHealth("unavailable", ("atlas_unavailable",)),
                "registry": FeatureHealth("disabled"),
                "artifacts": FeatureHealth("disabled"),
            }
        )
    )

    assert health.core_status == "healthy"
    assert "atlas_unavailable" in health.warnings


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"errors": ("source_inspection_failed",)}, "error"),
        ({"graph_exists": False, "graph_valid": False}, "missing"),
        ({"graph_exists": True, "graph_valid": False}, "error"),
        ({"graph_source_digest": "c" * 64}, "stale"),
        ({"graph_projection_digest": "c" * 64}, "stale"),
        ({"coverage_skips": 1}, "partial"),
        ({}, "healthy"),
    ],
)
def test_core_precedence(changes: dict[str, object], status: str) -> None:
    assert assess_health(healthy_state(**changes)).core_status == status


def test_unapproved_coverage_is_error_not_admitted_partial() -> None:
    health = assess_health(healthy_state(coverage_skips=1, unapproved_skips=1))

    assert health.core_status == "error"
    assert "extraction_coverage_unapproved" in health.issues


def test_unapproved_coverage_error_precedes_stale() -> None:
    health = assess_health(
        healthy_state(
            graph_source_digest="c" * 64,
            coverage_skips=1,
            unapproved_skips=1,
        )
    )

    assert health.core_status == "error"


def test_v1_projection_compatibility_is_explicit_not_synthetic() -> None:
    selected = healthy_state(
        manifest_schema_version=1,
        artifact_schema_version=1,
        current_projection_digest=None,
        graph_projection_digest=None,
    )

    assert selected.projection_matches is True
    assert replace(selected, artifact_schema_version=2).projection_matches is False


def test_live_inspection_supplies_both_current_digests_to_owned_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    from tests.support import write_manifest_v2

    write_manifest_v2(repo)
    (repo / "graphify-out").mkdir()
    supplied: dict[str, object] = {}

    def recording_validate(root: Path, value: ProjectManifest, **kwargs: object):
        del root, value
        supplied.update(kwargs)
        return SimpleNamespace(
            artifact_schema_version=2,
            source_digest=kwargs["expected_source_digest"],
            projection_digest=kwargs["expected_projection_digest"],
            graphify_version="0.9.48",
            skipped_count=0,
            unapproved_skips=0,
            impact_trust="navigation",
            impact_limitations=("pre_dedup_edge_projection_unavailable",),
        )

    monkeypatch.setattr(
        "project_knowledge.health.validate_owned_graph", recording_validate
    )

    state = inspect_project_state(repo, manifest_v2())

    access = supplied.pop("repository_access")
    assert isinstance(access, RepositoryAccess)
    assert supplied == {
        "expected_source_digest": state.current_source_digest,
        "expected_projection_digest": state.current_projection_digest,
    }
    assert state.source_matches and state.projection_matches


def test_direct_health_inspection_is_noncreating_and_tree_read_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    from tests.support import write_manifest_v2

    write_manifest_v2(repo)
    before = snapshot_tree(repo)

    selected = inspect_project_state(repo, manifest_v2())

    assert selected.graph_exists is False
    assert assess_health(selected).core_status == "missing"
    assert snapshot_tree(repo) == before
    assert not (repo / ".project-knowledge").exists()


def manifest() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=("generated/**",),
        track_html=True,
        graphify_version="0.9.48",
    )


def _write_v1_manifest(repo: Path) -> None:
    write_v1_manifest_file(
        repo,
        project_id="demo",
        display_name="Demo",
        include_roots=["src"],
        obsidian_namespace="Projects/demo/Generated",
    )


def state(
    *,
    graph_exists: bool = True,
    graph_valid: bool = True,
    source_matches: bool = True,
    atlas_available: bool = True,
    registry_matches: bool = True,
    impact_analysis_trusted: bool = True,
    errors: tuple[str, ...] = (),
) -> KnowledgeState:
    current = "a" * 64
    recorded = current if source_matches else "b" * 64
    return KnowledgeState(
        project_id="demo",
        manifest_schema_version=2,
        artifact_schema_version=2,
        graph_exists=graph_exists,
        graph_valid=graph_valid,
        graph_version="0.9.48" if graph_exists else None,
        current_source_digest=current,
        graph_source_digest=recorded,
        current_projection_digest="c" * 64,
        graph_projection_digest="c" * 64,
        features={
            "atlas": FeatureHealth(
                "available" if atlas_available else "unavailable",
                () if atlas_available else ("atlas_unavailable",),
            ),
            "registry": FeatureHealth(
                "available" if registry_matches else "unavailable",
                () if registry_matches else ("registry_mismatch",),
            ),
            "artifacts": FeatureHealth("disabled"),
        },
        impact_trust="trusted" if impact_analysis_trusted else "navigation",
        errors=errors,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (state(errors=("inspection_failed",)), "error"),
        (state(graph_exists=False, graph_valid=False), "missing"),
        (state(graph_valid=False), "error"),
        (state(source_matches=False), "stale"),
        (state(atlas_available=False), "healthy"),
        (state(registry_matches=False), "healthy"),
        (state(), "healthy"),
    ],
)
def test_health_status_uses_documented_precedence(
    value: KnowledgeState, expected: str
) -> None:
    """Changing branch order would hide the most actionable health failure."""
    assert assess_health(value).status == expected


def test_health_issues_are_stable_codes_not_paths_or_raw_diagnostics() -> None:
    """Returning state inputs directly could leak private paths or stderr."""
    health = assess_health(
        state(
            source_matches=False,
            atlas_available=False,
            registry_matches=False,
        )
    )

    assert health.issues == ("source_digest_mismatch",)
    assert health.warnings == ("atlas_unavailable", "registry_mismatch")
    assert health.source_matches is False
    assert health.to_dict() == {
        "schema_version": 2,
        "core_status": "stale",
        "status": "stale",
        "project_id": "demo",
        "graph_version": "0.9.48",
        "source_matches": False,
        "projection_matches": True,
        "features": {
            "artifacts": {"status": "disabled", "issues": []},
            "atlas": {"status": "unavailable", "issues": ["atlas_unavailable"]},
            "registry": {"status": "unavailable", "issues": ["registry_mismatch"]},
        },
        "trust": {"impact": "trusted", "limitations": []},
        "issues": ["source_digest_mismatch"],
        "warnings": ["atlas_unavailable", "registry_mismatch"],
        "atlas_available": False,
        "registry_matches": False,
        "impact_analysis_trusted": True,
    }


def test_health_marks_untrusted_impact_analysis_as_navigation_warning() -> None:
    health = assess_health(state(impact_analysis_trusted=False))

    assert health.status == "healthy"
    assert health.impact_analysis_trusted is False
    assert health.issues == ()
    assert health.warnings == ("impact_evidence_incomplete",)


def test_missing_graph_never_defaults_to_trusted_impact_analysis(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    _write_v1_manifest(repo)

    inspected = inspect_project_state(repo, manifest())
    health = assess_health(inspected)

    assert inspected.graph_exists is False
    assert health.impact_analysis_trusted is False


def test_health_normalizes_unknown_error_details_to_a_stable_code() -> None:
    """A caller-provided inspection detail must not become a path leak."""
    health = assess_health(
        state(errors=("failed reading /Users/alice/Private Vault/api-token.txt",))
    )

    assert health.status == "error"
    assert health.issues == ("inspection_failed",)


def test_safe_input_snapshot_recomputes_digest_without_creating_files(
    tmp_path: Path,
) -> None:
    """Using the staging writer for health would mutate a read-only check."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("print('ok')\n", encoding="utf-8")
    before = snapshot_tree(tmp_path)

    first = safe_input_snapshot(repo, manifest())
    second = safe_input_snapshot(repo, manifest())

    assert first == second
    assert first.files == (PurePosixPath("src/app.py"),)
    assert len(first.source_digest) == 64
    assert snapshot_tree(tmp_path) == before


def test_safe_input_snapshot_uses_v2_projection_source_digest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    selected = manifest_v2()

    projection = inspect_projection(repo, selected)
    snapshot = safe_input_snapshot(repo, selected)

    assert snapshot.source_digest == projection.source_digest
    assert snapshot.files == tuple(item.path for item in projection.files)


def test_safe_input_snapshot_reads_each_source_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freshness must combine scanning and hashing instead of doubling source I/O."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("print('ok')\n", encoding="utf-8")

    import project_knowledge.health as health_module
    import project_knowledge.secrets_scan as secrets_module

    health_read = health_module._read_stable_regular
    scanner_read = secrets_module._read_regular
    reads = 0

    def count_health_read(root_descriptor: int, relative: PurePosixPath) -> bytes:
        nonlocal reads
        reads += 1
        return health_read(root_descriptor, relative)

    def count_scanner_read(root_descriptor: int, relative: PurePosixPath) -> bytes:
        nonlocal reads
        reads += 1
        return scanner_read(root_descriptor, relative)

    monkeypatch.setattr(health_module, "_read_stable_regular", count_health_read)
    monkeypatch.setattr(secrets_module, "_read_regular", count_scanner_read)

    safe_input_snapshot(repo, manifest())

    assert reads == 1


def test_safe_input_snapshot_honors_reviewed_contextual_secret_exception(
    tmp_path: Path,
) -> None:
    from project_knowledge.secrets_scan import scan_payload

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    payload = 'api_key = "correct-horse-battery-staple"\n'
    (repo / "src/settings.ts").write_text(payload, encoding="utf-8")
    finding = next(
        item
        for item in scan_payload(PurePosixPath("src/settings.ts"), payload.encode())
        if item.detector == "generic_secret_assignment"
    )
    (repo / ".graphify-secret-exceptions.yaml").write_text(
        "schema_version: 1\n"
        "exceptions:\n"
        "  - path: src/settings.ts\n"
        "    detector: generic_secret_assignment\n"
        f"    fingerprint: {finding.fingerprint}\n"
        "    reason: reviewed public fixture\n",
        encoding="utf-8",
    )

    snapshot = safe_input_snapshot(repo, manifest())

    assert snapshot.files == (PurePosixPath("src/settings.ts"),)


def test_safe_input_snapshot_rejects_a_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path-based health digest must not read outside the opted-in repository."""
    repo = tmp_path / "repo"
    source = repo / "src"
    source.mkdir(parents=True)
    (source / "app.py").write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("private\n", encoding="utf-8")

    import project_knowledge.health as health_module

    original = health_module._open_directory_at
    swapped = False

    def swap(parent_descriptor: int, name: str) -> int:
        nonlocal swapped
        if name == "src" and not swapped:
            swapped = True
            source.rename(repo / "original-src")
            os.symlink(outside, source, target_is_directory=True)
        return original(parent_descriptor, name)

    monkeypatch.setattr(health_module, "_open_directory_at", swap)

    with pytest.raises(OSError):
        safe_input_snapshot(repo, manifest())
    assert swapped


def test_inspection_treats_source_digest_mismatch_as_authoritative(
    tmp_path: Path,
) -> None:
    """A fresh timestamp must never override source drift."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    output = repo / "graphify-out"
    output.mkdir()
    graph = json.dumps({"project_id": "demo", "graphify_version": "0.9.48", "source_digest": "0" * 64, "nodes": [{"id": "app", "source_file": "src/app.py"}], "edges": [], "graph_health": analyze_graph([{"id": "app", "source_file": "src/app.py"}], [], semantics=production_graphify_compatibility().semantics).to_dict(), "extraction_coverage": {"schema_version": 1, "total_staged_files": 1, "represented_source_paths": ["src/app.py"], "skipped": []}}).encode() + b"\n"
    (output / "graph.json").write_bytes(graph)
    report = b"# report\n"
    html = b"<title>demo</title>\n"
    ownership = {
        "schema_version": 1,
        "project_id": "demo",
        "graphify_version": "0.9.48",
        "source_digest": "0" * 64,
        "graph_digest": hashlib.sha256(graph).hexdigest(),
        "artifact_digests": {
            "GRAPH_REPORT.md": hashlib.sha256(report).hexdigest(),
            "graph.html": hashlib.sha256(html).hexdigest(),
            "graph.json": hashlib.sha256(graph).hexdigest(),
        },
        "generated_at": "2999-01-01T00:00:00+00:00",
        "files": [
            ".project-knowledge-ownership.json",
            "GRAPH_REPORT.md",
            "graph.html",
            "graph.json",
        ],
        "skipped_count": 0,
        "unapproved_skips": 0,
        "impact_analysis_trusted": False,
    }
    (output / ".project-knowledge-ownership.json").write_text(
        json.dumps(ownership), encoding="utf-8"
    )
    (output / "GRAPH_REPORT.md").write_bytes(report)
    (output / "graph.html").write_bytes(html)
    _write_v1_manifest(repo)

    inspected = inspect_project_state(
        repo,
        manifest(),
        atlas_available=True,
        registry_matches=True,
    )

    assert inspected.graph_valid is True
    assert assess_health(inspected).status == "stale"
    assert assess_health(inspected).source_matches is False


def test_inspection_rejects_unowned_graphify_query_cache(tmp_path: Path) -> None:
    """Only ownership-bound artifact bytes can enter live health."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    query_cache = repo / "graphify-out/cache"
    query_cache.mkdir()
    (query_cache / "last_query_stamp").write_text("query stamp\n", encoding="utf-8")

    inspected = inspect_project_state(
        repo,
        manifest(),
        atlas_available=True,
        registry_matches=True,
    )

    assert inspected.graph_valid is False
    assert assess_health(inspected).status == "error"
    assert assess_health(inspected).issues == ("graph_invalid",)


def test_inspection_rejects_tampered_owned_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    (repo / "graphify-out/GRAPH_REPORT.md").write_text("tampered\n", encoding="utf-8")

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False


def test_inspection_recomputes_graph_integrity_instead_of_trusting_ownership(
    tmp_path: Path,
) -> None:
    """Consistent file digests must not let forged impact trust pass health."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    output = repo / "graphify-out"

    graph_path = output / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["graph_health"]["collapsed_edges"] = 0
    graph["graph_health"]["structurally_valid"] = False
    graph_payload = json.dumps(graph).encode() + b"\n"
    graph_path.write_bytes(graph_payload)

    ownership_path = output / ".project-knowledge-ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    graph_digest = hashlib.sha256(graph_payload).hexdigest()
    ownership["graph_digest"] = graph_digest
    ownership["artifact_digests"]["graph.json"] = graph_digest
    ownership["impact_analysis_trusted"] = True
    ownership_path.write_text(json.dumps(ownership), encoding="utf-8")

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False
    assert assess_health(inspected).issues == ("graph_invalid",)


@pytest.mark.parametrize(
    "field,value",
    [
        ("edge_count", False),
        ("edge_count", 0.0),
        ("structurally_valid", 1),
    ],
)
def test_inspection_rejects_coercible_integrity_metadata_scalar_types(
    tmp_path: Path, field: str, value: object
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    output = repo / "graphify-out"

    graph_path = output / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["graph_health"][field] = value
    graph_payload = json.dumps(graph).encode() + b"\n"
    graph_path.write_bytes(graph_payload)

    ownership_path = output / ".project-knowledge-ownership.json"
    ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
    graph_digest = hashlib.sha256(graph_payload).hexdigest()
    ownership["graph_digest"] = graph_digest
    ownership["artifact_digests"]["graph.json"] = graph_digest
    ownership_path.write_text(json.dumps(ownership), encoding="utf-8")

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False
    assert assess_health(inspected).issues == ("graph_invalid",)


@pytest.mark.parametrize("cache_entry", ["unexpected", "nested"])
def test_inspection_rejects_unknown_graphify_cache_entries(
    tmp_path: Path, cache_entry: str
) -> None:
    """Runtime-cache compatibility must not permit unowned files or directories."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    query_cache = repo / "graphify-out/cache"
    query_cache.mkdir()
    target = query_cache / cache_entry
    if cache_entry == "nested":
        target.mkdir()
        (target / "unexpected").write_text("not owned\n", encoding="utf-8")
    else:
        target.write_text("not owned\n", encoding="utf-8")

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False
    assert assess_health(inspected).issues == ("graph_invalid",)


def test_inspection_rejects_symlinked_graphify_query_stamp(tmp_path: Path) -> None:
    """The permitted query stamp must not become a path outside generated output."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    query_cache = repo / "graphify-out/cache"
    query_cache.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private\n", encoding="utf-8")
    os.symlink(outside, query_cache / "last_query_stamp")

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False
    assert assess_health(inspected).issues == ("graph_invalid",)


def test_inspection_rejects_malformed_or_nonfinite_ownership_json(
    tmp_path: Path,
) -> None:
    """Permissive JSON could mark a corrupt generated graph as usable."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    output = repo / "graphify-out"
    output.mkdir()
    (output / "graph.json").write_text("{}", encoding="utf-8")
    (output / ".project-knowledge-ownership.json").write_text(
        '{"schema_version": NaN}', encoding="utf-8"
    )
    _write_v1_manifest(repo)

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_exists is True
    assert inspected.graph_valid is False
    assert assess_health(inspected).status == "error"
    assert assess_health(inspected).issues == ("graph_invalid",)


def test_inspection_rejects_duplicate_ownership_json_keys(tmp_path: Path) -> None:
    """Ambiguous on-disk metadata must not select trust state by parser order."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("safe\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    ownership_path = repo / "graphify-out/.project-knowledge-ownership.json"
    payload = ownership_path.read_text(encoding="utf-8")
    ownership_path.write_text(
        payload.replace("{", '{"schema_version":1,', 1),
        encoding="utf-8",
    )

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False
    assert assess_health(inspected).issues == ("graph_invalid",)


def snapshot_tree(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "link", os.readlink(path).encode()))
        elif path.is_dir():
            entries.append((relative, "dir", b""))
        else:
            entries.append((relative, "file", path.read_bytes()))
    return tuple(entries)


def _write_valid_graph_output(repo: Path) -> None:
    _write_v1_manifest(repo)
    output = repo / "graphify-out"
    output.mkdir()
    source_digest = safe_input_snapshot(repo, manifest()).source_digest
    graph = json.dumps({"project_id": "demo", "graphify_version": "0.9.48", "source_digest": source_digest, "nodes": [{"id": "app", "source_file": "src/app.py"}], "edges": [], "graph_health": analyze_graph([{"id": "app", "source_file": "src/app.py"}], [], semantics=production_graphify_compatibility().semantics).to_dict(), "extraction_coverage": {"schema_version": 1, "total_staged_files": 1, "represented_source_paths": ["src/app.py"], "skipped": []}}).encode() + b"\n"
    (output / "graph.json").write_bytes(graph)
    report = b"# report\n"
    html = b"<title>demo</title>\n"
    ownership = {
        "schema_version": 1,
        "project_id": "demo",
        "graphify_version": "0.9.48",
        "source_digest": source_digest,
        "graph_digest": hashlib.sha256(graph).hexdigest(),
        "artifact_digests": {
            "GRAPH_REPORT.md": hashlib.sha256(report).hexdigest(),
            "graph.html": hashlib.sha256(html).hexdigest(),
            "graph.json": hashlib.sha256(graph).hexdigest(),
        },
        "generated_at": "2999-01-01T00:00:00+00:00",
        "files": [
            ".project-knowledge-ownership.json",
            "GRAPH_REPORT.md",
            "graph.html",
            "graph.json",
        ],
        "skipped_count": 0,
        "unapproved_skips": 0,
        "impact_analysis_trusted": False,
    }
    (output / ".project-knowledge-ownership.json").write_text(
        json.dumps(ownership), encoding="utf-8"
    )
    (output / "GRAPH_REPORT.md").write_bytes(report)
    (output / "graph.html").write_bytes(html)
