from __future__ import annotations

from pathlib import Path
import subprocess

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_public_product_metadata_and_docs_are_complete() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "atlasweaver"
    assert project["version"] == "0.2.0"
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
