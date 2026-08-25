from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PINNED_GRAPHIFY_VERSION = "0.9.48"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_project_knowledge(
    repo: Path, command: str, *arguments: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "project_knowledge.cli",
            command,
            "--repo",
            str(repo),
            *(str(value) for value in arguments),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def require_pinned_graphify() -> str:
    executable = shutil.which("graphify")
    if executable is None:
        pytest.skip("pinned Graphify executable is unavailable")
    version = subprocess.run(
        [executable, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if version.returncode != 0 or PINNED_GRAPHIFY_VERSION not in version.stdout:
        pytest.skip("pinned Graphify 0.9.48 is unavailable")
    return executable


def test_real_graphify_stage_adapt_validate_promote_health_cycle(
    tmp_path: Path,
) -> None:
    graphify = require_pinned_graphify()
    repo = tmp_path / "repo"
    write(repo / "src/app.py", "def run():\n    return 1\n")
    write(
        repo / ".graphify-project.yaml",
        "schema_version: 1\n"
        "project_id: integration-demo\n"
        "display_name: Integration Demo\n"
        "include_roots: [src]\n"
        "output_dir: graphify-out\n"
        "obsidian_namespace: Projects/integration-demo/Generated\n"
        "excludes: []\n"
        "track_html: false\n"
        "graphify_version: 0.9.48\n",
    )
    stage = tmp_path / "private-stage"
    receipt = tmp_path / "private-stage.receipt.json"
    staged = run_project_knowledge(
        repo,
        "stage",
        "--destination",
        stage,
        "--receipt",
        receipt,
    )
    assert staged.returncode == 0, staged.stdout + staged.stderr

    graphify_workspace = tmp_path / "graphify-workspace"
    extracted = subprocess.run(
        [
            graphify,
            "extract",
            str(stage),
            "--out",
            str(graphify_workspace),
            "--code-only",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert extracted.returncode == 0, extracted.stdout + extracted.stderr
    clustered = subprocess.run(
        [
            graphify,
            "cluster-only",
            str(graphify_workspace),
            "--no-viz",
            "--no-label",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert clustered.returncode == 0, clustered.stdout + clustered.stderr

    raw_candidate = graphify_workspace / "graphify-out"
    candidate = tmp_path / "candidate"
    adapted = run_project_knowledge(
        repo,
        "adapt",
        "--staged-input",
        stage,
        "--receipt",
        receipt,
        "--raw-candidate",
        raw_candidate,
        "--destination",
        candidate,
    )
    assert adapted.returncode == 0, adapted.stdout + adapted.stderr
    adapted_graph = json.loads((candidate / "graph.json").read_text(encoding="utf-8"))
    assert adapted_graph["project_id"] == "integration-demo"
    assert adapted_graph["graphify_version"] == PINNED_GRAPHIFY_VERSION
    assert adapted_graph["graph_health"]["impact_analysis_trusted"] is False

    validated = run_project_knowledge(
        repo, "validate", "--candidate", candidate
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr
    promoted = run_project_knowledge(repo, "promote", "--candidate", candidate)
    assert promoted.returncode == 0, promoted.stdout + promoted.stderr

    health = run_project_knowledge(repo, "health")
    assert health.returncode == 0, health.stdout + health.stderr
    healthy_document = json.loads(health.stdout)
    assert healthy_document["source_matches"] is True
    assert healthy_document["impact_analysis_trusted"] is False
    assert "graph_integrity_degraded" in healthy_document["issues"]

    write(repo / "src/app.py", "def run():\n    return 2\n")
    stale = run_project_knowledge(repo, "health")
    assert stale.returncode == 0
    stale_document = json.loads(stale.stdout)
    assert stale_document["status"] == "stale"
    assert stale_document["source_matches"] is False
