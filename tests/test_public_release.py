from __future__ import annotations

from pathlib import Path
import re
import subprocess
import zipfile

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_public_product_metadata_and_docs_are_complete() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "atlasweaver"
    assert project["version"] == "0.3.5"
    assert project["scripts"]["project-knowledge"] == "project_knowledge.cli:main"
    assert project["license"] == "Apache-2.0"
    for relative in (
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "examples/.graphify-project.yaml",
        "examples/.graphifyignore",
        ".graphify-project.yaml",
        ".graphifyignore",
        ".graphify-secret-exceptions.yaml",
        ".github/workflows/ci.yml",
        ".github/workflows/graphify-compatibility.yml",
    ):
        assert (ROOT / relative).is_file(), relative


def test_public_tree_contains_no_private_product_context() -> None:
    forbidden = (
        "threads" + "-content-stack",
        "gni" + "da",
        "makevil" + "way",
        "Atlas Recovery " + "2026",
    )
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    for relative in result.stdout.split(b"\0"):
        if not relative:
            continue
        path = ROOT / relative.decode("utf-8")
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        assert all(value.casefold() not in text for value in forbidden), path


def test_built_wheel_contains_managed_agent_skill_resources(tmp_path: Path) -> None:
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    wheels = tuple(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert {
        "project_knowledge/compatibility_fixtures/graphify_0_9_48.json",
        "project_knowledge/compatibility_fixtures/graphify_0_9_51.json",
        "project_knowledge/compatibility_fixtures/runtime_probe.py",
        "project_knowledge/resources/skills/using-project-knowledge-graphs/SKILL.md",
        "project_knowledge/resources/skills/using-project-knowledge-graphs/agents/openai.yaml",
        "project_knowledge/resources/skills/using-project-knowledge-graphs/references/workflow.md",
    } <= names


def test_compatibility_workflow_is_read_only_and_digest_pinned() -> None:
    text = (ROOT / ".github/workflows/graphify-compatibility.yml").read_text(
        encoding="utf-8"
    )

    assert "contents: read" in text
    assert "pull-requests: write" not in text
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in text
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in text
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in text
    assert "uv==0.8.14" in text
    assert "project_knowledge.compat_probe" in text
    assert "pull_request_target" not in text
    assert "gh pr" not in text
    assert "git push" not in text


def test_compatibility_workflow_separates_supported_and_scheduled_checks() -> None:
    text = (ROOT / ".github/workflows/graphify-compatibility.yml").read_text(
        encoding="utf-8"
    )

    assert "push:" in text and "pull_request:" in text
    assert "23 3 * * 2" in text
    assert "if: github.event_name == 'schedule'" in text
    assert "if: always()" in text
    assert "name: graphify-upstream-compatibility" in text
    assert "path: graphify-compatibility-report.json" in text
    assert "retention-days: 14" in text
    assert "tests/test_compatibility.py" in text
    assert "tests/test_graphify_0_9_48_adapter.py" in text
    assert "tests/test_graphify_0_9_51_adapter.py" in text
    assert "0.9.48" not in text


def test_workflows_have_no_floating_official_actions_or_write_permissions() -> None:
    workflows = tuple(sorted((ROOT / ".github/workflows").glob("*.y*ml")))
    assert workflows
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        assert re.search(r"actions/[^@\s]+@v[0-9]+", text) is None, path
        writes = re.findall(r"(?m)^\s+([a-z-]+):\s*write\s*$", text)
        if path.name == "atlasweaver-publish.yml":
            assert sorted(writes) == ["attestations", "contents", "id-token"]
        elif path.name == "atlasweaver-self-publish.yml":
            assert sorted(writes) == [
                "attestations", "attestations", "contents", "contents",
                "id-token", "id-token",
            ]
        else:
            assert writes == [], path


def test_primary_ci_resolves_graphify_from_registry_and_keeps_adoption_gates() -> None:
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "production_graphify_compatibility as p" in text
    assert 'uv tool install "graphifyy==$GRAPHIFY_VERSION"' in text
    assert "graphifyy==0.9.48" not in text
    assert "tests/test_compatibility.py tests/test_graphify_0_9_48_adapter.py tests/test_graphify_0_9_51_adapter.py tests/test_evidence.py" in text
    assert "tests/test_real_graphify_pipeline.py tests/test_cli_adoption.py tests/test_queries.py" in text
    assert "tests/test_bundles_pack.py tests/test_bundles_parse.py tests/test_agent_install.py tests/test_fleet.py tests/test_workflows.py" in text


def test_dogfood_and_example_manifests_are_v2_and_optional_by_default() -> None:
    from project_knowledge.manifest import load_manifest

    dogfood = load_manifest(ROOT / ".graphify-project.yaml", ROOT)
    assert dogfood.schema_version == 2
    assert dogfood.graphify_version == "0.9.51"
    assert dogfood.project_uid is not None
    assert dogfood.features.atlas == "disabled"
    assert dogfood.features.registry == "disabled"
    assert dogfood.artifacts.provider == "github-release"
    assert dogfood.artifacts.repository == "MarkusMakEvil/atlasweaver"
    assert dogfood.artifacts.repository_id == 1343065113
    assert dogfood.artifacts.channel == "main"
    assert dogfood.artifacts.source_ref == "refs/heads/main"
    assert dogfood.artifacts.signer_workflow == (
        "MarkusMakEvil/atlasweaver/.github/workflows/atlasweaver-publish.yml"
    )
    assert dogfood.artifacts.signer_digest == (
        "552e33a928a7a7d5cac5721fb7d39f5b857ac1f2"
    )

    example = load_manifest(
        ROOT / "examples/.graphify-project.yaml", ROOT / "examples"
    )
    assert example.schema_version == 2
    assert example.graphify_version == "0.9.51"
    assert example.project_uid is not None
    assert example.features.atlas == "disabled"
    assert example.features.registry == "disabled"
    assert example.artifacts.provider == "none"
