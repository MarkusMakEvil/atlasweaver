from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from project_knowledge.compatibility import production_graphify_compatibility
from project_knowledge.integrity import analyze_graph


SCHEMA_VERSION = 1


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    write(repo / "src/app.py", "def run():\n    return 1\n")
    write(
        repo / ".graphify-project.yaml",
        """schema_version: 1
project_id: demo
display_name: Demo
include_roots: [src]
output_dir: graphify-out
obsidian_namespace: Projects/demo/Generated
excludes: []
track_html: true
graphify_version: 0.9.48
""",
    )
    return repo


def run_cli(
    repo: Path,
    command: str,
    *arguments: object,
    env: dict[str, str] | None = None,
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
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def payload(
    result: subprocess.CompletedProcess[str], *, schema_version: int = SCHEMA_VERSION
) -> dict[str, object]:
    assert result.stderr == ""
    value = json.loads(result.stdout)
    assert value["schema_version"] == schema_version
    return value


def tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    if not root.exists() and not root.is_symlink():
        return ()
    found: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found.append((relative, "link", os.readlink(path).encode()))
        elif path.is_dir():
            found.append((relative, "dir", b""))
        else:
            found.append((relative, "file", path.read_bytes()))
    return tuple(found)


def graphify_executable(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "graphify-calls.jsonl"
    executable = write(
        tmp_path / "graphify",
        f"""#!{sys.executable}
import json
import os
import pathlib
import sys
path = pathlib.Path({str(calls)!r})
with path.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
arguments = sys.argv[1:]
if arguments == ['--version']:
    print('graphify 0.9.48')
elif arguments == ['--help']:
    print('Commands:\\n  extract\\n  diagnose\\n  cluster-only\\n  query\\n  explain\\n  path\\n  global\\n  export\\n  install')
elif arguments and arguments[0] == 'extract':
    output = pathlib.Path(arguments[arguments.index('--out') + 1]) / 'graphify-out'
    output.mkdir(parents=True)
    (output / 'graph.json').write_text(json.dumps({{
        'nodes': [{{'id': 'probe'}}], 'edges': [], 'hyperedges': [],
        'input_tokens': 0, 'output_tokens': 0
    }}), encoding='utf-8')
elif arguments[:2] == ['diagnose', 'multigraph']:
    print(json.dumps({{
        'schema_version': 1,
        'summary': {{
            'node_count': 1, 'raw_edge_count': 0, 'missing_endpoint_edges': 0,
            'dangling_endpoint_edges': 0, 'self_loop_edges': 0,
            'exact_duplicate_edges': 0, 'undirected_unique_endpoint_pairs': 0,
            'undirected_same_endpoint_collapsed_edges': 0,
            'same_endpoint_group_count': 0, 'relation_variant_groups': 0,
            'source_file_variant_groups': 0, 'source_location_variant_groups': 0,
            'context_variant_groups': 0, 'post_build_graph_type': 'Graph',
            'post_build_node_count': 1, 'post_build_edge_count': 0,
            'effective_directed': False
        }}
    }}))
elif arguments and arguments[0] == 'cluster-only':
    graph = pathlib.Path(arguments[arguments.index('--graph') + 1])
    graph.write_text(json.dumps({{
        'directed': False, 'multigraph': False, 'graph': {{}},
        'nodes': [{{'id': 'probe'}}], 'links': [], 'hyperedges': []
    }}), encoding='utf-8')
elif arguments[:2] == ['global', 'add']:
    home = pathlib.Path(os.environ['HOME']) / '.graphify'
    home.mkdir(parents=True)
    key = arguments[arguments.index('--as') + 1]
    (home / 'global-graph.json').write_text(
        json.dumps({{'nodes': [{{'id': 'probe'}}], 'links': []}}), encoding='utf-8'
    )
    (home / 'global-manifest.json').write_text(
        json.dumps({{'repos': {{key: {{}}}}}}), encoding='utf-8'
    )
elif arguments and arguments[0] == 'install':
    platform = arguments[arguments.index('--platform') + 1]
    skill = pathlib.Path(os.environ['HOME']) / f'.{{platform}}/skills/graphify/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('# Graphify\\n', encoding='utf-8')
else:
    raise SystemExit(9)
""",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable, calls


def test_parser_exposes_only_the_ten_workflow_commands() -> None:
    """A missing or accidental extra command would break the skill contract."""
    from project_knowledge.cli import build_parser

    parser = build_parser()
    choices = next(
        action.choices
        for action in parser._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    )
    assert set(choices) == {
        "detect",
        "preflight",
        "stage",
        "adapt",
        "scan-secrets",
        "validate",
        "promote",
        "atlas-prepare",
        "atlas-promote",
        "health",
    }


def test_detect_json_is_read_only_and_path_free(tmp_path: Path) -> None:
    """Detection must not bootstrap state or expose the local repository path."""
    repo = project(tmp_path)
    before = tree_snapshot(repo)

    result = run_cli(repo, "detect", "--json")

    assert result.returncode == 0
    assert payload(result) == {
        "schema_version": 1,
        "command": "detect",
        "status": "detected",
        "project_id": "demo",
        "graph_status": "missing",
        "source_matches": False,
    }
    assert str(repo) not in result.stdout
    assert tree_snapshot(repo) == before


def test_preflight_json_is_non_mutating_and_probes_exact_graphify_argv(
    tmp_path: Path,
) -> None:
    """Preflight runs the exact confined nine-command Graphify probe sequence."""
    repo = project(tmp_path)
    _, calls = graphify_executable(tmp_path)
    before = tree_snapshot(repo)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
    }

    result = run_cli(repo, "preflight", "--json", env=environment)

    assert result.returncode == 0
    assert payload(result) == {
        "schema_version": 1,
        "command": "preflight",
        "status": "ready",
        "project_id": "demo",
        "graphify_version": "0.9.48",
        "safe_file_count": 1,
        "obsidian_export": False,
    }
    assert tree_snapshot(repo) == before
    assert not (repo / ".project-knowledge").exists()
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded[:2] == [["--version"], ["--help"]]
    source = Path(recorded[2][1])
    smoke_root = source.parent
    code_output = smoke_root / "code"
    semantic_output = smoke_root / "semantic"
    native_graph = code_output / "graphify-out/graph.json"
    assert recorded[2:] == [
        ["extract", str(source), "--out", str(code_output), "--no-cluster", "--code-only"],
        ["extract", str(source), "--out", str(semantic_output), "--no-cluster", "--backend", "ollama", "--model", "atlasweaver-capability-probe", "--mode", "deep"],
        ["diagnose", "multigraph", "--graph", str(native_graph), "--undirected", "--json"],
        ["cluster-only", str(code_output), "--graph", str(native_graph), "--no-label", "--no-viz"],
        ["global", "add", str(native_graph), "--as", "atlasweaver/00000000-0000-4000-8000-000000000000"],
        ["install", "--platform", "codex"],
        ["install", "--platform", "agents"],
    ]


@pytest.mark.parametrize("option", ["--graphify-binary", "--assistant-skill"])
def test_preflight_exposes_no_executable_or_skill_override(
    tmp_path: Path, option: str
) -> None:
    repo = project(tmp_path)
    result = run_cli(repo, "preflight", option, str(tmp_path / "untrusted"), "--json")

    assert result.returncode == 2


def test_scan_secrets_is_read_only_redacted_and_path_confined(tmp_path: Path) -> None:
    repo = project(tmp_path)
    secret = "correct-horse-battery-staple"
    write(repo / "src/settings.ts", f'api_key = "{secret}"\n')
    before = tree_snapshot(repo)

    result = run_cli(repo, "scan-secrets", "--json")

    assert result.returncode == 0
    document = payload(result)
    assert document["command"] == "scan-secrets"
    assert document["status"] == "scanned"
    assert document["finding_count"] == 1
    assert document["excepted_count"] == 0
    assert document["findings"] == [
        {
            "detector": "generic_secret_assignment",
            "path": "src/settings.ts",
            "line": 1,
            "fingerprint": document["findings"][0]["fingerprint"],
        }
    ]
    assert str(document["findings"][0]["fingerprint"]).startswith("sha256:")
    assert secret not in result.stdout
    assert str(repo) not in result.stdout
    assert tree_snapshot(repo) == before


def test_stage_is_the_only_command_that_creates_sanitized_input(
    tmp_path: Path,
) -> None:
    """Stage must require and populate only the caller-selected destination."""
    repo = project(tmp_path)
    write(repo / "src/api-token.txt", "private\n")
    destination = tmp_path / "safe-input"
    receipt = tmp_path / "safe-input.receipt.json"

    result = run_cli(
        repo,
        "stage",
        "--destination",
        destination,
        "--receipt",
        receipt,
        "--json",
    )

    assert result.returncode == 0
    document = payload(result)
    assert document["command"] == "stage"
    assert document["status"] == "staged"
    assert document["project_id"] == "demo"
    assert document["file_count"] == 1
    assert len(str(document["source_digest"])) == 64
    assert (destination / "src/app.py").is_file()
    assert receipt.is_file()
    assert not (destination / "src/api-token.txt").exists()
    assert str(destination) not in result.stdout
    assert str(receipt) not in result.stdout


def test_stage_requires_explicit_receipt_path(tmp_path: Path) -> None:
    result = run_cli(
        project(tmp_path),
        "stage",
        "--destination",
        tmp_path / "safe-input",
    )

    assert result.returncode == 2
    assert "--receipt" in result.stderr


def test_stage_receipt_failure_removes_new_staged_tree(tmp_path: Path) -> None:
    repo = project(tmp_path)
    destination = tmp_path / "safe-input"
    receipt = tmp_path / "receipt.json"
    receipt.write_text("caller owned\n", encoding="utf-8")

    result = run_cli(
        repo,
        "stage",
        "--destination",
        destination,
        "--receipt",
        receipt,
        "--json",
    )

    assert result.returncode == 1
    assert payload(result)["error"]["code"] == "staging_failed"
    assert not destination.exists()
    assert receipt.read_text(encoding="utf-8") == "caller owned\n"


@pytest.mark.parametrize(
    ("command", "required"),
    [
        ("stage", "--destination"),
        ("adapt", "--staged-input"),
        ("validate", "--candidate"),
        ("promote", "--candidate"),
        ("atlas-prepare", "--candidate"),
        ("atlas-promote", "--candidate"),
    ],
)
def test_mutating_commands_require_exact_paths(
    tmp_path: Path, command: str, required: str
) -> None:
    """Implicit mutation destinations would make automation unsafe."""
    result = run_cli(project(tmp_path), command)

    assert result.returncode == 2
    assert required in result.stderr


def write_graph_candidate(candidate: Path, source_digest: str) -> None:
    graph = {
        "project_id": "demo",
        "graphify_version": "0.9.48",
        "source_digest": source_digest,
        "nodes": [{"id": "app", "source_file": "src/app.py"}],
        "edges": [],
        "graph_health": analyze_graph(
            [{"id": "app", "source_file": "src/app.py"}],
            [],
            semantics=production_graphify_compatibility().semantics,
        ).to_dict(),
        "extraction_coverage": {"schema_version": 1, "total_staged_files": 1, "represented_source_paths": ["src/app.py"], "skipped": []},
    }
    write(candidate / "graph.json", json.dumps(graph) + "\n")
    write(candidate / "GRAPH_REPORT.md", "# Graph Report\n\n`src/app.py`\n")
    write(candidate / "graph.html", "<!doctype html><title>Demo</title>\n")


def write_raw_graph_candidate(candidate: Path) -> Path:
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "app",
                "source_file": "src/app.py",
                "provenance": "EXTRACTED",
            }
        ],
        "links": [],
    }
    write(candidate / "graph.json", json.dumps(graph) + "\n")
    write(candidate / "GRAPH_REPORT.md", "# Graph Report\n\n`src/app.py`\n")
    write(candidate / "graph.html", "<!doctype html><title>Demo</title>\n")
    return candidate


