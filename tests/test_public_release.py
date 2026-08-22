from __future__ import annotations

from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_public_product_metadata_and_docs_are_complete() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "atlasweaver"
    assert project["version"] == "0.1.0"
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
    excluded = {".git", ".venv", "dist", "__pycache__"}

    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in excluded for part in path.relative_to(ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        assert all(value.casefold() not in text for value in forbidden), path
