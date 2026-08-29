from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from importlib import resources
try:
    from importlib.resources.abc import Traversable
except ImportError:  # Python 3.10
    from importlib.abc import Traversable
import json
import os
from pathlib import Path
import stat
from subprocess import CompletedProcess

import pytest

from project_knowledge.agent_install import (
    AgentInstallDependencies,
    AgentInstallError,
    AgentInstallRequest,
    install_agent,
    uninstall_agent,
)
from project_knowledge.compatibility import (
    production_graphify_compatibility,
    render_graphify_agent_install,
)
from project_knowledge.graphify import (
    ResolvedGraphifyExecutable,
    resolve_graphify_executable,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills/using-project-knowledge-graphs"
REQUIRED_HELP = """Usage: graphify <command>\n\nCommands:
  extract <path>
  diagnose multigraph
  cluster-only <path>
  query \"<question>\"
  explain \"node\"
  path \"A\" \"B\"
  global add <graph.json>
  export callflow-html
  install --platform codex
"""


@dataclass(frozen=True)
class RunnerCall:
    argv: tuple[str, ...]
    environment: dict[str, str]


class AgentRunner:
    """Materialize the official probe artifacts while recording the real boundary."""

    def __init__(
        self,
        *,
        version: str = production_graphify_compatibility().version,
        omit_probe_platform: str | None = None,
        fail_install_home: Path | None = None,
        create_unmanaged_atlas_home: Path | None = None,
        replace_skills_home: Path | None = None,
        replacement_skills: Path | None = None,
    ) -> None:
        self.version = version
        self.omit_probe_platform = omit_probe_platform
        self.fail_install_home = fail_install_home
        self.create_unmanaged_atlas_home = create_unmanaged_atlas_home
        self.replace_skills_home = replace_skills_home
        self.replacement_skills = replacement_skills
        self.calls: list[RunnerCall] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcess[str]:
        assert text and capture_output and not check and timeout > 0
        call = tuple(argv)
        environment = dict(env)
        self.calls.append(RunnerCall(call, environment))
        home = Path(environment["HOME"])

        if call[1:] == ("--version",):
            return CompletedProcess(call, 0, f"graphify {self.version}\n", "")
        if call[1:] == ("--help",):
            return CompletedProcess(call, 0, REQUIRED_HELP, "")
        if call[1] == "extract":
            output = Path(call[call.index("--out") + 1]) / "graphify-out"
            output.mkdir(parents=True)
            (output / "graph.json").write_text(
                json.dumps(
                    {
                        "nodes": [{"id": "probe"}],
                        "edges": [],
                        "hyperedges": [],
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                ),
                encoding="utf-8",
            )
            return CompletedProcess(call, 0, "", "")
        if call[1:3] == ("diagnose", "multigraph"):
            summary = {
                "node_count": 1,
                "raw_edge_count": 0,
                "missing_endpoint_edges": 0,
                "dangling_endpoint_edges": 0,
                "self_loop_edges": 0,
                "exact_duplicate_edges": 0,
                "undirected_unique_endpoint_pairs": 0,
                "undirected_same_endpoint_collapsed_edges": 0,
                "same_endpoint_group_count": 0,
                "relation_variant_groups": 0,
                "source_file_variant_groups": 0,
                "source_location_variant_groups": 0,
                "context_variant_groups": 0,
                "post_build_graph_type": "Graph",
                "post_build_node_count": 1,
                "post_build_edge_count": 0,
                "effective_directed": False,
            }
            return CompletedProcess(
                call, 0, json.dumps({"schema_version": 1, "summary": summary}), ""
            )
        if call[1] == "cluster-only":
            graph = Path(call[call.index("--graph") + 1])
            graph.write_text(
                json.dumps(
                    {
                        "directed": False,
                        "multigraph": False,
                        "graph": {},
                        "nodes": [{"id": "probe"}],
                        "links": [],
                        "hyperedges": [],
                    }
                ),
                encoding="utf-8",
            )
            return CompletedProcess(call, 0, "", "")
        if call[1:3] == ("global", "add"):
            registry = home / ".graphify"
            registry.mkdir(parents=True)
            key = call[call.index("--as") + 1]
            (registry / "global-graph.json").write_text(
                json.dumps({"nodes": [{"id": "probe"}], "links": []}),
                encoding="utf-8",
            )
            (registry / "global-manifest.json").write_text(
                json.dumps({"repos": {key: {}}}), encoding="utf-8"
            )
            return CompletedProcess(call, 0, "", "")
        if call[1] == "install":
            platform = call[call.index("--platform") + 1]
            if self.fail_install_home is not None and home == self.fail_install_home:
                return CompletedProcess(call, 9, "", "install failed")
            if (
                self.create_unmanaged_atlas_home is not None
                and home == self.create_unmanaged_atlas_home
            ):
                unmanaged = (
                    home
                    / f".{platform}/skills/using-project-knowledge-graphs/SKILL.md"
                )
                unmanaged.parent.mkdir(parents=True, exist_ok=True)
                unmanaged.write_text("human\n", encoding="utf-8")
            if self.replace_skills_home is not None and home == self.replace_skills_home:
                skills = home / f".{platform}/skills"
                displaced = skills.with_name("skills-displaced")
                skills.replace(displaced)
                assert self.replacement_skills is not None
                skills.symlink_to(self.replacement_skills, target_is_directory=True)
            if platform != self.omit_probe_platform:
                target = home / f".{platform}/skills/graphify/SKILL.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# Graphify\n", encoding="utf-8")
            return CompletedProcess(call, 0, "", "")
        raise AssertionError(call)


def resolved_graphify_executable(tmp_path: Path) -> ResolvedGraphifyExecutable:
    launcher = tmp_path / "graphify"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    return resolve_graphify_executable(test_override=launcher)


def agent_dependencies(
    runner: AgentRunner,
    resolved: ResolvedGraphifyExecutable,
    *,
    resource_root=None,
) -> AgentInstallDependencies:
    return AgentInstallDependencies(
        runner=runner,
        resolve_graphify=lambda: resolved,
        resource_root=(
            resources.files("project_knowledge").joinpath(
                "resources/skills/using-project-knowledge-graphs"
            )
            if resource_root is None
            else resource_root
        ),
    )


def _tree_digest(root: Path, marker: str | None = None) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if marker == relative:
            continue
        metadata = path.lstat()
        assert not path.is_symlink()
        kind = b"d" if path.is_dir() else b"f"
        digest.update(kind + b"\0" + relative.encode("utf-8") + b"\0")
        if kind == b"f":
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _resource_digest(root: Traversable) -> str:
    digest = hashlib.sha256()

    def visit(directory: Traversable, prefix: str = "") -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir() and not child.is_file():
                digest.update(b"d\0" + relative.encode("utf-8") + b"\0\0")
                visit(child, relative)
            else:
                assert child.is_file() and not child.is_dir()
                with resources.as_file(child) as materialized:
                    payload = materialized.read_bytes()
                digest.update(
                    b"f\0" + relative.encode("utf-8") + b"\0" + payload + b"\0"
                )

    visit(root)
    return digest.hexdigest()


def _destination(user_home: Path, platform: str) -> Path:
    return (
        user_home
        / (".codex" if platform == "codex" else ".agents")
        / "skills/using-project-knowledge-graphs"
    )


def test_packaged_skill_exactly_matches_reviewed_source_tree() -> None:
    packaged = resources.files("project_knowledge").joinpath(
        "resources/skills/using-project-knowledge-graphs"
    )
    assert _resource_digest(packaged) == _tree_digest(SOURCE_SKILL)


@pytest.mark.parametrize("platform", ["codex", "agents"])
def test_install_delegates_graphify_then_atomically_installs_owned_skill(
    tmp_path: Path, platform: str
) -> None:
    user_home = tmp_path / "user"
    runner = AgentRunner()
    resolved = resolved_graphify_executable(tmp_path)
    rendered = render_graphify_agent_install(
        production_graphify_compatibility(),
        binary=resolved.path,
        platform=platform,  # type: ignore[arg-type]
    )

    result = install_agent(
        AgentInstallRequest(platform, user_home),  # type: ignore[arg-type]
        dependencies=agent_dependencies(runner, resolved),
    )

    assert result.status == "installed"
    assert rendered.canonical_argv == (
        "<graphify>",
        "install",
        "--platform",
        platform,
    )
    assert runner.calls[-1].argv == rendered.argv
    assert runner.calls[-1].environment == {
        "HOME": str(user_home.resolve()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
    }
    assert all(call.argv[0] == str(resolved.path) for call in runner.calls)
    assert all(
        call.environment["HOME"] != str(user_home.resolve())
        for call in runner.calls[:-1]
    )
    destination = _destination(user_home, platform)
    marker = json.loads((destination / ".atlasweaver-managed.json").read_text())
    assert marker == {
        "manager": "atlasweaver-agent-installer",
        "platform": platform,
        "resource_digest": result.resource_digest,
        "schema_version": 1,
    }
    assert _tree_digest(destination, ".atlasweaver-managed.json") == result.resource_digest
    assert stat.S_IMODE(user_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (destination / ".atlasweaver-managed.json").stat().st_mode
    ) == 0o600


@pytest.mark.parametrize("state", ["unmanaged", "modified"])
def test_install_refuses_unmanaged_or_modified_destination_before_graphify(
    tmp_path: Path, state: str
) -> None:
    user_home = tmp_path / "user"
    destination = _destination(user_home, "codex")
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("human\n", encoding="utf-8")
    if state == "modified":
        (destination / ".atlasweaver-managed.json").write_text(
            json.dumps(
                {
                    "manager": "atlasweaver-agent-installer",
                    "platform": "codex",
                    "resource_digest": "0" * 64,
                    "schema_version": 1,
                }
            ),
            encoding="utf-8",
        )
    runner = AgentRunner()

    with pytest.raises(
        AgentInstallError,
        match="agent_destination_unmanaged|agent_destination_modified",
    ):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                runner, resolved_graphify_executable(tmp_path)
            ),
        )

    assert runner.calls == []
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "human\n"


