from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path, PurePosixPath

import pytest

from project_knowledge.health import (
    KnowledgeState,
    assess_health,
    inspect_project_state,
    safe_input_snapshot,
)
from project_knowledge.models import ProjectManifest


def manifest() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=(),
        track_html=True,
        graphify_version="0.9.48",
    )


def state(
    *,
    graph_exists: bool = True,
    graph_valid: bool = True,
    source_matches: bool = True,
    atlas_available: bool = True,
    registry_matches: bool = True,
    errors: tuple[str, ...] = (),
) -> KnowledgeState:
    current = "a" * 64
    recorded = current if source_matches else "b" * 64
    return KnowledgeState(
        project_id="demo",
        graph_exists=graph_exists,
        graph_valid=graph_valid,
        graph_version="0.9.48" if graph_exists else None,
        current_source_digest=current,
        graph_source_digest=recorded,
        atlas_available=atlas_available,
        registry_matches=registry_matches,
        errors=errors,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (state(errors=("inspection_failed",)), "error"),
        (state(graph_exists=False, graph_valid=False), "missing"),
        (state(graph_valid=False), "missing"),
        (state(source_matches=False), "stale"),
        (state(atlas_available=False), "partial"),
        (state(registry_matches=False), "partial"),
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

    assert health.issues == (
        "source_digest_mismatch",
        "atlas_unavailable",
        "registry_mismatch",
    )
    assert health.source_matches is False
    assert health.to_dict() == {
        "status": "stale",
        "project_id": "demo",
        "graph_version": "0.9.48",
        "source_matches": False,
        "atlas_available": False,
        "registry_matches": False,
        "issues": [
            "source_digest_mismatch",
            "atlas_unavailable",
            "registry_mismatch",
        ],
    }


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
    graph = json.dumps({"nodes": [{"id": "app", "source_file": "src/app.py"}], "edges": [], "extraction_coverage": {"schema_version": 1, "total_staged_files": 1, "represented_source_paths": ["src/app.py"], "skipped": []}}).encode() + b"\n"
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
    }
    (output / ".project-knowledge-ownership.json").write_text(
        json.dumps(ownership), encoding="utf-8"
    )
    (output / "GRAPH_REPORT.md").write_bytes(report)
    (output / "graph.html").write_bytes(html)

    inspected = inspect_project_state(
        repo,
        manifest(),
        atlas_available=True,
        registry_matches=True,
    )

    assert inspected.graph_valid is True
    assert assess_health(inspected).status == "stale"
    assert assess_health(inspected).source_matches is False


def test_inspection_accepts_official_graphify_query_cache(tmp_path: Path) -> None:
    """A normal Graphify query must not make a valid promoted graph unhealthy."""
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

    assert inspected.graph_valid is True
    assert assess_health(inspected).status == "healthy"


def test_inspection_rejects_tampered_owned_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/app.py").write_text("current\n", encoding="utf-8")
    _write_valid_graph_output(repo)
    (repo / "graphify-out/GRAPH_REPORT.md").write_text("tampered\n", encoding="utf-8")

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_valid is False


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

    inspected = inspect_project_state(repo, manifest())

    assert inspected.graph_exists is True
    assert inspected.graph_valid is False
    assert assess_health(inspected).status == "missing"
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
    output = repo / "graphify-out"
    output.mkdir()
    graph = json.dumps({"nodes": [{"id": "app", "source_file": "src/app.py"}], "edges": [], "extraction_coverage": {"schema_version": 1, "total_staged_files": 1, "represented_source_paths": ["src/app.py"], "skipped": []}}).encode() + b"\n"
    (output / "graph.json").write_bytes(graph)
    report = b"# report\n"
    html = b"<title>demo</title>\n"
    ownership = {
        "schema_version": 1,
        "project_id": "demo",
        "graphify_version": "0.9.48",
        "source_digest": safe_input_snapshot(repo, manifest()).source_digest,
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
    }
    (output / ".project-knowledge-ownership.json").write_text(
        json.dumps(ownership), encoding="utf-8"
    )
    (output / "GRAPH_REPORT.md").write_bytes(report)
    (output / "graph.html").write_bytes(html)