def staged_input(repo: Path, tmp_path: Path) -> tuple[Path, str]:
    destination = tmp_path / "safe-input"
    result = run_cli(
        repo,
        "stage",
        "--destination",
        destination,
        "--receipt",
        tmp_path / "safe-input.receipt.json",
        "--json",
    )
    assert result.returncode == 0
    return destination, str(payload(result)["source_digest"])


def test_adapt_binds_real_stage_receipt_and_writes_separate_candidate(
    tmp_path: Path,
) -> None:
    repo = project(tmp_path)
    stage = tmp_path / "safe-input"
    receipt = tmp_path / "safe-input.receipt.json"
    staged = run_cli(
        repo,
        "stage",
        "--destination",
        stage,
        "--receipt",
        receipt,
        "--json",
    )
    assert staged.returncode == 0
    raw = write_raw_graph_candidate(tmp_path / "raw")
    candidate = tmp_path / "candidate"

    result = run_cli(
        repo,
        "adapt",
        "--staged-input",
        stage,
        "--receipt",
        receipt,
        "--raw-candidate",
        raw,
        "--destination",
        candidate,
        "--json",
    )

    assert result.returncode == 0
    document = payload(result)
    assert document["status"] == "adapted"
    assert document["project_id"] == "demo"
    assert document["node_count"] == 1
    assert document["edge_count"] == 0
    assert document["skipped_count"] == 0
    assert candidate.is_dir()
    assert (candidate / "graph.json").is_file()
    assert str(stage) not in result.stdout
    assert str(receipt) not in result.stdout
    assert str(raw) not in result.stdout
    assert str(candidate) not in result.stdout


