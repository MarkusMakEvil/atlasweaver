from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "skills/using-project-knowledge-graphs"
INSTALLER = ROOT / "scripts/install-project-knowledge-skill"
SKILL_NAME = "using-project-knowledge-graphs"
MARKER = ".project-knowledge-managed.json"


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
    codex_home: Path, *, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(INSTALLER), "--codex-home", str(codex_home)],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result


def test_installer_copies_exact_tree_and_preserves_siblings(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    sibling = home / "skills/existing/SKILL.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep\n", encoding="utf-8")

    run_installer(home, cwd=tmp_path)

    destination = home / "skills" / SKILL_NAME
    assert tree_digest(destination, ignore_marker=True) == tree_digest(CANONICAL)
    assert sibling.read_text(encoding="utf-8") == "keep\n"
    marker = json.loads((destination / MARKER).read_text(encoding="utf-8"))
    assert marker == {
        "schema_version": 1,
        "manager": "project-knowledge-skill-installer",
        "source_digest": tree_digest(CANONICAL),
    }


def test_installer_refuses_unmanaged_destination(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    destination = home / "skills" / SKILL_NAME
    destination.mkdir(parents=True)
    human = destination / "SKILL.md"
    human.write_text("human content\n", encoding="utf-8")

    result = run_installer(home, check=False)

    assert result.returncode != 0
    assert "unmanaged" in result.stderr.lower()
    assert human.read_text(encoding="utf-8") == "human content\n"


def test_installer_refuses_a_tampered_managed_destination(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    run_installer(home)
    destination = home / "skills" / SKILL_NAME
    (destination / "SKILL.md").write_text("tampered\n", encoding="utf-8")

    result = run_installer(home, check=False)

    assert result.returncode != 0
    assert "modified" in result.stderr.lower()
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "tampered\n"


def test_installer_is_idempotent_and_leaves_no_transaction_files(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    run_installer(home)
    first = tree_digest(home / "skills" / SKILL_NAME)

    run_installer(home)

    assert tree_digest(home / "skills" / SKILL_NAME) == first
    assert sorted(path.name for path in (home / "skills").iterdir()) == [SKILL_NAME]


def test_installer_refuses_symlink_destination(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    skills = home / "skills"
    outside = tmp_path / "outside"
    skills.mkdir(parents=True)
    outside.mkdir()
    os.symlink(outside, skills / SKILL_NAME)

    result = run_installer(home, check=False)

    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert list(outside.iterdir()) == []
