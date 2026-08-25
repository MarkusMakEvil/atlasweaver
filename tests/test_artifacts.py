from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import shutil
from time import perf_counter

import pytest

from project_knowledge.artifacts import (
    OWNERSHIP_MANIFEST,
    ArtifactValidationError,
    FileSystem,
    ValidatedGraph,
    promote_graph,
    validate_candidate,
)
from project_knowledge.integrity import analyze_graph
from project_knowledge.models import ProjectManifest
from project_knowledge.staging import StagedInput


@pytest.fixture
def manifest() -> ProjectManifest:
    return ProjectManifest(
        schema_version=1,
        project_id="demo",
        display_name="Demo",
        include_roots=(PurePosixPath("src"),),
        output_dir=PurePosixPath("graphify-out"),
        obsidian_namespace=PurePosixPath("Projects/demo/Generated"),
        excludes=("src/generated/**",),
        track_html=True,
        graphify_version="0.9.48",
    )


@pytest.fixture
def staged(tmp_path: Path) -> StagedInput:
    root = tmp_path / "staged-input"
    write(root / "src/app.py", "def run(): pass\n")
    write(root / "src/lib.py", "def helper(): pass\n")
    return StagedInput(
        root=root,
        source_digest="1" * 64,
        files=(PurePosixPath("src/app.py"), PurePosixPath("src/lib.py")),
    )