def test_adapt_rejects_repo_source_drift_after_staging(tmp_path: Path) -> None:
    repo = project(tmp_path)
    stage = tmp_path / "safe-input"
    receipt = tmp_path / "safe-input.receipt.json"
    assert run_cli(
        repo,
        "stage",
        "--destination",
        stage,
        "--receipt",
        receipt,
        "--json",
    ).returncode == 0
    raw = write_raw_graph_candidate(tmp_path / "raw")
    write(repo / "src/app.py", "def run():\n    return 2\n")

    result = run_cli(
        repo,
        "adapt",
        "--staged-input",
        stage,
        "--receipt",
        receipt,
        "--raw-candidate",
        raw,
        "--destination",
        tmp_path / "candidate",
        "--json",
    )

    assert result.returncode == 1
    assert payload(result)["error"]["code"] == "staging_failed"
    assert not (tmp_path / "candidate").exists()


def test_adapt_removes_candidate_when_source_drifts_after_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from project_knowledge import cli as cli_module
    from project_knowledge.staging import StagingError

    repo = project(tmp_path)
    stage = tmp_path / "safe-input"
    receipt = tmp_path / "safe-input.receipt.json"
    assert run_cli(
        repo,
        "stage",
        "--destination",
        stage,
        "--receipt",
        receipt,
        "--json",
    ).returncode == 0
    raw = write_raw_graph_candidate(tmp_path / "raw")
    destination = tmp_path / "candidate"
    real_check = cli_module._require_current_input
    checks = 0

    def drift_after_write(repo_path: Path, manifest: object, expected: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise StagingError("simulated post-adapt source drift")
        real_check(repo_path, manifest, expected)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "_require_current_input", drift_after_write)

    result = cli_module.main(
        [
            "adapt",
            "--repo",
            str(repo),
            "--staged-input",
            str(stage),
            "--receipt",
            str(receipt),
            "--raw-candidate",
            str(raw),
            "--destination",
            str(destination),
            "--json",
        ]
    )
    capsys.readouterr()

    assert result == 1
    assert checks == 2
    assert not destination.exists()


