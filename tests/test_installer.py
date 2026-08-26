from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "skills/using-project-knowledge-graphs"
INSTALLER = ROOT / "scripts/install-project-knowledge-skill"
SKILL_NAME = "using-project-knowledge-graphs"
MARKER = ".atlasweaver-managed.json"


def tree_digest(root: Path, *, ignore_marker: bool = False) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if ignore_marker and relative == MARKER:
            continue
        assert not path.is_symlink()
        kind = b"d" if path.is_dir() else b"f"
        digest.update(kind + b"\0" + relative.encode() + b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def run_installer(
    codex_home: Path,
    *,
    cwd: Path | None = None,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(INSTALLER), "--codex-home", str(codex_home)],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def fake_graphify(tmp_path: Path) -> tuple[dict[str, str], Path]:
    binary_root = tmp_path / "bin"
    binary_root.mkdir()
    calls = tmp_path / "graphify-calls.jsonl"
    launcher = binary_root / "graphify"
    launcher.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

call = {{"argv": sys.argv[1:], "env": dict(os.environ)}}
with Path({str(calls)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(call) + "\\n")
arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("graphify 0.9.48")
elif arguments == ["--help"]:
    print("Usage: graphify <command>\\n\\nCommands:\\n  extract <path>\\n  diagnose multigraph\\n  cluster-only <path>\\n  query <question>\\n  explain <node>\\n  path <source> <target>\\n  global add <graph>\\n  export callflow-html\\n  install --platform codex")
elif arguments and arguments[0] == "extract":
    output = Path(arguments[arguments.index("--out") + 1]) / "graphify-out"
    output.mkdir(parents=True)
    (output / "graph.json").write_text(json.dumps({{
        "nodes": [{{"id": "probe"}}], "edges": [], "hyperedges": [],
        "input_tokens": 0, "output_tokens": 0,
    }}), encoding="utf-8")
elif arguments[:2] == ["diagnose", "multigraph"]:
    print(json.dumps({{
        "schema_version": 1,
        "summary": {{
            "node_count": 1, "raw_edge_count": 0, "missing_endpoint_edges": 0,
            "dangling_endpoint_edges": 0, "self_loop_edges": 0,
            "exact_duplicate_edges": 0, "undirected_unique_endpoint_pairs": 0,
            "undirected_same_endpoint_collapsed_edges": 0,
            "same_endpoint_group_count": 0, "relation_variant_groups": 0,
            "source_file_variant_groups": 0, "source_location_variant_groups": 0,
            "context_variant_groups": 0, "post_build_graph_type": "Graph",
            "post_build_node_count": 1, "post_build_edge_count": 0,
            "effective_directed": False,
        }},
    }}))
elif arguments and arguments[0] == "cluster-only":
    graph = Path(arguments[arguments.index("--graph") + 1])
    graph.write_text(json.dumps({{
        "directed": False, "multigraph": False, "graph": {{}},
        "nodes": [{{"id": "probe"}}], "links": [], "hyperedges": [],
    }}), encoding="utf-8")
elif arguments[:2] == ["global", "add"]:
    root = Path(os.environ["HOME"]) / ".graphify"
    root.mkdir(parents=True)
    key = arguments[arguments.index("--as") + 1]
    (root / "global-graph.json").write_text(json.dumps({{"nodes": [{{"id": "probe"}}], "links": []}}), encoding="utf-8")
    (root / "global-manifest.json").write_text(json.dumps({{"repos": {{key: {{}}}}}}), encoding="utf-8")
elif arguments and arguments[0] == "install":
    platform = arguments[arguments.index("--platform") + 1]
    target = Path(os.environ["HOME"]) / f".{{platform}}/skills/graphify/SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Graphify\\n", encoding="utf-8")
else:
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return {**os.environ, "PATH": f"{binary_root}{os.pathsep}{os.defpath}"}, calls


def test_installer_copies_exact_tree_and_preserves_siblings(tmp_path: Path) -> None:
    environment, calls = fake_graphify(tmp_path)
    user_home = tmp_path / "user"
    home = user_home / ".codex"
    sibling = home / "skills/existing/SKILL.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep\n", encoding="utf-8")

    run_installer(home, cwd=tmp_path, environment=environment)

    destination = home / "skills" / SKILL_NAME
    assert tree_digest(destination, ignore_marker=True) == tree_digest(CANONICAL)
    assert sibling.read_text(encoding="utf-8") == "keep\n"
    marker = json.loads((destination / MARKER).read_text(encoding="utf-8"))
    assert marker == {
        "schema_version": 1,
        "manager": "atlasweaver-agent-installer",
        "platform": "codex",
        "resource_digest": tree_digest(CANONICAL),
    }
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert recorded[-1]["argv"] == ["install", "--platform", "codex"]
    assert {key: recorded[-1]["env"][key] for key in ("HOME", "LANG", "LC_ALL", "PATH")} == {
        "HOME": str(user_home.resolve()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
    }
    assert (home / "skills/graphify/SKILL.md").is_file()


def test_installer_refuses_unmanaged_destination(tmp_path: Path) -> None:
    environment, _ = fake_graphify(tmp_path)
    home = tmp_path / "user/.codex"
    destination = home / "skills" / SKILL_NAME
    destination.mkdir(parents=True)
    human = destination / "SKILL.md"
    human.write_text("human content\n", encoding="utf-8")

    result = run_installer(home, check=False, environment=environment)

    assert result.returncode != 0
    assert "unmanaged" in result.stderr.lower()
    assert human.read_text(encoding="utf-8") == "human content\n"


def test_installer_refuses_a_tampered_managed_destination(tmp_path: Path) -> None:
    environment, _ = fake_graphify(tmp_path)
    home = tmp_path / "user/.codex"
    run_installer(home, environment=environment)
    destination = home / "skills" / SKILL_NAME
    (destination / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    result = run_installer(home, check=False, environment=environment)

    assert result.returncode != 0
    assert "modified" in result.stderr.lower()
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "tampered\n"


def test_installer_is_idempotent_and_leaves_no_transaction_files(tmp_path: Path) -> None:
    environment, _ = fake_graphify(tmp_path)
    home = tmp_path / "user/.codex"
    run_installer(home, environment=environment)
    first = tree_digest(home / "skills" / SKILL_NAME)

    run_installer(home, environment=environment)

    assert tree_digest(home / "skills" / SKILL_NAME) == first
    assert sorted(path.name for path in (home / "skills").iterdir()) == [
        "graphify",
        SKILL_NAME,
    ]


def test_installer_refuses_symlink_destination(tmp_path: Path) -> None:
    environment, _ = fake_graphify(tmp_path)
    home = tmp_path / "user/.codex"
    skills = home / "skills"
    outside = tmp_path / "outside"
    skills.mkdir(parents=True)
    outside.mkdir()
    os.symlink(outside, skills / SKILL_NAME)

    result = run_installer(home, check=False, environment=environment)

    assert result.returncode != 0
    assert "agent_destination_unmanaged" in result.stderr.lower()
    assert list(outside.iterdir()) == []


def test_installer_rejects_platform_root_that_graphify_cannot_derive_from_home(
    tmp_path: Path,
) -> None:
    environment, calls = fake_graphify(tmp_path)
    platform_root = tmp_path / "user/codex-home"

    result = run_installer(
        platform_root, check=False, environment=environment
    )

    assert result.returncode != 0
    assert "home" in result.stderr.lower()
    assert not calls.exists()
