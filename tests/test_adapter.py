from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import os

import pytest

from project_knowledge.adapter import AdapterError, adapt_candidate
from project_knowledge.artifacts import validate_candidate
from project_knowledge.models import ProjectManifest
from project_knowledge.staging import StagedInput


def manifest(*, track_html: bool = False) -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=(),
        track_html=track_html,
        graphify_version="0.9.48",
    )


def staged(tmp_path: Path) -> StagedInput:
    root = tmp_path / "stage"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text("def run(): pass\n", encoding="utf-8")
    (root / "src/unrepresented.py").write_text("VALUE = 1\n", encoding="utf-8")
    return StagedInput(
        root=root,
        source_digest="1" * 64,
        files=(
            PurePosixPath("src/app.py"),
            PurePosixPath("src/unrepresented.py"),
        ),
    )


def raw_candidate(root: Path, **graph_updates: object) -> Path:
    root.mkdir(parents=True)
    graph: dict[str, object] = {
        "directed": False,
        "multigraph": False,
        "graph": {"hyperedges": []},
        "nodes": [
            {
                "id": "function:run",
                "label": "run",
                "source_file": "src/app.py",
                "provenance": "EXTRACTED",
            }
        ],
        "links": [],
    }
    graph.update(graph_updates)
    (root / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (root / "GRAPH_REPORT.md").write_text("# Graph Report\n", encoding="utf-8")
    (root / "graph.html").write_text("<!doctype html>\n", encoding="utf-8")
    (root / ".graphify_python").write_text("/private/local/python\n", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache/ignored").write_text("runtime\n", encoding="utf-8")
    return root


def test_adapter_enriches_native_graph_without_mutating_raw_candidate(
    tmp_path: Path,
) -> None:
    source = raw_candidate(tmp_path / "raw")
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    stage = staged(tmp_path)
    destination = tmp_path / "adapted"

    result = adapt_candidate(source, destination, stage, manifest())

    assert result.root == destination.resolve()
    assert result.source_digest == stage.source_digest
    assert result.node_count == 1
    assert result.edge_count == 0
    assert result.skipped_count == 1
    assert {path.name for path in destination.iterdir()} == {
        "graph.json",
        "GRAPH_REPORT.md",
    }
    document = json.loads((destination / "graph.json").read_text(encoding="utf-8"))
    assert document["project_id"] == "demo"
    assert document["graphify_version"] == "0.9.48"
    assert document["source_digest"] == stage.source_digest
    assert document["graph"] == {"hyperedges": []}
    assert document["links"] == []
    assert document["extraction_coverage"] == {
        "schema_version": 1,
        "total_staged_files": 2,
        "represented_source_paths": ["src/app.py"],
        "skipped": [
            {
                "path": "src/unrepresented.py",
                "reason": "not represented by Graphify 0.9.48",
                "approved": False,
            }
        ],
    }
    assert document["graph_health"] == {
        "schema_version": 1,
        "dangling_edges": 0,
        "missing_endpoints": 0,
        "self_loops": 0,
        "duplicate_edges": 0,
        "collapsed_edges": None,
        "impact_analysis_trusted": False,
    }
    assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before
    validated = validate_candidate(destination, stage, manifest())
    assert validated.node_count == 1
    assert validated.unapproved_skips == 1


def test_adapter_includes_html_only_when_manifest_tracks_it(tmp_path: Path) -> None:
    source = raw_candidate(tmp_path / "raw")

    adapt_candidate(source, tmp_path / "adapted", staged(tmp_path), manifest(track_html=True))

    assert (tmp_path / "adapted/graph.html").is_file()


def test_adapter_removes_graphify_empty_source_path_sentinels(tmp_path: Path) -> None:
    source = raw_candidate(
        tmp_path / "raw",
        nodes=[
            {
                "id": "function:run",
                "source_file": "src/app.py",
                "provenance": "EXTRACTED",
            },
            {
                "id": "external:any",
                "source_file": "",
                "source_location": "",
                "provenance": "EXTRACTED",
            },
        ],
    )
    stage = staged(tmp_path)
    destination = tmp_path / "adapted"

    adapt_candidate(source, destination, stage, manifest())

    document = json.loads((destination / "graph.json").read_text(encoding="utf-8"))
    external = next(node for node in document["nodes"] if node["id"] == "external:any")
    assert "source_file" not in external
    validate_candidate(destination, stage, manifest())


def test_adapter_canonicalizes_graphify_node_source_aliases(tmp_path: Path) -> None:
    stage = staged(tmp_path)
    module_path = PurePosixPath("src/project_knowledge/__init__.py")
    module = stage.root / module_path
    module.parent.mkdir(parents=True)
    module.write_text("\n", encoding="utf-8")
    stage = StagedInput(
        root=stage.root,
        source_digest=stage.source_digest,
        files=(*stage.files, module_path),
    )
    source = raw_candidate(
        tmp_path / "raw",
        nodes=[
            {
                "id": "module:project_knowledge",
                "label": "project_knowledge/__init__.py",
                "norm_label": "project_knowledge/__init__.py",
                "source_file": module_path.as_posix(),
                "provenance": "EXTRACTED",
            }
        ],
    )
    destination = tmp_path / "adapted"

    adapt_candidate(source, destination, stage, manifest())

    document = json.loads((destination / "graph.json").read_text(encoding="utf-8"))
    assert document["nodes"][0]["label"] == module_path.as_posix()
    assert document["nodes"][0]["norm_label"] == module_path.as_posix()
    validate_candidate(destination, stage, manifest())


@pytest.mark.parametrize("field", ["project_id", "graphify_version", "source_digest", "extraction_coverage"])
def test_adapter_rejects_pre_enriched_identity(tmp_path: Path, field: str) -> None:
    source = raw_candidate(tmp_path / "raw", **{field: "forged"})

    with pytest.raises(AdapterError, match="already contains wrapper metadata"):
        adapt_candidate(source, tmp_path / "adapted", staged(tmp_path), manifest())

    assert not (tmp_path / "adapted").exists()


def test_adapter_rejects_source_paths_outside_staged_snapshot(tmp_path: Path) -> None:
    source = raw_candidate(
        tmp_path / "raw",
        nodes=[
            {
                "id": "x",
                "source_file": "src/private.py",
                "provenance": "EXTRACTED",
            }
        ],
    )

    with pytest.raises(AdapterError, match="source path is not in staged input"):
        adapt_candidate(source, tmp_path / "adapted", staged(tmp_path), manifest())

    assert not (tmp_path / "adapted").exists()


def test_adapter_refuses_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "adapted"
    destination.mkdir()
    marker = destination / "owned.txt"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(AdapterError, match="destination already exists"):
        adapt_candidate(raw_candidate(tmp_path / "raw"), destination, staged(tmp_path), manifest())

    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_adapter_rejects_destination_parent_swapped_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_knowledge import adapter as adapter_module

    source = raw_candidate(tmp_path / "raw")
    parent = tmp_path / "selected-parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = parent / "adapted"
    real_open_parent = adapter_module._open_stable_directory
    swapped = False

    def swap_after_open(path: Path, description: str):
        nonlocal swapped
        result = real_open_parent(path, description)
        if description == "adapted candidate parent" and not swapped:
            swapped = True
            parent.rename(displaced)
            os.symlink(outside, parent, target_is_directory=True)
        return result

    monkeypatch.setattr(adapter_module, "_open_stable_directory", swap_after_open)

    with pytest.raises(AdapterError):
        adapt_candidate(source, destination, staged(tmp_path), manifest())

    assert swapped
    assert not (outside / "adapted").exists()
    assert not (displaced / "adapted").exists()


@pytest.mark.parametrize(
    "links",
    [
        [
            {
                "source": "missing",
                "target": "function:run",
                "type": "calls",
                "confidence": "EXTRACTED",
            }
        ],
        [
            {
                "source": "function:run",
                "type": "calls",
                "confidence": "EXTRACTED",
            }
        ],
    ],
)
def test_adapter_rejects_dangling_or_missing_edge_endpoints(
    tmp_path: Path, links: list[dict[str, object]]
) -> None:
    source = raw_candidate(tmp_path / "raw", links=links)

    with pytest.raises(AdapterError, match="edge endpoints"):
        adapt_candidate(source, tmp_path / "adapted", staged(tmp_path), manifest())

    assert not (tmp_path / "adapted").exists()