def test_validate_is_ephemeral_so_the_same_candidate_can_be_promoted(
    tmp_path: Path,
) -> None:
    """The documented validate→promote sequence must not poison its candidate."""
    repo = project(tmp_path)
    _, digest = staged_input(repo, tmp_path)
    candidate = tmp_path / "candidate"
    write_graph_candidate(candidate, digest)

    result = run_cli(
        repo,
        "validate",
        "--candidate",
        candidate,
        "--json",
    )

    assert result.returncode == 0
    document = payload(result)
    assert document["status"] == "valid"
    assert document["node_count"] == 1
    assert document["edge_count"] == 0
    assert not (candidate / ".project-knowledge-ownership.json").exists()

    promoted = run_cli(repo, "promote", "--candidate", candidate, "--json")
    assert promoted.returncode == 0
    assert payload(promoted)["status"] == "promoted"


def test_promote_validates_raw_candidate_then_uses_task4_transaction(
    tmp_path: Path,
) -> None:
    """Promotion must not bypass candidate validation or choose a hidden target."""
    repo = project(tmp_path)
    _, digest = staged_input(repo, tmp_path)
    candidate = tmp_path / "candidate"
    write_graph_candidate(candidate, digest)

    result = run_cli(
        repo,
        "promote",
        "--candidate",
        candidate,
        "--json",
    )

    assert result.returncode == 0
    document = payload(result)
    assert document["status"] == "promoted"
    assert len(str(document["graph_digest"])) == 64
    assert (repo / "graphify-out/graph.json").is_file()
    assert (repo / ".project-knowledge/transactions").is_dir()
    assert str(candidate) not in result.stdout


def raw_atlas_export(root: Path) -> Path:
    write(
        root / "Components/Auth.md",
        "---\nsource_kind: component\nsource_path: src/app.py\n"
        "graph_node_id: component:auth\n---\n# Auth\n",
    )
    write(
        root / "graph.canvas",
        json.dumps(
            {
                "nodes": [
                    {"id": "1", "type": "file", "file": "Components/Auth.md"}
                ],
                "edges": [],
            }
        ),
    )
    return root