@pytest.fixture
def candidate(tmp_path: Path, staged: StagedInput, manifest: ProjectManifest) -> Path:
    root = tmp_path / "candidate"
    write_valid_candidate(root, staged, manifest)
    return root


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_valid_candidate(
    root: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    nodes: list[dict[str, object]] | None = None,
    edges: list[dict[str, object]] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_graph(
        root,
        staged,
        manifest,
        nodes=nodes,
        edges=edges,
    )
    write(root / "GRAPH_REPORT.md", "# Graph Report\n\nSources: `src/app.py` and `src/lib.py`.\n")
    if manifest.track_html:
        write(root / "graph.html", "<!doctype html><title>Demo graph</title>\n")


def write_graph(
    root: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    *,
    nodes: list[dict[str, object]] | None = None,
    edges: list[dict[str, object]] | None = None,
    **metadata: object,
) -> None:
    resolved_nodes = nodes if nodes is not None else [
        {"id": "app", "source_file": "src/app.py"},
        {"id": "lib", "source_file": "src/lib.py"},
    ]
    resolved_edges = edges if edges is not None else [
        {"source": "app", "target": "lib", "relation": "calls", "provenance": "EXTRACTED", "source_file": "src/app.py"}
    ]
    represented = sorted({str(value) for item in [*resolved_nodes, *resolved_edges] if isinstance(item, dict) for key, value in item.items() if key in {"source_file", "source_path", "path", "file"} and isinstance(value, str)})
    skipped = [{"path": path.as_posix(), "reason": "fixture not represented", "approved": True} for path in staged.files if path.as_posix() not in represented]
    try:
        graph_health = analyze_graph(resolved_nodes, resolved_edges).to_dict()
    except (AttributeError, TypeError, ValueError):
        graph_health = analyze_graph([{"id": "fixture"}], []).to_dict()
    document: dict[str, object] = {
        "project_id": manifest.project_id,
        "graphify_version": manifest.graphify_version,
        "source_digest": staged.source_digest,
        "nodes": resolved_nodes,
        "edges": resolved_edges,
        "graph_health": graph_health,
        "extraction_coverage": {"schema_version": 1, "total_staged_files": len(staged.files), "represented_source_paths": represented, "skipped": skipped},
    }
    document.update(metadata)
    write(root / "graph.json", json.dumps(document, indent=2) + "\n")


def test_candidate_requires_complete_staged_extraction_coverage(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    document = json.loads((candidate / "graph.json").read_text())
    del document["extraction_coverage"]
    write(candidate / "graph.json", json.dumps(document))
    with pytest.raises(ArtifactValidationError, match="coverage"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_requires_recomputed_graph_health(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    document = json.loads((candidate / "graph.json").read_text())
    del document["graph_health"]
    write(candidate / "graph.json", json.dumps(document))

    with pytest.raises(ArtifactValidationError, match="graph health"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_forged_graph_health_counter(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    document = json.loads((candidate / "graph.json").read_text())
    document["graph_health"]["impact_analysis_trusted"] = True
    write(candidate / "graph.json", json.dumps(document))

    with pytest.raises(ArtifactValidationError, match="graph health"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_claimed_collapsed_edge_evidence_for_graphify_0948(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    """The pinned extractor cannot prove a zero pre-build collapse count."""
    document = json.loads((candidate / "graph.json").read_text())
    document["graph_health"]["collapsed_edges"] = 0
    document["graph_health"]["impact_analysis_trusted"] = True
    write(candidate / "graph.json", json.dumps(document))

    with pytest.raises(ArtifactValidationError, match="collapsed edge evidence"):
        validate_candidate(candidate, staged, manifest)


def load_ownership(root: Path) -> dict[str, object]:
    return json.loads((root / OWNERSHIP_MANIFEST).read_text(encoding="utf-8"))


def test_valid_candidate_is_bound_to_project_version_and_staged_source(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    validated = validate_candidate(candidate, staged, manifest)

    assert validated.root == candidate.resolve()
    assert validated.source_digest == staged.source_digest
    assert validated.node_count == 2
    assert validated.edge_count == 1
    assert len(validated.graph_digest) == 64

    ownership = load_ownership(candidate)
    assert ownership["schema_version"] == 1
    assert ownership["project_id"] == "demo"
    assert ownership["graphify_version"] == "0.9.48"
    assert ownership["source_digest"] == staged.source_digest
    assert ownership["impact_analysis_trusted"] is False
    assert ownership["graph_digest"] == validated.graph_digest
    assert set(ownership["artifact_digests"]) == {
        "GRAPH_REPORT.md",
        "graph.html",
        "graph.json",
    }
    assert ownership["files"] == [
        OWNERSHIP_MANIFEST,
        "GRAPH_REPORT.md",
        "graph.html",
        "graph.json",
    ]
    generated = datetime.fromisoformat(str(ownership["generated_at"]))
    assert generated.tzinfo is not None


def test_ownership_manifest_survives_partial_os_writes(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_knowledge import artifacts

    real_write = artifacts.os.write

    def partial_write(descriptor: int, payload: bytes) -> int:
        return real_write(descriptor, payload[:7])

    monkeypatch.setattr(artifacts.os, "write", partial_write)

    validated = validate_candidate(candidate, staged, manifest)

    assert load_ownership(candidate)["graph_digest"] == validated.graph_digest


@pytest.mark.parametrize("missing", ["graph.json", "GRAPH_REPORT.md", "graph.html"])
def test_candidate_requires_policy_artifacts(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    missing: str,
) -> None:
    (candidate / missing).unlink()

    with pytest.raises(ArtifactValidationError, match="required artifact"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_html_when_policy_disables_it(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    manifest = replace(manifest, track_html=False)

    with pytest.raises(ArtifactValidationError, match="HTML policy"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_accepts_missing_html_when_policy_disables_it(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    manifest = replace(manifest, track_html=False)
    (candidate / "graph.html").unlink()

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2
    assert load_ownership(candidate)["files"] == [
        OWNERSHIP_MANIFEST,
        "GRAPH_REPORT.md",
        "graph.json",
    ]


def test_candidate_rejects_malformed_json(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(candidate / "graph.json", '{"nodes": [}')

    with pytest.raises(ArtifactValidationError, match="malformed JSON"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_nonfinite_json_numbers(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    raw = (candidate / "graph.json").read_text(encoding="utf-8")
    write(candidate / "graph.json", raw.replace('"nodes":', '"score": NaN,\n  "nodes":', 1))

    with pytest.raises(ArtifactValidationError, match="malformed JSON"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_overflowing_json_float(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    raw = (candidate / "graph.json").read_text(encoding="utf-8")
    write(candidate / "graph.json", raw.replace('"nodes":', '"score": 1e400,\n  "nodes":', 1))

    with pytest.raises(ArtifactValidationError, match="non-finite"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_duplicate_json_keys(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    raw = (candidate / "graph.json").read_text(encoding="utf-8")
    write(candidate / "graph.json", raw.replace('"project_id":', '"project_id": "demo",\n  "project_id":', 1))

    with pytest.raises(ArtifactValidationError, match="duplicate JSON key"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize("artifact", ["graph.json", "GRAPH_REPORT.md", "graph.html"])
def test_candidate_rejects_conflict_markers(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    artifact: str,
) -> None:
    path = candidate / artifact
    write(path, path.read_text(encoding="utf-8") + "\n<<<<<<< ours\n=======\n>>>>>>> theirs\n")

    with pytest.raises(ArtifactValidationError, match="conflict marker"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize("source", ["/tmp/app.py", "../src/app.py", "src/../../outside.py", "C:\\secret\\app.py"])
def test_candidate_rejects_absolute_or_escaping_source_paths(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    source: str,
) -> None:
    write_graph(candidate, staged, manifest, nodes=[{"id": "x", "source": source}], edges=[])

    with pytest.raises(ArtifactValidationError, match="confined relative source path"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_source_outside_staged_snapshot(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write_graph(candidate, staged, manifest, nodes=[{"id": "x", "source": "src/new.py"}], edges=[])

    with pytest.raises(ArtifactValidationError, match="not present in staged input"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_excluded_source(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write_graph(candidate, staged, manifest, nodes=[{"id": "x", "source": ".env"}], edges=[])

    with pytest.raises(ArtifactValidationError, match="excluded source path"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "leak",
    [
        ".env",
        "workspace/private.md",
        "src/auth-token.yaml",
        "/Users/alice/private/repo.py",
        "/opt/company/private/repo.py",
    ],
)
def test_candidate_rejects_excluded_or_absolute_text_leaks(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    leak: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"# Graph Report\n\nUnexpected source: `{leak}`\n")

    with pytest.raises(ArtifactValidationError, match="artifact path leak"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    ("artifact", "content"),
    [
        ("graph.html", '<a href="/opt/company/private/repo.py">source</a>'),
        ("GRAPH_REPORT.md", "Generated from path=/opt/company/private/repo.py"),
        ("GRAPH_REPORT.md", "Generated from `src/not-staged.py`"),
    ],
)
def test_candidate_rejects_path_leaks_in_attributes_assignments_and_unstaged_refs(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    artifact: str,
    content: str,
) -> None:
    write(candidate / artifact, content)

    with pytest.raises(ArtifactValidationError, match="artifact path leak"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_path_scan_allows_prose_and_web_links(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(
        candidate / "GRAPH_REPORT.md",
        "# Graph Report\n\nArchitecture/runtime trade-offs remain documented.\n",
    )
    write(
        candidate / "graph.html",
        '<a href="https://example.com/docs">Documentation</a>',
    )

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize(
    "reference",
    [
        "/usr/local/bin",
        "src/Makefile",
    ],
)
def test_candidate_rejects_extensionless_filesystem_references(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"Generated from `{reference}`\n")

    with pytest.raises(ArtifactValidationError, match="artifact path leak"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "reference",
    [
        "#node",
        "mailto:maintainer@example.com",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "https://example.com/docs",
    ],
)
def test_candidate_allows_safe_non_file_html_references(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "graph.html", f'<a href="{reference}">reference</a>')

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


def test_candidate_rejects_javascript_html_reference(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(candidate / "graph.html", '<a href="javascript:alert(1)">unsafe</a>')

    with pytest.raises(ArtifactValidationError, match="unsafe URI scheme"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_rejects_javascript_markdown_reference(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(candidate / "GRAPH_REPORT.md", "[open](javascript:alert(1))\n")

    with pytest.raises(ArtifactValidationError, match="unsafe URI scheme"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "markdown",
    [
        '[source](<src/app.py#L1> "source title")',
        "[source](src/app.py?raw=1 'source title')",
        "[source](src/app.py#L1 (source title))",
    ],
)
def test_candidate_accepts_inline_destinations_with_optional_titles(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    markdown: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", markdown + "\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize("markdown", ["[empty]()", "[empty](  )", "[empty](<>)"])
def test_candidate_preserves_empty_inline_destinations(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    markdown: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", markdown + "\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize(
    "markdown",
    [
        '[open](<javascript:alert(1)> "unsafe")',
        "<javascript:alert(1)>",
        '[danger]: javascript:alert(1) "unsafe"\n\n[open][danger]',
    ],
)
def test_candidate_rejects_unsafe_schemes_in_every_markdown_reference_context(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    markdown: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", markdown + "\n")

    with pytest.raises(ArtifactValidationError, match="unsafe URI scheme"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize("usage", ["[source][source-id]", "[source-id][]", "[source-id]"])
def test_candidate_accepts_staged_reference_definitions_and_usages(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    usage: str,
) -> None:
    write(
        candidate / "GRAPH_REPORT.md",
        f'[source-id]: <src/app.py?raw=1#L1> "source title"\n\n{usage}\n',
    )

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


def test_candidate_rejects_unstaged_reference_definition(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(
        candidate / "GRAPH_REPORT.md",
        '[source]: src/missing.py#L1 "source title"\n\n[open][source]\n',
    )

    with pytest.raises(ArtifactValidationError, match="unstaged source path"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "reference",
    [
        "<https://example.com/docs>",
        "<mailto:maintainer@example.com>",
        '[safe]: data:image/svg+xml;base64,PHN2Zy8+ "inline image"\n\n[safe]',
    ],
)
def test_candidate_accepts_safe_markdown_autolinks_and_reference_definitions(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", reference + "\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


def test_malformed_markdown_reference_scan_is_bounded(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(candidate / "GRAPH_REPORT.md", "[x](" * 5_000)

    started = perf_counter()
    with pytest.raises(ArtifactValidationError, match="malformed Markdown"):
        validate_candidate(candidate, staged, manifest)
    elapsed = perf_counter() - started

    assert elapsed < 1.0


@pytest.mark.parametrize(
    "reference",
    [
        "#node",
        "mailto:maintainer@example.com",
        "data:image/svg+xml;base64,PHN2Zy8+",
        "https://example.com/docs.html",
    ],
)
def test_candidate_allows_safe_non_file_markdown_references(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"[reference]({reference})\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize(
    "reference",
    [
        "src/app.py#L1",
        "src/app.py?raw=1",
    ],
)
def test_candidate_allows_staged_markdown_reference_suffixes(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"[source]({reference})\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize(
    "reference",
    [
        "src/missing.py#L1",
        "src/Makefile?raw=1",
    ],
)
def test_candidate_rejects_unstaged_markdown_reference_suffixes(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"[source]({reference})\n")

    with pytest.raises(ArtifactValidationError, match="unstaged source path"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_does_not_treat_scheme_like_prose_as_a_reference(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    write(candidate / "GRAPH_REPORT.md", "Decision:runtime remains ordinary prose.\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize(
    "reference",
    [
        "src/app.py#L1",
        "src/app.py?raw=1",
        "https://example.com/docs.html",
    ],
)
def test_candidate_allows_safe_decorated_report_tokens(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"Reference: `{reference}`\n")

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.node_count == 2


@pytest.mark.parametrize(
    "reference",
    [
        "src/missing.py#L1",
        "src/Makefile?raw=1",
    ],
)
def test_candidate_rejects_unstaged_decorated_report_tokens(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    reference: str,
) -> None:
    write(candidate / "GRAPH_REPORT.md", f"Reference: `{reference}`\n")

    with pytest.raises(ArtifactValidationError, match="unstaged source path"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    ("field", "wrong", "message"),
    [
        ("project_id", "other", "project ID mismatch"),
        ("graphify_version", "0.9.47", "Graphify version mismatch"),
        ("source_digest", "2" * 64, "source digest mismatch"),
    ],
)
def test_candidate_rejects_identity_mismatches(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    field: str,
    wrong: str,
    message: str,
) -> None:
    write_graph(candidate, staged, manifest, **{field: wrong})

    with pytest.raises(ArtifactValidationError, match=message):
        validate_candidate(candidate, staged, manifest)


def test_candidate_requires_the_pinned_graphify_release(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    manifest = replace(manifest, graphify_version="0.9.49")
    write_graph(candidate, staged, manifest)

    with pytest.raises(ArtifactValidationError, match="0.9.48"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "nodes",
    [
        {},
        ["not-an-object"],
        [{"source": "src/app.py"}],
        [{"id": "", "source": "src/app.py"}],
        [
            {"id": "duplicate", "source": "src/app.py"},
            {"id": "duplicate", "source": "src/lib.py"},
        ],
    ],
)
def test_candidate_rejects_invalid_nodes(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    nodes: object,
) -> None:
    write_graph(candidate, staged, manifest, nodes=nodes, edges=[])

    with pytest.raises(ArtifactValidationError, match="node"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "edges",
    [
        {},
        ["not-an-object"],
        [{"source": "app", "target": "missing", "relation": "calls", "provenance": "EXTRACTED"}],
        [{"source": "app", "target": "lib", "provenance": "EXTRACTED"}],
        [{"source": "app", "target": "lib", "relation": "calls"}],
    ],
)
def test_candidate_rejects_invalid_edges(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    edges: object,
) -> None:
    write_graph(candidate, staged, manifest, edges=edges)

    with pytest.raises(ArtifactValidationError, match="edge"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize("provenance", ["extracted", "VERIFIED", 1, None])
def test_candidate_rejects_invalid_provenance_enum(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    provenance: object,
) -> None:
    edges = [
        {
            "source": "app",
            "target": "lib",
            "relation": "calls",
            "provenance": provenance,
        }
    ]
    write_graph(candidate, staged, manifest, edges=edges)

    with pytest.raises(ArtifactValidationError, match="provenance"):
        validate_candidate(candidate, staged, manifest)


@pytest.mark.parametrize(
    "edge",
    [
        {
            "source": "app",
            "target": "lib",
            "relation": "calls",
            "type": "imports",
            "provenance": "EXTRACTED",
        },
        {
            "source": "app",
            "target": "lib",
            "relation": "calls",
            "provenance": "EXTRACTED",
            "confidence": "INFERRED",
        },
    ],
)
def test_candidate_rejects_conflicting_edge_aliases(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    edge: dict[str, object],
) -> None:
    write_graph(candidate, staged, manifest, edges=[edge])

    with pytest.raises(ArtifactValidationError, match="conflicting edge"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_accepts_matching_edge_aliases(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    edge = {
        "source": "app",
        "target": "lib",
        "relation": "calls",
        "type": "calls",
        "provenance": "EXTRACTED",
        "confidence": "EXTRACTED",
    }
    write_graph(candidate, staged, manifest, edges=[edge])

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.edge_count == 1


def test_candidate_accepts_native_links_and_confidence_provenance(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    document = {
        "project_id": manifest.project_id,
        "graphify_version": manifest.graphify_version,
        "source_digest": staged.source_digest,
        "nodes": [
            {"id": "app", "source_file": "src/app.py"},
            {"id": "lib", "source_file": "src/lib.py"},
        ],
        "links": [
            {
                "source": "app",
                "target": "lib",
                "relation": "calls",
                "confidence": "INFERRED",
                "confidence_score": 0.75,
                "source_file": "src/app.py",
            }
        ],
        "graph_health": analyze_graph(
            [
                {"id": "app", "source_file": "src/app.py"},
                {"id": "lib", "source_file": "src/lib.py"},
            ],
            [
                {
                    "source": "app",
                    "target": "lib",
                    "relation": "calls",
                    "confidence": "INFERRED",
                    "confidence_score": 0.75,
                    "source_file": "src/app.py",
                }
            ],
        ).to_dict(),
        "extraction_coverage": {
            "schema_version": 1,
            "total_staged_files": 2,
            "represented_source_paths": ["src/app.py", "src/lib.py"],
            "skipped": [],
        },
    }
    write(candidate / "graph.json", json.dumps(document))

    validated = validate_candidate(candidate, staged, manifest)

    assert validated.edge_count == 1


def test_candidate_rejects_symlinked_artifacts(
    candidate: Path, staged: StagedInput, manifest: ProjectManifest, tmp_path: Path
) -> None:
    external = tmp_path / "external-report.md"
    write(external, "# External\n")
    (candidate / "GRAPH_REPORT.md").unlink()
    (candidate / "GRAPH_REPORT.md").symlink_to(external)

    with pytest.raises(ArtifactValidationError, match="regular file"):
        validate_candidate(candidate, staged, manifest)


def test_candidate_mutation_after_inspection_cannot_enter_trusted_snapshot(
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from project_knowledge import artifacts

    original_write = artifacts._write_new_json

    def mutate_before_ownership(path: Path, document: Mapping[str, object]) -> None:
        write(candidate / "GRAPH_REPORT.md", "# Mutated\n\nworkspace/private.md\n")
        original_write(path, document)

    monkeypatch.setattr(artifacts, "_write_new_json", mutate_before_ownership)

    with pytest.raises(ArtifactValidationError, match="changed during validation"):
        validate_candidate(candidate, staged, manifest)

    assert not (candidate / OWNERSHIP_MANIFEST).exists()


class FaultingFS(FileSystem):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.triggered = False

    def checkpoint(self, operation: str) -> None:
        if operation == self.failure and not self.triggered:
            self.triggered = True
            raise OSError(f"injected {operation} failure")


class RecordingFaultingFS(FaultingFS):
    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.fsynced_directories: list[Path] = []

    def fsync_directory(self, path: Path) -> None:
        self.fsynced_directories.append(path)
        super().fsync_directory(path)


def write_committed_graph(repo: Path, marker: str = "old") -> Path:
    target = repo / "graphify-out"
    write(target / "marker", marker)
    return target


def test_promotion_installs_candidate_and_durably_cleans_unique_backup(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    old = write_committed_graph(repo)
    old_inode = old.stat().st_ino
    validated = validate_candidate(candidate, staged, manifest)

    result = promote_graph(validated, repo)

    assert result.target == repo / "graphify-out"
    assert result.backup.parent == repo / ".project-knowledge/rollback"
    assert result.digest == validated.graph_digest
    assert not (result.target / "marker").exists()
    assert json.loads((result.target / "graph.json").read_text(encoding="utf-8"))["source_digest"] == staged.source_digest
    assert not result.backup.exists()
    assert result.target.stat().st_ino != old_inode


def test_identical_owned_graph_promotion_is_idempotent(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = promote_graph(validate_candidate(candidate, staged, manifest), repo)
    installed_inode = first.target.stat().st_ino
    fresh = tmp_path / "fresh"
    write_valid_candidate(fresh, staged, manifest)
    second = promote_graph(validate_candidate(fresh, staged, manifest), repo)
    assert second.changed is False
    assert second.target.stat().st_ino == installed_inode
    assert list((repo / ".project-knowledge/rollback").iterdir()) == []


@pytest.mark.parametrize("failure", ["copy-stage", "fsync-stage", "rename-backup", "fsync-backup", "rename-candidate", "fsync-promote"])
def test_failed_promotion_keeps_previous_graph(
    tmp_path: Path,
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    failure: str,
) -> None:
    repo = tmp_path / "repo"
    write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    faulting_fs = FaultingFS(failure)

    with pytest.raises(OSError, match="injected"):
        promote_graph(validated, repo, fs=faulting_fs)

    assert faulting_fs.triggered
    assert (repo / "graphify-out/marker").read_text(encoding="utf-8") == "old"
    assert candidate.is_dir()


def test_failed_first_promotion_leaves_no_untrusted_graph(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    validated = validate_candidate(candidate, staged, manifest)

    with pytest.raises(OSError, match="fsync-promote"):
        promote_graph(validated, repo, fs=FaultingFS("fsync-promote"))

    assert not (repo / "graphify-out").exists()


def test_first_use_state_directories_are_durable_before_staging(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    fs = RecordingFaultingFS("copy-stage")

    with pytest.raises(OSError, match="copy-stage"):
        promote_graph(validated, repo, fs=fs)

    assert repo.resolve() in fs.fsynced_directories
    assert (repo.resolve() / ".project-knowledge") in fs.fsynced_directories
    assert (repo / "graphify-out/marker").read_text(encoding="utf-8") == "old"


def test_promotion_rejects_candidate_changed_after_validation(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    write(candidate / "GRAPH_REPORT.md", "tampered after validation\n")

    with pytest.raises(ArtifactValidationError, match="changed after validation"):
        promote_graph(validated, repo)

    assert (repo / "graphify-out/marker").read_text(encoding="utf-8") == "old"


def test_promotion_rejects_symlinked_transaction_state_directory(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    write_committed_graph(repo)
    external = tmp_path / "external-state"
    external.mkdir()
    (repo / ".project-knowledge").symlink_to(external, target_is_directory=True)
    validated = validate_candidate(candidate, staged, manifest)

    with pytest.raises(ArtifactValidationError, match="private directory"):
        promote_graph(validated, repo)

    assert (repo / "graphify-out/marker").read_text(encoding="utf-8") == "old"
    assert list(external.iterdir()) == []


def test_journal_temp_symlink_cannot_overwrite_external_file(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    transactions = repo / ".project-knowledge/transactions"
    transactions.mkdir(parents=True)
    external = tmp_path / "external-journal"
    write(external, "do-not-touch")
    predictable_temp = transactions / f"{validated.graph_digest}.tmp"
    predictable_temp.symlink_to(external)

    result = promote_graph(validated, repo)

    assert result.target.is_dir()
    assert external.read_text(encoding="utf-8") == "do-not-touch"
    assert predictable_temp.is_symlink()


def test_promotion_rejects_forged_validated_graph(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    write_committed_graph(repo)
    real = validate_candidate(candidate, staged, manifest)
    forged = ValidatedGraph(
        root=real.root,
        source_digest=real.source_digest,
        graph_digest=real.graph_digest,
        node_count=real.node_count,
        edge_count=real.edge_count,
    )

    with pytest.raises(ArtifactValidationError, match="current process"):
        promote_graph(forged, repo)

    assert (repo / "graphify-out/marker").read_text(encoding="utf-8") == "old"


def test_interrupted_backup_is_recovered_before_a_new_attempt(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    target = write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    backup = repo / ".project-knowledge/rollback" / validated.graph_digest
    backup.parent.mkdir(parents=True)
    target.rename(backup)
    transaction = repo / ".project-knowledge/transactions" / f"{validated.graph_digest}.json"
    write(transaction, json.dumps({"schema_version": 1, "state": "backed_up", "graph_digest": validated.graph_digest}))

    with pytest.raises(OSError, match="copy-stage"):
        promote_graph(validated, repo, fs=FaultingFS("copy-stage"))

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not transaction.exists()


def test_random_id_transaction_is_recovered_before_a_new_attempt(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    target = write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    transaction_id = "a" * 32
    backup = repo / ".project-knowledge/rollback" / transaction_id
    backup.parent.mkdir(parents=True)
    target.rename(backup)
    transaction = repo / ".project-knowledge/transactions" / f"{transaction_id}.json"
    write(transaction, json.dumps({"schema_version": 1, "state": "backed_up", "graph_digest": validated.graph_digest}))

    with pytest.raises(OSError, match="copy-stage"):
        promote_graph(validated, repo, fs=FaultingFS("copy-stage"))

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not transaction.exists()


def test_tampered_report_is_not_accepted_as_an_unchanged_owned_target(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    validated = validate_candidate(candidate, staged, manifest)
    assert promote_graph(validated, repo).changed is True
    write(repo / "graphify-out/GRAPH_REPORT.md", "tampered\n")

    result = promote_graph(validated, repo)

    assert result.changed is True
    assert (repo / "graphify-out/GRAPH_REPORT.md").read_text() != "tampered\n"


def test_prepared_journal_recovers_backup_created_before_state_advance(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    target = write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    backup = repo / ".project-knowledge/rollback" / validated.graph_digest
    backup.parent.mkdir(parents=True)
    target.rename(backup)
    promotion_stage = repo / ".project-knowledge/staging" / validated.graph_digest
    shutil.copytree(candidate, promotion_stage)
    transaction = repo / ".project-knowledge/transactions" / f"{validated.graph_digest}.json"
    write(
        transaction,
        json.dumps(
            {
                "schema_version": 1,
                "state": "prepared",
                "graph_digest": validated.graph_digest,
            }
        ),
    )

    with pytest.raises(OSError, match="copy-stage"):
        promote_graph(validated, repo, fs=FaultingFS("copy-stage"))

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not transaction.exists()


def test_interrupted_promoted_target_is_rolled_back_before_retry(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    target = write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    backup = repo / ".project-knowledge/rollback" / validated.graph_digest
    backup.parent.mkdir(parents=True)
    target.rename(backup)
    shutil.copytree(candidate, target)
    transaction = repo / ".project-knowledge/transactions" / f"{validated.graph_digest}.json"
    write(transaction, json.dumps({"schema_version": 1, "state": "promoted", "graph_digest": validated.graph_digest}))

    with pytest.raises(OSError, match="copy-stage"):
        promote_graph(validated, repo, fs=FaultingFS("copy-stage"))

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert not transaction.exists()


def test_recovery_rejects_symlinked_rollback_before_touching_valid_target(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    target = write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    external = tmp_path / "external-rollback"
    write(external / "marker", "external")
    backup = repo / ".project-knowledge/rollback" / validated.graph_digest
    backup.parent.mkdir(parents=True)
    backup.symlink_to(external, target_is_directory=True)
    transaction = repo / ".project-knowledge/transactions" / f"{validated.graph_digest}.json"
    write(
        transaction,
        json.dumps(
            {
                "schema_version": 1,
                "state": "promoted",
                "graph_digest": validated.graph_digest,
            }
        ),
    )

    with pytest.raises(ArtifactValidationError, match="rollback graph"):
        promote_graph(validated, repo)

    assert (target / "marker").read_text(encoding="utf-8") == "old"
    assert (external / "marker").read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize("state", ["prepared", "promoted"])
def test_interrupted_first_promotion_is_removed_before_retry(
    tmp_path: Path,
    candidate: Path,
    staged: StagedInput,
    manifest: ProjectManifest,
    state: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    validated = validate_candidate(candidate, staged, manifest)
    target = repo / "graphify-out"
    shutil.copytree(candidate, target)
    transaction = repo / ".project-knowledge/transactions" / f"{validated.graph_digest}.json"
    write(
        transaction,
        json.dumps(
                {
                    "schema_version": 1,
                    "state": state,
                "graph_digest": validated.graph_digest,
            }
        ),
    )

    with pytest.raises(OSError, match="copy-stage"):
        promote_graph(validated, repo, fs=FaultingFS("copy-stage"))

    assert not target.exists()
    assert not transaction.exists()


def test_recovery_fsync_failure_still_restores_previous_graph(
    tmp_path: Path, candidate: Path, staged: StagedInput, manifest: ProjectManifest
) -> None:
    repo = tmp_path / "repo"
    target = write_committed_graph(repo)
    validated = validate_candidate(candidate, staged, manifest)
    backup = repo / ".project-knowledge/rollback" / validated.graph_digest
    backup.parent.mkdir(parents=True)
    target.rename(backup)
    transaction = repo / ".project-knowledge/transactions" / f"{validated.graph_digest}.json"
    write(transaction, json.dumps({"schema_version": 1, "state": "backed_up", "graph_digest": validated.graph_digest}))

    with pytest.raises(OSError, match="fsync-recovery"):
        promote_graph(validated, repo, fs=FaultingFS("fsync-recovery"))

    assert (target / "marker").read_text(encoding="utf-8") == "old"