def test_idempotent_reinstall_reports_already_current(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    runner = AgentRunner()
    dependencies = agent_dependencies(runner, resolved_graphify_executable(tmp_path))
    first = install_agent(AgentInstallRequest("codex", user_home), dependencies=dependencies)
    second = install_agent(AgentInstallRequest("codex", user_home), dependencies=dependencies)

    assert first.status == "installed"
    assert second.status == "already_current"
    assert second.resource_digest == first.resource_digest
    assert sorted(path.name for path in (_destination(user_home, "codex").parent).iterdir()) == [
        "graphify",
        "using-project-knowledge-graphs",
    ]


def test_graphify_failure_leaves_existing_atlas_destination_unchanged(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user"
    resolved = resolved_graphify_executable(tmp_path)
    install_agent(
        AgentInstallRequest("codex", user_home),
        dependencies=agent_dependencies(AgentRunner(), resolved),
    )
    destination = _destination(user_home, "codex")
    before = _tree_digest(destination)
    failing = AgentRunner(fail_install_home=user_home.resolve())

    with pytest.raises(AgentInstallError, match="agent_install_failed"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(failing, resolved),
        )

    assert _tree_digest(destination) == before


def test_install_refuses_destination_created_during_graphify_delegation(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user"
    runner = AgentRunner(create_unmanaged_atlas_home=user_home.resolve())

    with pytest.raises(AgentInstallError, match="agent_destination_unmanaged"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                runner, resolved_graphify_executable(tmp_path)
            ),
        )

    human = _destination(user_home, "codex") / "SKILL.md"
    assert human.read_text(encoding="utf-8") == "human\n"


def test_install_refuses_platform_tree_swapped_during_graphify_delegation(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user"
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    runner = AgentRunner(
        replace_skills_home=user_home.resolve(), replacement_skills=outside
    )

    with pytest.raises(AgentInstallError, match="agent_destination_modified"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                runner, resolved_graphify_executable(tmp_path)
            ),
        )

    assert not (outside / "using-project-knowledge-graphs").exists()


@pytest.mark.parametrize(
    "marker_payload",
    [
        '{"manager":"atlasweaver-agent-installer","manager":"other","platform":"codex","resource_digest":"%s","schema_version":1}\n'
        % ("0" * 64),
        json.dumps(
            {
                "extra": True,
                "manager": "atlasweaver-agent-installer",
                "platform": "codex",
                "resource_digest": "0" * 64,
                "schema_version": 1,
            }
        ),
        '{"manager":"atlasweaver-agent-installer","platform":"codex","resource_digest":NaN,"schema_version":1}\n',
    ],
)
def test_marker_parser_rejects_duplicate_unknown_and_nonfinite_json(
    tmp_path: Path, marker_payload: str
) -> None:
    destination = _destination(tmp_path / "user", "codex")
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("content\n", encoding="utf-8")
    (destination / ".atlasweaver-managed.json").write_text(
        marker_payload, encoding="utf-8"
    )
    runner = AgentRunner()

    with pytest.raises(AgentInstallError, match="agent_destination_unmanaged"):
        install_agent(
            AgentInstallRequest("codex", tmp_path / "user"),
            dependencies=agent_dependencies(
                runner, resolved_graphify_executable(tmp_path)
            ),
        )
    assert runner.calls == []


@pytest.mark.parametrize("platform", ["other", "", None])
def test_install_rejects_unsupported_platform_without_graphify(
    tmp_path: Path, platform: object
) -> None:
    runner = AgentRunner()
    with pytest.raises(AgentInstallError, match="agent_platform_unsupported"):
        install_agent(
            AgentInstallRequest(platform, tmp_path / "user"),  # type: ignore[arg-type]
            dependencies=agent_dependencies(
                runner, resolved_graphify_executable(tmp_path)
            ),
        )
    assert runner.calls == []


@pytest.mark.parametrize("component", ["home", "platform", "skills", "destination"])
def test_install_rejects_symlinked_path_components(
    tmp_path: Path, component: str
) -> None:
    user_home = tmp_path / "user"
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "home":
        user_home.symlink_to(outside, target_is_directory=True)
    else:
        user_home.mkdir()
        platform_root = user_home / ".codex"
        if component == "platform":
            platform_root.symlink_to(outside, target_is_directory=True)
        else:
            platform_root.mkdir()
            skills = platform_root / "skills"
            if component == "skills":
                skills.symlink_to(outside, target_is_directory=True)
            else:
                skills.mkdir()
                (skills / "using-project-knowledge-graphs").symlink_to(
                    outside, target_is_directory=True
                )
    runner = AgentRunner()

    with pytest.raises(AgentInstallError, match="agent_(home|destination)_unmanaged"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                runner, resolved_graphify_executable(tmp_path)
            ),
        )
    assert runner.calls == []


def test_install_rejects_symlinked_package_entry_and_cleans_stage(
    tmp_path: Path,
) -> None:
    resource_root = tmp_path / "resource"
    resource_root.mkdir()
    target = resource_root / "target"
    target.write_text("content\n", encoding="utf-8")
    (resource_root / "SKILL.md").symlink_to(target)
    user_home = tmp_path / "user"
    runner = AgentRunner()

    with pytest.raises(AgentInstallError, match="agent_resource_invalid"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                runner,
                resolved_graphify_executable(tmp_path),
                resource_root=resource_root,
            ),
        )
    skills = user_home / ".codex/skills"
    assert not _destination(user_home, "codex").exists()
    assert not skills.exists() or all(
        not path.name.startswith(".using-project-knowledge-graphs.")
        for path in skills.iterdir()
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_install_rejects_nonregular_package_resource_before_graphify(
    tmp_path: Path,
) -> None:
    import shutil

    resource_root = tmp_path / "resource"
    shutil.copytree(SOURCE_SKILL, resource_root)
    workflow = resource_root / "references/workflow.md"
    workflow.unlink()
    os.mkfifo(workflow)
    runner = AgentRunner()

    with pytest.raises(AgentInstallError, match="agent_resource_invalid"):
        install_agent(
            AgentInstallRequest("codex", tmp_path / "user"),
            dependencies=agent_dependencies(
                runner,
                resolved_graphify_executable(tmp_path),
                resource_root=resource_root,
            ),
        )

    assert runner.calls == []


@pytest.mark.parametrize("extra", [False, True])
def test_install_rejects_incomplete_or_unknown_package_resource_before_graphify(
    tmp_path: Path, extra: bool
) -> None:
    resource_root = tmp_path / "resource"
    resource_root.mkdir()
    (resource_root / "SKILL.md").write_text("incomplete\n", encoding="utf-8")
    if extra:
        (resource_root / "unreviewed.txt").write_text("extra\n", encoding="utf-8")
    runner = AgentRunner()

    with pytest.raises(AgentInstallError, match="agent_resource_invalid"):
        install_agent(
            AgentInstallRequest("codex", tmp_path / "user"),
            dependencies=agent_dependencies(
                runner,
                resolved_graphify_executable(tmp_path),
                resource_root=resource_root,
            ),
        )

    assert runner.calls == []


def test_probe_version_or_capability_mismatch_leaves_destination_absent(
    tmp_path: Path,
) -> None:
    for runner in (
        AgentRunner(version="0.9.49"),
        AgentRunner(omit_probe_platform="agents"),
    ):
        user_home = tmp_path / f"user-{len(runner.calls)}-{runner.version}-{runner.omit_probe_platform}"
        with pytest.raises(AgentInstallError, match="agent_install_failed"):
            install_agent(
                AgentInstallRequest("codex", user_home),
                dependencies=agent_dependencies(
                    runner, resolved_graphify_executable(tmp_path)
                ),
            )
        assert not _destination(user_home, "codex").exists()


def test_install_resolves_graphify_exactly_once(tmp_path: Path) -> None:
    runner = AgentRunner()
    resolved = resolved_graphify_executable(tmp_path)
    calls = 0

    def resolve() -> ResolvedGraphifyExecutable:
        nonlocal calls
        calls += 1
        return resolved

    dependencies = agent_dependencies(runner, resolved)
    dependencies = AgentInstallDependencies(
        runner=dependencies.runner,
        resolve_graphify=resolve,
        resource_root=dependencies.resource_root,
    )

    install_agent(AgentInstallRequest("codex", tmp_path / "user"), dependencies=dependencies)

    assert calls == 1


def test_replace_failure_restores_owned_destination_and_cleans_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_knowledge import agent_install

    user_home = tmp_path / "user"
    resolved = resolved_graphify_executable(tmp_path)
    install_agent(
        AgentInstallRequest("codex", user_home),
        dependencies=agent_dependencies(AgentRunner(), resolved),
    )
    destination = _destination(user_home, "codex")
    before = _tree_digest(destination)
    changed_resource = tmp_path / "changed-resource"
    import shutil

    shutil.copytree(SOURCE_SKILL, changed_resource)
    (changed_resource / "SKILL.md").write_text("changed resource\n", encoding="utf-8")
    original_replace = os.replace
    failed = False

    def fail_stage_commit(source, target):  # type: ignore[no-untyped-def]
        nonlocal failed
        if (
            not failed
            and Path(target) == destination
            and ".stage-" in Path(source).name
        ):
            failed = True
            raise OSError("injected replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(agent_install.os, "replace", fail_stage_commit)
    with pytest.raises(AgentInstallError, match="agent_install_failed"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                AgentRunner(), resolved, resource_root=changed_resource
            ),
        )

    assert _tree_digest(destination) == before
    assert sorted(path.name for path in destination.parent.iterdir()) == [
        "graphify",
        "using-project-knowledge-graphs",
    ]


def test_parent_fsync_failure_restores_owned_destination_and_cleans_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_knowledge import agent_install
    import shutil

    user_home = tmp_path / "user"
    resolved = resolved_graphify_executable(tmp_path)
    install_agent(
        AgentInstallRequest("codex", user_home),
        dependencies=agent_dependencies(AgentRunner(), resolved),
    )
    destination = _destination(user_home, "codex")
    before = _tree_digest(destination)
    changed_resource = tmp_path / "changed-resource"
    shutil.copytree(SOURCE_SKILL, changed_resource)
    (changed_resource / "SKILL.md").write_text("changed resource\n", encoding="utf-8")
    original_fsync_directory = agent_install._fsync_directory
    failed = False

    def fail_backup_fsync(path: Path) -> None:
        nonlocal failed
        backup_exists = any(
            item.name.startswith(".using-project-knowledge-graphs.backup-")
            for item in destination.parent.iterdir()
        )
        if not failed and path == destination.parent and backup_exists:
            failed = True
            raise OSError("injected fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(agent_install, "_fsync_directory", fail_backup_fsync)

    with pytest.raises(AgentInstallError, match="agent_install_failed"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                AgentRunner(), resolved, resource_root=changed_resource
            ),
        )

    assert _tree_digest(destination) == before
    assert sorted(path.name for path in destination.parent.iterdir()) == [
        "graphify",
        "using-project-knowledge-graphs",
    ]


def test_partial_copy_failure_cleans_stage_without_claiming_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from project_knowledge import agent_install

    original_copy = agent_install._copy_resource_tree

    def fail_after_partial_copy(root: Traversable, stage: Path) -> None:
        (stage / "partial.txt").write_text("partial\n", encoding="utf-8")
        raise AgentInstallError("agent_resource_invalid")

    monkeypatch.setattr(agent_install, "_copy_resource_tree", fail_after_partial_copy)
    user_home = tmp_path / "user"

    with pytest.raises(AgentInstallError, match="agent_resource_invalid"):
        install_agent(
            AgentInstallRequest("codex", user_home),
            dependencies=agent_dependencies(
                AgentRunner(), resolved_graphify_executable(tmp_path)
            ),
        )

    monkeypatch.setattr(agent_install, "_copy_resource_tree", original_copy)
    skills = user_home / ".codex/skills"
    assert not _destination(user_home, "codex").exists()
    assert sorted(path.name for path in skills.iterdir()) == ["graphify"]


def test_uninstall_removes_only_owned_tree_and_keeps_siblings(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    runner = AgentRunner()
    install_agent(
        AgentInstallRequest("codex", user_home),
        dependencies=agent_dependencies(runner, resolved_graphify_executable(tmp_path)),
    )
    sibling = user_home / ".codex/skills/human/SKILL.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("human\n", encoding="utf-8")
    install_call_count = len(runner.calls)

    result = uninstall_agent("codex", user_home)

    assert result.status == "uninstalled"
    assert not _destination(user_home, "codex").exists()
    assert sibling.read_text(encoding="utf-8") == "human\n"
    assert len(runner.calls) == install_call_count


def test_uninstall_refuses_modified_tree(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    install_agent(
        AgentInstallRequest("codex", user_home),
        dependencies=agent_dependencies(
            AgentRunner(), resolved_graphify_executable(tmp_path)
        ),
    )
    destination = _destination(user_home, "codex")
    (destination / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(AgentInstallError, match="agent_destination_modified"):
        uninstall_agent("codex", user_home)
    assert destination.exists()


def test_reinstall_and_uninstall_refuse_symlinked_destination_entry(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user"
    dependencies = agent_dependencies(
        AgentRunner(), resolved_graphify_executable(tmp_path)
    )
    install_agent(AgentInstallRequest("codex", user_home), dependencies=dependencies)
    destination = _destination(user_home, "codex")
    skill = destination / "SKILL.md"
    original = tmp_path / "original.md"
    skill.replace(original)
    skill.symlink_to(original)

    with pytest.raises(AgentInstallError, match="agent_destination_modified"):
        install_agent(AgentInstallRequest("codex", user_home), dependencies=dependencies)
    with pytest.raises(AgentInstallError, match="agent_destination_modified"):
        uninstall_agent("codex", user_home)

    assert skill.is_symlink()