def test_atlas_prepare_and_cross_process_promote_preserve_human_notes(
    tmp_path: Path,
) -> None:
    """Atlas promotion must reload/revalidate disk evidence and touch Generated only."""
    repo = project(tmp_path)
    candidate = raw_atlas_export(tmp_path / "atlas-candidate")
    prepared = run_cli(repo, "atlas-prepare", "--candidate", candidate, "--json")
    assert prepared.returncode == 0
    assert payload(prepared)["status"] == "prepared"

    atlas = tmp_path / "Obsidian Vault"
    human = write(atlas / "Projects/demo/Notes/manual.md", "keep me\n")
    promoted = run_cli(
        repo,
        "atlas-promote",
        "--candidate",
        candidate,
        "--atlas",
        atlas,
        "--json",
    )

    assert promoted.returncode == 0
    document = payload(promoted)
    assert document["status"] == "promoted"
    assert document["changed"] is True
    assert human.read_text() == "keep me\n"
    assert (atlas / "Projects/demo/Generated/_project.base").is_file()
    assert str(atlas) not in promoted.stdout


def test_health_reports_source_drift_without_atlas_path_or_sensitive_names(
    tmp_path: Path,
) -> None:
    """Health output must remain useful without disclosing local machine context."""
    repo = project(tmp_path)
    atlas = tmp_path / "Private Obsidian Vault"
    atlas.mkdir()

    result = run_cli(
        repo,
        "health",
        "--atlas",
        atlas,
        "--json",
    )

    assert result.returncode == 0
    document = payload(result, schema_version=2)
    assert document["command"] == "health"
    assert document["status"] == "missing"
    assert document["issues"] == ["graph_missing"]
    assert str(atlas) not in result.stdout
    assert "Private Obsidian Vault" not in result.stdout


def test_operational_errors_have_stable_sanitized_json_and_exit_code(
    tmp_path: Path,
) -> None:
    """Raw exceptions can contain secret-shaped filenames and absolute paths."""
    repo = project(tmp_path)
    staged_input(repo, tmp_path)
    sensitive = tmp_path / "customer-api-token-candidate"
    sensitive.mkdir()

    result = run_cli(
        repo,
        "validate",
        "--candidate",
        sensitive,
        "--json",
    )

    assert result.returncode == 1
    document = json.loads(result.stdout)
    assert document == {
        "schema_version": 1,
        "command": "validate",
        "status": "error",
        "error": {
            "code": "artifact_invalid",
            "message": "graph candidate validation failed",
        },
    }
    assert result.stderr == ""
    assert str(sensitive) not in result.stdout
    assert "customer-api-token-candidate" not in result.stdout


def test_repo_symlink_fails_closed_without_echoing_target(tmp_path: Path) -> None:
    """Resolving a caller-supplied repo symlink could cross the selected boundary."""
    real = project(tmp_path / "private-target")
    alias = tmp_path / "repo-alias"
    os.symlink(real, alias, target_is_directory=True)

    result = run_cli(alias, "detect", "--json")

    assert result.returncode == 1
    document = json.loads(result.stdout)
    assert document["error"] == {
        "code": "invalid_repo",
        "message": "repository must be a real directory",
    }
    assert str(real) not in result.stdout


def test_console_entrypoint_is_declared_only_with_the_working_module() -> None:
    """The deferred package command must now resolve to the implemented main."""
    result = subprocess.run(
        [shutil.which("uv") or "uv", "run", "project-knowledge", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "{detect,preflight,stage,adapt,scan-secrets,validate,promote,atlas-prepare,atlas-promote,health}" in result.stdout
    assert "Traceback" not in result.stderr


def test_onboarding_script_installs_normal_shell_command(tmp_path: Path) -> None:
    """A workspace-only entrypoint disappears outside `uv run`."""
    environment = os.environ.copy()
    environment["UV_TOOL_DIR"] = str(tmp_path / "tools")
    environment["UV_TOOL_BIN_DIR"] = str(tmp_path / "bin")
    result = subprocess.run(
        [sys.executable, "scripts/install-project-knowledge-tool"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    smoke = subprocess.run([tmp_path / "bin/project-knowledge", "--help"], text=True, capture_output=True, check=False)
    assert smoke.returncode == 0
    assert "preflight" in smoke.stdout
